"""SVN 历史 diff 路由：对 resources 下文件取 SVN 历史版本并做单元格 diff。

依赖 svn CLI（svn log / svn cat）。仅两方对比，复用 engine.parser.read_excel 解析。
"""
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query

from config import MERGE_DIR, RESOURCES_DIR, SVN_DEMO_WC_DIR
from engine.parser import read_excel


def _fmt_rev(rev) -> str:
    """SVN revision → 'r' + 五位零填充字符串（如 3 → 'r00003'）。

    与 merge_branch._fmt_rev 同实现，保证 /api/svn/log 与 /api/merge/branch/log
    返回的 rev 格式统一，前端 select option value 与 preview-base source_rev 对齐。
    """
    try:
        n = int(rev)
    except (TypeError, ValueError):
        return ""
    return f"r{n:05d}"

router = APIRouter(prefix="/api/svn", tags=["svn"])


def _svn_dir() -> Path:
    if not RESOURCES_DIR.is_dir():
        raise HTTPException(500, f"资源目录不存在: {RESOURCES_DIR}")
    return RESOURCES_DIR


def _resolve_rel(rel: str) -> Path:
    """把相对路径解析到 resources 内，禁止越界。"""
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        raise HTTPException(400, "path 不能为空")
    target = (RESOURCES_DIR / rel).resolve()
    try:
        target.relative_to(RESOURCES_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "path 必须在 resources 目录内")
    return target


def _resolve_branch_path(path: str, base_dir: Path = MERGE_DIR) -> Path:
    """把分支/子目录路径解析为绝对路径。

    设计选择（与 `_resolve_rel` 区别）：`_resolve_rel` 只服务于 resources 下的
    单文件 diff 场景，越界即报错；而"分支/子目录派生点定位"面向的是
    merge/ 目录下的分支工作副本（未来可能还有独立的 SVN fixture repo 工作副本），
    路径语义更松散。因此这里：
      1. 若 path 本身已是存在的绝对路径，仍必须落在允许的基准目录白名单内
         （MERGE_DIR / SVN_DEMO_WC_DIR / RESOURCES_DIR），否则拒绝——防止绝对路径
         绕过目录限制逃逸到系统任意目录；
      2. 否则按相对路径拼到 base_dir（默认 MERGE_DIR，因为分支/子目录比较场景
         发生在 merge/ 目录下），并复用与 `_resolve_rel` 相同的越界校验模式，
         防止 `../..` 之类穿越到系统其他目录。
      3. 最终只做"存在性检查"是否交给调用方决定（本函数只负责路径解析），
         svn log 若查不到路径会在 `_run` 层面报错。
    """
    raw = (path or "").strip()
    if not raw:
        raise HTTPException(400, "path 不能为空")
    p = Path(raw)
    # 允许的基准目录白名单：合并与资源根目录及其下的 svn_demo fixture。
    allowed_roots = [d.resolve() for d in (MERGE_DIR, SVN_DEMO_WC_DIR, RESOURCES_DIR) if d]
    if p.is_absolute():
        # 绝对路径也强制白名单校验，杜绝 _resolve_branch_path 被用作逃逸通道。
        try:
            rp = p.resolve()
        except (OSError, RuntimeError):
            rp = p
        if not any(_is_within(rp, root) for root in allowed_roots):
            raise HTTPException(400, "绝对路径必须在允许的基准目录内")
        if not rp.exists():
            raise HTTPException(400, f"路径不存在: {path}")
        return rp
    rel = raw.lstrip("/\\")
    target = (base_dir / rel).resolve()
    try:
        target.relative_to(base_dir.resolve())
    except ValueError:
        raise HTTPException(400, "path 必须在允许的基准目录内")
    return target


def _is_within(path: Path, root: Path) -> bool:
    """path 是否等于或在 root 目录内（两者须为 resolve 后的绝对路径）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _run(cmd: List[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        raise HTTPException(500, "未找到 svn 命令，请确认 SVN CLI 已安装")
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "svn 命令超时")
    if r.returncode != 0:
        raise HTTPException(500, f"svn 失败: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


@router.get("/files")
def list_files():
    """列出 resources 下所有 xlsx 文件（相对路径），供前端选择。"""
    files = []
    for fp in sorted(RESOURCES_DIR.rglob("*.xlsx")):
        if fp.name.startswith("~$"):
            continue
        files.append(str(fp.relative_to(RESOURCES_DIR)).replace("\\", "/"))
    return {"files": files}


@router.get("/log")
def svn_log(path: str = Query(...), limit: int = Query(50, ge=1, le=500)):
    """取指定文件的 svn 提交历史。"""
    target = _resolve_rel(path)
    if not target.exists() and not target.parent.exists():
        raise HTTPException(400, f"路径不存在: {path}")
    out = _run(["svn", "log", "--xml", "-l", str(limit), str(target)])
    root = ET.fromstring(out)
    entries = []
    for le in root.findall("logentry"):
        rev = le.get("revision")
        author = (le.findtext("author") or "").strip()
        date = (le.findtext("date") or "").strip()
        msg = (le.findtext("msg") or "").strip()
        entries.append({"rev": _fmt_rev(rev), "author": author, "date": date, "msg": msg})
    return {"path": path, "entries": entries}


def _svn_wc_root(target: Path) -> Optional[Path]:
    """返回 target 所属 SVN 工作副本根目录（含 .svn 的最近祖先），非工作副本返回 None。"""
    cur = target if target.is_dir() else target.parent
    while cur != cur.parent:
        if (cur / ".svn").exists():
            return cur
        cur = cur.parent
    return None


def _run_soft(cmd: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """`_run` 的"软失败"版本：不抛 HTTPException，返回 (stdout, error_code)。

    设计选择（任务1.3）：`/branch-point` 端点的调用方（未来的分支/子目录对比接口）
    需要在"svn 不可用"或"派生点解析失败"时，明确地走 merge_base_override 兜底
    流程，而不是把它当成一次请求失败。如果这里像 `/log`、`/diff` 一样直接抛
    HTTPException(500)，调用方拿到的是一个异常/500 响应，很难区分"这是真的
    服务器错误"还是"预期内的降级信号"，也不利于前端展示"未能自动定位 SVN
    派生点，已使用手工指定基准"这类提示文案。因此本端点统一走 HTTP 200 +
    结构化响应体（`ok`/`error_code`/`message`），把"需要 fallback"当作一种
    正常的业务结果，而不是异常。

    兼容 Windows：TortoiseSVN/unisvn 对绝对路径做大小写解析时偶发 E720005，
    而工作副本内相对路径 + cwd=工作副本根 稳定。`svn log` 这类命令的最末参数若是
    工作副本内的绝对路径，改写为相对路径并设置 cwd 后执行；其它命令原样透传。
    """
    if not cmd:
        return None, "svn_unavailable"
    run_cmd = list(cmd)
    cwd = None
    last = run_cmd[-1]
    lp = Path(last)
    if lp.is_absolute():
        root = _svn_wc_root(lp)
        if root is not None:
            run_cmd[-1] = "." if lp == root else lp.relative_to(root).as_posix()
            cwd = str(root)
    try:
        r = subprocess.run(run_cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, cwd=cwd)
    except FileNotFoundError:
        return None, "svn_unavailable"
    except subprocess.TimeoutExpired:
        return None, "svn_unavailable"
    if r.returncode != 0:
        return None, "svn_unavailable"
    return r.stdout, None


def _match_path_entry(paths_el, target_basename: str, target_path_str: str):
    """在 <paths> 下按 basename/后缀匹配到对应的 <path> 元素。

    svn log -v --xml 里 <path> 的文本内容通常是仓库内绝对路径（如
    `/trunk/xxx/branch_a`），而调用方传入的 path 是工作副本相对/绝对路径，
    两者字面值不一致，所以按 basename 或后缀做宽松匹配；若该 logentry 下只有
    一个 <path> 元素，直接就用那一个（覆盖单文件/单目录提交的常见情况）。
    """
    entries = list(paths_el.findall("path")) if paths_el is not None else []
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    for pe in entries:
        text = (pe.text or "").strip()
        if not text:
            continue
        if text == target_path_str or text.endswith("/" + target_basename) or text.endswith(target_basename):
            return pe
    return entries[0]


@router.get("/branch-point")
def branch_point(path: str = Query(..., description="要定位派生点的目标路径（分支或子目录）"),
                  source: Optional[str] = Query(None, description="期望的源路径，用于校验（可选）")):
    """自动定位分支/子目录的派生点（copyfrom 信息），供 merge-base 快照生成使用。

    实现对应 openspec change `merge-svn-dual-mode` 任务 1.1-1.3：
      1.1 用 `svn log -v --stop-on-copy --xml` 找到目标路径最早一条提交的
          copyfrom-path/copyfrom-rev。
      1.2 若该提交没有 copyfrom（纯新建路径），降级为"首次提交版本号 - 1"，
          并标记 inferred=true。
      1.3 svn 不可用 / XML 解析失败 / 规则1.2也无法确定版本号时，返回
          `{"ok": false, "error_code": ..., "message": ...}`（HTTP 200），
          不抛 HTTPException，理由见 `_run_soft` 注释。

    注：实际逻辑在 `_resolve_branch_point` 纯函数里，便于其他路由直接 import 调用
    （FastAPI 的 Query 默认值在直接调用时会以 Query 对象传入，不是真值）。
    """
    source_val = source if isinstance(source, str) else None
    return _resolve_branch_point(path, source_val)


def _resolve_branch_point(path: str, source: Optional[str] = None) -> dict:
    """`branch_point` 端点的纯函数实现，可被其他路由直接 import 调用。

    路由函数因 FastAPI Query 默认值问题不便直接调用，故抽出此纯函数。
    """
    target = _resolve_branch_path(path)

    out, err = _run_soft(["svn", "log", "-v", "--stop-on-copy", "--xml", str(target)])
    if err is not None:
        return {"ok": False, "error_code": err, "message": "未找到 svn 命令或 svn log 执行失败，请使用 merge_base_override 手工指定基准"}

    try:
        root = ET.fromstring(out)
        logentries = root.findall("logentry")
        if not logentries:
            return {"ok": False, "error_code": "parse_failed", "message": "svn log 未返回任何记录，请使用 merge_base_override 手工指定基准"}
        # svn log 默认按 revision 降序返回，--stop-on-copy 只是提前截断，不改变
        # 排序方向；直接用 min 取 revision 最小的一条更稳妥，不依赖顺序假设。
        earliest = min(logentries, key=lambda le: int(le.get("revision")))
        rev = int(earliest.get("revision"))
        author = (earliest.findtext("author") or "").strip()
        date = (earliest.findtext("date") or "").strip()
        msg = (earliest.findtext("msg") or "").strip()
        log_entry = {"rev": rev, "author": author, "date": date, "msg": msg}

        paths_el = earliest.find("paths")
        path_el = _match_path_entry(paths_el, target.name, str(target))

        copyfrom_path = path_el.get("copyfrom-path") if path_el is not None else None
        copyfrom_rev_raw = path_el.get("copyfrom-rev") if path_el is not None else None

        if copyfrom_path and copyfrom_rev_raw:
            # 任务1.1：有 copyfrom 记录，直接用它作为派生点。
            result = {
                "ok": True,
                "copyfrom_path": copyfrom_path,
                "copyfrom_rev": int(copyfrom_rev_raw),
                "log_entry": log_entry,
                "inferred": False,
            }
        else:
            # 任务1.2：纯新建路径（无 copyfrom），降级为"首次提交版本号 - 1"。
            # copyfrom_path 无法从 svn 得知真实源路径，这里设为 path 本身
            # （约定：调用方应把它理解为"同名路径在 copyfrom_rev 时刻的内容"，
            # 而不是别的路径），并用 inferred=true 明确告知这是推断结果。
            if rev <= 1:
                return {"ok": False, "error_code": "parse_failed",
                        "message": "目标路径首次提交为 r1，无法推断上一版本号，请使用 merge_base_override 手工指定基准"}
            result = {
                "ok": True,
                "copyfrom_path": path,
                "copyfrom_rev": rev - 1,
                "log_entry": log_entry,
                "inferred": True,
            }
    except ET.ParseError as e:
        return {"ok": False, "error_code": "parse_failed", "message": f"svn log XML 解析失败: {e}，请使用 merge_base_override 手工指定基准"}

    if source:
        resolved_copyfrom = result.get("copyfrom_path")
        if resolved_copyfrom and not (resolved_copyfrom == source or str(resolved_copyfrom).endswith(source) or source.endswith(str(resolved_copyfrom))):
            result["warning"] = f"解析出的 copyfrom_path({resolved_copyfrom}) 与传入的 source({source}) 不一致，请人工核对"

    return result


def _cat_to_temp(target: Path, rev: int, tmp_dir: Path) -> Path:
    """svn cat -r rev 取文件内容写到临时文件，返回路径。"""
    out_path = tmp_dir / f"{target.stem}_r{rev}{target.suffix}"
    try:
        with open(out_path, "wb") as wf:
            r = subprocess.run(
                ["svn", "cat", "-r", str(rev), str(target)],
                stdout=wf, stderr=subprocess.PIPE, timeout=60,
            )
    except FileNotFoundError:
        raise HTTPException(500, "未找到 svn 命令")
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "svn cat 超时")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise HTTPException(500, f"svn cat r{rev} 失败: {err}")
    return out_path


def _val_str(v) -> str:
    if v is None:
        return ""
    return str(v)


def _diff_two_sheets(old_data: Dict[str, List[List]], new_data: Dict[str, List[List]]) -> List[dict]:
    """两方 sheet 对比，以第一列为主键对齐行，返回每个 sheet 的 diff 结果。"""
    sheets = list(dict.fromkeys(list(old_data.keys()) + list(new_data.keys())))
    result = []
    for name in sheets:
        old_rows = old_data.get(name, [])
        new_rows = new_data.get(name, [])
        if not old_rows and not new_rows:
            continue
        # 表头取 new（或 old）第一行
        headers_old = [str(c) if c is not None else "" for c in old_rows[0]] if old_rows else []
        headers_new = [str(c) if c is not None else "" for c in new_rows[0]] if new_rows else []
        headers = headers_new if headers_new else headers_old
        # 主键 -> 行（跳过表头第1行；空主键按行号兜底）
        def index(rows):
            m = {}
            for i, r in enumerate(rows[1:], start=1):
                key = _val_str(r[0]) if r else ""
                if not key:
                    key = f"__row{i}"
                m[key] = r
            return m
        old_map, new_map = index(old_rows), index(new_rows)
        all_keys = list(dict.fromkeys(list(old_map.keys()) + list(new_map.keys())))
        # 自然排序：数值优先
        def ksort(k):
            s = k.replace("__row", "")
            try:
                return (0, float(s), "")
            except ValueError:
                return (1, 0.0, re.sub(r"(\d+)", lambda m: m.group(1).zfill(10), s))
        all_keys.sort(key=ksort)
        max_cols = max(len(headers), max((len(r) for r in list(old_map.values()) + list(new_map.values())), default=0))
        diff_rows = []
        stats = {"added": 0, "removed": 0, "changed": 0, "matched": 0}
        for key in all_keys:
            in_old = key in old_map
            in_new = key in new_map
            if not in_old and in_new:
                stats["added"] += 1
                rtype = "added"
            elif in_old and not in_new:
                stats["removed"] += 1
                rtype = "removed"
            else:
                rtype = "matched"
            old_r = old_map.get(key, [])
            new_r = new_map.get(key, [])
            cells = []
            row_changed = False
            for ci in range(max_cols):
                ov = old_r[ci] if ci < len(old_r) else None
                nv = new_r[ci] if ci < len(new_r) else None
                changed = _val_str(ov) != _val_str(nv)
                if changed and rtype == "matched":
                    row_changed = True
                cells.append({
                    "col": ci,
                    "old": ov,
                    "new": nv,
                    "changed": changed,
                })
            if rtype == "matched" and row_changed:
                stats["changed"] += 1
            elif rtype == "matched":
                stats["matched"] += 1
            diff_rows.append({"key": key, "type": rtype, "cells": cells})
        result.append({
            "name": name,
            "headers": headers,
            "rows": diff_rows,
            "stats": stats,
            "header_changed": headers_old != headers_new,
        })
    return result


@router.get("/diff")
def svn_diff(
    path: str = Query(...),
    rev1: int = Query(...),
    rev2: int = Query(...),
):
    """取 rev1 与 rev2 两版本 xlsx 的单元格 diff。"""
    target = _resolve_rel(path)
    if not target.suffix.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "仅支持 xlsx/xlsm 文件")
    tmp_dir = Path(tempfile.mkdtemp(prefix="svn_diff_"))
    try:
        old_path = _cat_to_temp(target, rev1, tmp_dir)
        new_path = _cat_to_temp(target, rev2, tmp_dir)
        old_data = read_excel(str(old_path))
        new_data = read_excel(str(new_path))
        sheets = _diff_two_sheets(old_data, new_data)
        total = {"added": 0, "removed": 0, "changed": 0, "matched": 0}
        for s in sheets:
            for k in total:
                total[k] += s["stats"][k]
        return {
            "path": path,
            "rev1": rev1,
            "rev2": rev2,
            "sheets": sheets,
            "total": total,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
