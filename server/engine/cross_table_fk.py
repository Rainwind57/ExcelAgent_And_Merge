"""跨表（不同 xlsx 文件之间）外键关系静态声明 + 全局同步。

背景：
  ref_integrity.py::validate_sheet_references 只能处理"同一次 _build_group 调用
  范围内"的外键——即同一张表文件（可跨 sheet，见 merge_stages.py::_build_group
  的 id_mapping 聚合）。不同 xlsx 文件（如 item.xlsx 引用 reward.xlsx）各自独立
  调用 _build_group，彼此的 id_mapping/主键集合互不可见，无法在那一层同步。

  另外，merge_strategies.yaml 的 base_priority 标记只到"列名"级别（如
  "reward ID" → base_priority），不知道"这一列引用的是哪张表的主键"，
  跨表关系必须显式登记——见 rules/fill/_reference.md §2 的人工整理关系。
  本模块只登记当前 demo 数据（merge/svn/demo_svn/wc）里能验证到的条目，
  新增跨表关系需显式补充 CROSS_TABLE_FK_RULES。

调用时机：
  branch_compare / subdir_compare 一次 HTTP 请求内会比对多张表（table_names），
  每张表各自跑完 _build_group（第一/二遍：同表内比对 + 同表跨 sheet 外键同步）
  产出 FileGroup 后，才能拿到"每张表完整的 id_mapping / 主键集合"。
  sync_cross_table_refs 是"第三遍"：全部表 compare 完成后，在返回响应前调用一次，
  按声明的规则跨表扫描同步。

局限（有意为之，避免误报/误改）：
  - 只处理本次请求里同时被比对到的表对；某张表未被选入本次 compare（如前端
    只选了 item 没选 reward），对应规则直接跳过——拿不到目标表数据宁可不同步，
    不能凭空猜测。
  - 只支持单值 int/str 主键外键列（cell.versions 里单值），不支持 list[int]
    形式的多值外键（如 combat.npc_ids、pve_combat_npc.spell_ids 是逗号分隔/
    多列展开，merge_strategies.yaml 里这类列也没标 base_priority）——超出本轮范围。
  - CROSS_TABLE_FK_RULES 里的 src_table/dst_table 用 group_name（前端 /compare
    传的 table_names 元素），嵌套表带路径如 "item/item"；如与实际部署的
    demo 目录结构不符（分表拆分/改名），需同步更新本文件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .id_resolver import PK_COL, _cell_val, _cell_versions, _pk_str, _row_cells, _row_type

# 派生文件名 -> "分支角色"（与具体表名无关）。
#
# merge_branch.py::_compare_one_table / merge_subdir.py::_compare_one_table 给每张表
# 各自的派生文件命名为 "{flat_group_name}_{src|tgt}[_{suffix}].xlsx"（flat_group_name
# 是"这张表"的前缀，如 item.xlsx → "item__item"，reward.xlsx → "reward"）。
# id_mapping 里的 "file" 字段就是这个派生名——不同表的派生名前缀不同，同一分支在
# item.xlsx 里叫 "item__item_tgt.xlsx"，在 reward.xlsx 里叫 "reward_tgt.xlsx"，
# 直接用 (file, old_pk) 做跨表查找永远命中不到（前缀不同）。
# 但两者共享同一个 "_src"/"_tgt"（+ 可选 suffix）后缀——这个后缀才是跨表通用的
# "分支角色"标识（同一次 compare 请求里，"tgt" 永远对应同一个 target_branch）。
# 跨表匹配改用这个角色而非原始文件名。
_ROLE_RE = re.compile(r"_(src|tgt)(?:_(.+))?\.xlsx$", re.IGNORECASE)


def _file_role(fname: str) -> str:
    """从派生文件名提取跨表通用的"分支角色"键，提取不到则原样返回（如 base 文件名，
    remap_lookup 里本就不会出现 base，原样返回只是兜底不报错）。"""
    m = _ROLE_RE.search(fname or "")
    if not m:
        return fname or ""
    role, suffix = m.group(1).lower(), m.group(2)
    return f"{role}_{suffix}" if suffix else role


@dataclass(frozen=True)
class CrossTableFKRule:
    src_table: str    # 来源表 group_name（外键所在表，如 "item/item"）
    src_sheet: str    # 来源 sheet 名
    src_col: str      # 来源外键列表头文字（需与 headers 精确匹配）
    dst_table: str    # 目标表 group_name（主键所在表）
    dst_sheet: str     # 目标 sheet 名
    dst_pk_col: str    # 目标主键列表头文字（仅供文档记录，主键固定取第一列 PK_COL）


# 依据 rules/fill/_reference.md §2.2 道具链，人工核对 merge/svn/demo_svn/wc 实际
# 表头文字后登记（见 merge/scripts/seed_cross_table_ref_integrity_sync.py 的验证场景）：
#   item.xlsx·Chest."reward ID" → reward.xlsx·Reward.reward_id（开箱掉落奖励表）
CROSS_TABLE_FK_RULES: List[CrossTableFKRule] = [
    CrossTableFKRule(
        src_table="item/item", src_sheet="Chest", src_col="reward ID",
        dst_table="reward", dst_sheet="Reward", dst_pk_col="reward_id",
    ),
]


def _set_versions(cell: Any, versions: Dict[str, Any]) -> None:
    if isinstance(cell, dict):
        cell["versions"] = versions
    else:
        cell.versions = versions


def _set_value(cell: Any, value: Any) -> None:
    if isinstance(cell, dict):
        cell["value"] = value
    else:
        cell.value = value


def _sheet_of(group: Any, sheet_name: str) -> Any:
    if group is None:
        return None
    sheets = getattr(group, "sheets", None)
    if sheets is None and isinstance(group, dict):
        sheets = group.get("sheets")
    if not sheets:
        return None
    return sheets.get(sheet_name)


def _collect_id_mapping(group: Any) -> List[dict]:
    """汇总一张表（FileGroup）内所有 sheet 的 id_mapping（跨 sheet 聚合）。"""
    mapping: List[dict] = []
    sheets = getattr(group, "sheets", None) if group is not None else None
    if sheets is None and isinstance(group, dict):
        sheets = group.get("sheets")
    if not sheets:
        return mapping
    for sd in sheets.values():
        idr = getattr(sd, "id_resolution", None)
        if idr is None and isinstance(sd, dict):
            idr = sd.get("id_resolution")
        idr = idr or {}
        mapping.extend(idr.get("id_mapping", []) or [])
    return mapping


def _build_pk_set(group: Any, sheet_name: str) -> set:
    """目标表目标 sheet 的主键集合（合并后已含重映射后的新主键）。"""
    sd = _sheet_of(group, sheet_name)
    if sd is None:
        return set()
    rows = getattr(sd, "rows", None)
    if rows is None and isinstance(sd, dict):
        rows = sd.get("rows")
    rows = rows or []
    pks: set = set()
    for row in rows:
        if _row_type(row) == "deleted":
            continue
        cells = _row_cells(row)
        if cells:
            pk = _pk_str(_cell_val(cells[PK_COL]))
            if pk:
                pks.add(pk)
    return pks


def sync_cross_table_refs(groups: Dict[str, Any],
                           rules: Optional[List[CrossTableFKRule]] = None) -> Dict[str, dict]:
    """跨表外键同步：本次请求内所有已比对表 compare 完成后调用一次。

    groups: branch_compare/subdir_compare 产出的 {group_name: FileGroup}（就地改写）。
    rules: 缺省用 CROSS_TABLE_FK_RULES；测试可传自定义规则。

    返回: {"{src_table}::{src_sheet}": {remapped_refs, dangling: [...], checked}}
    供上层日志/调试查看；同时就地：
      1. 命中 id_mapping 的外键值同步改写（cell.versions[fn] / 单值时同步 cell.value）；
      2. 命中不到目标表主键集合的记为悬空，追加进该 sheet 原有 ref_integrity（复用
         前端已有的 "🔗 已同步外键 N 处" 展示字段，不需要新字段）。
    """
    rules = rules if rules is not None else CROSS_TABLE_FK_RULES
    report: Dict[str, dict] = {}
    mapping_cache: Dict[str, List[dict]] = {}
    pkset_cache: Dict[str, set] = {}

    for rule in rules:
        src_group = groups.get(rule.src_table)
        dst_group = groups.get(rule.dst_table)
        if src_group is None or dst_group is None:
            continue  # 本次请求未同时比对到源表/目标表 → 跳过，不误报不同步
        src_sd = _sheet_of(src_group, rule.src_sheet)
        if src_sd is None:
            continue

        if rule.dst_table not in mapping_cache:
            mapping_cache[rule.dst_table] = _collect_id_mapping(dst_group)
        pk_key = f"{rule.dst_table}::{rule.dst_sheet}"
        if pk_key not in pkset_cache:
            pkset_cache[pk_key] = _build_pk_set(dst_group, rule.dst_sheet)

        id_mapping = mapping_cache[rule.dst_table]
        # 跨表匹配用"分支角色"（见 _file_role 顶部注释），不能直接用原始文件名
        # （不同表各自的派生文件名前缀不同，原始文件名永远不会跨表相等）。
        remap_lookup: Dict[tuple, str] = {
            (_file_role(m["file"]), m["old_pk"]): m["new_pk"] for m in id_mapping
        }
        old_to_new: Dict[str, List[str]] = {}
        for m in id_mapping:
            old_to_new.setdefault(m["old_pk"], []).append(m["new_pk"])
        dst_pks = pkset_cache[pk_key]

        headers = list(getattr(src_sd, "headers", None) or (src_sd.get("headers") if isinstance(src_sd, dict) else []) or [])
        try:
            ci = headers.index(rule.src_col)
        except ValueError:
            continue

        rows = getattr(src_sd, "rows", None)
        if rows is None and isinstance(src_sd, dict):
            rows = src_sd.get("rows")
        rows = rows or []

        remapped_refs = 0
        dangling: List[dict] = []
        checked = 0
        for ri, row in enumerate(rows):
            if _row_type(row) == "deleted":
                continue
            cells = _row_cells(row)
            if ci >= len(cells):
                continue
            cell = cells[ci]
            versions = dict(_cell_versions(cell))
            if not versions:
                continue
            changed = False
            for fn, v in list(versions.items()):
                v_str = _pk_str(v)
                if not v_str:
                    continue
                checked += 1
                new_pk = remap_lookup.get((_file_role(fn), v_str))
                if new_pk:
                    versions[fn] = new_pk
                    v_str = new_pk
                    remapped_refs += 1
                    changed = True
                if v_str not in dst_pks:
                    dangling.append({
                        "ri": ri, "ci": ci, "col_header": rule.src_col,
                        "value": v_str,
                        "reason": f"跨表外键目标不存在（{rule.dst_table}·{rule.dst_sheet} 无此主键）",
                    })
            if changed:
                _set_versions(cell, versions)
                cur_val = _pk_str(_cell_val(cell))
                if cur_val and cur_val in old_to_new:
                    new_vals = old_to_new[cur_val]
                    if len(new_vals) == 1:
                        _set_value(cell, new_vals[0])
                    # 多值歧义（同 old_pk 跨分支重映射到不同 new_pk）时保留原显示值，
                    # 与 ref_integrity.py::validate_sheet_references 的 M5 处理一致。

        key = f"{rule.src_table}::{rule.src_sheet}"
        report[key] = {"remapped_refs": remapped_refs, "dangling": dangling, "checked": checked}
        if checked:
            # checked>0 说明确实扫到了该列有值的引用（不管本次是否命中重映射/悬空），
            # 都要重建该列旧的悬空判定——即便本次判定全部有效（dangling=[]），也要
            # 清掉旧判定，否则"目标表未参与本次单表比对时的误报"会一直残留。
            ref_res = getattr(src_sd, "ref_integrity", None)
            if ref_res is None and isinstance(src_sd, dict):
                ref_res = src_sd.get("ref_integrity")
            if isinstance(ref_res, dict):
                # 该列此前在"单表内 validate_sheet_references"那一遍因看不到目标表
                # 主键集合，可能已把本列的部分/全部引用误判为悬空（reason 里带
                # "本表主键不存在"）——这里拿到目标表数据后重新校验了一遍，结果
                # 更准确，先剔除那一遍对同一列（ci）的旧判定，再并入本次结果，
                # 避免同一处引用同时出现"悬空"和"已同步"两条互相矛盾的记录。
                old_dangling = ref_res.get("dangling", []) or []
                ref_res["dangling"] = [d for d in old_dangling if d.get("ci") != ci] + dangling
                ref_res["remapped_refs"] = ref_res.get("remapped_refs", 0) + remapped_refs
                ref_res["checked"] = ref_res.get("checked", 0) + checked
            else:
                new_ref_res = {"remapped_refs": remapped_refs, "dangling": dangling, "checked": checked}
                if isinstance(src_sd, dict):
                    src_sd["ref_integrity"] = new_ref_res
                else:
                    src_sd.ref_integrity = new_ref_res
    return report
