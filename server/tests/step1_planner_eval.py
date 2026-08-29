# -*- coding: utf-8 -*-
"""Step1 单独评测：planner_style_inputs.json → ParseAgent.parse → 与 expected_answer 比对。

用途：
    绕过 V2 orchestrator / Step2 / Step3 / Step4，只跑 Step1（Locator 粗路由 +
    split_multi_intent 分段 + DecomposeAgent schema 注入 LLM 拆分 + produces 推断），
    用真实 codemaker serve，把每条用例产出的 NLIntent JSON 与 expected_answer 逐条
    比对，输出：
      - 表路由准确率（table/sheet/action 三元组命中）
      - 字段级精准（expected 键命中数 / 键值一致数 / 多余键）
      - 占位符（produces/consumes 标签）闭环率（#3 指标）
      - 意图数量 vs 期望数量

用法（repo 根）：
    python -m tests.step1_planner_eval --only 0                 # 只跑 case0
    python -m tests.step1_planner_eval --only 0,1,3             # 跑若干条
    python -m tests.step1_planner_eval                          # 全部 7 条（很久）

前提：codemaker serve 已启动（.env 配置好 CODEMAKER_SERVER_URL 等）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
_SERVER_DIR = _TESTS_DIR.parent
_ROOT = _SERVER_DIR.parent
_RES = _ROOT / "resources"
_REPORT_DIR = _TESTS_DIR / "reports"
_DEFAULT_CASES = _TESTS_DIR / "cases" / "planner_style_inputs.json"


def _load_dotenv(env_path: Path) -> None:
    """极简 .env 加载（必须在 import agent 之前，CodemakerClient 模块级固化凭据）。"""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_ROOT / ".env")

if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ── 归一化与比对原语 ─────────────────────────────────────────

def _stem_of(table: str) -> str:
    """表路径 → stem：'school\\school.xlsx' → 'school'。"""
    s = str(table or "").replace("\\", "/").strip()
    if s.endswith(".xlsx"):
        s = s[:-5]
    return s.rsplit("/", 1)[-1].lower()


def _norm_sheet(sheet: str) -> str:
    return str(sheet or "").strip().lower()


_ACTION_MAP = {"add": "add", "modify": "set", "delete": "delete", "get": "get"}


def _is_placeholder(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("<") and v.strip().endswith(">")


def _canon_key(s: str) -> str:
    """字段键规范化：去类型冒号/索引/换行备注，得纯列名。

    '门派类型\n1：物理\n2：法术' → '门派类型'
    'school_ability_id[0]:int' → 'school_ability_id'
    """
    import re as _re
    s = str(s or "").strip()
    if ":" in s:
        s = s.split(":", 1)[0]
    s = _re.sub(r"\[\d+\]", "", s)
    s = s.split("\n", 1)[0].strip()
    s = _re.sub(r"^\d+[：:]\s*", "", s)
    return s.strip()


# (stem, sheet) -> {"canon": {canon_key: raw_header}, "type": {type_norm: raw_header}}
_HEADER_MAP: dict = {}
_HEADER_MAP_LOADED = False


def _load_header_map(res: Path) -> None:
    """加载真实表头映射（row1 显示名 ↔ row2 规范名），供键模糊匹配。

    expected_answer 用 row2 规范名（school_id/value），LLM 产出用 row1 显示名
    （门派id/原文），需要真实表头做桥接。一次加载全局复用。
    """
    global _HEADER_MAP, _HEADER_MAP_LOADED
    if _HEADER_MAP_LOADED:
        return
    _HEADER_MAP_LOADED = True
    try:
        from agent.real_cli import RealCodeMakerCLI
        cli = RealCodeMakerCLI(workspace=res)
        tables = {p.stem.lower(): p for p in cli.list_tables()}
        for stem, p in tables.items():
            try:
                for sh in cli.get_sheets(p):
                    if "说明" in sh or "CONFIG" in sh.upper():
                        continue
                    h = cli.read_header(p, sh)
                    t = cli.read_type_row(p, sh)
                    canon_map = {}
                    type_map = {}
                    row1totype = {}
                    for a, b in zip(h, t):
                        if not a:
                            continue
                        canon = _canon_key(a)
                        if not canon:
                            continue
                        canon_map[canon.lower()] = a
                        b_norm = str(b or "").split(":")[0].strip()
                        if b_norm:
                            type_map[_canon_key(b_norm).lower()] = a
                            type_map[b_norm.lower()] = a
                            row1totype[canon.lower()] = _canon_key(b_norm).lower()
                    _HEADER_MAP[(stem, sh.lower())] = {
                        "canon": canon_map, "type": type_map,
                        "row1totype": row1totype,
                    }
            except Exception:
                pass
    except Exception:
        pass


def _resolve_act_key(stem: str, sheet: str, ak: str) -> set:
    """实际字段键 → 可能对应的期望键集合（经真实表头桥接）。"""
    ak = str(ak or "")
    canon = _canon_key(ak).lower()
    out = {_canon_key(ak)}
    entry = _HEADER_MAP.get((str(stem or "").lower(), str(sheet or "").lower()))
    if entry:
        # 实际键是 row1 显示名 → 桥到 row2 规范名（expected 用 row2）
        typ = entry["row1totype"].get(canon)
        if typ:
            out.add(typ)
        raw = entry["canon"].get(canon)
        if raw:
            out.add(_canon_key(raw))
            out.add(str(raw))
        # 实际键是 row2 规范名 → 桥到 row1 显示名（expected 可能用 row1）
        raw2 = entry["type"].get(canon)
        if raw2:
            out.add(_canon_key(raw2))
            out.add(str(raw2))
            rev = entry["row1totype"].get(_canon_key(raw2).lower())
            if rev:
                out.add(rev)
    return out


def _ph_label(v: Any) -> str:
    return str(v).strip().strip("<>").strip().lower()


def _ph_key_of(v: Any) -> str:
    """取 label 最后一个 _id 段（"new_school_id" → "school"），供标签语义匹配。"""
    l = _ph_label(v)
    for seg in ("_level", "_lv"):
        if seg in l:
            l = l.split(seg)[0]
    if l.endswith("_id"):
        l = l[:-3]
    if l.startswith("new_"):
        l = l[4:]
    parts = [p for p in re.split(r"[_\W]+", l) if p and p != "new"]
    if parts:
        tail = re.sub(r"\d+$", "", parts[-1])
        return tail or parts[-1]
    return l


def _ph_labels_match(a: Any, b: Any) -> bool:
    return _ph_label(a) == _ph_label(b) or _ph_key_of(a) == _ph_key_of(b)


def _collect_placeholder_labels(v: Any) -> set[str]:
    labels: set[str] = set()
    if _is_placeholder(v):
        labels.add(_ph_label(v))
    elif isinstance(v, dict):
        for item in v.values():
            labels.update(_collect_placeholder_labels(item))
    elif isinstance(v, (list, tuple)):
        for item in v:
            labels.update(_collect_placeholder_labels(item))
    return labels


def _norm_value(v: Any) -> Any:
    """把 list/tuple 字符串形式归一为可比较结构。"""
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    if not s:
        return None
    if (s[0] in "[(") and (s[-1] in "])"):
        try:
            import ast
            return ast.literal_eval(s)
        except Exception:
            return s
    # 纯数字字符串归一为数字
    try:
        if s.lstrip("-").isdigit():
            return int(s)
        return float(s)
    except ValueError:
        return s


def _values_equal(exp: Any, act: Any) -> bool:
    """值相等判定：占位符比对 label，其余递归/字面比较。

    占位符语义匹配：期望 <new_school_id>，实际 <new_school_id_2> 也算命中
    （LLM 多行同表会带 _N 后缀，但引用的还是"新增的 school 行"）。
    """
    if _is_placeholder(exp) or _is_placeholder(act):
        if _is_placeholder(exp) and _is_placeholder(act):
            if _ph_labels_match(exp, act):
                return True
        return False
    e = _norm_value(exp)
    a = _norm_value(act)
    if isinstance(e, (list, tuple)) or isinstance(a, (list, tuple)):
        el = [str(x) for x in (e if isinstance(e, (list, tuple)) else [e])]
        al = [str(x) for x in (a if isinstance(a, (list, tuple)) else [a])]
        return el == al
    return str(e) == str(a)


def _match_op(exp: dict, act: dict) -> dict:
    """单条 expected op vs 单条 actual intent 比对，返回命中详情。"""
    exp_table = _stem_of(exp.get("table", ""))
    act_table = _stem_of(act.get("table", ""))
    exp_sheet = _norm_sheet(exp.get("sheet", ""))
    act_sheet = _norm_sheet(act.get("sheet", ""))
    exp_action = _ACTION_MAP.get(exp.get("operation", ""), exp.get("operation", ""))
    act_action = str(act.get("action", "") or "").strip().lower()

    r = {
        "exp": f"{exp_table}/{exp_sheet}/{exp_action}",
        "act": f"{act_table}/{act_sheet}/{act_action}",
        "table_ok": exp_table == act_table,
        "sheet_ok": exp_sheet == act_sheet,
        "action_ok": exp_action == act_action,
    }
    # 定位（modify/delete）
    if exp_action in ("set", "delete"):
        exp_rk = exp.get("row_key") or {}
        loc_ok = None
        act_lf = act.get("locator_field") or ""
        act_lv = act.get("locator_value") or ""
        act_lfs = act.get("locator_fields") or []
        act_lvs = act.get("locator_values") or []
        if exp_rk:
            keys = list(exp_rk.keys())
            vals = [str(exp_rk[k]) for k in keys]
            if act_lfs and len(act_lfs) == len(keys):
                loc_ok = [str(k) for k in act_lfs] == keys and [str(v) for v in act_lvs] == vals
            else:
                loc_ok = (str(act_lf) == keys[0]) and (str(act_lv) in vals)
        r["locator_ok"] = loc_ok

    # 字段比对（add/set）
    exp_fields = exp.get("row_content") or {}
    act_fields = act.get("fields") or {}
    if not isinstance(act_fields, dict):
        act_fields = {}

    # 实际字段键 → 期望键映射。优先级：精确规范键 → 经真实表头桥接的候选集
    # （LLM 用 row1 显示名、expected 用 row2 规范名时也能对上）。
    # 注意：同一规范键可能对应多条期望键（school_ability_id[0..3]），用
    # 多值队列贪心分配，不能 dict 坍缩。
    act_items = list(act_fields.items())
    exp_norm: dict[str, list] = {}
    for k in exp_fields.keys():
        exp_norm.setdefault(_canon_key(k), []).append(k)

    key_map: dict[int, str] = {}
    used_ek: set = set()

    def _take(ai: int, canon: str) -> bool:
        bucket = exp_norm.get(canon) or []
        for k in bucket:
            if k not in used_ek:
                key_map[ai] = k
                used_ek.add(k)
                return True
        return False

    for ai, (ak, _av) in enumerate(act_items):
        akn = _canon_key(ak)
        if akn in exp_norm:
            _take(ai, akn)
    for ai, (ak, _av) in enumerate(act_items):
        if ai in key_map:
            continue
        akn = _canon_key(ak)
        bridge = _resolve_act_key(act_table, act_sheet, ak)
        for bn in bridge:
            bn = _canon_key(bn)
            if bn in exp_norm and _take(ai, bn):
                break
        if ai in key_map:
            continue
        for ek in exp_norm.keys():
            a_s, e_s = akn.lower(), ek.lower()
            if e_s == a_s or (len(e_s) >= 2 and (e_s in a_s or a_s in e_s)):
                if _take(ai, ek):
                    break

    keys_found = 0
    keys_value_ok = 0
    ph_total = 0
    ph_ok = 0
    extra_keys = []
    for ai, (ak, av) in enumerate(act_items):
        ek = key_map.get(ai)
        if ek is None:
            extra_keys.append(str(ak))
            continue
        ev = exp_fields[ek]
        keys_found += 1
        if _values_equal(ev, av):
            keys_value_ok += 1
        if _is_placeholder(ev):
            ph_total += 1
            if _values_equal(ev, av):
                ph_ok += 1

    r.update({
        "exp_keys": len(exp_fields),
        "keys_found": keys_found,
        "keys_value_ok": keys_value_ok,
        "extra_keys": extra_keys,
        "ph_total": ph_total,
        "ph_ok": ph_ok,
        "full_match": (r["table_ok"] and r["sheet_ok"] and r["action_ok"]
                       and keys_found == len(exp_fields)
                       and keys_value_ok == len(exp_fields)
                       and not extra_keys),
    })
    return r


def _intent_to_dict(it: Any) -> dict:
    """NLIntent → 可 JSON 化的精简 dict（对齐 expected_answer 结构）。"""
    fields = (getattr(it, "extras", None) or {}).get("fields") or {}
    d = {
        "action": getattr(it, "action", ""),
        "table": getattr(it, "table_hint", None),
        "sheet": getattr(it, "sheet_hint", None),
        "fields": {str(k): v for k, v in fields.items()} if isinstance(fields, dict) else {},
    }
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        d["locator_field"], d["locator_value"] = lf, lv
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs:
        d["locator_fields"], d["locator_values"] = list(lfs), list(lvs)
    pl = getattr(it, "produces_label", None)
    if pl:
        d["produces"] = str(pl)
    cl = getattr(it, "consumes_labels", None) or []
    if cl:
        d["consumes"] = [str(c) for c in cl]
    return d


def _greedy_align(expected: list, actual: list) -> list:
    """expected 逐条在 actual 中贪心找最优匹配（按命中分），返回逐条 match dict。"""
    rows = []
    used = set()
    for e in expected:
        best = None
        best_score = -1
        for j, a in enumerate(actual):
            if j in used:
                continue
            m = _match_op(e, a)
            score = (int(m["table_ok"]) * 4 + int(m["sheet_ok"]) * 2
                     + int(m["action_ok"]) * 1
                     + m.get("keys_found", 0) * 0.5)
            if score > best_score:
                best_score = score
                best = (j, m)
        if best is not None:
            used.add(best[0])
            rows.append(best[1])
        else:
            rows.append({"exp": f"{_stem_of(e.get('table',''))}/{e.get('sheet','')}/{e.get('operation','')}",
                         "act": "〈缺失〉", "unmatched": True})
    return rows


# ── 单 case 执行 ─────────────────────────────────────────────

def run_case(case: dict, pa, la, da) -> dict:
    text = case.get("input") or ""
    expected = case.get("expected_answer") or []
    t0 = time.time()

    locator_result = None
    loc_err = ""
    try:
        locator_result = la.locate(text)
    except Exception as e:
        loc_err = f"{type(e).__name__}: {e}"

    intents: list = []
    parse_err = ""
    try:
        intents = pa.parse(text)
    except Exception as e:
        parse_err = f"{type(e).__name__}: {e}"

    dur = time.time() - t0
    actual = [_intent_to_dict(it) for it in intents]

    # locator 信息
    cands = [c.stem for c in (locator_result.candidates if locator_result else [])]
    fk_n = len(locator_result.fk_edges) if locator_result else 0
    cross = bool(locator_result and locator_result.is_cross_table)

    rows = _greedy_align(expected, actual)
    n_exp = len(expected)
    n_full = sum(1 for r in rows if r.get("full_match"))
    table_hits = sum(1 for r in rows if r.get("table_ok"))
    sheet_hits = sum(1 for r in rows if r.get("sheet_ok"))
    action_hits = sum(1 for r in rows if r.get("action_ok"))
    keys_exp = sum(r.get("exp_keys", 0) for r in rows)
    keys_found = sum(r.get("keys_found", 0) for r in rows)
    keys_ok = sum(r.get("keys_value_ok", 0) for r in rows)
    ph_total = sum(r.get("ph_total", 0) for r in rows)
    ph_ok = sum(r.get("ph_ok", 0) for r in rows)
    loc_total = sum(1 for r in rows if r.get("locator_ok") is not None)
    loc_ok = sum(1 for r in rows if r.get("locator_ok"))

    # 悬空占位符：#3 指标（actual fields 里有 <label> 但没有任何 expected 的 label 与之相等）
    exp_labels = set()
    for e in expected:
        if _is_placeholder(e.get("produces")):
            exp_labels.add(_ph_label(e.get("produces")))
        exp_labels.update(_collect_placeholder_labels(e.get("row_content") or {}))
    ph_unresolved = 0
    for a in actual:
        for k, v in (a.get("fields") or {}).items():
            if _is_placeholder(v):
                # 该 label 在 expected 全量 produces 里不存在 → 悬空
                if not any(_ph_labels_match(v, f"<{label}>") for label in exp_labels):
                    ph_unresolved += 1
    ph_resolved_total = 0
    for a in actual:
        for k, v in (a.get("fields") or {}).items():
            if _is_placeholder(v):
                if any(_ph_labels_match(v, f"<{label}>") for label in exp_labels):
                    ph_resolved_total += 1

    return {
        "name": (case.get("_meta") or {}).get("name", ""),
        "input": text,
        "dur_s": round(dur, 1),
        "loc_err": loc_err,
        "parse_err": parse_err,
        "locator": {"candidates": cands, "fk_edges": fk_n, "is_cross_table": cross},
        "n_expected": n_exp,
        "n_actual": len(actual),
        "actual": actual,
        "rows": rows,
        "metrics": {
            "full_match": n_full,
            "table_hit": table_hits,
            "sheet_hit": sheet_hits,
            "action_hit": action_hits,
            "keys_found": keys_found,
            "keys_ok": keys_ok,
            "keys_exp": keys_exp,
            "extra_key_ops": sum(1 for r in rows if r.get("extra_keys")),
            "ph_total": ph_total,
            "ph_ok": ph_ok,
            "ph_unresolved": ph_unresolved,
            "ph_resolved_total": ph_resolved_total,
            "loc_total": loc_total,
            "loc_ok": loc_ok,
        },
    }


def _fmt_pct(n: int, d: int) -> str:
    return f"{n}/{d}" + (f" ({n/d*100:.0f}%)" if d else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-file", default=str(_DEFAULT_CASES))
    ap.add_argument("--only", default="", help="逗号分隔的 case 下标，空=全部")
    args = ap.parse_args()

    with open(args.cases_file, encoding="utf-8") as f:
        cases = json.load(f)
    idxs = [int(x) for x in args.only.split(",") if x.strip()] if args.only else list(range(len(cases)))
    cases = [c for i, c in enumerate(cases) if i in idxs]

    # 直接构造 Step1 三件套（不走 AgentService，不经 run_v2）
    from agent.real_cli import RealCodeMakerCLI
    from agent.excel.parser.codemaker_parser import CodemakerNLParser
    from agent.excel.subagent.locator_agent import LocatorAgent
    from agent.excel.subagent.decompose_agent import DecomposeAgent
    from agent.excel.parse_agent import ParseAgent

    cli = RealCodeMakerCLI(workspace=_RES)
    parser = CodemakerNLParser(directory=str(_RES), enable_skill=True)

    # §问题A 防御：_table_index.json 可能被其他测试（test_composite_pk_locate 等）
    # 覆盖成单表/陈旧索引（曾出现只剩 _test_composite_pk 一张表的 1.9KB 索引），
    # 导致 LocatorAgent 规则定位全 miss。这里检测陈旧（表数过少或含 _test 前缀）
    # 并用真实 resources/ 重建，保证 Step1 评测与真实表一致。
    from agent.excel.locator.table_index import build_index, load_index
    try:
        _idx = load_index()
    except Exception:
        _idx = []
    if len(_idx) < 5 or any(getattr(t, "stem", "").startswith("_test") for t in _idx):
        _p(f"[index] 索引陈旧（{len(_idx)} 表），用真实 resources/ 重建（~50s）...")
        build_index(_RES)
        _p("[index] 重建完成")

    la = LocatorAgent(parser=parser, cli=cli)
    da = DecomposeAgent(parser=parser, cli=cli)
    pa = ParseAgent(parser=parser, cli=cli, locator_agent=la, decompose_agent=da)

    # 加载真实表头映射（row1 显示名 ↔ row2 规范名），供字段键模糊匹配
    _load_header_map(_RES)

    _p(f"cases_file={args.cases_file}  共 {len(cases)} 条")
    _p(f"model={os.environ.get('CODEMAKER_MODEL','')}  serve={os.environ.get('CODEMAKER_SERVER_URL','')}")
    _p("=" * 80)

    results = []
    for ci, case in enumerate(cases):
        _p(f"\n[{ci}] {(case.get('_meta') or {}).get('name', '')[:60]}")
        r = run_case(case, pa, la, da)
        results.append(r)
        m = r["metrics"]
        _p(f"  input: {r['input'][:60]}...")
        _p(f"  locator: candidates={r['locator']['candidates']} cross={r['locator']['is_cross_table']}"
           f" fk={r['locator']['fk_edges']}" + (f" err={r['loc_err']}" if r["loc_err"] else ""))
        _p(f"  parse: {r['n_actual']} 条 intent（期望 {r['n_expected']}），耗时 {r['dur_s']}s"
           + (f" err={r['parse_err']}" if r["parse_err"] else ""))
        _p(f"  路由: table {_fmt_pct(m['table_hit'], r['n_expected'])}"
           f" / sheet {_fmt_pct(m['sheet_hit'], r['n_expected'])}"
           f" / action {_fmt_pct(m['action_hit'], r['n_expected'])}"
           f" / 整条全对 {_fmt_pct(m['full_match'], r['n_expected'])}")
        _p(f"  字段: 键命中 {_fmt_pct(m['keys_found'], m['keys_exp'])}"
           f" / 键值一致 {_fmt_pct(m['keys_ok'], m['keys_exp'])}"
           f" / 多余键意图数 {m['extra_key_ops']}")
        _p(f"  占位符: 闭环 {_fmt_pct(m['ph_ok'], m['ph_total'])}"
           f" / 悬空 {m['ph_unresolved']}（#3，可解析 {m['ph_resolved_total']}）"
           + (f" / 定位 {_fmt_pct(m['loc_ok'], m['loc_total'])}" if m["loc_total"] else ""))

    # 汇总
    _p("\n" + "=" * 80)
    _p("汇总")
    agg = {
        "cases": len(results), "expected": 0, "actual": 0,
        "table_hit": 0, "sheet_hit": 0, "action_hit": 0, "full": 0,
        "keys_exp": 0, "keys_found": 0, "keys_ok": 0,
        "ph_total": 0, "ph_ok": 0, "ph_unresolved": 0,
        "loc_total": 0, "loc_ok": 0,
    }
    for r in results:
        m = r["metrics"]
        agg["expected"] += r["n_expected"]
        agg["actual"] += r["n_actual"]
        agg["table_hit"] += m["table_hit"]
        agg["sheet_hit"] += m["sheet_hit"]
        agg["action_hit"] += m["action_hit"]
        agg["full"] += m["full_match"]
        agg["keys_exp"] += m["keys_exp"]
        agg["keys_found"] += m["keys_found"]
        agg["keys_ok"] += m["keys_ok"]
        agg["ph_total"] += m["ph_total"]
        agg["ph_ok"] += m["ph_ok"]
        agg["ph_unresolved"] += m["ph_unresolved"]
        agg["loc_total"] += m["loc_total"]
        agg["loc_ok"] += m["loc_ok"]
    _p(f"  期望意图 {agg['expected']} / 实际产出 {agg['actual']} 条")
    _p(f"  表路由 {_fmt_pct(agg['table_hit'], agg['expected'])}"
       f" / sheet {_fmt_pct(agg['sheet_hit'], agg['expected'])}"
       f" / action {_fmt_pct(agg['action_hit'], agg['expected'])}"
       f" / 整条全对 {_fmt_pct(agg['full'], agg['expected'])}")
    _p(f"  字段键命中 {_fmt_pct(agg['keys_found'], agg['keys_exp'])}"
       f" / 键值一致 {_fmt_pct(agg['keys_ok'], agg['keys_exp'])}")
    _p(f"  占位符闭环 {_fmt_pct(agg['ph_ok'], agg['ph_total'])} / 悬空 {agg['ph_unresolved']}")
    if agg["loc_total"]:
        _p(f"  定位 {_fmt_pct(agg['loc_ok'], agg['loc_total'])}")

    # 落盘实际产出 JSON 供人工对照
    _REPORT_DIR.mkdir(exist_ok=True)
    out = {
        "cases_file": str(Path(args.cases_file).name),
        "model": os.environ.get("CODEMAKER_MODEL", ""),
        "results": results,
        "summary": agg,
    }
    outp = _REPORT_DIR / "step1_planner_eval_latest.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _p(f"\n实际产出已写: {outp}")


if __name__ == "__main__":
    main()
