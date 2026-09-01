"""TableAgent：把自然语言意图落地到 Excel 增删查改操作。"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Any, Optional, Tuple

from ..cli.cli_interface import CodeMakerCLI, CLICallResult
from ..locator.column_matcher import ColumnMatcher, ColumnMatch, _clean_header
from .enum_resolver import get_enum_resolver
from .live_enum import resolve_label_full
from .cross_table_splitter import CrossTableIntentSplitter, detect_cross_table_action
from ..parser.nl_parser import NLIntent
from ..parser.codemaker_parser import CodemakerNLParser
from .table_resolver import TableResolver, TableResolve
from .skill_loader import ColumnAliasConfig, RowAliasConfig, TableContextConfig, ParserConfig, ShortFormConfig, SheetAliasConfig, AntiPatternConfig
from .evidence_logger import get_evidence_logger
from .skill_updater import get_skill_updater
from .dialog_logger import get_dialog_logger
from .confidence_config import (
    ACCEPT_THRESHOLD,
    ROW_METHOD_CONFIDENCE,
    ROW_AMBIGUOUS_PENALTY,
    ROW_PREFIX_STRIP_PENALTY,
    COLUMN_LOW_CONFIDENCE_HINT,
)

logger = logging.getLogger(__name__)


def _values_equal(a: Any, b: Any) -> bool:
    """写后验证容差比对（移植自 tests/table_case_eval.py）。

    语义：None/空等价、数值容差 1e-6、str strip、list 递归。
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) < 1e-6
    except (TypeError, ValueError):
        pass
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    return str(a).strip() == str(b).strip()


# 全局解析器配置，由 skill_loader 从 YAML/JSON 配置文件中加载
_PARSER_CFG = ParserConfig.load()

# 歧义消解 LLM 调用单独短超时：失败快速回退到"保留歧义→删除拒绝/查询取其一"分支，
# 避免单次消歧挂起耗尽上层 120s 预算（K09 删白泽曾因消歧轮询挂满 120s 超时）。
_DISAMBIGUATE_TIMEOUT = int(os.environ.get("CODEMAKER_DISAMBIGUATE_TIMEOUT", "30"))

# T2: 行号覆盖兜底正则——用户在歧义后明确指定行号（如"用行6""选行6""第6行""选第6行"）
# 匹配时跳过 _locate_row 直接读该行；不匹配普通指令（如"改为5""id为1001"）。
# 两个分支：① 用/选 + 行/第 + N [+ 行]；② 第 + N + 行。
_ROW_OVERRIDE_RE = re.compile(r'(?:用|选)\s*(?:行|第)\s*(\d+)\s*行?|第\s*(\d+)\s*行')

# 值约束配置缓存（懒加载）：{table_stem: {sheet: {col_name: {type: ...}}}}
_VALUE_CONSTRAINTS: Optional[dict] = None


def _load_value_constraints() -> dict:
    """懒加载 skills/L1_derived/value_constraints.yaml，返回 {stem: {sheet: {col: {type}}}}。

    文件缺失或 yaml 不可用时返回空 dict，保证降级运行。
    T12: L1 自动派生文件迁移到 L1_derived/，回退根目录兼容未迁移环境。
    用户规则（rules/validate/*.md 内嵌 yaml）合并覆盖 L1 派生（用户 > 自动）。
    """
    global _VALUE_CONSTRAINTS
    if _VALUE_CONSTRAINTS is not None:
        return _VALUE_CONSTRAINTS
    try:
        import yaml
        base = Path(__file__).resolve().parents[1] / "skills"
        p_l1 = base / "L1_derived" / "value_constraints.yaml"
        p = p_l1 if p_l1.exists() else base / "value_constraints.yaml"
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            _VALUE_CONSTRAINTS = raw.get("tables", {}) or {}
        else:
            _VALUE_CONSTRAINTS = {}
    except Exception:
        _VALUE_CONSTRAINTS = {}
    # 用户校验规则 overlay 深合并（type/min/max/unique/regex，优先级高于 L1 派生）
    try:
        from .rules_loader import get_value_constraints_overlay
        overlay = get_value_constraints_overlay()
        if overlay:
            _deep_merge_tables(_VALUE_CONSTRAINTS, overlay)
    except Exception:
        logger.debug("合并用户校验规则失败", exc_info=True)
    return _VALUE_CONSTRAINTS


def _deep_merge_tables(base: dict, extra: dict) -> dict:
    """递归深合并 extra 到 base（extra 优先，dict 级深合并，list 整值替换）。"""
    for k, v in extra.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_tables(base[k], v)
        else:
            base[k] = v
    return base

def _is_business_sheet(name: str) -> bool:
    """判断 sheet 名是否为业务数据 sheet（排除 config 及非业务标记的 sheet）。"""
    if not name or name.lower() == "config":
        return False
    return not any(m in name for m in _PARSER_CFG.non_business_markers)


# 列举/参考 sheet 标记：这些 sheet 是「可用资源目录」（只读清单），
# 写入会撞合并单元格/破坏目录结构。写操作应拒。
# 「当前可用的」前缀（实测 anim_montage_sequence/effect_actor 等表用此命名）；
# 「...列表」后缀兜底（如「可用列表」「可选列表」）。
# 不用裸「可用」「可选」前缀——太宽，会误伤「可用资源表」等真业务表。
_LISTING_SHEET_PREFIXES = ("当前可用的",)
_LISTING_SHEET_SUFFIXES = ("列表",)


def _is_listing_sheet(name: str) -> bool:
    """判断 sheet 是否为列举/参考目录 sheet（只读，不可写入业务数据）。"""
    if not name:
        return False
    n = name.strip()
    if any(n.startswith(p) for p in _LISTING_SHEET_PREFIXES):
        return True
    return any(n.endswith(s) for s in _LISTING_SHEET_SUFFIXES)


# 占位符分类：区分可选留空 vs 必填/跨表引用。
#   <auto>            = LLM 显式标「留空待补」（用户没提的可选列）→ 静默软跳过
#   <new_xxx_id> 等   = 跨表引用/必填占位，前序产出没对上 → 弹问用户补值
import re as _re_placeholder
_AUTO_PLACEHOLDER_RE = _re_placeholder.compile(r"<\s*auto\s*>")
_PLACEHOLDER_RE = _re_placeholder.compile(r"<([^>]+)>")


def _is_auto_placeholder(v: str) -> bool:
    """值整段是 <auto>（可含空白）→ 视为可选留空标记。"""
    if not isinstance(v, str):
        return False
    return _AUTO_PLACEHOLDER_RE.fullmatch(v.strip()) is not None


def _classify_placeholder_fields(fields: dict) -> tuple[list[str], list[str]]:
    """把 fields 中的占位列拆为 (auto_cols, required_cols)。

    auto_cols：值 == <auto>，可选留空，不弹问。
    required_cols：值含 <...> 但非 <auto>，必填/跨表引用，需弹问补值。
    非占位值的列不进任一列表。
    """
    if not isinstance(fields, dict):
        return [], []
    auto_cols: list[str] = []
    required_cols: list[str] = []
    for k, v in fields.items():
        if not isinstance(v, str) or "<" not in v:
            continue
        if not _PLACEHOLDER_RE.search(v):
            continue
        if _is_auto_placeholder(v):
            auto_cols.append(k)
        else:
            required_cols.append(k)
    return auto_cols, required_cols


# 依赖标记 → 大白话业务名（供占位符未解错误提示，让策划知道到底缺哪个前置项）。
_DEP_LABEL_WORDS = {
    "conv": "对话", "convoption": "对话选项", "option": "对话选项",
    "interaction": "交互", "npc": "NPC", "prefab": "场景实体",
    "reward": "奖励包", "combat": "战斗配置", "spell": "技能",
    "quest": "任务", "item": "道具", "activity": "活动", "mail": "邮件",
    "pet": "灵兽", "building": "建筑",
}


def _humanize_dep_label(label: str) -> str:
    """把 producer 标签（如 new_interaction_conv_id）翻成大白话 + 保留原标记备查。

    例：new_conv_id → 对话(new_conv_id)；new_pve_combat_npc_id → NPC(new_pve_combat_npc_id)。
    """
    import re as _re_dl
    core = _re_dl.sub(r"^new_|_?id$", "", str(label or "").strip().lower())
    words = [w for w in core.split("_") if w]
    hit = next((_DEP_LABEL_WORDS[w] for w in words if w in _DEP_LABEL_WORDS), None)
    return f"{hit}({label})" if hit else (label or "前置数据")


def _col_types_by_header(table_stem: str, sheet: str, headers: list) -> dict:
    """按实际表头名产出 {col_header: type_str}，供 Step3 计划 prompt 约束 LLM 按类型填值。

    value_constraints.yaml 的列名是干净短名（如「建筑类型」），但实际表头常带
    换行/括号后缀（如「建筑类型\\n（和代码中枚举值保持一致）」）。此处按
    「vc key 是表头核心名（去 \\n/括号后缀）的前缀」匹配，命中即取其类型。
    未命中的列不进 dict（prompt 仅展示已知类型列，避免误导）。
    """
    if not headers:
        return {}
    vc = _load_value_constraints().get(table_stem, {}).get(sheet, {}).get("columns", {})
    if not isinstance(vc, dict) or not vc:
        return {}
    import re as _re_ct
    out: dict = {}
    for h in headers:
        if not h:
            continue
        core = _re_ct.split(r"[\n（(]", str(h), 1)[0].strip()
        if not core:
            continue
        # 精确命中优先，其次前缀包含
        if core in vc:
            out[str(h)] = vc[core].get("type", "") if isinstance(vc[core], dict) else ""
            continue
        for k, meta in vc.items():
            if core == k or core.startswith(k) or k in core:
                out[str(h)] = meta.get("type", "") if isinstance(meta, dict) else ""
                break
    return {k: v for k, v in out.items() if v}


def _strip_lead_verbs(text: str) -> str:
    """去掉自然语言开头的引导动词（如"请""帮忙"等），方便后续解析。"""
    t = text.strip()
    lead = tuple(_PARSER_CFG.lead_verbs)
    while t and t[0] in lead:
        t = t[1:].strip()
    return t


def _find_column_spans(text: str, matcher: ColumnMatcher) -> list[Tuple[int, int, ColumnMatch]]:
    """在文本中扫描所有可能的列名片段，返回 (起始位置, 结束位置, 匹配结果) 列表。

    算法：从每个位置出发，尝试不同长度的子串，用 matcher 匹配。
    每个起始位置收集所有 score >= 0.6 的匹配，然后从中选分数最高的(Top选择)
    """
    spans: list[Tuple[int, int, ColumnMatch]] = []
    n = len(text)
    for start in range(n):
        candidates: list[Tuple[int, int, ColumnMatch]] = []
        for length in range(1, n - start + 1):
            seg = text[start:start + length].strip()
            if not seg:
                continue
            m = matcher.match(seg)
            if m is not None and m.score >= 0.6:
                candidates.append((start, start + length, m))
        if candidates:
            # 取分数最高者（而非最短）
            best = max(candidates, key=lambda x: x[2].score)
            spans.append(best)
    return spans


def _value_after(text: str, span: Tuple[int, int, ColumnMatch], spans: list, idx: int) -> Optional[str]:
    """提取某个列名片段之后、下一个列名片段之前的值文本。

    Args:
        text: 原始自然语言文本
        span: 当前列名在 text 中的 (start, end, ColumnMatch)
        spans: 所有已识别的列名片段列表
        idx: 当前 span 在 spans 中的索引

    Returns:
        两个列名片段之间的文本（去除"的为是"等前缀），若为空则返回 None。
    """
    end = span[1]
    next_start = len(text)
    if idx + 1 < len(spans):
        next_start = spans[idx + 1][0]
    val = text[end:next_start].strip()
    val = val.lstrip("的为是").strip()
    return val or None


# 常见分隔词：用户语句中连接列名和值的关键词
# 含"填/写/置"等赋值动作动词（如"金币公式填 800"→值"800"），长变体在前（startswith 先匹配）。
_SEPARATORS = ("修改为", "改为", "改成", "设为", "设置为", "加上去", "加上", "添加", "增加", "新增",
               "填写", "填上", "填入", "填", "写上", "写入", "写", "置为", "置", "加", "为", "是")

# 值尾部残留的中文/英文句读（如"填 800；"→"800"、"守军。"→"守军"），提取值时应裁掉。
_TAIL_PUNCT = "；;。，,、！!？?：: 　"

# 中文/英文引号，用户输入或 LLM 输出中可能残留，需统一清洗
_QUOTES = ("\u201c", "\u201d", "\u2018", "\u2019", '"', "'", "「", "」", "『", "』")

# 成对引号：(开, 闭)。半角引号开闭同字符，全角/书名号开闭不同。
_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),   # " "
    ("\u2018", "\u2019"),   # ‘ ’
    ("「", "」"),
    ("『", "』"),
)


def _clean_quotes(text: str) -> str:
    """只去除**成对**引号的外层字符，保留引号内内容的语义边界。

    用户常输入「新增宠物名为"朱雀"」，成对引号不应作为值的一部分，但也不能
    无差别删除所有引号——否则会破坏内容内本身含引号/撇号的语义。故仅剥离
    成对引号的外层，未配对的引号原样保留。
    """
    if not text:
        return text
    for open_q, close_q in _QUOTE_PAIRS:
        if open_q == close_q:
            q = re.escape(open_q)
            text = re.sub(f"{q}([^{q}]*){q}", r"\1", text)
        else:
            o, c = re.escape(open_q), re.escape(close_q)
            text = re.sub(f"{o}([^{o}{c}]*){c}", r"\1", text)
    return text


def _strip_separators(text: str) -> str:
    """去掉文本开头可能存在的分隔关键词（如"修改为""设置为""填"等）+ 尾部句读，并清洗引号。"""
    t = _clean_quotes(text).strip()
    for sep in _SEPARATORS:
        if t.startswith(sep):
            t = t[len(sep):].strip()
            break
    # 裁掉值尾部残留的句读（如"填 800；"经上面剥"填"后为"800；"→"800"）。
    # 仅当裁完是纯数字/小数或括号结构（坐标/列表）时才采用，否则保留原值——
    # 让"包也建一下，"这类文本碎片保留句读，供 _do_append 碎片行守卫识别，不误清。
    _t2 = t.rstrip(_TAIL_PUNCT)
    if _t2 and _t2 != t and (
            _t2.lstrip("-").replace(".", "", 1).isdigit()
            or (_t2[:1] in "([" and _t2[-1:] in ")]")):
        t = _t2
    return t


def _serialize_list_value(v):
    """数组值序列化为 Excel 单元格字符串。

    Excel 单元格不可直接存 list/tuple（openpyxl 会抛错或写失败），按
    table_constraints 规范：list 用逗号分隔不写 []，tuple 用 () 包裹。
    嵌套子项（list/tuple）统一用 () 包裹，表示一个结构化单元（如坐标点）。

    规则：
      [1,2,3]                  → "1,2,3"
      (1,2,3)                  → "(1,2,3)"
      [[30,0,40]]              → "(30,0,40)"
      [[30,0,40],[50,0,60]]    → "(30,0,40),(50,0,60)"
      ["WorldAttackCon",80,100]→ "WorldAttackCon,80,100"

    非 list/tuple 原样返回。
    """
    if isinstance(v, list):
        return ",".join(_serialize_list_item(x) for x in v)
    if isinstance(v, tuple):
        return "(" + ",".join(_serialize_list_item(x) for x in v) + ")"
    return v


def _serialize_complex_cell_value(v):
    """Serialize non-scalar values so Excel writers never receive raw dict/list."""
    if isinstance(v, dict):
        import json as _json
        return _json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return _serialize_list_value(v)


def _serialize_list_item(x):
    """序列化数组单个元素：嵌套 list/tuple 用 () 包裹，标量 str()。"""
    if isinstance(x, (list, tuple)):
        return "(" + ",".join(str(e) for e in x) + ")"
    return str(x)


@dataclass
class RowMatch:
    """行定位结果：不再是裸 int，携带置信度与证据，供调用方判断是否需要二次确认。

    Attributes:
        row: 命中的 Excel 行号
        value: 命中单元格的原始文本
        confidence: 置信度 [0,1]，由匹配层级 + 是否歧义 + 是否前缀剥离共同决定
        method: 命中方式（exact/startswith/contains_direct/contains_paren_stripped/
                contains_num_stripped，可附加 "+prefix_stripped"/"+llm_arbitrated"）
        ambiguous: 同一层级是否命中了多行（歧义）
        alternatives: 同层级的其他候选 [(row, value), ...]，供仲裁/展示
    """
    row: int
    value: str
    confidence: float
    method: str
    ambiguous: bool = False
    alternatives: list[Tuple[int, str]] = field(default_factory=list)


@dataclass
class AgentStep:
    """Agent 执行过程中的单步记录。

    Attributes:
        name: 步骤名称（如 "resolve_table"、"locate_row"）
        ok: 该步是否成功
        detail: 补充说明信息
    """
    name: str
    ok: bool
    detail: str = ""


@dataclass
class AgentResult:
    """Agent 执行结果，聚合所有步骤及最终状态。

    Attributes:
        ok: 整体是否成功。None=未知（写操作未完成验证），True=所有写步骤验证通过或查询成功，
            False=任一写步骤验证失败或步骤显式失败。写操作由 _verify_write_back 驱动，
            查询操作（get/col）构造时即置 True。
        intent: 解析出的自然语言意图
        steps: 所有执行步骤的记录
        final: CLI 最终调用结果（如 write_cell、read_cell 的返回值）
        message: 面向用户的结果描述
        needs_confirm: 是否需要二次确认（如级联删除前预览，True 时需调用方确认后重试）
        confirm_token: 确认令牌，重试时回传以跳过 dry-run 直接执行
        result_rows: 表体结构数据，供前端渲染"列名+行值"表格。
            每项形如 {"col": int, "col_name": str, "old_value": Any, "new_value": Any}，
            语义随 action 而异：
              set   → old_value=修改前, new_value=修改后（仅含变更列）
              add   → new_value=新增值（生成的行）
              delete→ old_value=被删值（删除的行内容）
              get   → new_value=查询到的值
        table_stem: 目标表格文件名 stem，用于结果表头展示
        table_sheet: 目标 sheet 名
    """
    ok: Optional[bool] = None
    intent: Optional[NLIntent] = None
    steps: list[AgentStep] = field(default_factory=list)
    final: Optional[CLICallResult] = None
    message: str = ""
    needs_confirm: bool = False
    confirm_token: Optional[str] = None
    # 确认类型："cascade"=存在关联数据需选是否级联；"confidence"=定位置信度低需确认删除
    confirm_kind: str = ""
    result_rows: list[dict] = field(default_factory=list)
    table_stem: str = ""
    table_sheet: str = ""
    # 复合操作（多指令）的子任务分组。单指令时为空。
    # 每项 {intent_action, ok, message, steps, result_rows, table_stem, table_sheet}，
    # 供前端按子任务分段渲染（定位→操作→结果），而非平铺所有步骤。
    sub_tasks: list[dict] = field(default_factory=list)
    # ── T6 运行证据层（供 skill 自动更新消费，旁路写入不阻断主流程）──
    # 列定位证据：直接从 ColumnMatch 拷贝（column_matcher.py:68-73）
    col_evidence: dict = field(default_factory=dict)
    # 行定位证据：直接从 RowMatch 拷贝
    row_evidence: dict = field(default_factory=dict)
    # 用户是否在下一轮纠正了本轮定位结果（由 agent_service 跨轮识别回填）
    user_corrected: bool = False
    # 用户纠正后的真实列名/行值（user_corrected=True 时非空）
    corrected_to: Optional[str] = None
    # 会话 id（跨轮纠正识别用，由 agent_service 注入）
    session_id: str = ""
    # ── D4 错误吞噬治理：诊断标记（供上层定位"半成品/索引不一致"问题）──
    # 写后索引刷新失败 → True（数据已写但索引未更新，下次查询可能漏）
    index_dirty: bool = False
    # 回滚失败 → True（写操作失败但回滚也失败，半成品残留）
    dirty_data: bool = False
    # ── multi-table-orchestration D4 跨表事务：失败表列表（供上层诊断）──
    failed_tables: list = field(default_factory=list)
    # ── #38/#40 结构化失败清单：阻断错误经中断反问仍未解/用户跳过时落此，
    # 供 Step6 汇总显式指出错误位置 + 前端渲染失败块。每项：
    # {type, table, sheet, col, root_cause, attempted_strategies, suggestion, snip, status, user_reply}
    failures: list[dict] = field(default_factory=list)
    # F1: AI 意图校验的非阻断建议清单（如"AI 校验建议补充 X 意图"），
    # 不实时弹问、不阻塞执行，交 Step6 全局 summary 如实列出，用户下一轮补描述。
    ai_suggestions: list[str] = field(default_factory=list)
    # R9: 查询多行命中时，所有命中的完整行数据列表（get 操作 && 多行命中）。
    # 每项 {"row": int, "columns": [{"col": int, "col_name": str, "value": Any}, ...]}
    multi_rows: list[dict] = field(default_factory=list)
    # ── 配表模式增强：思考过程 + 跨表搜索续传 ──
    # 思考步骤（细粒度，比 steps 更友好）：每项 {"phase": str, "detail": str}
    # phase: 解析/路由/定位/校验/执行/跨表探索。agent 核心直接填，供前端折叠展示。
    thinking_steps: list[dict] = field(default_factory=list)
    # 跨表搜索候选结果（行未命中→用户确认跨表找→搜索后填）：
    # 每项 {"table_stem": str, "sheet": str, "match_type": "exact"|"similar",
    #       "matches": [{"row": int, "value": str, "score": float}]}
    cross_table_candidates: list[dict] = field(default_factory=list)
    # 暂态搜索上下文（行未命中暂停时填，供 confirm_token 续传）：
    # {"table_stem": str, "sheet": str, "col_name": str, "col_idx": int,
    #  "value": str, "top5": [{"row": int, "value": str, "score": float}]}
    pending_search: Optional[dict] = None
    # ── C 方案：部分成功（行已写入但缺值列待补）──
    # needs_user_fill：缺值列清单，每项 {"col","table","sheet","reason"}
    # <auto> 占位命中非主键 int 列时收集，行照常写入（缺该列），不中断事务。
    # 响应回前端提示用户补值，符合"agent 先填骨架，策划后补细节"工作流。
    needs_user_fill: list[dict] = field(default_factory=list)
    # partial：True=部分成功（有 needs_user_fill 但行已写入），事务层据此不中断后续
    partial: bool = False
    # 思考流式回调：add_thinking 时同步调用，供 stream 接口实时推送。
    # 签名：(phase: str, detail: str) -> None。由 stream 调用方注入。
    on_thinking: Optional[callable] = None
    # 步骤流式回调：add 时同步调用，供 stream 接口实时推送步骤进度。
    # 签名：(payload: dict) -> None，payload={name,ok,detail}。由 stream 调用方注入。
    on_step: Optional[callable] = None

    def add(self, name: str, ok: bool, detail: str = ""):
        """添加一个执行步骤，并按 ok 驱动整体结果。

        - 步骤失败（ok=False）→ 整体 ok 置 False
        - 步骤成功（ok=True）且整体 ok 仍 None（未知）→ 整体 ok 置 True（首步成功确立基线）
        - 步骤成功且整体 ok 已有值 → 不变（不覆盖既有的失败/成功状态）
        """
        self.steps.append(AgentStep(name, ok, detail))
        if not ok:
            self.ok = False
        elif self.ok is None:
            self.ok = True
        sink = self.on_step
        if sink is not None:
            try:
                sink({"name": name, "ok": ok, "detail": detail})
            except Exception:
                pass

    @property
    def aggregated_message(self) -> str:
        """D5 规范化聚合 message（按 ok 状态从 steps 生成，不拼接成功文本）。

        - ok=True → "成功：{step_names}"
        - ok=False → "失败：{首个失败 step.name} - {detail}"（不含成功步骤文本）
        - ok=None → "未完成"
        顶层 message 字段保留（兼容 72 处直接赋值 + eval 解析），本 property 供
        需要规范化消息的调用方（如 _finalize_crud）使用。
        """
        if self.ok is None:
            return "未完成"
        if self.ok:
            names = "、".join(s.name for s in self.steps) or "完成"
            return f"成功：{names}"
        # ok=False：找首个失败 step
        for s in self.steps:
            if not s.ok:
                return f"失败：{s.name} - {s.detail}"
        return "失败"

    def add_thinking(self, phase: str, detail: str):
        """添加一条思考步骤（不影响 ok，纯展示）。

        phase: 解析/路由/定位/校验/执行/跨表探索/计划/汇总。
        与 steps 并行，供前端折叠渲染思考过程。
        若 on_thinking 回调已注入（res 级或 TableAgent 级，stream 模式），
        同步推送实现真流式。

        多指令聚合模式（_suppress_phase_thinking=True）：抑制 Step2-6 阶段的
        thinking 推送（路由/计划/校验/执行/汇总/回滚/重试），由外层统一发
        全局阶段标记，避免多任务阶段在气泡间来回跳变。解析/细分仍推送。
        """
        if phase and detail:
            self.thinking_steps.append({"phase": phase, "detail": detail})
            # 多指令聚合模式：抑制内部阶段 thinking 的 sink 推送
            if getattr(self, '_suppress_phase_thinking', False):
                if phase in ("路由", "定位", "跨表探索", "计划", "校验",
                             "执行", "汇总", "回滚", "重试", "细分"):
                    return
            sink = self.on_thinking
            if sink is None and hasattr(self, '_agent_thinking_sink'):
                sink = self._agent_thinking_sink
            if sink is not None:
                try:
                    sink(phase, detail)
                except Exception:
                    pass


def _dispatch_action(agent: "TableAgent", intent: NLIntent, path: "Path",
                      sheet: str, res: "AgentResult") -> "AgentResult":
    """按 intent.action 派发到对应 _run_*（统一 _dispatch 与 _retry_dispatch，消除双份）。

    作为模块级自由函数（非 TableAgent 方法）：_dispatch/_retry_dispatch 闭包把 agent
    作首参传入，兼容 SimpleNamespace 等测试 mock（只需 mock _run_add 等，不需新方法）。
    col_add/col_delete/col_rename/col_list 映射为 col_op 后走 _run_col。
    默认按 set（修改）处理。
    """
    if intent.action == "modify":
        intent.action = "set"
    if intent.action == "add":
        return agent._run_add(intent, path, sheet, res)
    if intent.action == "delete":
        return agent._run_delete(intent, path, sheet, res)
    if intent.action == "get":
        return agent._run_get(intent, path, sheet, res)
    if intent.action == "col" or intent.action in (
            "col_add", "col_delete", "col_rename", "col_list"):
        if intent.action != "col":
            intent.extras.setdefault("col_op", intent.action)
        return agent._run_col(intent, path, sheet, res)
    return agent._run_set(intent, path, sheet, res)


class TableAgent:
    """表格操作 Agent：将自然语言意图解析为 Excel 的增删查改操作并执行。

    核心流程：
        1. 解析自然语言 → NLIntent
        2. 定位表格文件 (_resolve_table)
        3. 定位目标 sheet (_resolve_sheet)
        4. 按 action 分发到 _run_set / _run_delete / _run_get / _run_add
        5. 各分支内部完成：定位列 → 定位行 → 执行 CLI 操作

    Attributes:
        cli: 底层 Excel CLI 操作接口
        parser: 自然语言解析器
        resolver: 表格文件解析器
        column_cfg: 列别名配置
        row_cfg: 行定位规则配置
        ctx_cfg: 表格上下文配置（关键词→sheet 消歧）
    """

    def __init__(self, cli: CodeMakerCLI, parser: CodemakerNLParser,
                 resolver: TableResolver | None = None,
                 column_cfg: ColumnAliasConfig | None = None,
                 row_cfg: RowAliasConfig | None = None,
                 ctx_cfg: TableContextConfig | None = None,
                 short_form_cfg: ShortFormConfig | None = None,
                 sheet_cfg: SheetAliasConfig | None = None,
                 live_index: bool = True, enable_skill: bool = True,
                 enable_verify_repair_loop: bool = True,
                 enable_skill_tools_recovery: bool = True,
                 verify_repair_max_rounds: int = 3,
                 skill_tool_call_limit: int = 4):
        """初始化 TableAgent，各配置参数若不传则自动从配置文件加载默认值。

        Args:
            live_index: 是否在写操作后增量刷新全局表格索引。主 Agent（操作真实
                resources/）为 True；dry-run 预览的临时副本 Agent 为 False，
                避免把临时目录的文件污染进全局 _table_index.json。
            enable_skill: 是否挂载 skill（列别名/行规则/上下文/反模式/短形式）。
                True=正常加载 yaml；False=各 cfg 用空实例，定位仅靠原始 header
                匹配，且不写 evidence/不喂 skill_updater。供 A/B 对照测试用。
            enable_verify_repair_loop: 是否启用 verify→repair 迭代环（写后规则校验门控
                + 失败触发 repair→execute 回流，最多 verify_repair_max_rounds 轮）。
                False 退回原线性 pipeline + 单轮 retry。快路径优先：成功路径零额外 LLM 往返。
                环境变量 CODEMAKER_VERIFY_REPAIR_LOOP=0 可强制关闭（超时降级用）。
            enable_skill_tools_recovery: repair Level 2 是否绑定 skill tools（ReAct 探查）。
                False 时 Level 2 退回纯 LLM 诊断。
            verify_repair_max_rounds: repair 最大轮数（含首次执行共 max_rounds+1 次尝试）。
            skill_tool_call_limit: 单次 repair Level 2 的 skill tool 调用上限。
        """
        self.cli = cli
        self.parser = parser
        self.enable_skill = enable_skill
        # 环境变量覆盖：CODEMAKER_VERIFY_REPAIR_LOOP=0 强制关闭迭代环（超时降级），
        # CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS 可调轮数（默认 3）。
        # 迭代环每轮含 LLM 调用，3 轮串行最坏 ~90s；关闭后退回单轮 retry（~30s）。
        _env_loop = os.environ.get("CODEMAKER_VERIFY_REPAIR_LOOP", "").strip()
        if _env_loop in ("0", "false", "False", "off"):
            enable_verify_repair_loop = False
        self.enable_verify_repair_loop = enable_verify_repair_loop
        # §3 ExecuteAgent 去 LLM：=1 时 _phase_execute 失败路径跳过 verify-repair + D3 retry
        # 的 LLM 诊断/重试，失败直接结构化进 res.failures（#40），诊断+反模式归纳交 §5 ConcludeAgent。
        # 默认关（保持现状：失败路径含 LLM 诊断+重试+verify-repair 修复）。
        # §并发安全：用 threading.local 承载，避免单实例共享时 services.run_single
        # 临时设 True 被并发请求读到（实例属性突变跨线程互踩）。
        self._no_llm_local = threading.local()
        self._no_llm_local.val = os.getenv("CODEMAKER_EXECUTE_NO_LLM", "0") != "0"
        # P26：批级事务/部分回滚。opt-in（CODEMAKER_BATCH_TRANSACTIONAL=1），
        # 任一子任务硬失败 → 回滚整批前序已 commit op（不限于 G8 直接依赖 producer）。
        # 默认 off（保留 G8 链回滚：仅回滚失败步直接依赖的前序，避免牵连无关独立 op）。
        # P26 场景：district 成功 + combat 失败且非 combat 直接依赖 district 时，
        # 默认 district 留半成品（重跑 UNIQUE_VIOLATION）；strict 模式回滚 district 整批原子。
        self.batch_transactional = os.getenv("CODEMAKER_BATCH_TRANSACTIONAL", "0") == "1"
        # P19：execute_no_llm × enable_verify_repair_loop 互斥校验。前者=1 时
        # _phase_execute 失败路径早返跳 verify_repair（agent.py:5608），后者默认
        # True → 失败路径零修复（配置陷阱，低频但行为不确定）。同开时 warning
        # 提示（不强制改，保用户显式意图；CI/自动化应避免此组合）。
        self._check_p19_mutex_conflict()
        self.enable_skill_tools_recovery = enable_skill_tools_recovery
        _env_rounds = os.environ.get("CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS", "").strip()
        if _env_rounds:
            try:
                verify_repair_max_rounds = max(1, int(_env_rounds))
            except ValueError:
                pass
        self.verify_repair_max_rounds = verify_repair_max_rounds
        self.skill_tool_call_limit = skill_tool_call_limit
        # 思考流式回调（实例级，stream 模式注入），add_thinking 同步推送
        self._agent_thinking_sink = None
        # 步骤/工具流式回调（实例级，stream 模式注入），add 实时推送步骤进度
        self._agent_step_sink = None
        self._agent_tool_sink = None
        # LLM 调用计数器（capability: llm-call-instrumentation）：线程本地，per-run reset/snapshot
        # 必须在 StepAIEnhancer init 前赋值（后者依赖它，否则 AttributeError 致 6步流程降级）
        from ...llm_counter import LLMCounter
        self._llm_counter = LLMCounter()
        # P27：4-step NL 路径中间态 checkpoint {session_id: {stage: {intents, timestamp}}}。
        # parse/validate 后拍 NLIntent 序列化，stall 可从中间态续跑免 Step1 重 LLM
        # decompose。opt-in CODEMAKER_4STEP_CHECKPOINT=1。TableAgent 实例级（agent_service
        # 复用 self.agent，跨 run() 持久；session_id 隔离多会话）。
        self._nl_checkpoints: dict = {}
        # 6 步流程 AI 增强层：复用 parser 的 CodemakerClient 通道
        # LLM 失败时各 Step 自动降级走原规则路径，不阻塞流程
        self._ai_enhancer = None
        try:
            from .step_ai_enhancer import StepAIEnhancer
            _client = getattr(parser, "client", None)
            _model = getattr(parser, "model", "") or ""
            _dir = getattr(parser, "directory", "") or ""
            if _client is not None:
                self._ai_enhancer = StepAIEnhancer(
                    client=_client, model=_model, directory=_dir,
                    thinking_sink=lambda p, d: self._agent_thinking_sink(p, d) if self._agent_thinking_sink else None,
                    llm_counter=self._llm_counter,
                )
        except Exception:
            logger.warning("StepAIEnhancer 初始化失败，6步流程将降级为纯规则路径", exc_info=True)
        # 原则11/R8h:三 agent 串行链(Locator→Decompose→Validator)替代硬编码模板。
        # 复用 parser 通道 + cli,与 StepAIEnhancer 共享 LLMCounter。
        self._locator_agent = None
        self._decompose_agent = None
        self._validator_agent = None
        # O22 §9.1 ReplanAgent：失败后离线重规划（CODEMAKER_REPLAN_ON_FAILURE=0 默认关）
        self._replan_agent = None
        try:
            from ..subagent.locator_agent import LocatorAgent
            from ..subagent.decompose_agent import DecomposeAgent
            from ..subagent.validator_agent import ValidatorAgent
            from ..subagent.replan_agent import ReplanAgent
            _sink = lambda p, d: self._agent_thinking_sink(p, d) if self._agent_thinking_sink else None
            self._locator_agent = LocatorAgent(
                parser=parser, thinking_sink=_sink, cli=self.cli)
            self._decompose_agent = DecomposeAgent(
                parser=parser, thinking_sink=_sink, cli=self.cli)
            self._validator_agent = ValidatorAgent(
                parser=parser, thinking_sink=_sink, cli=self.cli)
            self._replan_agent = ReplanAgent(
                parser=parser, thinking_sink=_sink)
        except Exception:
            logger.warning("三 agent + ReplanAgent 初始化失败,跨表链路径降级走原规则", exc_info=True)
        if enable_skill:
            self.column_cfg = column_cfg or ColumnAliasConfig.load()
            self.row_cfg = row_cfg or RowAliasConfig.load()
            self.ctx_cfg = ctx_cfg or TableContextConfig.load()
            self.short_form_cfg = short_form_cfg or ShortFormConfig.load()
            self.sheet_cfg = sheet_cfg or SheetAliasConfig.load()
            self.anti_pattern_cfg = AntiPatternConfig.load()
        else:
            self.column_cfg = column_cfg or ColumnAliasConfig()
            self.row_cfg = row_cfg or RowAliasConfig()
            self.ctx_cfg = ctx_cfg or TableContextConfig()
            self.short_form_cfg = short_form_cfg or ShortFormConfig()
            self.sheet_cfg = sheet_cfg or SheetAliasConfig()
            self.anti_pattern_cfg = AntiPatternConfig()
        # resolver 在 skill 配置后构造，注入 sheet_cfg 让 TableResolver 走别名消歧
        self.resolver = resolver or TableResolver(ctx_cfg=self.ctx_cfg,
                                                  sheet_cfg=self.sheet_cfg)
        self.live_index = live_index
        # 5.5：跨表搜索走 _table_index.json，缓存加载结果（写后刷新时置空）。
        self._index_cache = None
        # ColumnMatcher 实例缓存：(stem, sheet, headers 签名) -> matcher，索引刷新时清空。
        self._matcher_cache: dict = {}
        # 类型行规范名 alias 缓存（原则9）：(path, sheet, headers 签名) -> {规范名: row1表头}
        self._type_alias_cache: dict = {}
        # 4.3：写操作快照回滚。仅在操作真实 resources 的主 Agent 挂 auditor，
        # dry-run 临时副本不挂（无需回滚，避免污染备份目录）。
        self.auditor = None
        if live_index:
            try:
                from .backup_audit import BackupAuditor
                ws = getattr(cli, "workspace", None)
                if ws:
                    self.auditor = BackupAuditor(workspace=ws)
            except Exception:
                self.auditor = None
        # verify-repair 迭代环：SkillExecutor（repair Level 2 skill tool 引擎）+ playbook 路由。
        # RepairContext 为 per-run scratchpad，在 run() 内构造、run 结束丢弃（不在此实例化）。
        self.skill_executor = None
        self.repair_playbook = None
        if enable_verify_repair_loop or enable_skill_tools_recovery:
            try:
                from .llm_context import SkillExecutor
                from ..repair.repair_playbook import RepairPlaybook
                self.skill_executor = SkillExecutor.for_agent(cli=cli)
                self.repair_playbook = RepairPlaybook()
            except Exception:
                logger.warning("SkillExecutor/RepairPlaybook 初始化失败，verify-repair 将降级", exc_info=True)
                self.skill_executor = None
                self.repair_playbook = None
        # 5.8：主 Agent 初始化时懒刷新一次索引，拾取会话开始前的外部改表，
        # 使解析阶段注入的路由/列名为最新（dry-run 临时副本 Agent 不触发）。
        if live_index:
            self._refresh_index_after_write(None)

    @property
    def execute_no_llm(self) -> bool:
        """读当前线程的 no_llm 标志（threading.local 隔离并发请求）。"""
        _local = getattr(self, "_no_llm_local", None)
        if _local is None:
            return False
        return bool(getattr(_local, "val", False))

    @execute_no_llm.setter
    def execute_no_llm(self, value: bool) -> None:
        """写当前线程的 no_llm 标志（仅影响本线程，不污染并发请求）。"""
        _local = getattr(self, "_no_llm_local", None)
        if _local is None:
            # 绕过 __init__ 的测试场景（object.__new__）懒初始化
            _local = threading.local()
            self._no_llm_local = _local
        _local.val = bool(value)

    # §低危修复：_cancel_event 改 thread-local property（同 execute_no_llm 模式）。
    # 原 self._cancel_event 实例属性被单实例 agent 跨请求共享：agent_service.worker
    # 设 self.agent._cancel_event = cancel_event，并发请求 A/B 互踩（B 的 cancel_event
    # 覆盖 A 的 → A 的取消信号被 B 读到或 A 取消无法触发）。property 让读写都走
    # 当前线程 local，per-request 隔离。29 处 getattr(self, "_cancel_event", None)
    # 与赋值透明走 property，无需改调用点。
    @property
    def _cancel_event(self) -> Any:
        _local = getattr(self, "_cancel_local", None)
        if _local is None:
            return None
        return getattr(_local, "val", None)

    @_cancel_event.setter
    def _cancel_event(self, value: Any) -> None:
        _local = getattr(self, "_cancel_local", None)
        if _local is None:
            _local = threading.local()
            self._cancel_local = _local
        _local.val = value

    def _check_p19_mutex_conflict(self) -> None:
        """P19：execute_no_llm × enable_verify_repair_loop 互斥校验。

        前者=1 时 _phase_execute 失败路径早返跳 verify_repair（见 _phase_execute
        内 `if self.execute_no_llm: return res`），后者默认 True → 失败路径
        零修复（配置陷阱，低频但行为不确定）。同开时 warning 提示（不强制改，
        保用户显式意图；CI/自动化应避免此组合）。
        """
        if getattr(self, "execute_no_llm", False) and getattr(self, "enable_verify_repair_loop", False):
            logger.warning(
                "P19 互斥：CODEMAKER_EXECUTE_NO_LLM=1 与 enable_verify_repair_loop="
                "True 同开。前者使 _phase_execute 失败早返跳 verify_repair → "
                "失败路径零修复。建议显式 CODEMAKER_VERIFY_REPAIR_LOOP=0 或不设 "
                "EXECUTE_NO_LLM。"
            )

    @staticmethod
    def _compute_rollback_targets(orig_idx: int, partitions: list,
                                   deps_map: dict, batch_transactional: bool,
                                   has_deps: bool) -> tuple[str, list]:
        """P26：硬失败时计算回滚目标集。

        返回 (mode, targets)。
        - batch_transactional=True → "P26-batch-transactional"，回滚整批前序已
          commit op（不限直接依赖，批级原子）。
        - 否则 has_deps=True → "G8-chain"，仅回滚 _deps_map[orig_idx] 直接依赖
          producer（避免牵连无关独立 op）。
        - 否则 → "none"，空集（无依赖不回滚前序）。
        """
        if batch_transactional:
            targets = [pi for pi in range(len(partitions))
                       if partitions[pi].get("backup")
                       and not partitions[pi].get("rolled_back")
                       and pi != orig_idx]
            return "P26-batch-transactional", targets
        if has_deps:
            return "G8-chain", list(deps_map.get(orig_idx, set()))
        return "none", []

    def _save_nl_checkpoint(self, session_id: str, stage: str,
                             intents: list,
                             completed_op_keys: Optional[list] = None) -> bool:
        """P27：拍 4-step NL 路径中间态 checkpoint（parse/validate 后 + Step5 增量）。

        序列化 NLIntent[]（to_checkpoint_dict）到 _nl_checkpoints[session_id][stage]，
        stall 可从中间态续跑免 Step1 重 LLM decompose。opt-in
        CODEMAKER_4STEP_CHECKPOINT=1 时写，默认 off。失败静默返 False 不阻断。

        completed_op_keys（O14）：Step5 增量 save 时传已成功 op 的 orig_idx 列表，
        供 resume 跳过已成功 op（filter ordered_idx）。缺省 None→空 list（parse/validate
        后拍，无 op 完成）。
        """
        if os.getenv("CODEMAKER_4STEP_CHECKPOINT", "0") != "1":
            return False
        if not session_id or not intents:
            return False
        try:
            from ..parser.nl_parser import NLIntent
            serialized = [it.to_checkpoint_dict()
                          if isinstance(it, NLIntent) else it
                          for it in intents]
            self._nl_checkpoints.setdefault(session_id, {})[stage] = {
                "intents": serialized,
                "timestamp": datetime.now().isoformat(),
                "stage": stage,
                "completed_op_keys": list(completed_op_keys) if completed_op_keys else [],
            }
            return True
        except Exception:
            logger.debug("P27 _save_nl_checkpoint 失败 stage=%s", stage, exc_info=True)
            return False

    def _load_nl_checkpoint(self, session_id: str, stage: str):
        """P27：加载 NL checkpoint（_save_nl_checkpoint 的逆）。

        返回反序列化的 NLIntent[] 或 None（无 checkpoint）。供 stall 续跑：
        跳过已成功 Step5 op + 从未完成的中间态继续。
        """
        ckpt = self._nl_checkpoints.get(session_id, {}).get(stage)
        if not ckpt:
            return None
        try:
            from ..parser.nl_parser import NLIntent
            return [NLIntent.from_checkpoint_dict(d)
                    for d in ckpt.get("intents") or []]
        except Exception:
            logger.debug("P27 _load_nl_checkpoint 失败 stage=%s", stage, exc_info=True)
            return None

    def _resume_from_checkpoint(self, session_id: str) -> tuple:
        """P27+O14：stall 续跑入口。返回 (resumed_intents, stage, completed_op_keys)
        或 (None, None, None)。

        优先 post_validate checkpoint（最远中间态），回退 post_parse。
        opt-in CODEMAKER_4STEP_CHECKPOINT=1 + CODEMAKER_4STEP_RESUME 非空时
        run() 入口调用。调用方据 resumed_intents 跳过 Step1 parse + completed_op_keys
        跳过已成功 Step5 op（filter ordered_idx），从未完成中间态继续。
        """
        if os.getenv("CODEMAKER_4STEP_CHECKPOINT", "0") != "1":
            return None, None, None
        if not session_id:
            return None, None, None
        for stage in ("post_validate", "post_parse"):
            ckpt = self._nl_checkpoints.get(session_id, {}).get(stage)
            if not ckpt:
                continue
            intents = self._load_nl_checkpoint(session_id, stage)
            if intents:
                completed = list(ckpt.get("completed_op_keys") or [])
                return intents, stage, completed
        return None, None, None

    def _save_nl_progress(self, session_id: str, intents: list,
                          completed_op_keys: list) -> bool:
        """O14：Step5 增量回写 checkpoint（每成功 op 后调）。

        更新 post_validate（或回退 post_parse）stage 的 intents + completed_op_keys，
        使 stall 后 resume 能跳过更多已成功 op。intents 须含已执行 op 的 execution
        字段（to_checkpoint_dict 已序列化）。completed_op_keys = 已成功 op 的
        orig_idx 列表。opt-in CODEMAKER_4STEP_CHECKPOINT=1，失败静默 False。
        """
        if os.getenv("CODEMAKER_4STEP_CHECKPOINT", "0") != "1":
            return False
        if not session_id or not intents:
            return False
        ckpts = self._nl_checkpoints.get(session_id, {})
        for stage in ("post_validate", "post_parse"):
            if stage not in ckpts:
                continue
            return self._save_nl_checkpoint(
                session_id, stage, intents, completed_op_keys)
        return False

    def _make_matcher(self, headers: list, stem: str, sheet: str,
                      path: Path = None) -> ColumnMatcher:
        """构建带列别名 + 短形式扩展 + 类型行规范名的列匹配器（统一注入点）。

        按 (stem, sheet, headers 签名) 缓存实例，避免每 op 重建。
        表结构(headers)变化或索引刷新后自动失效（_matcher_cache 清空）。

        原则9（R8c）：type_aliases（row2 规范名→row1 表头）合并进 yaml_aliases，
        使 matcher stage1 对 plain row2 名（item_type/quality/name 等非点分键）也精确命中。
        原 R8 type_aliases 仅经 _translate_dotted_keys 处理点分键，plain 键漏覆盖。
        """
        key = (stem, sheet, tuple(str(h) for h in headers))
        cached = self._matcher_cache.get(key)
        if cached is not None:
            return cached
        yaml_aliases = self.column_cfg.all_aliases(stem, sheet)
        # 合并 type_aliases（row2 规范名）使 plain 规范键也能 stage1 命中
        if path is not None:
            try:
                ta = self._type_aliases(path, sheet, headers)
                if ta:
                    merged = dict(yaml_aliases)
                    merged.update(ta)
                    yaml_aliases = merged
            except Exception:
                pass
        matcher = ColumnMatcher(
            headers,
            yaml_aliases,
            short_forms=self.short_form_cfg.reverse_map(stem, sheet),
        )
        self._matcher_cache[key] = matcher
        return matcher

    def _type_aliases(self, path: Path, sheet: str, headers: list) -> dict[str, str]:
        """构建类型行(row2)规范名→row1表头 的 alias map（原则9）。

        splitter 产出点分规范名（option_function.function_type / option_function.data.1.conv_id），
        read_header 返回 row1 中文，末段匹配常失败。此 map 让 _translate_dotted_keys
        把整名规范键映射到对应 row1 列名，交 matcher stage1 精确命中。
        按 (path, sheet, headers 签名) 缓存。
        """
        key = (str(path), sheet, tuple(str(h) for h in headers))
        cached = self._type_alias_cache.get(key)
        if cached is not None:
            return cached
        out: dict[str, str] = {}
        try:
            type_row = self.cli.read_type_row(path, sheet) if self.cli else []
        except Exception:
            type_row = []
        for h, t in zip(headers, type_row):
            if not t or not h:
                continue
            tname = str(t).split(":")[0].strip()
            if tname and tname != str(h):
                out[tname] = h
        self._type_alias_cache[key] = out
        return out

    # 泛化实体前缀：这些词是"类别名"而非具体对象名，定位时应剥离，
    # 由后续的具体名（如"饕餮"）来定位行。用于 _resolve_table 与前缀剥离重试。
    _ENTITY_PREFIXES = frozenset({
        "任务", "道具", "法术", "装备", "帮派", "法宝", "仙友",
        "灵兽", "宠物", "英雄", "建筑", "场景", "活动",
    })

    # 泛 id 词：用户/LLM 用这些词指代 id 列时，若 sheet 有多个 id 列则需消歧
    _GENERIC_ID_TERMS = frozenset({"id", "ID", "Id", "编号"})

    def _id_like_columns(self, headers: list) -> list[Tuple[int, str]]:
        """返回 headers 中 id/编号 类列 [(1-based idx, cleaned name)]，供多 id 消歧。"""
        out: list[Tuple[int, str]] = []
        for i, h in enumerate(headers, start=1):
            if not h:
                continue
            hn = str(h).split(":")[0].strip()
            if not hn:
                continue
            hl = hn.lower()
            if ("id" in hl) or ("编号" in hn):
                if not any(bad in hn for bad in ("描述", "icon", "图标")):
                    out.append((i, hn))
        return out

    def _check_id_ambiguity(self, headers: list, key: str) -> tuple[bool, list[Tuple[int, str]]]:
        """字段键为泛 id 词且 sheet 有>1 个 id 列 → 歧义。返回 (是否歧义, 候选列表)。

        用于 add/set 多字段写入前拦截：避免泛「id」被 dict 别名静默命中某一具体 id 列
        （如 ability 表的 id→被动id 过时别名），改让用户用具体列名（神通id/技能id/被动id）重试。

        精确列名优先：泛 id 词恰等于某候选列名（如 key='编号' 命中表头 '编号' 列，
        同表另有 '交互效果编号'）→ 不判歧义，直接用该精确匹配列。仅当泛 id 词
        不精确等于任何候选列名时才判歧义（如 key='id' 但表头只有 '神通id'/'技能id'）。
        """
        if not key or key.strip().lower() not in {t.lower() for t in self._GENERIC_ID_TERMS}:
            return False, []
        cands = self._id_like_columns(headers)
        if len(cands) <= 1:
            return False, cands
        key_clean = key.strip()
        for idx, name in cands:
            if name == key_clean:
                return False, [(idx, name)]
        return True, cands

    def _refresh_index_after_write(self, path: Path) -> bool:
        """写操作后增量刷新表格索引，保证多轮操作中行级倒排索引实时更新。

        新增/删除/改名等结构性写入会改变行级倒排索引的键（如新增"测试兽"后，
        下一轮"查询测试兽"才能被 _resolve_table 策略1 命中）。refresh_if_changed
        按 MD5 增量重扫，仅重扫变更文件，开销可控。失败静默（不影响写入主流程）。

        仅当 live_index=True（操作真实 resources/）时刷新；dry-run 预览的临时
        副本不刷新，避免临时目录文件污染全局 _table_index.json。

        返回 True 表示刷新成功（或非 live_index 跳过），False 表示刷新失败
        （调用方可据此设置 AgentResult.index_dirty）。
        """
        if not self.live_index:
            return True
        try:
            from ..locator.table_index import refresh_if_changed
            ws = getattr(self.cli, "workspace", None)
            if ws is not None:
                res = refresh_if_changed(Path(ws))
                # 5.8：索引刷新后重置 skill_context 缓存（路由/列名依赖索引，
                # 否则新增/改名的表/列在解析注入时仍是旧数据）。
                if getattr(res, "refreshed", False):
                    try:
                        from .skill_context import reset_skill_context_cache
                        reset_skill_context_cache()
                    except Exception:
                        logger.warning("skill_context 缓存重置失败（索引已刷新但路由/列名缓存可能过时）", exc_info=True)
                    # 5.5：索引变更后置空本 Agent 的索引缓存，下次跨表搜索重载。
                    self._index_cache = None
                    # 索引变更可能伴随表结构/列名变化，清空 matcher 缓存。
                    self._matcher_cache = {}
                    self._type_alias_cache = {}
            return True
        except Exception:
            logger.warning("索引刷新失败（数据已写但行级倒排索引未更新，下次查询可能漏命中）", exc_info=True)
            return False

    def _get_index(self):
        """5.5：加载并缓存 `_table_index.json`（写后刷新时置空重载）。失败返回 []。"""
        if self._index_cache is None:
            try:
                from ..locator.table_index import load_index
                self._index_cache = load_index()
            except Exception:
                self._index_cache = []
        return self._index_cache


    def _auto_sort_after_write(self, path: Path, sheet: str, res: AgentResult) -> None:
        """写操作后按主键列(第1列)升序重排数据行。

        遵循用户工作流"写操作→排序→最后更新公式"：
          - 排序阶段整行搬移值+样式，数据区内公式按行置换重写行引用
            （行内计算随行搬移，如 F3=SUM(B3:E3) 移到 row7 后改 SUM(B7:E7)）。
          - 区外聚合公式（汇总行）引用物理行号，纯排序不改；但插入新行后
            其范围可能未含新行 → 扫描语义缺口，标 step 供 AI 决策扩展。
        排序失败不阻断主流程，仅记一步。
        """
        if not hasattr(self.cli, "sort_sheet"):
            return
        try:
            r = self.cli.sort_sheet(path, sheet, key_col=1, ascending=True)
            if r.ok:
                fw = r.data.get("formula_rewritten", 0)
                note = f"，公式行引用重写{fw}个" if fw else ""
                res.add("auto_sort", True,
                        f"按第1列升序重排 {r.data.get('sorted', 0)} 行{note}")
            else:
                res.add("auto_sort", False, r.error or "")
                return
        except Exception as e:
            res.add("auto_sort", False, str(e))
            return

        # 聚合范围缺口检测：区外汇总公式范围未含全部数据行 → 标缺口供 AI 扩展
        try:
            from ..formula.formula_semantics import scan_sheet_formulas
            dsr = self.cli._resolve_data_start(path, sheet)
            ws = self.cli._load(path)[sheet]
            last_row = self.cli._last_data_row(ws, dsr)
            sems = scan_sheet_formulas(path, sheet, data_start_row=dsr)
            gaps: list[str] = []
            for sem in sems:
                # 区外聚合公式：聚合函数 + 不在数据末行 + 引用覆盖数据区
                if not sem.is_aggregate or sem.is_last_row or not sem.covers_data_area:
                    continue
                for ref in sem.refs:
                    if ref.rows is None:
                        continue
                    rlo, rhi = ref.rows
                    if rhi < last_row and rlo >= dsr:
                        gaps.append(f"{sem.cell}({sem.formula}) 末行{rhi}<数据末行{last_row}")
                        break
            if gaps:
                res.add("formula_gap", True,
                        f"聚合范围未含全部数据行，需AI扩展：{'；'.join(gaps)}")
        except Exception as e:
            res.add("formula_gap", False, str(e))

    def _resolve_table(self, intent: NLIntent) -> tuple[Path | None, str | None]:
        """根据意图定位目标表格文件和 sheet。

        策略（按优先级，结合上下文置信度）：
            0a. 显式 table_hint **精确 stem** 命中（LLM 已判明实体类别，高置信 → 最优先）
            1.  行级倒排索引：用 locator_value 全局搜索（最可靠信号）
            0b. table_hint 模糊子串/中文别名命中（低置信 → 降级到倒排索引之后，
                避免"新增灵兽朱雀"类被弱 hint 抢先误路由）
            2.  TableResolver 解析（score >= 0.20 且非辅助sheet）
            3.  回退到 TableResolver 低分数结果
            4.  用 intent.table_hint/sheet_hint 模糊匹配

        splitter 源意图 table_hint 由规则模板显式设定（高置信），精确 stem 命中
        优先于行索引——否则 modify 的 locator_value（如 reward_id 10090）会在
        行索引里命中 item 等它表，覆盖正确 table_hint（reward），导致改错表。
        """
        # splitter 源：精确 stem 优先（规则高置信，跳过行索引抢匹配）
        if intent.table_hint and (intent.extras or {}).get("source") == "splitter":
            tables = self.cli.list_tables()
            hint = intent.table_hint.strip()
            for p in tables:
                if p.stem == hint:
                    return p, intent.sheet_hint

        # 策略1：行级倒排索引 — 最精准的定位方式。
        # 对定位值做分词生成多候选（如"灵兽饕餮"→["灵兽饕餮","饕餮"]，剥离"灵兽"类
        # 泛化前缀），对每个候选在所有表的 row_index 中打分，取最高分：
        #   完全相等=3 · 前缀命中=2 · 被包含=1 · 反向包含=0.5，再乘候选长度。
        # 取代原先"首个双向子串命中即返回"的贪婪逻辑（会被短噪声词误命中错表）。
        # 优先于策略0a（LLM hint）：modify/delete 有 locator 时，行索引精确匹配
        # 比 LLM 可能误给的 table_hint（如「法宝」→fabao）更可靠。add 无 locator
        # 时此块跳过，自然落到策略0a。
        if intent.locator_value:
            from ..parser.segmenter import candidate_terms
            idx = self._get_index()
            tables = self.cli.list_tables()
            stem_to_path = {tp.stem: tp for tp in tables}
            lv = intent.locator_value
            cands = [c for c in candidate_terms(lv)
                     if c not in self._ENTITY_PREFIXES]
            if lv not in cands:
                cands.insert(0, lv)
            best: tuple[float, Path, str] | None = None
            for cand in cands:
                if len(cand) < 2:
                    continue
                for t in idx:
                    tp = stem_to_path.get(t.stem)
                    if tp is None:
                        continue
                    # perf_*/qa_test_* 是性能/测试数据表，行索引命中降权 0.3
                    # （避免"灵兽饕餮"→perf_pet_100k 误路由，应优先业务表 pet.xlsx）
                    _penalty = 0.3 if (t.stem.startswith("perf_")
                                       or t.stem.startswith("qa_test")) else 1.0
                    for s in t.sheets:
                        for _col, mapping in s.row_index.items():
                            for k in mapping:
                                if k == cand:
                                    w = 3.0
                                elif k.startswith(cand):
                                    w = 2.0
                                elif cand in k:
                                    w = 1.0
                                elif k in cand:
                                    w = 0.5
                                else:
                                    continue
                                score = w * len(cand) * _penalty
                                if best is None or score > best[0]:
                                    best = (score, tp, s.name)
            if best is not None:
                return best[1], best[2]

        # 策略0a：显式 table_hint **精确 stem** 命中——LLM 明确给出合法表名，
        # 置信度高。放在行索引之后：add 无 locator 时落到此，modify 有 locator
        # 但行索引未命中（如新 id 不在索引）时也落到此。
        if intent.table_hint:
            tables = self.cli.list_tables()
            hint = intent.table_hint.strip()
            for p in tables:
                if p.stem == hint:
                    return p, intent.sheet_hint

        # 策略0b：table_hint 模糊子串/中文别名命中（低置信，降级到倒排索引之后）。
        # 倒排未命中时才用弱 hint，避免弱 hint 抢先误路由。
        if intent.table_hint:
            tables = self.cli.list_tables()
            hint = intent.table_hint.strip()
            # 双向子串模糊命中（hint="pet" 匹配 pet_level 等，取最短 stem 最具体）
            sub_hits = [p for p in tables if hint in p.stem or p.stem in hint]
            if sub_hits:
                sub_hits.sort(key=lambda p: len(p.stem))
                return sub_hits[0], intent.sheet_hint
            # 中文实体别名 → 文件（如"灵兽"→pet.xlsx）
            try:
                from ..locator.alias_mapping import AliasMapping
                am = AliasMapping.load()
                fp = am.lookup(hint)
                if fp:
                    target_stem = Path(fp).stem
                    for p in tables:
                        if p.stem == target_stem:
                            return p, intent.sheet_hint
            except Exception:
                pass

        # 策略2：TableResolver
        r = self.resolver.resolve(intent.raw)
        if r is not None and r.score >= 0.20:
            helper_kw = ("说明", "备注", "副本", "Sheet1", "sheet1", "CONFIG")
            if not any(kw in r.sheet for kw in helper_kw):
                tables = self.cli.list_tables()
                for tp in tables:
                    if tp.stem == r.table_stem:
                        return tp, r.sheet
                ws = getattr(self.cli, "workspace", None)
                if ws is not None:
                    p = Path(ws) / r.table_path
                    if p.exists():
                        return p, r.sheet

        # 策略3：TableResolver 低分回退
        if r is not None:
            tables = self.cli.list_tables()
            for tp in tables:
                if tp.stem == r.table_stem:
                    return tp, r.sheet

        # 策略4：用 intent 中的 table_hint / sheet_hint 模糊匹配
        tables = self.cli.list_tables()
        if not tables:
            return None, None
        if intent.table_hint:
            for p in tables:
                if intent.table_hint in p.stem or p.stem in intent.table_hint:
                    return p, intent.sheet_hint
        if intent.sheet_hint:
            for p in tables:
                if intent.sheet_hint in self.cli.get_sheets(p):
                    return p, intent.sheet_hint
        # 策略5：模糊候选 + LLM 消歧（高置信度直接采用，低置信度交 LLM 选）
        query = intent.table_hint or intent.locator_value or intent.raw
        sug = self._fuzzy_suggest(query, [p.stem for p in tables])
        if sug:
            top_stem, top_score = sug[0]
            if top_score >= 0.6:
                top_p = next((p for p in tables if p.stem == top_stem), None)
                if top_p is not None:
                    return top_p, intent.sheet_hint
            cand_stems = [s for s, _ in sug[:5]]
            reply = self._llm_disambiguate(
                f"用户表格操作指令：「{intent.raw}」\n候选表名：{', '.join(cand_stems)}\n"
                f"请判断最可能操作的是哪张表，只回答一个表名（从候选中选），不要其他文字。")
            if reply:
                for p in tables:
                    if p.stem in reply or reply in p.stem:
                        return p, intent.sheet_hint
        return None, None

    def _resolve_sheet(self, path: Path, intent: NLIntent) -> str | None:
        """在多 sheet 场景下确定目标 sheet。

        消歧策略（按优先级）：
            1. 若 intent.sheet_hint 直接命中 → 直接返回
            2. 多业务 sheet + 有 locator_value → 逐 sheet 用行定位匹配评分，
               选得分最高者（exact > startswith > contains，行数多的主表加分）
            3. 用 ctx_cfg 中的关键词映射消歧
            4. 返回首个业务 sheet
            5. 兜底返回第一个 sheet

        Returns:
            匹配的 sheet 名，无法确定时返回 None。
        """
        sheets = self.cli.get_sheets(path)
        stem = path.stem
        # 策略1：直接命中（须业务 sheet，防 LLM 猜到说明/CONFIG sheet）
        if intent.sheet_hint and intent.sheet_hint in sheets \
                and _is_business_sheet(intent.sheet_hint):
            return intent.sheet_hint
        # 策略1.5：sheet 别名（sheet_hint 或文本中的别名 → 真实 sheet）
        alias_sn = self.sheet_cfg.resolve(stem, intent.sheet_hint or "")
        if alias_sn and alias_sn in sheets and _is_business_sheet(alias_sn):
            return alias_sn
        for alias, real_sn in self.sheet_cfg.aliases_for(stem).items():
            if alias and alias in (intent.raw or "") and real_sn in sheets and _is_business_sheet(real_sn):
                return real_sn
        # 策略2：多业务 sheet + 有行定位值 → 逐 sheet 行定位评分
        # 取匹配度最高的（exact > contains，定位列最小为非"名称"则精确度更优）
        business_sheets = [s for s in sheets if _is_business_sheet(s)]
        if len(business_sheets) > 1 and intent.locator_value:
            best_sheet = None
            best_score = 0
            for s in business_sheets:
                headers = self.cli.read_header(path, s)
                matcher = self._make_matcher(headers, stem, s, path)
                rules = self.row_cfg.rules_for(stem, s)
                loc_col_name = "名称"  # 默认定位列名
                match_mode = "contains"  # 默认匹配模式
                for rl in rules:
                    if "locator_column" in rl:
                        loc_col_name = rl["locator_column"]
                    if "match" in rl:
                        match_mode = rl["match"]
                lm = matcher.match(loc_col_name)
                if lm:
                    found = self._locate_row(path, s, lm.index, intent.locator_value, match_mode)
                    if found is not None:
                        # 评分: exact=3, startswith=2, contains=1
                        score = {"exact": 3, "startswith": 2, "contains": 1}.get(match_mode, 1)
                        # 主数据表（行数多）加分，打破平局
                        rows = self.cli.read_sheet(path, s)
                        score += len(rows) * 0.001
                        if score > best_score:
                            best_score = score
                            best_sheet = s
            if best_sheet is not None:
                return best_sheet
        # 策略3：用 ctx_cfg 关键词消歧
        ctx_sheet = self.ctx_cfg.sheet_for_keywords(stem, intent.raw)
        if ctx_sheet and ctx_sheet in sheets and _is_business_sheet(ctx_sheet):
            return ctx_sheet
        # 策略3.5：多业务 sheet 无法用规则消歧 → LLM 从候选 sheet 中选一个
        if len(business_sheets) > 1:
            reply = self._llm_disambiguate(
                f"用户对表「{stem}」的操作指令：「{intent.raw}」\n"
                f"该表有多个 sheet：{', '.join(business_sheets)}\n"
                f"请判断最可能操作哪个 sheet，只回答一个 sheet 名，不要其他文字。")
            if reply:
                for s in business_sheets:
                    if s in reply or reply in s:
                        return s
        # 策略4：返回首个业务 sheet
        for s in sheets:
            if _is_business_sheet(s):
                return s
        # 策略5：兜底
        return sheets[0] if sheets else None

    def _locate_row(self, path: Path, sheet: str, col_idx: int, value: str,
                    match_mode: str = "exact",
                    allow_exact_fallback: bool = False) -> RowMatch | None:
        """在指定 sheet 的指定列中查找匹配值的行，返回带置信度的 RowMatch。

        Args:
            path: 表格文件路径
            sheet: sheet 名称
            col_idx: 定位列的序号（1-based）
            value: 待匹配的定位值
            match_mode: 匹配模式 — "exact"（精确）、"startswith"（前缀）、"contains"（包含）
            allow_exact_fallback: 仅 get 路径传 True。exact 模式完全未命中时，
                自动回退 contains 重试一次（治 locator_value 被截断，如"饕餮一阶"
                误解析为"饕餮"导致 exact 失败）。命中降置信度 + 标记 method，
                多行命中走歧义机制（不静默采用）。set/delete 不传，避免误改/误删。

        Returns:
            RowMatch（含 row/confidence/method/ambiguous/alternatives），未找到返回 None。

        匹配分层策略（仅 contains 模式使用多层回退，层级越靠后置信度越低）：
            层1 contains_direct：全角括号标准化后直接 in 判断，conf=0.90
            层2 contains_paren_stripped：双方去掉括号内文本后再 match，conf=0.75
            层3 contains_num_stripped：再去掉末尾中/日数字后 match，conf=0.55
                （容忍编号后缀差异，但也是最容易把"技能1"误配到"技能2"的风险层）
            兜底：若定位值带关键词前缀（如"任务""道具"），去掉后再递归尝试，
                命中后在原置信度基础上乘以 ROW_PREFIX_STRIP_PENALTY

        同一层级内命中多行视为"歧义"：取第一行作为主结果，
        其余记入 alternatives，并对置信度打折（ROW_AMBIGUOUS_PENALTY），
        调用方应据此触发二次确认/LLM 仲裁，而非静默采用首行。

        索引快路径（locate_row_via_index）：入口先查 _table_index.json 的 row_index
        倒排（仅名称/id 类列），命中经单元格实际值校验后直接返回，跳过全表遍历。
        索引列覆盖不到或校验不通过 → 回退原分层遍历。
        """
        # 索引快路径：row_index 倒排命中 + 单元格实际值校验防索引过期
        if hasattr(self.cli, "locate_row_via_index"):
            idx_hit = self.cli.locate_row_via_index(path, sheet, col_idx, value, match_mode)
            if idx_hit:
                row_nums, _col_name, _matched = idx_hit
                method = {"exact": "index_exact", "startswith": "index_startswith",
                          "contains": "index_contains"}.get(match_mode, "index_contains")
                conf = ROW_METHOD_CONFIDENCE.get(method, 0.85)
                verified: list[tuple[int, str]] = []
                for r in row_nums:
                    v = self.cli._read_cell_value(path, sheet, r, col_idx) \
                        if hasattr(self.cli, "_read_cell_value") else None
                    if v is not None:
                        verified.append((r, str(v).strip()))
                if verified:
                    ambiguous = len(verified) > 1
                    if ambiguous:
                        conf = round(conf * ROW_AMBIGUOUS_PENALTY, 4)
                    return RowMatch(row=verified[0][0], value=verified[0][1],
                                    confidence=conf, method=method,
                                    ambiguous=ambiguous,
                                    alternatives=verified[1:])

        rows = self.cli.read_sheet(path, sheet)
        start = self.cli._resolve_data_start(path, sheet) if hasattr(self.cli, "_resolve_data_start") else getattr(self.cli, "data_start_row", 5)

        # 括号递归清洗：去除所有嵌套括号内容（如 "名称(稀有(限定))" -> "名称"）
        def _strip_parens(s: str) -> str:
            prev = None
            while prev != s:
                prev = s
                s = re.sub(r'[（(][^）()]*[）)]', '', s)
            return s

        # 末尾数字清洗：去掉末尾中文/阿拉伯数字（如 "技能3" -> "技能"）
        def _strip_trailing_num(s: str) -> str:
            return re.sub(r'[\d一二三四五六七八九十百千万]+$', '', s)

        val_norm = value.replace("\uff08", "(").replace("\uff09", ")")
        val_no_paren = _strip_parens(val_norm)
        val_stripped = _strip_trailing_num(val_no_paren)

        # 按层级收集命中，而非命中即返回——用于检测同层歧义（多行同名）
        layer_hits: dict[str, list[Tuple[int, str]]] = {
            "exact": [], "startswith": [],
            "contains_direct": [], "contains_paren_stripped": [], "contains_num_stripped": [],
        }
        for i, row in enumerate(rows):
            cell = row[col_idx - 1] if 0 <= col_idx - 1 < len(row) else None
            if cell is None:
                continue
            cs = str(cell).strip()
            cs_norm = cs.replace("\uff08", "(").replace("\uff09", ")")
            if match_mode == "contains":
                if val_norm in cs_norm:
                    layer_hits["contains_direct"].append((start + i, cs))
                    continue
                cs_no_paren = _strip_parens(cs)
                if val_no_paren and cs_no_paren and val_no_paren in cs_no_paren:
                    layer_hits["contains_paren_stripped"].append((start + i, cs))
                    continue
                cs_stripped = _strip_trailing_num(cs_no_paren)
                if val_stripped and cs_stripped and val_stripped in cs_stripped:
                    layer_hits["contains_num_stripped"].append((start + i, cs))
            elif match_mode == "startswith":
                if cs_norm.startswith(val_norm):
                    layer_hits["startswith"].append((start + i, cs))
            else:
                if cs_norm == val_norm:
                    layer_hits["exact"].append((start + i, cs))

        for layer in ("exact", "startswith", "contains_direct",
                      "contains_paren_stripped", "contains_num_stripped"):
            hits = layer_hits.get(layer) or []
            if not hits:
                continue
            conf = ROW_METHOD_CONFIDENCE[layer]
            ambiguous = len(hits) > 1
            if ambiguous:
                conf = round(conf * ROW_AMBIGUOUS_PENALTY, 4)
            row_no, cell_val = hits[0]
            return RowMatch(row=row_no, value=cell_val, confidence=conf, method=layer,
                            ambiguous=ambiguous, alternatives=hits[1:])

        sub = self._locate_row_strip_prefix(path, sheet, col_idx, value, match_mode, depth=0)
        if sub is not None:
            sub.confidence = round(sub.confidence * ROW_PREFIX_STRIP_PENALTY, 4)
            sub.method = sub.method + "+prefix_stripped"
            return sub
        # exact 模式最终兜底（仅 get 路径 allow_exact_fallback=True 时启用）：
        # locator_value 被截断（如"饕餮一阶"→"饕餮"）致 exact 全空，回退 contains
        # 重试一次。命中降置信度 + 标记，多行命中走歧义机制（不静默采用首行）。
        if match_mode == "exact" and allow_exact_fallback:
            fb = self._locate_row(path, sheet, col_idx, value, "contains")
            if fb is not None:
                fb.confidence = round(fb.confidence * 0.7, 4)
                fb.method = fb.method + "+exact_fallback"
                return fb
        return None

    def _locate_row_strip_prefix(self, path, sheet, col_idx, value, match_mode, depth=0) -> RowMatch | None:
        """去除常见业务前缀后递归重试。深度限制防无限递归。"""
        if depth >= 3:
            return None
        keywords_to_strip = self._ENTITY_PREFIXES
        for kw in keywords_to_strip:
            if value.startswith(kw) and len(value) > len(kw):
                stripped = value[len(kw):].strip()
                if stripped:
                    result = self._locate_row(path, sheet, col_idx, stripped, match_mode)
                    if result is not None:
                        return result
        return None

    def _composite_resolve_col(self, path: Path, sheet: str, col_name: str) -> Optional[int]:
        """复合主键列名 → col_idx（1-based）解析。

        读 row1 显示名 + row2 规范名，按 col_name 精确/包含匹配。
        匹配 row2 规范名（如 residence_id）优先，因复合主键通常用规范名。
        失败返 None。
        """
        try:
            hdrs = self.cli.read_header(path, sheet) or []
        except Exception:
            return None
        target = (col_name or "").strip().lower()
        if not target:
            return None
        # row2 规范名匹配（去括号/类型后缀）
        for i, h in enumerate(hdrs, 1):
            base = str(h).split("（")[0].split("(")[0].strip().lower()
            if base == target:
                return i
        # row1 显示名子串匹配
        for i, h in enumerate(hdrs, 1):
            if target in str(h).lower():
                return i
        return None

    def _locate_row_composite(self, path: Path, sheet: str,
                               col_fields: list[str], col_values: list[str],
                               match_mode: str = "exact",
                               allow_exact_fallback: bool = False) -> RowMatch | None:
        """复合主键行定位：多列值同时匹配取交集行。

        case5 ResidenceEntry 用 (residence_id, obstacle_id) 双键定位。
        策略：每列单独 _locate_row 取候选行集合，求交集。交集单行 → 返回该行
        （置信度取最低列，标记 method=composite）；多行 → 歧义；空 → None。
        列名经 _match_locator_col 解析为 col_idx，解析失败的列跳过。
        至少需 2 个有效列才走复合，否则回退单列（返首列 _locate_row 结果）。
        """
        if not col_fields or len(col_fields) < 2 or len(col_fields) != len(col_values):
            # 不足复合主键条件，回退单列
            if col_fields and col_values:
                lm = self._composite_resolve_col(path, sheet, col_fields[0])
                if lm is not None:
                    return self._locate_row(path, sheet, lm, col_values[0],
                                            match_mode, allow_exact_fallback)
            return None
        # 解析每列名 → col_idx + 各列候选行集合
        col_row_sets: list[set[int]] = []
        col_meta: list[tuple[int, str]] = []  # (col_idx, value) 供回查
        for fld, val in zip(col_fields, col_values):
            ci = self._composite_resolve_col(path, sheet, fld)
            if ci is None:
                # 列名解析失败：复合主键要求全列有效，缺一列则整体失败返 None
                # （不回退单列，避免双键表用单列误定位到错误行）
                return None
            rm = self._locate_row(path, sheet, ci, val, match_mode,
                                 allow_exact_fallback)
            if rm is None:
                # 任一列无命中：复合主键交集必空，整体失败
                return None
            rows = {rm.row} | {r for r, _ in (rm.alternatives or [])}
            if not rows:
                return None
            col_row_sets.append(rows)
            col_meta.append((ci, val))
        if len(col_row_sets) < 2:
            # 有效列不足 2，回退首个有效列结果
            for ci, v in col_meta:
                return self._locate_row(path, sheet, ci, v, match_mode,
                                       allow_exact_fallback)
            return None
        # 求交集
        inter = col_row_sets[0]
        for s in col_row_sets[1:]:
            inter &= s
        if not inter:
            return None
        # 取交集首行（多行歧义），置信度取最低列 × 歧义惩罚
        row = sorted(inter)[0]
        # 找回该行在首列的实际值
        first_ci, first_val = col_meta[0]
        cell_val = first_val
        try:
            if hasattr(self.cli, "_read_cell_value"):
                v = self.cli._read_cell_value(path, sheet, row, first_ci)
                if v is not None:
                    cell_val = str(v).strip()
        except Exception:
            pass
        ambiguous = len(inter) > 1
        conf = 0.80
        if ambiguous:
            conf = round(conf * 0.85, 4)
        alts = [(r, cell_val) for r in sorted(inter)[1:]]
        return RowMatch(row=row, value=cell_val, confidence=conf,
                        method="composite", ambiguous=ambiguous,
                        alternatives=alts)

    def _resolve_row_ambiguity(self, intent: NLIntent, row_match: RowMatch,
                               path: Path = None, sheet: str = "") -> RowMatch:
        """行定位命中同层多个候选（歧义）时，结合候选整行数据让 LLM 仲裁最终行。

        注入整行数据（列名=值）让 LLM 基于其他字段做智能判断，
        而非仅靠行号+定位列值。path/sheet 为空时降级为原行为。
        LLM 不可用/未给出可解析答案时，原样返回 row_match。
        """
        if not row_match.ambiguous:
            return row_match
        all_cands = [(row_match.row, row_match.value)] + list(row_match.alternatives)
        # 构造候选描述：行号 + 定位列值 + 整行数据（列名=值）
        cand_lines: list[str] = []
        if path is not None and sheet:
            headers = []
            try:
                headers = self.cli.read_header(path, sheet) or []
            except Exception:
                headers = []
            for r, v in all_cands:
                row_data = self._read_row_data(path, sheet, r)
                pairs = []
                for ci, val in row_data.items():
                    name = self._col_name(headers, ci) if ci <= len(headers) else f"列{ci}"
                    pairs.append(f"{name}={val}")
                row_desc = ", ".join(pairs) if pairs else "（空行）"
                cand_lines.append(f"候选行{r}「{v}」：{row_desc}")
        else:
            cand_lines = [f"候选行{r}「{v}」" for r, v in all_cands]
        desc = "\n".join(cand_lines)
        reply = self._llm_disambiguate(
            f"用户指令：「{intent.raw}」\n"
            f"定位「{intent.locator_value}」时命中了多个同名/同类候选，"
            f"请根据各行其他字段判断用户最可能指向哪一行：\n{desc}\n"
            f"只回答该行号数字，不要其他文字。")
        if reply:
            m = re.search(r"\d+", reply)
            if m:
                picked = int(m.group())
                for r, v in all_cands:
                    if r == picked:
                        return RowMatch(row=r, value=v,
                                        confidence=max(row_match.confidence, ACCEPT_THRESHOLD),
                                        method=row_match.method + "+llm_arbitrated")
        return row_match

    # ── 找不到时的候选建议（模糊匹配最可能区域）──

    def _fuzzy_suggest(self, query, candidates, top_k: int = 3):
        """用 FuzzyMatcher 在候选集合中找最相似的 top_k，返回 [(value, score)]。"""
        if query is None:
            return []
        q = str(query).strip()
        if not q or not candidates:
            return []
        from ..locator.fuzzy_matcher import FuzzyMatcher
        m = FuzzyMatcher(top_k=top_k)
        return [(c.value, c.score) for c in m.search(q, [str(c) for c in candidates])]

    def _suggest_rows(self, path: Path, sheet: str, col_idx: int,
                      value, top_k: int = 3) -> list[tuple[int, str, float]]:
        """对定位列做模糊匹配，返回最接近的 [(行号, 值, 分数)]，供"最可能区域"提示。"""
        try:
            rows = self.cli.read_sheet(path, sheet)
        except Exception:
            return []
        start = self.cli._resolve_data_start(path, sheet) if hasattr(self.cli, "_resolve_data_start") else getattr(self.cli, "data_start_row", 5)
        vals: list[tuple[int, str]] = []
        for i, row in enumerate(rows):
            cell = row[col_idx - 1] if 0 <= col_idx - 1 < len(row) else None
            if cell is not None and str(cell).strip():
                vals.append((start + i, str(cell)))
        sug = self._fuzzy_suggest(value, [v for _, v in vals], top_k=top_k)
        by_val: dict[str, int] = {}
        for r, v in vals:
            by_val.setdefault(v, r)
        return [(by_val[v], v, sc) for v, sc in sug if v in by_val]

    @staticmethod
    def _fmt_rows(items: list[tuple[int, str, float]]) -> str:
        if not items:
            return ""
        return "。最可能在：" + "、".join(f"行{r}「{v}」" for r, v, _ in items[:3])

    @staticmethod
    def _fmt_simple(items: list[tuple[str, float]]) -> str:
        if not items:
            return ""
        return "。最接近：" + "、".join(v for v, _ in items[:3])

    def _cross_table_search(self, value: str, exclude_stem: str = "",
                            top_k_tables: int = 8) -> list[dict]:
        """跨表搜索定位值。5.5：优先走 `_table_index.json` 的 row_index 倒排，
        避免全量 openpyxl 开表；索引不可用时回退 `_cross_table_search_scan`。

        返回 [{"table_stem", "sheet", "match_type", "matches": [{row, value, score}]}]
        """
        if not value or not value.strip():
            return []
        idx = self._get_index()
        if not idx:
            return self._cross_table_search_scan(value, exclude_stem, top_k_tables)
        v = value.strip()
        non_business = ("说明", "Sheet1", "程序用勿删", "程序用", "勿删", "备注", "CONFIG", "tips")
        results: list[dict] = []
        for t in idx:
            if t.stem == exclude_stem:
                continue
            for s in t.sheets:
                if any(m in s.name for m in non_business):
                    continue
                # 名称类列优先；无名称列则退回 row_index 全部键（多为 id 列）
                name_cols = [c for c in s.row_index
                             if ("名称" in c or "名字" in c or c.endswith("名"))]
                if not name_cols:
                    name_cols = list(s.row_index.keys())
                if not name_cols:
                    continue
                exact: list[dict] = []
                all_vals: list[tuple[int, str]] = []
                for col in name_cols:
                    for k, rows in s.row_index[col].items():
                        if not k:
                            continue
                        for r in rows:
                            all_vals.append((r, k))
                        if v in k or k in v:
                            for r in rows:
                                exact.append({"row": r, "value": k, "score": 1.0})
                if exact:
                    results.append({"table_stem": t.stem, "sheet": s.name,
                                    "match_type": "exact", "matches": exact[:5]})
                elif all_vals:
                    sim = self._fuzzy_suggest(v, [val for _, val in all_vals], top_k=3)
                    by_val = {val: r for r, val in all_vals}
                    sim_rows = [{"row": by_val[val], "value": val, "score": sc}
                                for val, sc in sim if val in by_val]
                    if sim_rows:
                        results.append({"table_stem": t.stem, "sheet": s.name,
                                        "match_type": "similar", "matches": sim_rows})
        results.sort(key=lambda r: (r["match_type"] != "exact", -len(r["matches"])))
        return results[:top_k_tables]

    def _cross_table_search_scan(self, value: str, exclude_stem: str = "",
                                 top_k_tables: int = 8) -> list[dict]:
        """跨表搜索定位值：在所有表的所有 sheet 中找完全匹配 + 相近项。

        流程：
          1. 遍历所有表（排除 exclude_stem），跳过非业务 sheet
          2. 对每个 sheet，取名称列（含"名称"或第一列），读全部值
          3. contains 匹配 → 完全匹配（match_type=exact）
          4. 无完全匹配 → FuzzyMatcher top3 相近项（match_type=similar）
          5. 只收集有匹配/相近的表，最终按 exact 优先排序，截 top_k_tables

        返回 [{"table_stem", "sheet", "match_type", "matches": [{row, value, score}]}]
        """
        if not value or not value.strip():
            return []
        tables = self.cli.list_tables()
        non_business = ("说明", "Sheet1", "程序用勿删", "程序用", "勿删", "备注", "CONFIG", "tips")
        results: list[dict] = []
        for tp in tables:
            if tp.stem == exclude_stem:
                continue
            sheets = self.cli.get_sheets(tp)
            for sheet in sheets:
                if any(m in sheet for m in non_business):
                    continue
                try:
                    rows = self.cli.read_sheet(tp, sheet)
                except Exception:
                    continue
                if not rows:
                    continue
                start = self.cli._resolve_data_start(tp, sheet) if hasattr(self.cli, "_resolve_data_start") else getattr(self.cli, "data_start_row", 5)
                # 取表头（cli 已有 read_header，自动探测表头行）
                try:
                    headers = self.cli.read_header(tp, sheet) or []
                except Exception:
                    headers = []
                # 找名称列（含"名称"/"名字"，否则第一列）
                name_idx = -1
                if headers:
                    for i, h in enumerate(headers):
                        if h and ("名称" in str(h) or "名字" in str(h)):
                            name_idx = i
                            break
                if name_idx < 0 and headers:
                    name_idx = 0
                if name_idx < 0:
                    continue
                exact: list[dict] = []
                vals: list[tuple[int, str]] = []
                for i, row in enumerate(rows):
                    cell = row[name_idx] if name_idx < len(row) else None
                    if cell is None or not str(cell).strip():
                        continue
                    csv = str(cell)
                    abs_row = start + i
                    if value in csv or csv in value:
                        exact.append({"row": abs_row, "value": csv, "score": 1.0})
                    vals.append((abs_row, csv))
                if exact:
                    results.append({"table_stem": tp.stem, "sheet": sheet,
                                    "match_type": "exact", "matches": exact[:5]})
                elif vals:
                    sim = self._fuzzy_suggest(value, [v for _, v in vals], top_k=3)
                    by_val = {v: r for r, v in vals}
                    sim_rows = [{"row": by_val[v], "value": v, "score": sc}
                                for v, sc in sim if v in by_val]
                    if sim_rows:
                        results.append({"table_stem": tp.stem, "sheet": sheet,
                                        "match_type": "similar", "matches": sim_rows})
        # exact 优先，similar 次之，截 top_k
        results.sort(key=lambda r: (r["match_type"] != "exact", -len(r["matches"])))
        return results[:top_k_tables]

    def _trigger_cross_table_search_pause(self, res: AgentResult, path: Path,
                                          sheet: str, loc_match, intent: NLIntent,
                                          sug: list[tuple[int, str, float]]) -> AgentResult:
        """行未命中 → 填充 pending_search/needs_confirm，暂停等用户确认跨表搜索。

        供 _run_set/_run_get/_run_delete 各分支共用。
        """
        hint = self._fmt_rows(sug)
        res.add_thinking("定位", f"在 {path.stem}/{sheet} 的「{loc_match.column or ''}」列中查找「{intent.locator_value}」")
        res.add_thinking("定位", f"当前表未找到完全匹配。在定位列做模糊搜索，相近项：{hint or '无'}")
        res.add_thinking("定位", "已列出当前表最可能的 5 行，供用户判断是否就是其中之一")
        res.add_thinking("跨表探索", "若上述候选均非目标，可确认后到其他相近表中查找")
        # 给 top5 候选行补摘要（前 3 个非空列）供前端直接展示行内容
        top5: list[dict] = []
        for r, v, sc in sug:
            summary: dict = {}
            try:
                header = self.cli.read_header(path, sheet)
                row_data = self._read_row_data(path, sheet, r)
                cnt = 0
                for ci in range(1, len(header) + 1):
                    if cnt >= 3:
                        break
                    name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
                    val = row_data.get(ci)
                    if val is not None and str(val).strip() != "":
                        summary[name] = val
                        cnt += 1
            except Exception:
                pass
            top5.append({"row": r, "value": v, "score": sc, "summary": summary})
        res.pending_search = {
            "table_stem": path.stem, "sheet": sheet,
            "col_name": loc_match.column or "",
            "col_idx": loc_match.index,
            "value": intent.locator_value or "",
            "top5": top5,
        }
        res.needs_confirm = True
        res.confirm_token = (f"search:{path.stem}:{sheet}:{loc_match.index}:"
                             f"{intent.locator_value}")
        res.confirm_kind = "cross_table_search"
        res.message = (f"在 {path.stem}/{sheet} 未找到「{intent.locator_value}」"
                       f"（定位列={loc_match.column}）。\n"
                       f"已列出 {len(top5)} 行最可能的候选。"
                       f"确认后将在其他相近表中查找「{intent.locator_value}」。")
        return res

    @staticmethod
    def _row_confidence_note(row_match: "RowMatch") -> str:
        """行定位置信度不足或存在歧义时，追加到 res.message 末尾的提示，
        供用户/前端直接看到，而不是只留在内部 step 日志里。"""
        if row_match.ambiguous:
            alt = "、".join(f"行{r}「{v}」" for r, v in row_match.alternatives[:3])
            return f"（注意：「{row_match.value}」命中多行同名候选，本次采用行{row_match.row}，其余候选：{alt}，如有误请手动指定行号重试）"
        if row_match.confidence < ACCEPT_THRESHOLD:
            return f"（置信度{row_match.confidence:.2f}，命中方式={row_match.method}，建议核对是否为目标行）"
        return ""

    @staticmethod
    def _column_confidence_note(col_match: ColumnMatch) -> str:
        """列定位置信度不足时，追加到 res.message 末尾的提示。"""
        if col_match.score < ACCEPT_THRESHOLD:
            return COLUMN_LOW_CONFIDENCE_HINT
        return ""

    @staticmethod
    def _extract_row_override(text: str) -> Optional[int]:
        """从用户原文提取显式行号覆盖（兜底正则，LLM 未给出 row_override 时使用）。

        匹配"用行6""选行6""用第6行""选第6行""第6行"等；不匹配"改为5""id为1001"。
        Returns:
            正整数行号，或 None（无匹配）。
        """
        if not text:
            return None
        m = _ROW_OVERRIDE_RE.search(text)
        if not m:
            return None
        n = m.group(1) or m.group(2)
        try:
            num = int(n)
        except (TypeError, ValueError):
            return None
        return num if num > 0 else None

    def _apply_row_override(self, intent: NLIntent, path: Path, sheet: str,
                            loc_match: ColumnMatch) -> Optional[RowMatch]:
        """若用户显式指定行号，校验该行存在并构造合成 RowMatch，跳过 _locate_row。

        优先用 LLM 解析的 intent.row_override；为空时用兜底正则从 intent.raw 提取。
        行越界或读取失败 → 返回 None，回退到正常行定位流程。

        Args:
            intent: 已解析意图（可能含 row_override）
            path/sheet/loc_match: 表/sheet/定位列（用于读该行单元格确认存在）

        Returns:
            RowMatch(method="row_override", confidence=1.0)，或 None
        """
        ro = intent.row_override
        if ro is None:
            ro = self._extract_row_override(intent.raw or "")
        if ro is None:
            return None
        r = self.cli.read_cell(path, sheet, ro, loc_match.index)
        if not r.ok:
            return None  # 行越界，回退正常定位
        val = r.data
        return RowMatch(
            row=ro,
            value=str(val) if val is not None else "",
            confidence=1.0,
            method="row_override",
            ambiguous=False,
            alternatives=[],
        )

    @staticmethod
    def _col_name(headers: list, idx: int) -> str:
        """取 1-based 列号对应的清洗后列名（去类型标注，如 "技能id:int" → "技能id"）。"""
        if not headers or idx < 1 or idx > len(headers):
            return ""
        h = headers[idx - 1]
        if h is None:
            return ""
        return str(h).split(":")[0].strip()

    @staticmethod
    def _add_result_row(res: AgentResult, col: int, col_name: str,
                        old_value=None, new_value=None):
        """向 res.result_rows 追加一行表体数据（列号/列名/旧值/新值）。"""
        res.result_rows.append({
            "col": col,
            "col_name": col_name or "",
            "old_value": old_value,
            "new_value": new_value,
        })

    def _llm_disambiguate(self, question: str) -> str | None:
        """调用 codemaker LLM 做消歧，返回纯文本答复；不可用或失败返回 None。

        用于表/sheet 定位规则无法决断时，从候选中让 LLM 选一个。低置信度才触发，
        避免每次定位都走 LLM 拖慢响应。
        """
        try:
            client = getattr(self.parser, "client", None)
            if client is None or not client.health_check():
                return None
            # 5.1：复用 parser 已建 session，避免每次消歧都新建 session。
            sid = None
            if hasattr(self.parser, "_ensure_session"):
                try:
                    sid = self.parser._ensure_session()
                except Exception:
                    sid = None
            if not sid:
                ws = getattr(self.cli, "workspace", "")
                sess = client.create_session(directory=str(ws) if ws else "")
                if not sess.ok:
                    return None
                sid = sess.session_id
            resp = client.prompt(sid, question, timeout=_DISAMBIGUATE_TIMEOUT,
                                 cancel_event=getattr(self, "_cancel_event", None))
            return resp.response_text.strip() if resp.ok else None
        except Exception:
            return None

    def _resolve_locator_and_mode(self, path: Path, sheet: str,
                                  intent: NLIntent | None = None):
        """解析定位列及其匹配模式。

        读取表头，构建 ColumnMatcher，结合 row_cfg 中的规则确定：
        - 定位列名（默认"名称"）
        - 匹配模式（默认"contains"）

        优先级：
          1. intent.locator_field（用户显式指定的列名，如"删除神通id为3333"中的"神通id"）
             —— 命中表头即采用；match 模式取 row_cfg 中同列规则的配置，无规则则按列名
                启发式判定（id/编号类→exact，否则 contains）
          2. row_cfg 规则链（按优先级取首个能匹配表头的列）
          3. 默认"名称"列
          4. 回退 _table_index.json 的 row_index 已记忆定位列

        Returns:
            (loc_match, match_mode, matcher, stem, loc_col_name)
        """
        headers = self.cli.read_header(path, sheet)
        stem = path.stem
        matcher = self._make_matcher(headers, stem, sheet, path)

        rules = self.row_cfg.rules_for(stem, sheet)
        loc_col_name = "名称"
        match_mode = "contains"

        # 预计算每条规则实际命中的表头列（规则链 + 默认模式）
        default_mode: str | None = None
        rule_matches: list[tuple[dict, ColumnMatch]] = []
        if rules:
            for rl in rules:
                if "match" in rl and default_mode is None:
                    default_mode = rl["match"]
            for rl in rules:
                col = rl.get("locator_column")
                if not col:
                    continue
                cand = matcher.match_best(col)
                if cand is not None:
                    rule_matches.append((rl, cand))

        # 优先级 1：用户显式指定的定位列
        explicit = intent.locator_field if intent else None
        if explicit:
            cand = matcher.match_best(explicit)
            if cand is not None:
                loc_col_name = explicit
                # 取 row_cfg 中同列规则的 match 模式
                resolved_mode: str | None = None
                for rl, rm in rule_matches:
                    if rm.index == cand.index:
                        resolved_mode = rl.get("match")
                        break
                if resolved_mode is None:
                    # 启发式：id/编号类列默认 exact
                    low = explicit.lower()
                    resolved_mode = "exact" if ("id" in low or explicit.endswith("编号") or explicit.endswith("ID")) else "contains"
                return cand, resolved_mode, matcher, stem, loc_col_name

        # 优先级 1b：Step1 列名提取信号（extras["extracted_columns"]）topK 匹配当前 sheet 表头
        # 当 LLM 漏产 locator_field 时，用 Step1 ColumnExtractor 提取的列名 token 反查定位列。
        # 修复案例一 QA 饕餮——"属性"等 token 经 topK 命中真实列名，定位行；案例二"法宝名称"
        # 命中"法宝名称"列而非默认"名称"。仅在当前 sheet 表头匹配，避免跨表误命中。
        # §P1-8 防选错列：set/delete 需按"名称"类列定位行（业务行定位惯例），
        # 不取首个命中 token——"活动类型"先命中会覆盖应定位的"名称"。get 可用任意命中列。
        if not explicit and intent and intent.extras:
            extracted = intent.extras.get("extracted_columns")
            if extracted:
                _action = getattr(intent, "action", "") or ""
                # set/delete 优先名称类列；get 不限
                _name_like = lambda cn: ("名称" in cn or "名字" in cn
                                         or cn == "名称" or "title" in cn.lower())
                # 收集所有 token 的命中候选，再按 action 语义筛选
                all_cands: list = []
                for token in extracted:
                    if not token or len(token) < 2:
                        continue
                    cs = matcher.match_topk(token, k=3, min_score=0.5)
                    all_cands.extend(cs)
                # 去重保序
                seen_cols = set()
                uniq: list = []
                for c in all_cands:
                    if c.column not in seen_cols:
                        seen_cols.add(c.column)
                        uniq.append(c)
                # set/delete 优先选名称类列；无则回退首个命中
                cand = None
                if _action in ("set", "delete"):
                    cand = next((c for c in uniq if _name_like(c.column)), None)
                if cand is None and uniq:
                    cand = uniq[0]
                if cand is not None:
                    loc_col_name = cand.column
                    low = loc_col_name.lower()
                    resolved_mode = "exact" if (
                        "id" in low or loc_col_name.endswith("编号")
                        or loc_col_name.endswith("ID")) else "contains"
                    return cand, resolved_mode, matcher, stem, loc_col_name

        # 优先级 2：row_cfg 规则链（按用户输入类型自适应选列）
        loc_match = None
        if rule_matches:
            ordered = self._order_rules_by_input(rule_matches, intent)
            rl, cand = ordered[0]
            loc_match = cand
            loc_col_name = rl.get("locator_column") or loc_col_name
            match_mode = rl.get("match", default_mode or "contains")
        # 优先级 3：默认"名称"列
        if loc_match is None:
            loc_match = matcher.match_best(loc_col_name)
        # 优先级 4：回退 _table_index.json 的 row_index 已记忆的定位列（build_index 时按
        # "名称/id/编号"关键字识别并建倒排的列，如 formula_sum 的"赛季编号"）。
        if loc_match is None:
            idx = self._get_index()
            for t in idx:
                if t.stem != stem:
                    continue
                for s in t.sheets:
                    if s.name != sheet:
                        continue
                    for idx_col in s.row_index.keys():
                        cand = matcher.match_best(idx_col)
                        if cand is not None:
                            return cand, "contains", matcher, stem, idx_col
                    break
                break
        return loc_match, match_mode, matcher, stem, loc_col_name

    def _fill_locator_from_fields(self, intent, loc_match) -> None:
        """O20g：locator_value 为空时从 fields 字典兜底提取行定位值。

        DecomposeAgent LLM 漏产 locator_field/locator_value 时，fields 字典
        可能含定位列的值（如「删除活动名称为春节活动的行」LLM 产
        fields={"活动名称":"春节活动"}）。按 loc_match.column（已解析的定位
        列名）从 fields 取值，命中则填 intent.locator_value + locator_field。
        同时从 fields 移除该键（避免 delete 把它当修改列重复处理）。
        """
        if intent.locator_value or not loc_match or not intent.extras:
            return
        fields = intent.extras.get("fields")
        if not isinstance(fields, dict) or not fields:
            return
        col_name = (loc_match.column or "").split(":")[0]  # 去后缀（如 类型:int）
        if not col_name:
            return
        # 精确匹配 + 容忍前后空白
        for k in list(fields.keys()):
            if str(k).strip() == col_name:
                v = fields[k]
                if v is not None and str(v).strip() and not str(v).startswith("<"):
                    intent.locator_value = str(v)
                    intent.locator_field = str(k)
                    # delete 操作定位列非修改列，从 fields 移除避免误写
                    if intent.action == "delete":
                        fields.pop(k, None)
                break

    @staticmethod
    def _is_id_locator_column(name: str) -> bool:
        """列名是否为 ID/编号 类定位列。"""
        if not name:
            return False
        low = name.lower()
        return ("id" in low) or name.endswith("编号") or ("编号" in name)

    def _order_rules_by_input(self, rule_matches: list, intent: NLIntent):
        """按用户输入类型对定位规则重排（4.1）。

        纯数字输入 → ID/编号 列优先；文字输入 → 名称列优先。
        sorted 稳定，同类保持原顺序。
        """
        val = str((intent.locator_value if intent else "") or "").strip()
        is_numeric = bool(val) and re.fullmatch(r"\d+", val) is not None

        def _key(item):
            rl, cand = item
            col = rl.get("locator_column") or getattr(cand, "column", "")
            id_like = self._is_id_locator_column(col)
            if is_numeric:
                return 0 if id_like else 1
            return 1 if id_like else 0

        return sorted(rule_matches, key=_key)

    def _match_target_column(self, intent: NLIntent, matcher: ColumnMatcher,
                             stem: str, sheet: str, loc_index: int
                             ) -> tuple[ColumnMatch | None, str | None]:
        """从自然语言中匹配目标列名并提取对应的值。

        策略：
            1. 优先用字典别名精确匹配（遍历 column_cfg 中所有别名，在文本中找位置）
            2. 若别名匹配不到，退而用 _find_column_spans 做模糊列名扫描
            3. 排除定位列自身，以及低分短片段匹配（id/编号 列容易误匹配）
            4. 匹配后提取列名之后的值文本

        Args:
            intent: 解析后的意图
            matcher: 当前 sheet 的 ColumnMatcher
            stem: 文件名 stem
            sheet: sheet 名
            loc_index: 定位列的索引（需排除，避免把定位列当成目标列）

        Returns:
            (目标列的 ColumnMatch, 提取到的值文本)，匹配失败返回 (None, None)。
        """
        text = intent.raw
        headers = matcher.headers
        aliases = self.column_cfg.all_aliases(stem, sheet)

        # 策略0：完整列名精确子串匹配（回退通道；LLM fields 主路径由 _validate_fields 的
        # match_best 自纠处理，规则提取路径无 field key 时用此 text.find 兜底）。
        # source 标 exact_substr_fallback 标明其为回退信号，供上层区分主/回退命中。
        best_exact: tuple[int, str, ColumnMatch] | None = None
        for idx, h in enumerate(headers, start=1):
            h_name = h.split(":")[0] if h else ""
            if not h_name:
                continue
            pos = text.find(h_name)
            if pos >= 0 and idx != loc_index:
                m = ColumnMatch(column=h, score=0.95, index=idx, source="exact_substr_fallback")
                if best_exact is None or len(h_name) > len(best_exact[1]):
                    best_exact = (pos, h_name, m)
        if best_exact is not None and len(best_exact[1]) >= 3:
            _, _, m = best_exact
            tail = text[text.find(best_exact[1]) + len(best_exact[1]):].strip()
            tail = _strip_separators(tail)
            return m, (tail or None)

        # 策略1：字典别名精确匹配
        best: tuple[int, str, ColumnMatch] | None = None
        for alias, col_name in aliases.items():
            pos = text.find(alias)
            if pos < 0:
                continue
            m = None
            for idx, h in enumerate(headers, start=1):
                h_name = h.split(":")[0] if h else ""
                if h_name == col_name or h == col_name:
                    m = ColumnMatch(column=h, score=1.0, index=idx, source="dict")
                    break
            if m is None or m.index == loc_index:  # 排除定位列自身
                continue
            # 取位置最早、或在同位置取别名最长（更精确）的匹配
            if best is None or pos < best[0] or (pos == best[0] and len(alias) > len(best[1])):
                best = (pos, alias, m)
        if best is not None:
            _, alias, m = best
            tail = text[text.find(alias) + len(alias):].strip()
            tail = _strip_separators(tail)
            return m, (tail or None)
        # 策略1.5：短形式别名扩展（如"描"→描述、"级"→等级、"攻"→攻击）。
        # 全列名/别名均未命中时兜底，避免短输入落到策略2模糊而误配相邻列。
        # 目标列通常在句尾"的X"，故取位置最靠后、短形式最长的命中。
        short_forms = self.short_form_cfg.reverse_map(stem, sheet)
        best_sf: tuple[int, str, ColumnMatch] | None = None
        for short, col_name in short_forms.items():
            pos = text.rfind(short)
            if pos < 0:
                continue
            m = None
            for idx, h in enumerate(headers, start=1):
                h_name = h.split(":")[0] if h else ""
                if h_name == col_name or h == col_name:
                    m = ColumnMatch(column=h, score=1.0, index=idx, source="short_form")
                    break
            if m is None or m.index == loc_index:  # 排除定位列自身
                continue
            if (best_sf is None or pos > best_sf[0]
                    or (pos == best_sf[0] and len(short) > len(best_sf[1]))):
                best_sf = (pos, short, m)
        if best_sf is not None:
            pos, short, m = best_sf
            tail = text[pos + len(short):].strip()
            tail = _strip_separators(tail)
            return m, (tail or None)
        # 策略2：模糊列名扫描
        spans = _find_column_spans(text, matcher)
        # 排除定位列 和 不可靠的 id/编号 列短片段匹配
        spans = [s for s in spans
                 if s[2].index != loc_index
                 and not (s[2].index <= 2
                          and s[2].score < 1.0
                          and any(kw in (s[2].column or "").lower() for kw in ("id", "编号")))]
        if not spans:
            return None, None
        spans.sort(key=lambda x: x[0])  # 按文本位置排序，取最早出现的
        tgt_span = spans[0]
        tail = text[tgt_span[1]:].strip()
        tail = _strip_separators(tail)
        return tgt_span[2], (tail or None)

    def _get_col_type(self, stem: str, sheet: str, col_name: str) -> str:
        """从 value_constraints.yaml 查询列的类型标注（如 int/float/string/bool）。

        查找链：tables.{stem}.{sheet}.columns.{col_name}.type
        col_name 取表头冒号前的部分（如 "技能id:int" → "技能id"）。
        未找到返回空串。
        """
        if not col_name:
            return ""
        name = col_name.split(":")[0].strip()
        vc = _load_value_constraints()
        sheets = vc.get(stem, {})
        cols = sheets.get(sheet, {}).get("columns", {})
        info = cols.get(name) or cols.get(col_name)
        return (info or {}).get("type", "") or ""

    def _get_col_number_format(self, stem: str, sheet: str, col_name: str) -> str:
        """查列的 number_format（value_constraints.yaml 的 format 字段）。

        date/datetime 列返回 format（如 'yyyy-mm-dd hh:mm:ss'），其余返回空串。
        """
        if not col_name:
            return ""
        name = col_name.split(":")[0].strip()
        vc = _load_value_constraints()
        sheets = vc.get(stem, {})
        cols = sheets.get(sheet, {}).get("columns", {})
        info = cols.get(name) or cols.get(col_name)
        if not info:
            return ""
        ct = (info.get("type", "") or "").lower()
        if ct in ("date", "datetime"):
            return info.get("format", "") or ""
        return ""

    @staticmethod
    def _is_id_column(col_name: str) -> bool:
        """判断列名是否为 ID/编号 列（用于段校验触发）。"""
        import re
        if not col_name:
            return False
        name = col_name.split(":")[0].strip().lower()
        return bool(re.search(r"(^|_)(id)$|id$|^编号|编号", name))

    def _validate_id_scope(self, stem: str, sheet: str, col_name: str, value) -> tuple[bool, str]:
        """ID 段校验：value 是否落在 id_mgr 对应模块的预留段内。

        module 约定为 {stem}.{sheet}（与 id_mgr SETTING sheet 一致）。
        validator 未加载 id_mgr 或模块未注册 → 不校验（ok=True）。

        方法 F（O16）：加 id-claim 跨分支查重 — value 在账本中冲突 →
        pre_commit_hold(kind=id_conflict) 事件 + 建议下一空闲号，不静默改编号。
        需 RESOURCES_DIR 可访问；失败静默不阻断（返 ok=True）。
        """
        try:
            from engine.id_scope import get_id_scope_validator
            v = get_id_scope_validator()
            module = f"{stem}.{sheet}"
            if not v._id_mgr_loaded:
                return True, ""
            ok, reason = v.validate_value(module, value)
            if not ok:
                return False, reason
            # F4：id-claim 跨分支查重（段校验通过后）。命中冲突 → hold 事件 + 建议换号。
            try:
                from pathlib import Path as _P
                _res_dir = getattr(self, "_resources_dir", None)
                if _res_dir is None:
                    _res_dir = _P("resources")
                # 多分支模式：env CODEMAKER_ID_SCOPE_BRANCHES=path1;path2 传额外分支根
                # （默认 None=单根模式，向后兼容）。用于跨 SVN 分支编号账本校验。
                _extra_branches_env = os.getenv("CODEMAKER_ID_SCOPE_BRANCHES", "")
                _branches = None
                if _extra_branches_env:
                    _branches = [_P(_res_dir)] + [_P(p) for p in _extra_branches_env.split(";") if p.strip()]
                claim = v.claim_id(int(value), _P(_res_dir), branches=_branches)
                if claim.get("claimed"):
                    _locs = claim.get("conflict_locations") or []
                    _sug = claim.get("suggested_next")
                    _msg = (f"ID {value} 跨分支冲突：已存在于 "
                            f"{len(_locs)} 处（{[l.get('branch','?')+'/'+l.get('file','?') for l in _locs[:3]]}）")
                    if _sug:
                        _msg += f"，建议改用 {_sug}"
                    # pre_commit_hold 事件（kind=id_conflict）→ SSE + 软失败（_handle 模式）
                    try:
                        from routers.precommit_hold import PreCommitHoldEvent
                        _ev = PreCommitHoldEvent(
                            kind="id_conflict", severity="hold", count=len(_locs),
                            sheets={sheet: {"value": int(value),
                                            "locations": _locs[:20]}},
                            message=_msg, recommendation="change_id")
                        _sink = getattr(self, "_agent_subtask_sink", None)
                        if _sink is not None:
                            try:
                                _sink("pre_commit_hold", _ev.to_dict())
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return False, _msg
            except Exception:
                pass
            return True, ""
        except Exception:
            return True, ""

    # 中文枚举列缓存：(stem, sheet, col) -> bool，进程内不变（表结构静态）
    _cn_enum_col_cache: dict = {}

    def _is_cn_enum_column(self, stem: str, sheet: str, col_name: str) -> bool:
        """int 列标注但现有数据存中文 → 中文枚举列（如 activity_type:int 实际存
        「春节活动」）。此类列中文值合法，不应报「无法转 int」。"""
        key = (stem, sheet, str(col_name).split(":")[0].strip())
        if key in self._cn_enum_col_cache:
            return self._cn_enum_col_cache[key]
        result = False
        try:
            if not key[0] or not key[1] or not key[2] or self.cli is None:
                return False
            path = None
            for _t in (self.cli.list_tables() if hasattr(self.cli, "list_tables") else []):
                if getattr(_t, "stem", None) == stem or str(_t).endswith(f"/{stem}.xlsx") or str(_t).endswith(f"\\{stem}.xlsx"):
                    path = getattr(_t, "path", _t)
                    break
            if path is None:
                return False
            headers = self.cli.read_header(path, sheet)
            rows = self.cli.read_sheet(path, sheet)
            idx = None
            for i, h in enumerate(headers):
                if h and str(h).split(":")[0].strip() == key[2]:
                    idx = i
                    break
            if idx is None:
                return False
            import re as _re_cn
            for row in (rows or []):
                if idx < len(row):
                    v = row[idx]
                    if v is not None and _re_cn.search(r"[\u4e00-\u9fff]", str(v)):
                        result = True
                        break
        except Exception:
            result = False
        self._cn_enum_col_cache[key] = result
        return result

    def _coerce_value(self, col_type: str, value, stem: str = "", sheet: str = "", col_name: str = ""):
        """类型强制转换：按列类型把值转为对应 Python 类型，不满足类型约束则阻止写入。

        - int 列：int(value) 成功→写入；失败→查枚举映射→命中写入；仍未命中→硬错误阻止
        - float 列：float(value) 成功→写入；失败→硬错误阻止
        - bool 列：识别 0/1/true/false/yes/no；失败→硬错误阻止
        - string/未知：原样返回

        返回 (转换后的值, 警告或None, 错误或None)。
          warn  非 None：软提示，可继续写入（如范围提示）
          error 非 None：硬错误，必须阻止写入该字段
        """
        import sys
        # 复合值序列化：Excel 单元格不可存 list/tuple/dict。
        # 必须在类型分派前拦截，确保 add/set 任何路径都不会把非标量直传 openpyxl。
        if isinstance(value, dict):
            value = _serialize_complex_cell_value(value)
        if isinstance(value, (list, tuple)):
            value = _serialize_list_value(value)
        # G12+ 统一占位符软处理：所有类型（含 string/未知）列遇 <auto>/<xxx> → 软跳过。
        # <auto> = LLM 标记"输入未提及的缺失字段，待补"→ 默认为空（不写该列）。
        # <xxx>  = 占位符未替换（前序 op 失败未产出）→ 同样软跳过，不硬阻断。
        # 必须在 col_type 空检查之前拦截：string/未知列 col_type 为空时原 return value 会把
        # <auto> 字面值原样写入单元格（如 pve_combat_npc 门派/技能列），不符合"留空"语义。
        sv = str(value).strip() if value is not None else ""
        if sv == "<auto>":
            return None, f"列[{col_name}]标 <auto>（输入未提及，留空待补）", None
        if sv.startswith("<") and sv.endswith(">") and len(sv) >= 3:
            return None, f"列[{col_name}]占位符 {sv} 未替换，跳过该列", None
        # 空值（灌值守卫清空/枚举转码失败置空）→ 留空不写该列，不报错。
        # 原空串落到 int() 抛 ValueError → 报「值''无法转为整数」假失败。
        if sv == "":
            return None, None, None
        if value is None or not col_type:
            return value, None, None
        ct = col_type.lower().strip()
        warn = None
        error = None
        if ct in ("int", "integer"):
            # C 方案：<auto> 占位符 = LLM 标记"缺失必填列待补"。
            # 主键列由 _do_append 自增分支处理（PK_COL not in values）；
            # 非主键 int 列遇 <auto> → 软警告，值置 None 跳过写入，不触发硬错误。
            # 行照常写入（缺该列），事务不中断，needs_user_fill 收集后提示用户补值。
            # （<auto>/<xxx> 占位符已在顶部统一拦截，此处仅处理真实 int 值）
            try:
                return int(sv), None, None
            except (ValueError, TypeError):
                # 容错1：浮点数字符串（如 "703.0"/"703.5"）→ int(float()) 截尾
                # LLM 偶发把 int 值序列化成 float 字符串，语义上 703.0==703，不应硬失败。
                try:
                    return int(float(sv)), None, None
                except (ValueError, TypeError):
                    pass
                # 容错2：形如 "1×1"/"2x3" 的面积/尺寸字符串 → 拆出首个整数
                # （residence_building.area 列 LLM 易把 "1×1" 整体填入 int 列）
                if '×' in sv or 'x' in sv.lower():
                    m_area = re.search(r'(\d+)', sv)
                    if m_area:
                        try:
                            return int(m_area.group(1)), None, None
                        except (ValueError, TypeError):
                            pass
                # 尝试枚举映射（中文标签→int）：规则 enum_map > 工作区现场发现 > L1/pending
                if stem and sheet and col_name:
                    er = get_enum_resolver()
                    enum_val = resolve_label_full(
                        getattr(self, "cli", None), stem, sheet, col_name, sv,
                        resolver=er)
                    if enum_val is not None:
                        return enum_val, None, None
                    # §中文枚举列放行：int 列标注但现有数据存中文 → 中文值合法，
                    # 直接写入原值（activity_type:int 实际存「春节活动」）。
                    _is_cn_enum = getattr(self, "_is_cn_enum_column", None)
                    if _is_cn_enum is not None and _is_cn_enum(stem, sheet, col_name):
                        return value, None, None
                    # D10: LLM 辅助枚举发现（resolve 未命中 → LLM 推断 + register pending）
                    # §P0-4 零LLM gate：Step3 (execute_no_llm=True) 禁止发 LLM，
                    # 枚举推断交回 Step2 处理（Step2 应已拦 TYPE_MISMATCH + ask 用户）。
                    # 原无 gate 致 Step3 偷发 LLM 击穿 D4 硬约束。
                    if not getattr(self, "execute_no_llm", False):
                        analyzed = self._try_analyze_enum(stem, sheet, col_name, sv)
                        if analyzed is not None:
                            return analyzed, None, None
                # 类型约束硬失败：int 列不可写入非整数字符串
                error = (f"列[{col_name}]类型为 int，值'{value}'无法转为整数"
                         f"且无枚举映射，已阻止写入")
        elif ct in ("float", "double", "number"):
            try:
                return float(sv), None, None
            except (ValueError, TypeError):
                error = (f"列[{col_name}]类型为 float，值'{value}'无法转为浮点数，已阻止写入")
        elif ct in ("bool", "boolean"):
            bl = sv.lower()
            if bl in ("1", "true", "yes", "是", "真", "对", "开", "启用", "有", "y", "t"):
                return 1, None, None
            if bl in ("0", "false", "no", "否", "假", "错", "关", "禁用", "无", "n", "f"):
                return 0, None, None
            error = (f"列[{col_name}]类型为 bool，值'{value}'无法转为布尔值，已阻止写入")
        elif ct in ("date", "datetime"):
            from .date_normalizer import parse_date
            dt = parse_date(value)
            if dt is None:
                error = (f"列[{col_name}]类型为 {ct}，值'{value}'无法解析为日期，已阻止写入")
            else:
                return dt, None, None
        if warn:
            print(warn, file=sys.stderr)
        if error:
            print(f"✗ {error}", file=sys.stderr)
        return value, warn, error

    def _precoerce_enum_value(self, col_name: str, val, stem: str, sheet: str):
        """D10: 单值写前枚举预转换。

        int 列 + 非 int 值 → enum_resolver.resolve_label 命中替换为 int，未命中保留原值。
        字符串数字（如 "1"）直接 int()。非 int 列不修改。

        在 _coerce_value 前调用，减少硬错误触发（命中枚举的值直接转 int，_coerce_value 一次成功）。
        """
        if val is None or not col_name:
            return val
        col_type = self._get_col_type(stem, sheet, col_name)
        if not col_type or col_type.lower() not in ("int", "integer"):
            return val
        if isinstance(val, int) and not isinstance(val, bool):
            return val
        sv = str(val).strip()
        try:
            return int(sv)
        except (ValueError, TypeError):
            pass
        try:
            er = get_enum_resolver()
            enum_val = er.resolve_label(stem, sheet, col_name, sv)
            if enum_val is not None:
                return enum_val
        except Exception:
            pass
        return val

    def _precoerce_enum_fields(self, fields, stem: str, sheet: str):
        """D10: fields dict 写前枚举预转换。返回新 dict（原 dict 不变）。

        遍历 fields，对 int 列+非 int 值调 _precoerce_enum_value 命中替换。
        """
        if not isinstance(fields, dict) or not fields:
            return fields
        return {col_name: self._precoerce_enum_value(col_name, val, stem, sheet)
                for col_name, val in fields.items()}

    def _pretranslate_effect_fields(self, fields, headers: list) -> dict:
        """把 cross_table_splitter 产出的语义键翻译成真实 Row1 表头。

        splitter 产出的语义键（不再硬编码 effect.key 具体值）：
          "效果类型" = "奖励"/"传送"/"战斗"/"对话"  → 写入「交互效果编号」列
          "reward ID" / "战斗ID" / "对话ID" / "目标space ID"  → 写入对应效果数据列

        表头实际结构（Interaction 表）：
          "编号" (主键) | "交互效果编号" | "3001: 战斗ID" | "3002: reward ID" |
          "3003: 目标space ID" | "3006: 对话ID" | "4003: 洞府ID" | ...

        翻译规则：
          "效果类型" → 找含「效果编号」/「交互效果」的表头，值由 LLM 在 ai_plan 阶段
                       读表头推断效果码（3001/3002/...）后填入。本方法不硬编码值。
          "reward ID"/"战斗ID"/... → 匹配表头含该关键词的列（如 "3002: reward ID"）
          effect.* 旧格式（兼容）→ 走原点分键翻译逻辑
        无匹配时保留原 key（让后续 match 自然报错，不静默丢弃）。
        """
        if not isinstance(fields, dict) or not fields:
            return fields
        # 兼容旧 effect.* 点分键格式
        if any(str(k).startswith("effect.") for k in fields):
            return self._translate_legacy_effect_keys(fields, headers)
        # 新语义键翻译
        if not any(k in ("效果类型", "reward ID", "战斗ID", "对话ID", "目标space ID")
                   for k in fields):
            return fields
        clean_names = [_clean_header(h) for h in headers]
        out: dict = {}
        for k, v in fields.items():
            ks = str(k)
            if ks == "效果类型":
                # 找「交互效果编号」列，值留给 AI 推断（这里不填具体码）
                target = None
                for i, cn in enumerate(clean_names):
                    if "效果编号" in cn or "交互效果" in str(headers[i] or ""):
                        target = headers[i]
                        break
                out[target if target is not None else k] = v
            else:
                # 语义值键（reward ID/战斗ID/...）→ 匹配表头含该关键词的列
                target = None
                for i, h in enumerate(headers):
                    hs = str(h or "")
                    if ks in hs or ks.replace(" ", "") in hs.replace(" ", ""):
                        target = h
                        break
                out[target if target is not None else k] = v
        return out

    def _translate_legacy_effect_keys(self, fields, headers: list) -> dict:
        """兼容旧 effect.* 点分键格式（旧 splitter 产出，现已弃用）。

        旧格式：effect.key=3002, effect.data.3002.reward_id=xxx
        翻译规则同原 _pretranslate_effect_fields 逻辑。
        """
        clean_names = [_clean_header(h) for h in headers]
        out: dict = {}
        for k, v in fields.items():
            ks = str(k)
            if ks == "effect.key":
                target = None
                for i, cn in enumerate(clean_names):
                    if "效果编号" in cn or "交互效果" in str(headers[i] or ""):
                        target = headers[i]
                        break
                out[target if target is not None else k] = v
            elif ks.startswith("effect.data."):
                parts = ks.split(".")
                if len(parts) >= 3:
                    code = parts[2]
                    target = None
                    for i, cn in enumerate(clean_names):
                        if cn == code:
                            target = headers[i]
                            break
                    out[target if target is not None else k] = v
                else:
                    out[k] = v
            else:
                out[k] = v
        return out

    def _translate_dotted_keys(self, fields, headers: list, alias_keys: set = None,
                               type_aliases: dict = None) -> dict:
        """通用嵌套点路径键翻译：非 effect.* 的点分键取末段作列名匹配键。

        effect.* 已由 _translate_legacy_effect_keys 专用处理（按 effect.data.<code>.<field>
        匹配表头 code 列），此处不重复，仅处理其余点分键。

        别名优先（G11 修复）：若点分键在 column_aliases.yaml 有精确配置（alias_keys 含此键），
        保留原 key 不取末段，让下游 matcher.match 阶段1 alias==key 精确命中。
        避免取末段后丢精确映射、退化为模糊匹配误命中主键（如 option_function.data.1.conv_id
        取末段 conv_id → InteractionConvOption 表无 conv_id alias → 模糊匹配误命中"编号"主键）。

        原则9（R8）：type_aliases（row2 规范名→row1 表头）补全 splitter 点分规范键。
        - 精确命中（option_function.function_type）→ 映射到 row1 列名
        - 前缀2段命中（option_function.data.2.quest_id ↔ option_function.data.1.conv_id
          共享 [option_function, data] 前缀）→ 同一物理列（option_function.data.* 系列）
          避免末段"quest_id"对中文表头匹配失败

        策略：
          1. effect.* → 保留原 key（专用翻译已处理）
          2. 点分键 + 在 alias_keys 中 → 保留原 key（精确别名命中）
          3. 点分键 + 在 type_aliases 中精确命中 → 映射到 row1 列名（原则9）
          4. 点分键 + type_aliases 前缀2段命中 → 映射到 row1 列名（option_function.data.*）
          5. 点分键 + 均未命中 → 取末段作列名匹配键
             aptitude_base.StrPotCon            → StrPotCon
             option_function.data.1.reward_id   → reward_id（无 alias 时）
        """
        if not isinstance(fields, dict) or not fields:
            return fields
        if alias_keys is None:
            alias_keys = set()
        if type_aliases is None:
            type_aliases = {}
        out: dict = {}
        for k, v in fields.items():
            ks = str(k)
            if ks.startswith("effect."):
                out[k] = v
                continue
            if "." in ks:
                # 别名优先：原 key 在 alias 配置中 → 保留原 key
                if ks in alias_keys:
                    out[k] = v
                elif ks in type_aliases:
                    # 原则9：row2 规范名精确命中 → 映射到 row1 列名
                    out[type_aliases[ks]] = v
                else:
                    # 原则9：前缀2段命中（option_function.data.* 同列族）
                    ksegs = ks.split(".")
                    matched_col = None
                    if len(ksegs) >= 2:
                        for tname, rh in type_aliases.items():
                            tsegs = tname.split(".")
                            if len(tsegs) >= 2 and ksegs[:2] == tsegs[:2]:
                                matched_col = rh
                                break
                    if matched_col is not None:
                        out[matched_col] = v
                    else:
                        out[ksegs[-1]] = v
            else:
                out[k] = v
        return out

    def _try_analyze_enum(self, stem: str, sheet: str, col: str, label: str):
        """D10: LLM 推断枚举映射，register_label 写 pending，返回 int 或 None。

        _coerce_value int 列硬错误（resolve_label 未命中）时触发。
        无 parser/client → 跳过（返回 None，走原硬错误）。
        7.4: analyze_enum_columns 内部缓存，同列每会话仅调一次 LLM。
        7.5: confidence < 0.7 由 register_label 拒绝。
        """
        try:
            from .analyze_enum_columns import analyze_enum_column
            parser = getattr(self, "parser", None)
            client = getattr(parser, "client", None) if parser else None
            if client is None:
                return None
            sid = None
            if hasattr(parser, "_ensure_session"):
                try:
                    sid = parser._ensure_session()
                except Exception:
                    sid = None
            if sid is None:
                return None
            model = getattr(parser, "model", None)

            def llm_call(prompt: str) -> str:
                resp = client.prompt(sid, prompt, model=model,
                                     cancel_event=getattr(self, "_cancel_event", None))
                return getattr(resp, "response_text", "") or ""

            mapping = analyze_enum_column(stem, sheet, col, label, llm_call_fn=llm_call)
            if not mapping:
                return None
            info = mapping.get(label)
            if not info:
                return None
            er = get_enum_resolver()
            ok = er.register_label(stem, sheet, col, label,
                                   info["value"], info["confidence"])
            if ok:
                return info["value"]
        except Exception:
            logger.warning("analyze_enum_column failed", exc_info=True)
        return None

    def _run_set(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """执行"修改/设置"操作：定位行 + 目标列 → 写入单元格值。

        流程：
            1. 解析定位列和匹配模式
            2. 定位行
            3. 写值：
               - 若 LLM 提供 fields 字典（多字段）→ 逐列名转列号循环 write_cell
               - 否则单字段：匹配目标列 + 提取值 → 单次 write_cell
            4. 类型校验（best-effort 转换）
        """
        # 1. 解析定位列
        loc_match, match_mode, matcher, stem, loc_col_name = self._resolve_locator_and_mode(path, sheet, intent)
        if loc_match is None:
            sug = self._fuzzy_suggest(
                loc_col_name, [h.split(":")[0] for h in matcher.headers])
            hint = self._fmt_simple(sug)
            res.add("match_locator", False, f"未找到定位列[{loc_col_name}]{hint}")
            res.message = f"无法匹配定位列「{loc_col_name}」{hint}"
            return res
        res.add("match_locator", True,
                f"列{loc_match.column}({loc_match.index},mode={match_mode}) 值={intent.locator_value!r}")
        self._fill_col_evidence(res, loc_match, loc_col_name)

        # T8: 定位前列查 L3 反模式 — 歧义列强制 exact，绕过 contains 层级
        loc_col_plain = (loc_match.column or "").split(":")[0]
        ap = self._check_anti_pattern(stem, sheet, column=loc_col_plain,
                                      input_text=intent.raw if intent else "")
        if ap and ap.get("action") == "force_exact":
            match_mode = "exact"

        # T2: 用户显式指定行号（用行6/第6行）→ 跳过行定位直接读该行
        row_match = self._apply_row_override(intent, path, sheet, loc_match)
        if row_match is None:
            # 复合主键优先（case5 ResidenceEntry 双键）：locator_fields/values 非空且≥2列时
            # 走 _locate_row_composite 多列交集定位，跳过单值路径。
            if intent.locator_fields and len(intent.locator_fields) >= 2 \
                    and len(intent.locator_fields) == len(intent.locator_values):
                row_match = self._locate_row_composite(
                    path, sheet, intent.locator_fields, intent.locator_values,
                    match_mode)
                if row_match is not None:
                    res.add("locate_row", True,
                            f"复合主键定位命中 行{row_match.row}"
                            f"({' + '.join(intent.locator_fields)})")
            if row_match is None:
                # 2. 定位行
                # O20g：locator_value 为空时从 fields 兜底提取（DecomposeAgent LLM 漏产
                # locator_field/locator_value 时，fields 字典可能含定位列的值）。
                # 按 loc_match.column（已解析的定位列名）从 fields 取值。
                if not intent.locator_value:
                    self._fill_locator_from_fields(intent, loc_match)
                if not intent.locator_value:
                    res.add("locate_row", False, "缺少行定位值")
                    res.message = "缺少行定位值"
                    return res
                row_match = self._locate_row(path, sheet, loc_match.index, intent.locator_value, match_mode)
            if row_match is None:
                sug = self._suggest_rows(path, sheet, loc_match.index, intent.locator_value)
                # §fuzzy 兜底：精确失败 + top1 是 locator_value 超集 → 自动重试（仅 get，
                # set/delete 涉及写操作需用户明确确认，不自动改写定位值）
                if intent.action == "get":
                    _lv = str(intent.locator_value or "")
                    if sug and _lv:
                        _top_val = str(sug[0][1]) if len(sug[0]) > 1 else ""
                        if _top_val and _lv in _top_val:
                            row_match = self._locate_row(
                                path, sheet, loc_match.index, _top_val, match_mode)
                            if row_match is not None:
                                res.add("locate_row", True,
                                        f"fuzzy 兜底命中: {_lv}→{_top_val}(行{row_match.row})")
                if row_match is None:
                    res.add("locate_row", False,
                            f"未找到 {intent.locator_value}(mode={match_mode})")
                    return self._trigger_cross_table_search_pause(res, path, sheet, loc_match, intent, sug)
            # §删除不可逆：多行同名时禁止"替用户挑一行"。原流程先调
            # _resolve_row_ambiguity 让 LLM 仲裁 → 返回 ambiguous=False → 绕过下方
            # 的歧义分支，静默删掉 LLM 猜的那一行（5 行同名删 1 行且用户无感，
            # 剩余同名行还在）。改为：delete 保留歧义态，交下方候选确认/勾选删除。
            # get/set 非破坏性动作保留原仲裁（查错行可切候选，无数据损失）。
            if row_match.ambiguous and intent.action != "delete":
                row_match = self._resolve_row_ambiguity(intent, row_match, path, sheet)
        row = row_match.row
        row_note = self._row_confidence_note(row_match)
        res.add("locate_row", True,
                f"row={row} value={row_match.value!r} conf={row_match.confidence:.2f} "
                f"method={row_match.method}"
                + (f" [歧义候选{len(row_match.alternatives)}个]" if row_match.ambiguous else ""))
        self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                path, sheet, loc_match.index)

        # 3a. 多字段写入（LLM 提供 fields）
        fields = intent.extras.get("fields")
        if isinstance(fields, dict) and fields:
            # §已修正旧值剔除：Step2 交互修正后残留的同列旧值不再参与写盘
            fields = self._drop_resolved_stale_fields(intent, fields, res)
            # 点分键翻译：effect.* 走专用，其余点分键取末段作列名匹配键
            # G11: 别名优先，原 key 在 column_aliases 有配置则保留原 key（精确命中）
            # 原则9（R8）：type_aliases 补全 splitter 点分规范键
            fields = self._pretranslate_effect_fields(fields, matcher.headers)
            fields = self._translate_dotted_keys(
                fields, matcher.headers, set(matcher.yaml_aliases.keys()),
                self._type_aliases(path, sheet, matcher.headers))
            written: list[str] = []
            failed: list[str] = []
            key_touched = False
            for col_name, val in fields.items():
                # 多 id 消歧：泛「id/编号」且 sheet 有多个 id 列 → 中止提示重试
                ambig, cands = self._check_id_ambiguity(matcher.headers, str(col_name))
                if ambig:
                    names = "、".join(c for _, c in cands)
                    res.add("id_ambiguous", False, f"列[{col_name}]命中多个id列: {names}")
                    res.message = (f"「{col_name}」可匹配多个 id 列：{names}。"
                                   f"请用具体列名（如「{cands[0][1]}」）重试。")
                    return res
                # 精确 match 优先（LLM 给的多为精确列名），避免 match_best 分词出短 id 子候选
                # 与精确列名同分时误取（如「神通id」被分出「id」→误命中「技能id」）
                m = matcher.match(col_name) or matcher.match_best(col_name)
                if m is None:
                    failed.append(col_name)
                    res.add("match_target", False, f"未找到列[{col_name}]")
                    continue
                if m.index == loc_match.index:
                    # 跳过定位列自身（避免把定位值改掉）
                    continue
                # 空值校验：set 意图下缺失值视为非法（如"改为"后无值），
                # 不把空串当合法值写入，避免 ok=True 但实际未改。
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    failed.append(col_name)
                    res.add("match_target", False, f"字段[{col_name}]缺少值")
                    continue
                # T8: 写值前列查 L3 type_constraint — 反模式列写前强制确认
                tgt_col_plain = (m.column or "").split(":")[0]
                tap = self._check_anti_pattern(stem, sheet, column=tgt_col_plain)
                if (tap and tap.get("action") == "require_confirm"
                        and not intent.extras.get("__anti_pattern_confirmed__")):
                    res.needs_confirm = True
                    res.confirm_token = f"ap:{stem}:{sheet}:{tgt_col_plain}"
                    res.confirm_kind = "anti_pattern"
                    res.message = (f"列「{tgt_col_plain}」曾因类型不符被多次拒绝写入，"
                                   f"请确认是否继续写入「{val}」。")
                    return res
                col_type = self._get_col_type(stem, sheet, m.column)
                coerced, warn, error = self._coerce_value(col_type, val, stem, sheet, m.column)
                if warn:
                    res.add("coerce_value", True, warn)
                    res.add_thinking("校验", f"类型转换「{m.column}」: {warn}")
                if error:
                    res.add("coerce_value", False, error)
                    failed.append(col_name)
                    continue
                # 配表模式增强：写值前公式列保护（覆写公式格破坏汇总）
                try:
                    f = self.cli._detect_formula(path, sheet, row, m.index)
                    if f:
                        res.add_thinking("校验", f"目标列「{m.column}」行{row} 是公式格（{f}），覆写会破坏汇总")
                        res.needs_confirm = True
                        res.confirm_token = f"ap:{stem}:{sheet}:{tgt_col_plain}"
                        res.confirm_kind = "formula_overwrite"
                        res.message = (f"目标格「{m.column}」行{row} 是公式：{f}。"
                                       f"覆写会破坏汇总，确认是否继续？")
                        return res
                except Exception:
                    pass
                # 配表模式增强：唯一列查重（名称类列写前检查是否已有同值）
                col_plain = (m.column or "").split(":")[0]
                if "名称" in col_plain or "name" in col_plain.lower():
                    try:
                        all_rows = self.cli.read_sheet(path, sheet)
                        dsr = self.cli._resolve_data_start(path, sheet)
                        col_idx_0 = m.index - 1
                        dup_rows = []
                        for i, r in enumerate(all_rows):
                            cv = r[col_idx_0] if col_idx_0 < len(r) else None
                            if cv is not None and str(cv).strip() == str(coerced).strip():
                                dup_rows.append(dsr + i)
                        if dup_rows:
                            res.add_thinking("校验", f"唯一列「{m.column}」已有 {len(dup_rows)} 处同值「{coerced}」（行{dup_rows[:3]}），写入会造成重名")
                    except Exception:
                        pass
                # ID 列段校验（越界 → 阻止写入，避免跨表冲突）
                if self._is_id_column(m.column) and coerced is not None:
                    ok, reason = self._validate_id_scope(stem, sheet, m.column, coerced)
                    if not ok:
                        res.add("id_scope", False, reason)
                        res.message = reason
                        failed.append(col_name)
                        continue
                    # P0: 改主键级联影响预览门（写前）—— 避免改 PK 后引用行外键悬空
                    # ca-overview.md §2.3.2/§5：事后救火→事前预防
                    if m.index == 1:
                        confirmed = intent.extras.get("__cascade_set_pk_confirmed__") is True
                        if not confirmed:
                            try:
                                from ..repair.cascade_planner import preview_cascade_set_pk
                                old_pk_r = self.cli.read_cell(path, sheet, row, m.index)
                                old_pk = getattr(old_pk_r, "data", None)
                                if old_pk is not None:
                                    preview = preview_cascade_set_pk(
                                        self.cli, path, sheet, row, m.column, old_pk, coerced, stem)
                                    if preview["count"] > 0:
                                        res.needs_confirm = True
                                        res.confirm_token = f"cascade_set_pk:{stem}:{sheet}:{row}:{m.column}"
                                        res.confirm_kind = "cascade_set_pk"
                                        res.message = (
                                            f"将修改主键「{m.column}」: {old_pk} → {coerced}，"
                                            f"需联动更新 {preview['count']} 处引用行"
                                            f"（置信度: {preview['confidence']}）。\n"
                                            + "\n".join(preview["items"][:10])
                                        )
                                        intent.extras["__cascade_set_pk_pending__"] = {
                                            "col_name": m.column, "col_idx": m.index,
                                            "old_val": old_pk, "new_val": coerced,
                                            "affected": preview["affected"],
                                        }
                                        return res
                            except Exception:
                                logger.debug("PK 级联预览失败，降级走原逻辑", exc_info=True)
                # 修改前读取旧值（供表体展示 旧值→新值）
                old_v = None
                ro = self.cli.read_cell(path, sheet, row, m.index)
                if ro.ok:
                    old_v = ro.data
                nf = self._get_col_number_format(stem, sheet, m.column) or None
                verify = self._write_cell_and_verify(path, sheet, row, m.index, coerced, number_format=nf)
                if verify.get("ok"):
                    written.append(f"{m.column}={coerced}")
                    res.final = verify.get("cli_result")
                    intent.target_field = m.column
                    intent.value = coerced
                    self._add_result_row(res, m.index, m.column, old_v, coerced)
                    if not self._refresh_index_after_write(path):
                        res.index_dirty = True
                    if m.index == 1:
                        key_touched = True
                        # P0: 主键写入成功后执行级联更新（已确认路径）
                        pending = intent.extras.get("__cascade_set_pk_pending__")
                        if pending and intent.extras.get("__cascade_set_pk_confirmed__"):
                            try:
                                from ..repair.cascade_planner import apply_cascade_set_pk
                                cres = apply_cascade_set_pk(self.cli, pending["affected"])
                                ok_n = sum(1 for r in cres if r.get("ok"))
                                fail_n = len(cres) - ok_n
                                res.add_thinking(
                                    "级联",
                                    f"PK 级联更新: {ok_n} 处成功"
                                    + (f"，{fail_n} 处失败" if fail_n else ""))
                                if fail_n:
                                    res.add("cascade_update", False,
                                            f"{fail_n} 处引用行级联更新失败")
                            except Exception:
                                logger.warning("PK 级联更新执行失败", exc_info=True)
                else:
                    _werr = (verify.get("error")
                             or (verify.get("mismatched_fields") and "写后验证不符")
                             or "write 失败")
                    res.add("write", False, _werr)
                    failed.append(col_name)
                    # O20c：失败列结构化入 res.failures 带 failed_col，供 error_classifier
                    # 提取列名归 COLUMN_NOT_FOUND（原仅 message 字符串，classifier 难提取）。
                    try:
                        res.failures.append({
                            "code": 40, "kind": "write_failed",
                            "table": stem, "sheet": sheet,
                            "failed_col": col_name, "failed_val": coerced,
                            "message": _werr,
                        })
                    except Exception:
                        pass
            if written:
                res.add("write", True, f"写入 {len(written)} 列: {written}")
                res.message = f"{intent.locator_value} " + ", ".join(written) + row_note
                if key_touched:
                    self._auto_sort_after_write(path, sheet, res)
            elif failed:
                res.message = f"未能写入任何列，失败: {failed}"
            return res

        # 3b. 单字段路径（规则提取目标列+值）
        tgt_match, tgt_value = self._match_target_column(
            intent, matcher, stem, sheet, loc_match.index)
        if tgt_match is None:
            res.add("match_target", False, "目标列匹配失败")
            res.message = "无法匹配目标列，概率表格不存在该信息"
            # O20c：目标列匹配失败入 res.failures 带 failed_col（intent.target_field 候选）。
            _tgt_field = getattr(intent, "target_field", None) or ""
            try:
                res.failures.append({
                    "code": 40, "kind": "column_not_found",
                    "table": stem, "sheet": sheet,
                    "failed_col": _tgt_field, "message": f"无法匹配目标列：{_tgt_field}",
                })
            except Exception:
                pass
            return res
        intent.target_field = tgt_match.column
        intent.value = tgt_value
        res.add("match_target", True,
                f"列{tgt_match.column}({tgt_match.index},score={tgt_match.score:.2f}) 值={tgt_value!r}")

        # 4. 类型校验（强制转换，不满足类型约束则阻止写入）
        if intent.value is None:
            res.add("write", False, "目标值为空")
            res.message = "目标值为空"
            return res
        col_type = self._get_col_type(stem, sheet, tgt_match.column)
        coerced, warn, error = self._coerce_value(col_type, intent.value, stem, sheet, tgt_match.column)
        if warn:
            res.add("coerce_value", True, warn)
        if error:
            res.add("coerce_value", False, error)
            res.message = error
            return res
        intent.value = coerced

        # 5. 写入（先读旧值供表体展示 旧值→新值）
        old_v = None
        ro = self.cli.read_cell(path, sheet, row, tgt_match.index)
        if ro.ok:
            old_v = ro.data
        nf = self._get_col_number_format(stem, sheet, tgt_match.column) or None
        verify = self._write_cell_and_verify(path, sheet, row, tgt_match.index, intent.value, number_format=nf)
        res.final = verify.get("cli_result")
        # A2/AD1/AD2：消费 cli_result.hold_events → 软失败 + SSE（CLI 构造事件，agent 上报）。
        # 兼容轻量 mock（SimpleNamespace 等）：无 _handle_cli_hold_events 时跳过。
        if res.final is not None:
            _hold_handler = getattr(self, "_handle_cli_hold_events", None)
            if _hold_handler is not None:
                _hold_handler(res, res.final, sheet)
        if verify.get("ok"):
            res.add("write", True, f"写入 [{sheet}!{row},{tgt_match.index}] = {intent.value!r}（写后验证通过）")
            res.message = (f"{intent.locator_value} {tgt_match.column} {old_v} → {intent.value}"
                           + row_note + self._column_confidence_note(tgt_match))
            self._add_result_row(res, tgt_match.index, tgt_match.column, old_v, intent.value)
            if not self._refresh_index_after_write(path):
                res.index_dirty = True
            if tgt_match.index == 1:
                self._auto_sort_after_write(path, sheet, res)
        else:
            res.add("write", False, verify.get("error") or "写后验证不符")
            res.message = "写入失败"
        return res

    # 清空类动词：仅当意图明确是"清空某列"时才清空单元格，否则默认删整行
    _CLEAR_INTENT_VERBS = ("清空", "改为空", "设为空", "改成空", "置空", "清掉")

    def _is_clear_column_intent(self, intent: NLIntent) -> bool:
        """判断 delete 意图是否明确指向"清空某列"而非"删除整行"。

        判定条件（满足其一即可）：
            1. 原始文本含清空类动词（如"清空""置空"）
            2. parser 已解析出 target_field（说明用户明确指定了目标列）
        """
        if intent.target_field:
            return True
        raw = intent.raw or ""
        return any(v in raw for v in self._CLEAR_INTENT_VERBS)

    def _run_delete(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """执行"删除"操作。

        默认行为：删除整行（最符合"删除"直觉，避免误清空）。
        仅当意图明确是"清空某列"时（含清空类动词或已解析出 target_field），
        才清空对应单元格并在返回信息中明确说明"已清空 XX 列"。
        """
        # T8: 删除前查 L3 failed_operation — 反模式操作直接拒绝，不进 dry-run
        ap_op = self._check_anti_pattern(path.stem, sheet, operation="delete_row")
        if ap_op and ap_op.get("action") == "block_dry_run":
            res.add("anti_pattern_block", False,
                    f"此删除操作已标记为高风险反模式（{ap_op.get('id')}），请手动处理")
            res.message = "此删除操作已标记为高风险反模式，请手动处理"
            return res

        # 1. 解析定位列
        loc_match, match_mode, matcher, stem, loc_col_name = self._resolve_locator_and_mode(path, sheet, intent)
        if loc_match is None:
            sug = self._fuzzy_suggest(
                loc_col_name, [h.split(":")[0] for h in matcher.headers])
            hint = self._fmt_simple(sug)
            res.add("match_locator", False, f"未找到定位列[{loc_col_name}]{hint}")
            res.message = f"无法匹配定位列「{loc_col_name}」{hint}"
            return res
        res.add("match_locator", True,
                f"列{loc_match.column}({loc_match.index},mode={match_mode}) 值={intent.locator_value!r}")
        self._fill_col_evidence(res, loc_match, loc_col_name)

        # T8: 定位前列查 L3 反模式 — 歧义列强制 exact，绕过 contains 层级
        loc_col_plain = (loc_match.column or "").split(":")[0]
        ap = self._check_anti_pattern(stem, sheet, column=loc_col_plain,
                                      input_text=intent.raw if intent else "")
        if ap and ap.get("action") == "force_exact":
            match_mode = "exact"

        # T2: 用户显式指定行号（用行6/第6行）→ 跳过行定位直接读该行
        row_match = self._apply_row_override(intent, path, sheet, loc_match)
        if row_match is None:
            # 复合主键优先（case5 ResidenceEntry 双键）：locator_fields/values 非空且≥2列时
            # 走 _locate_row_composite 多列交集定位，跳过单值路径。
            if intent.locator_fields and len(intent.locator_fields) >= 2 \
                    and len(intent.locator_fields) == len(intent.locator_values):
                row_match = self._locate_row_composite(
                    path, sheet, intent.locator_fields, intent.locator_values,
                    match_mode)
                if row_match is not None:
                    res.add("locate_row", True,
                            f"复合主键定位命中 行{row_match.row}"
                            f"({' + '.join(intent.locator_fields)})")
            if row_match is None:
                # 2. 定位行
                # O20g：locator_value 为空时从 fields 兜底提取（DecomposeAgent LLM 漏产
                # locator_field/locator_value 时，fields 字典可能含定位列的值）。
                # 按 loc_match.column（已解析的定位列名）从 fields 取值。
                if not intent.locator_value:
                    self._fill_locator_from_fields(intent, loc_match)
                if not intent.locator_value:
                    res.add("locate_row", False, "缺少行定位值")
                    res.message = "缺少行定位值"
                    return res
                row_match = self._locate_row(path, sheet, loc_match.index, intent.locator_value, match_mode)
            if row_match is None:
                sug = self._suggest_rows(path, sheet, loc_match.index, intent.locator_value)
                # §fuzzy 兜底：精确失败 + top1 是 locator_value 超集 → 自动重试（仅 get，
                # set/delete 涉及写操作需用户明确确认，不自动改写定位值）
                if intent.action == "get":
                    _lv = str(intent.locator_value or "")
                    if sug and _lv:
                        _top_val = str(sug[0][1]) if len(sug[0]) > 1 else ""
                        if _top_val and _lv in _top_val:
                            row_match = self._locate_row(
                                path, sheet, loc_match.index, _top_val, match_mode)
                            if row_match is not None:
                                res.add("locate_row", True,
                                        f"fuzzy 兜底命中: {_lv}→{_top_val}(行{row_match.row})")
                if row_match is None:
                    res.add("locate_row", False,
                            f"未找到 {intent.locator_value}(mode={match_mode})")
                    return self._trigger_cross_table_search_pause(res, path, sheet, loc_match, intent, sug)
            # §删除不可逆：多行同名时禁止"替用户挑一行"。原流程先调
            # _resolve_row_ambiguity 让 LLM 仲裁 → 返回 ambiguous=False → 绕过下方
            # 的歧义分支，静默删掉 LLM 猜的那一行（5 行同名删 1 行且用户无感，
            # 剩余同名行还在）。改为：delete 保留歧义态，交下方候选确认/勾选删除。
            # get/set 非破坏性动作保留原仲裁（查错行可切候选，无数据损失）。
            if row_match.ambiguous and intent.action != "delete":
                row_match = self._resolve_row_ambiguity(intent, row_match, path, sheet)
        row = row_match.row
        row_note = self._row_confidence_note(row_match)
        res.add("locate_row", True,
                f"row={row_match.row} value={row_match.value!r} conf={row_match.confidence:.2f} "
                f"method={row_match.method}"
                + (f" [歧义候选{len(row_match.alternatives)}个]" if row_match.ambiguous else ""))
        self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                path, sheet, loc_match.index)
        # 删除是不可逆操作：LLM 仲裁后仍存在歧义时不能静默删任一行。改为 needs_confirm
        # 暂停：列出全部候选行（row_evidence.alternatives 已含 summary 供前端渲染候选卡片），
        # confirm_token 编码全部候选行号，确认=删除全部匹配行（契合「删除…名称为 X 的行」
        # 的语义=删除所有匹配行）；用户若只删部分，可单独发「删除 {stem} 第N行」走
        # _apply_row_override。原直接判 failure 不给选择 → activity 春节活动命中行11/12
        # 却只能整体失败，用户无法完成删除。
        if row_match.ambiguous:
            _cands = [(row_match.row, row_match.value)] + list(row_match.alternatives)
            _ambig_rows = sorted({_r for _r, _ in _cands})
            _alt = "、".join(f"行{_r}「{_v}」" for _r, _v in _cands)
            _ask_cb = getattr(self, "_ask_callback", None)
            # §多行选择删除（用户需求）：交互场景 + env CODEMAKER_AMBIG_DELETE_ASK=1 时，
            # 走 Step3 内 ask 通道发 mode_hint="row_multiselect" 卡片（候选行带 summary），
            # 前端渲染行前勾选框 + 确认按钮，reply 带 selected_rows=[行号...]，本步即时
            # 降序删选中行（单次交互，不走 confirm_token 二次往返）。env=0 / 非交互场景
            # 保持 needs_confirm + ambig_delete token（删全部候选，token 支持子集由前端拼），
            # 兼容现有确认按钮前端，不回归。边界独立于旧 verify/repair 路径。
            # _dry_run_flag：预演/预览路径的 ask_cb 是"自动接受建议"的假回调，不会
            # 返回 selected_rows，若放行会被当"用户未勾选"→ 静默走删全部候选，预演
            # 语义失真。预演场景直接回落 needs_confirm（只读不删）。
            if (_ask_cb is not None
                    and not getattr(self, "_dry_run_flag", False)
                    and os.getenv("CODEMAKER_AMBIG_DELETE_ASK", "1") != "0"):
                _cand_rows = [
                    {"row": _r, "value": _v,
                     "summary": self._row_summary(path, sheet, _r,
                                                 skip_col=loc_match.index)}
                    for _r, _v in _cands]
                _q = {
                    "reason": f"「{intent.locator_value}」命中 {len(_ambig_rows)} 行同名候选，请勾选要删除的行后确认",
                    "error_type": "ambiguous_delete",
                    "root_cause": f"删除不可逆且命中多行：{_alt}，需用户选择具体行",
                    "table": path.stem, "sheet": sheet,
                    "failed_col": loc_match.column or "",
                    "failed_val": intent.locator_value or "",
                    "attempted_strategies": "行定位 + LLM 仲裁仍歧义",
                    "mode_hint": "row_multiselect",
                    "rows": _cand_rows,
                    "suggestion": "勾选要删的行后点「确认删除」；不删点「跳过」",
                    "snip": (getattr(intent, "raw", "") or "")[:120],
                    "user_friendly": {
                        "reason": (f"「{intent.locator_value}」命中 {_ambig_rows} 多行同名"
                                   f"（{_alt}），删除不可逆，需你选要删哪几行。"),
                        "action": "勾选要删的行后点确认；不想删点跳过。"},
                }
                _reply = _ask_cb(_q) or {}
                if _reply.get("mode") == "skip":
                    res.ok = True
                    res.message = "已取消删除（用户跳过多行选择）"
                    res.add("delete_row", False, "用户跳过多行删除选择")
                    return res
                _sel = _reply.get("selected_rows") or []
                if not _sel:
                    # 前端未实现 row_multiselect → 回退 needs_confirm（删全部，兼容现前端）
                    res.needs_confirm = True
                    res.confirm_kind = "ambiguous_delete"
                    res.confirm_token = (
                        f"ambig_delete:{path.stem}:{sheet}:"
                        f"{','.join(str(_r) for _r in _ambig_rows)}")
                    res.add("locate_row_ambiguous", True,
                            f"命中 {len(_ambig_rows)} 行（前端未实现勾选，回退删全部）：{_alt}")
                    res.message = (
                        f"「{intent.locator_value}」命中 {len(_ambig_rows)} 行同名候选：{_alt}。"
                        f"确认将删除以上全部行。")
                    return res
                # 降序删选中行（保行号稳定）+ 记 result_rows
                header = self.cli.read_header(path, sheet)
                deleted = []
                for _r in sorted({_x for _x in _sel if _x}, reverse=True):
                    _rd = self._read_row_data(path, sheet, _r)
                    if not _rd:
                        continue
                    _rr = self.cli.delete_row(path, sheet, _r)
                    if _rr is not None and getattr(_rr, "ok", False):
                        res.add("delete_row", True, f"删除行 row={_r}")
                        for ci in sorted(_rd.keys()):
                            self._add_result_row(res, ci,
                                self._col_name(header, ci), old_value=_rd[ci])
                        deleted.append(_r)
                    else:
                        res.add("delete_row", False,
                                f"删除行 {_r} 失败：{getattr(_rr,'error','') if _rr else ''}")
                if deleted:
                    self._refresh_index_after_write(path)
                    res.ok = True
                    res.message = (f"已删除 {sheet} 行 "
                                   f"{','.join(str(r) for r in sorted(deleted))}（用户勾选）")
                else:
                    res.ok = False
                    res.message = "未删除任何行（勾选行已变更或删失败）"
                return res
            # 默认/非交互：needs_confirm 暂停（删全部候选，token 支持子集由前端拼）
            res.needs_confirm = True
            res.confirm_kind = "ambiguous_delete"
            res.confirm_token = (
                f"ambig_delete:{path.stem}:{sheet}:"
                f"{','.join(str(_r) for _r in _ambig_rows)}")
            res.add("locate_row_ambiguous", True,
                    f"命中 {len(_ambig_rows)} 行同名候选，暂停待确认删除：{_alt}")
            res.message = (
                f"「{intent.locator_value}」命中 {len(_ambig_rows)} 行同名候选：{_alt}。\n"
                f"确认将删除以上全部 {len(_ambig_rows)} 行；"
                f"若只删部分请单独指定行号（如「删除 {path.stem} 第11行」）。")
            return res
        row = row_match.row

        # 3. 判定意图：明确"清空某列"才清空单元格，否则默认删整行
        clear_column = self._is_clear_column_intent(intent)
        tgt_match = None
        if clear_column:
            tgt_match, _ = self._match_target_column(
                intent, matcher, stem, sheet, loc_match.index)

        if clear_column and tgt_match is not None:
            # 清空指定单元格
            intent.value = ""
            old_v = None
            ro = self.cli.read_cell(path, sheet, row, tgt_match.index)
            if ro.ok:
                old_v = ro.data
            r = self.cli.write_cell(path, sheet, row, tgt_match.index, "")
            res.final = r
            if r.ok:
                res.add("delete_cell", True, f"清空 [{sheet}!{row},{tgt_match.index}]")
                res.message = f"已清空：{sheet} 行{row} 列{tgt_match.column}({tgt_match.index})" + self._row_confidence_note(row_match)
                self._add_result_row(res, tgt_match.index, tgt_match.column, old_v, "")
                self._refresh_index_after_write(path)
            else:
                res.add("delete_cell", False, r.error or "")
                res.message = "清空失败"
        else:
            # 删除整行（默认行为）
            # 二次确认触发条件：1) 有级联关联数据 2) 行定位置信度不足
            #（低置信度时即使无级联关联也不能静默删除——删除不可逆，误删代价远高于误改）
            confirmed = intent.extras.get("__delete_confirmed__") is True
            if not confirmed:
                preview = self._preview_cascade_delete(path, sheet, row, stem)
                low_conf = row_match.confidence < ACCEPT_THRESHOLD
                if preview["count"] > 0 or low_conf:
                    res.needs_confirm = True
                    res.confirm_token = f"delete:{path.stem}:{sheet}:{row}"
                    # 有关联数据→cascade（确认=级联/取消=仅删当前）；仅低置信度→confidence（确认=删/取消=不删）
                    res.confirm_kind = "cascade" if preview["count"] > 0 else "confidence"
                    conf_note = (f"定位置信度{row_match.confidence:.2f}(命中方式={row_match.method})，"
                                if low_conf else "")
                    res.add("cascade_preview", True,
                            f"{conf_note}将级联删除 {preview['count']} 处关联数据")
                    cascade_note = (f"并级联删除 {preview['count']} 处关联数据。"
                                    if preview["count"] > 0 else "")
                    res.message = (
                        f"将删除 {sheet} 行{row}「{row_match.value}」。{conf_note}{cascade_note}\n"
                        + "\n".join(preview["items"][:10])
                    ).strip()
                    return res
            # 无级联影响或已确认 → 执行删除（含级联）
            # 注意：级联匹配依赖主行数据，必须在删主行前读取
            header = self.cli.read_header(path, sheet)
            row_data = self._read_row_data(path, sheet, row)
            r = self.cli.delete_row(path, sheet, row)
            res.final = r
            if r.ok:
                res.add("delete_row", True, f"删除行 row={row}")
                res.message = f"已删除：{sheet} 行{row}"
                # 表体：删除行的内容（列名+被删值）
                for ci in sorted(row_data.keys()):
                    self._add_result_row(res, ci, self._col_name(header, ci),
                                        old_value=row_data[ci])
                # 执行级联删除（已确认或无关联数据时直接执行）
                self._do_cascade_delete(path, sheet, header, row_data, stem)
                self._refresh_index_after_write(path)
            else:
                res.add("delete_row", False, r.error or "")
                res.message = "删除行失败"
        return res

    def _read_row_data(self, path: Path, sheet: str, row: int) -> dict[int, object]:
        """读取指定行的所有非空列值，返回 {col_idx: value}，供级联删除匹配。"""
        header = self.cli.read_header(path, sheet)
        result: dict[int, object] = {}
        try:
            ws = self.cli._load(path)[sheet]
            for ci in range(1, len(header) + 1):
                val = ws.cell(row, ci).value
                if val is not None:
                    result[ci] = val
        except Exception:
            pass
        return result

    def _preview_cascade_delete(self, path: Path, sheet: str, row: int,
                                stem: str) -> dict:
        """dry-run 预览级联删除影响，返回 {count, items}，不执行任何删除。"""
        try:
            from ..cli.xlsx_tool import _collect_cascade_deletes
            header = self.cli.read_header(path, sheet)
            row_data = self._read_row_data(path, sheet, row)
            pending = _collect_cascade_deletes(
                self.cli, path, sheet, header, row_data, stem)
            items = [s for _, _, _, s in pending]
            return {"count": len(pending), "items": items}
        except Exception as e:
            # 预览失败不阻断主删除流程，按无级联影响处理
            return {"count": 0, "items": [f"(级联预览失败: {e})"]}

    def _do_cascade_delete(self, path: Path, sheet: str, header: list[str],
                           row_data: dict[int, object], stem: str):
        """执行级联删除（主行已删除后调用，row_data 为删除前读取的快照）。
        失败仅记录，不阻断主流程。

        缺口5：反向引用清理。除原 _cascade_delete（同目录/前缀扫关联文件）外，
        用 cascade_resolver.get_referencing_tables(stem) 主动追溯声明式 FK 子表，
        跨目录（quest↔spawn_quest_entity 在不同目录）也能覆盖。
        """
        try:
            from ..cli.xlsx_tool import _cascade_delete
            if not row_data:
                return
            _cascade_delete(self.cli, path, sheet, header, row_data, stem,
                            cascade=True)
        except Exception:
            pass
        # 缺口5：声明式 FK 反向追溯子表。跨目录表（如 quest↔spawn_quest_entity
        # 不同目录）_find_related_files 同目录策略扫不到，用 get_referencing_tables
        # 显式拿子表 stem + source_col → 映射 path → 按 source_col 名找列删行。
        # source_col 是子表引用本表的列名（如 reward_id），用 _match_header 语义匹配。
        try:
            from .cascade_resolver import get_referencing_tables
            from ..cli.xlsx_tool import _match_header, _semantic_col_names
            ref_tables = get_referencing_tables(stem) or []
            if not ref_tables:
                return
            # stem → path 映射
            stem_to_path = {}
            try:
                for p in self.cli.list_tables():
                    stem_to_path[p.stem] = p
            except Exception:
                return
            for ref in ref_tables:
                ref_stem = ref.get("target_stem") or ref.get("source_stem") or ""
                source_col = ref.get("source_col") or ""
                if not ref_stem or ref_stem == stem:
                    continue
                ref_path = stem_to_path.get(ref_stem)
                if ref_path is None:
                    continue
                try:
                    for ref_sheet in self.cli.get_sheets(ref_path):
                        if not ref_sheet or "说明" in ref_sheet or "CONFIG" in ref_sheet:
                            continue
                        try:
                            ref_header = self.cli.read_header(ref_path, ref_sheet) or []
                        except Exception:
                            continue
                        # 用 source_col 显式匹配子表列（reward_id → 子表 reward_id 列）
                        matched_ci = None
                        if source_col:
                            for variant in _semantic_col_names(source_col):
                                matched_ci = _match_header(
                                    variant, ref_header, ref_stem, ref_sheet)
                                if matched_ci is not None:
                                    break
                        # source_col 匹配失败 → 回退主表 header 各列语义匹配（原逻辑）
                        if matched_ci is None:
                            pending = []
                        else:
                            pending = []
                            ws = self.cli._load(ref_path)[ref_sheet]
                            last_row = self.cli._last_data_row(
                                ws, getattr(self.cli, "data_start_row", 5))
                            # 用主行 row_data 各列值在子表 matched_ci 列找值相等行
                            for _ci, val in row_data.items():
                                if val is None:
                                    continue
                                for r in range(
                                        getattr(self.cli, "data_start_row", 5),
                                        last_row + 1):
                                    cell_val = ws.cell(r, matched_ci).value
                                    if cell_val is not None and str(cell_val) == str(val):
                                        pending.append(r)
                        for r in pending:
                            dr = self.cli.delete_row(ref_path, ref_sheet, r)
                            if getattr(dr, "ok", False):
                                try:
                                    self.add_thinking("级联",
                                        f"反向追溯删 {ref_stem}/{ref_sheet} 行{r}")
                                except Exception:
                                    pass
                except Exception:
                    pass
        except Exception:
            pass

    _WHOLE_ROW_RE = re.compile(r"(所有|全部|整个|整行|全)(属性|信息|字段|数据|列|内容|东西|值|情况)")
    # 泛指词:目标列匹配失败时,若文本含这些词则回退整行读取(如"查询刑天一阶的属性")
    _FUZZY_WHOLE_RE = re.compile(r"(属性|信息|数据|内容|面板|资料|详情|情况)")

    def _is_whole_row_get(self, intent: NLIntent) -> bool:
        """判断 get 意图是否为"读取整行全部列"（如"所有属性""全部信息"）。"""
        return bool(self._WHOLE_ROW_RE.search(intent.raw or ""))

    def _read_whole_row(self, intent: NLIntent, path: Path, sheet: str,
                        loc_match, match_mode: str, res: AgentResult) -> AgentResult:
        """整行读取全部非空列并填充 res;定位失败时填充失败 message。
        调用方得到返回值即表示流程终结,直接 return。
        R9: 多行命中时全部返回,不进行 LLM 仲裁。"""
        # T2: 用户显式指定行号（用行6/第6行）→ 跳过行定位直接读该行
        row_match = self._apply_row_override(intent, path, sheet, loc_match)
        if row_match is None:
            # 复合主键优先（case5 ResidenceEntry 双键）
            if intent.locator_fields and len(intent.locator_fields) >= 2 \
                    and len(intent.locator_fields) == len(intent.locator_values):
                row_match = self._locate_row_composite(
                    path, sheet, intent.locator_fields, intent.locator_values,
                    match_mode)
            if row_match is None:
                if not intent.locator_value:
                    res.add("locate_row", False, "缺少行定位值")
                    res.message = "缺少行定位值"
                    return res
                row_match = self._locate_row(path, sheet, loc_match.index,
                                             intent.locator_value, match_mode)
            if row_match is None:
                sug = self._suggest_rows(path, sheet, loc_match.index, intent.locator_value)
                # §fuzzy 兜底：精确失败 + top1 是 locator_value 超集（如"饕餮"→"饕餮一阶"）
                # → 自动用 top1 值重试行定位，避免直接暂停要求用户介入
                _lv = str(intent.locator_value or "")
                if sug and _lv:
                    _top_val = str(sug[0][1]) if len(sug[0]) > 1 else ""
                    if _top_val and _lv in _top_val:
                        row_match = self._locate_row(
                            path, sheet, loc_match.index, _top_val, match_mode)
                        if row_match is not None:
                            res.add("locate_row", True,
                                    f"fuzzy 兜底命中: {_lv}→{_top_val}(行{row_match.row})")
                if row_match is None:
                    hint = self._fmt_rows(sug)
                    res.add("locate_row", False,
                            f"未找到 {intent.locator_value}(mode={match_mode}){hint}")
                    res.add_thinking("定位", f"在 {path.stem}/{sheet} 未找到「{intent.locator_value}」"
                                     f"（定位列={loc_match.column}）。相近项：{hint or '无'}")
                    # 配表模式增强：行未命中 → 暂停，等用户确认是否跨表搜索
                    top5 = [{"row": r, "value": v, "score": sc} for r, v, sc in sug]
                    res.pending_search = {
                        "table_stem": path.stem, "sheet": sheet,
                        "col_name": loc_match.column or "",
                        "col_idx": loc_match.index,
                        "value": intent.locator_value or "",
                        "top5": top5,
                    }
                    res.needs_confirm = True
                    res.confirm_token = f"search:{path.stem}:{sheet}:{loc_match.index}:{intent.locator_value}"
                    res.confirm_kind = "cross_table_search"
                    res.message = (f"在 {path.stem}/{sheet} 未找到「{intent.locator_value}」。"
                                   f"{hint}\n是否在其他相近表中查找？")
                    return res
            # R9: 查询场景多行命中 → 全部展示,不走 LLM 仲裁
            if row_match.ambiguous:
                return self._read_whole_row_multi(intent, path, sheet, loc_match, row_match, res)

        row = row_match.row
        row_note = self._row_confidence_note(row_match)
        res.add("locate_row", True,
                f"row={row} value={row_match.value!r} conf={row_match.confidence:.2f} "
                f"method={row_match.method}")
        self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                path, sheet, loc_match.index)
        header = self.cli.read_header(path, sheet)
        row_data = self._read_row_data(path, sheet, row)
        pairs = []
        for ci in range(1, len(header) + 1):
            name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
            val = row_data.get(ci)
            if val is not None and str(val).strip() != "":
                pairs.append((name, val))
                self._add_result_row(res, ci, name, new_value=val)
        res.add("read_row", True, f"读取整行 [{sheet}!{row}] {len(pairs)} 个非空列")
        res.final = CLICallResult(
            ok=True,
            data={"row": row, "values": [{"col": n, "value": v} for n, v in pairs]},
        )
        lines = "\n".join(f"  {n} = {v}" for n, v in pairs)
        res.message = f"查询结果：{sheet} 行{row} 共 {len(pairs)} 个属性：\n{lines}" + row_note
        return res

    def _read_whole_row_multi(self, intent: NLIntent, path: Path, sheet: str,
                              loc_match, row_match: RowMatch, res: AgentResult) -> AgentResult:
        """R9: 整行查询多行命中 → 全部读取并展示为表格。"""
        header = self.cli.read_header(path, sheet)
        all_cands = [(row_match.row, row_match.value)] + list(row_match.alternatives)

        # 统计各候选行数据
        multi_rows: list[dict] = []
        for r, val in all_cands:
            row_data = self._read_row_data(path, sheet, r)
            cols = []
            for ci in range(1, len(header) + 1):
                name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
                v = row_data.get(ci)
                if v is not None and str(v).strip() != "":
                    cols.append({"col": ci, "col_name": name, "value": v})
            multi_rows.append({"row": r, "value": val, "columns": cols})

        res.multi_rows = multi_rows

        # 选出关键列（前5个非空列 + 主名称列）做摘要表格
        key_col_names = self._pick_key_columns(header, stem=path.stem, sheet=sheet)
        lines = []
        for mr in multi_rows:
            r = mr["row"]
            vals = []
            for cn in key_col_names:
                found = next((c["value"] for c in mr["columns"] if c["col_name"] == cn), "")
                vals.append(str(found) if found is not None else "")
            lines.append(f"  行{r} | " + " | ".join(vals))

        header_line = " | ".join(key_col_names)
        table_text = f"  {header_line}\n" + "\n".join(lines)
        res.message = (f"找到 {len(multi_rows)} 条匹配「{intent.locator_value}」的记录：\n"
                       f"{table_text}")

        # 将第一行数据写入 result_rows（向后兼容）
        first = multi_rows[0]
        for c in first["columns"]:
            self._add_result_row(res, c["col"], c["col_name"], new_value=c["value"])

        res.add("locate_row", True,
                f"命中{len(multi_rows)}行: {', '.join('r' + str(mr['row']) for mr in multi_rows)} "
                f"conf={row_match.confidence:.2f} method={row_match.method}")
        self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                path, sheet, loc_match.index)
        res.add("read_rows", True, f"读取 {len(multi_rows)} 行整行数据")
        res.final = CLICallResult(
            ok=True, data={"rows": multi_rows, "count": len(multi_rows)})
        return res

    @staticmethod
    def _pick_key_columns(header: list[str], stem: str = "", sheet: str = "") -> list[str]:
        """从表头中选出关键展示列（前5个 + 名称类列优先）。"""
        key = []
        seen = set()
        for h in header:
            if not h:
                continue
            name = h.split(":")[0]
            if name in seen:
                continue
            seen.add(name)
            # 名称/id/类型/品质/等级 类列优先
            if any(kw in name for kw in ["名称", "id", "ID", "类型", "品质", "等级", "描述"]):
                key.append(name)
                if len(key) >= 8:
                    break
        # 不够8个则用前几个补
        for h in header:
            if not h:
                continue
            name = h.split(":")[0]
            if name in seen:
                continue
            seen.add(name)
            key.append(name)
            if len(key) >= 8:
                break
        return key[:8]

    def _run_get(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """执行"查询/读取"操作：定位行 + 目标列 → 读取单元格值。"""
        # 1. 解析定位列
        loc_match, match_mode, matcher, stem, loc_col_name = self._resolve_locator_and_mode(path, sheet, intent)
        if loc_match is None:
            sug = self._fuzzy_suggest(
                loc_col_name, [h.split(":")[0] for h in matcher.headers])
            hint = self._fmt_simple(sug)
            res.add("match_locator", False, f"未找到定位列[{loc_col_name}]{hint}")
            res.message = f"无法匹配定位列「{loc_col_name}」{hint}"
            return res
        res.add("match_locator", True,
                f"列{loc_match.column}({loc_match.index},mode={match_mode}) 值={intent.locator_value!r}")
        self._fill_col_evidence(res, loc_match, loc_col_name)

        # T8: 定位前列查 L3 反模式 — 歧义列强制 exact，绕过 contains 层级
        loc_col_plain = (loc_match.column or "").split(":")[0]
        ap = self._check_anti_pattern(stem, sheet, column=loc_col_plain,
                                      input_text=intent.raw if intent else "")
        if ap and ap.get("action") == "force_exact":
            match_mode = "exact"

        # 1.5 整行查询：如"所有属性/所有信息/全部字段" → 读取整行全部列，跳过单列匹配
        if self._is_whole_row_get(intent):
            return self._read_whole_row(intent, path, sheet, loc_match, match_mode, res)

        # 1.6 目标列 == 定位列自身：用户查询的就是定位列（如"查询建筑帮派基地的建筑名称"，
        # 建筑名称即定位列）。此时 _match_target_column 会因排除定位列而误匹配相邻列，
        # 故直接返回定位值。
        # 条件收紧：raw 必须精确以"的+定位列名"结尾，避免"查询X的交互效果编号"因
        # endswith("编号")误触发（"交互效果编号"是另一列，非定位列自身）。
        loc_col_name = (loc_match.column or "").split(":")[0]
        raw = intent.raw or ""
        if (loc_col_name and len(loc_col_name) >= 2
                and raw.rstrip().endswith(f"的{loc_col_name}")):
            # 复合主键优先
            if intent.locator_fields and len(intent.locator_fields) >= 2 \
                    and len(intent.locator_fields) == len(intent.locator_values):
                row_match = self._locate_row_composite(
                    path, sheet, intent.locator_fields, intent.locator_values,
                    match_mode, allow_exact_fallback=True)
                if row_match is not None:
                    val = self._read_cell(path, sheet, row_match.row, loc_match.index)
                    res.add("read_cell", True,
                            f"复合主键读取定位列 [{sheet}!{row_match.row},{loc_match.index}] = {val!r}")
                    res.message = (f"查询结果：{sheet} 行{row_match.row} "
                                   f"列{loc_col_name} = {val}"
                                   + self._row_confidence_note(row_match))
                    self._add_result_row(res, loc_match.index, loc_col_name, new_value=val)
                    res.final = CLICallResult(
                        ok=True,
                        data={"value": val, "row": row_match.row,
                              "col": loc_match.index, "column": loc_col_name})
                    return res
            if not intent.locator_value:
                res.add("locate_row", False, "缺少行定位值")
                res.message = "缺少行定位值"
                return res
            row_match = self._locate_row(path, sheet, loc_match.index,
                                         intent.locator_value, match_mode,
                                         allow_exact_fallback=True)
            if row_match is None:
                sug = self._suggest_rows(path, sheet, loc_match.index, intent.locator_value)
                # §fuzzy 兜底：精确匹配失败 + top1 相近项是 locator_value 的超集 → 自动重试
                _lv = str(intent.locator_value or "")
                if sug and _lv:
                    _top_val = str(sug[0][1]) if len(sug[0]) > 1 else ""
                    if _top_val and _lv in _top_val:
                        row_match = self._locate_row(
                            path, sheet, loc_match.index, _top_val, match_mode,
                            allow_exact_fallback=True)
                        if row_match is not None:
                            res.add("locate_row", True,
                                    f"fuzzy 兜底命中: {_lv}→{_top_val}(行{row_match.row})")
                if row_match is None:
                    res.add("locate_row", False,
                            f"未找到 {intent.locator_value}(mode={match_mode})")
                    return self._trigger_cross_table_search_pause(res, path, sheet, loc_match, intent, sug)
            if row_match.ambiguous:
                # R9: 定位列自身多行命中 → 全部展示
                all_cands = [(row_match.row, row_match.value)] + list(row_match.alternatives)
                header = self.cli.read_header(path, sheet)
                lines = []
                multi_rows = []
                for r, v in all_cands:
                    lines.append(f"  行{r}: {v}")
                    # 读取整行数据供前端缓存
                    row_data = self._read_row_data(path, sheet, r)
                    cols = [{"col": loc_match.index, "col_name": loc_col_name, "value": v}]
                    for ci in range(1, len(header) + 1):
                        if ci == loc_match.index:
                            continue
                        name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
                        rv = row_data.get(ci)
                        if rv is not None and str(rv).strip() != "":
                            cols.append({"col": ci, "col_name": name, "value": rv})
                    multi_rows.append({"row": r, "value": v, "columns": cols})
                res.add("locate_row", True,
                        f"命中{len(all_cands)}行: {', '.join('r' + str(r) for r, _ in all_cands)} "
                        f"conf={row_match.confidence:.2f} method={row_match.method}")
                self._fill_row_evidence(res, row_match, intent.locator_value,
                                        path, sheet, loc_match.index)
                res.message = (f"找到 {len(all_cands)} 条匹配「{intent.locator_value}」的记录：\n"
                               + "\n".join(lines))
                self._add_result_row(res, loc_match.index, loc_col_name, new_value=all_cands[0][1])
                res.multi_rows = multi_rows
                res.final = CLICallResult(ok=True, data={"rows": res.multi_rows, "count": len(all_cands)})
                return res
            row = row_match.row
            val = row_match.value
            res.add("locate_row", True,
                    f"row={row} value={val!r} conf={row_match.confidence:.2f} "
                    f"method={row_match.method}")
            self._fill_row_evidence(res, row_match, intent.locator_value,
                                    path, sheet, loc_match.index)
            res.add("read_cell", True,
                    f"读取定位列自身 [{sheet}!{row},{loc_match.index}] = {val!r}")
            res.message = (f"查询结果：{sheet} 行{row} [{intent.locator_value}] "
                           f"列{loc_col_name} = {val}"
                           + self._row_confidence_note(row_match))
            self._add_result_row(res, loc_match.index, loc_col_name, new_value=val)
            res.final = CLICallResult(
                ok=True,
                data={"value": val, "row": row,
                      "col": loc_match.index, "column": loc_col_name})
            return res

        # 2. 匹配目标列
        tgt_match, tgt_value = self._match_target_column(
            intent, matcher, stem, sheet, loc_match.index)
        if tgt_match is None:
            # 回退:目标列匹配不到且文本含泛指词(属性/信息/数据等)→ 按整行读取
            if self._FUZZY_WHOLE_RE.search(intent.raw or ""):
                res.add("match_target", True, "目标列为泛指词,回退整行读取")
                return self._read_whole_row(intent, path, sheet, loc_match, match_mode, res)
            res.add("match_target", False, "目标列匹配失败")
            res.message = "无法匹配目标列"
            return res
        res.add("match_target", True, f"列{tgt_match.column}({tgt_match.index},score={tgt_match.score:.2f})")

        # 3. 定位行（无定位值时用空字符串尝试匹配）
        # T2: 用户显式指定行号（用行6/第6行）→ 跳过行定位直接读该行
        row_match = self._apply_row_override(intent, path, sheet, loc_match)
        if row_match is None:
            # 复合主键优先（case5 ResidenceEntry 双键）
            if intent.locator_fields and len(intent.locator_fields) >= 2 \
                    and len(intent.locator_fields) == len(intent.locator_values):
                row_match = self._locate_row_composite(
                    path, sheet, intent.locator_fields, intent.locator_values,
                    match_mode, allow_exact_fallback=True)
            if row_match is None:
                if not intent.locator_value:
                    intent.locator_value = ""  # 无定位值时读第一行或整列
                row_match = self._locate_row(path, sheet, loc_match.index, intent.locator_value, match_mode,
                                             allow_exact_fallback=True)
            if row_match is None:
                sug = self._suggest_rows(path, sheet, loc_match.index, intent.locator_value)
                # §fuzzy 兜底：精确匹配失败 + top1 相近项是 locator_value 的超集
                # （如"饕餮"→"饕餮一阶"行16，"饕餮一阶"包含"饕餮"）→ 自动用 top1 重试
                # 行定位，避免直接 needs_confirm 暂停要求用户介入。
                _lv = str(intent.locator_value or "")
                if sug and _lv:
                    _top_val = str(sug[0][1]) if len(sug[0]) > 1 else ""
                    if _top_val and _lv in _top_val:
                        row_match = self._locate_row(
                            path, sheet, loc_match.index, _top_val, match_mode,
                            allow_exact_fallback=True)
                        if row_match is not None:
                            res.add("locate_row", True,
                                    f"fuzzy 兜底命中: {_lv}→{_top_val}(行{row_match.row})")
                if row_match is None:
                    res.add("locate_row", False,
                            f"未找到 {intent.locator_value}(mode={match_mode})")
                    return self._trigger_cross_table_search_pause(res, path, sheet, loc_match, intent, sug)
                all_cands = [(row_match.row, row_match.value)] + list(row_match.alternatives)
                header = self.cli.read_header(path, sheet)
                lines = []
                cell_data = []
                multi_rows = []
                for r, v in all_cands:
                    cr = self.cli.read_cell(path, sheet, r, tgt_match.index)
                    cv = cr.data if cr.ok else "?"
                    lines.append(f"  行{r} | {tgt_match.column} = {cv}")
                    cell_data.append(cv)
                    # 同时读取整行数据供前端缓存切换
                    row_data = self._read_row_data(path, sheet, r)
                    cols = [{"col": tgt_match.index, "col_name": tgt_match.column, "value": cv}]
                    for ci in range(1, len(header) + 1):
                        if ci == tgt_match.index:
                            continue
                        name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
                        rv = row_data.get(ci)
                        if rv is not None and str(rv).strip() != "":
                            cols.append({"col": ci, "col_name": name, "value": rv})
                    multi_rows.append({"row": r, "value": v, "columns": cols})
                res.add("locate_row", True,
                        f"命中{len(all_cands)}行: {', '.join('r' + str(r) for r, _ in all_cands)} "
                        f"conf={row_match.confidence:.2f} method={row_match.method}")
                self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                        path, sheet, loc_match.index)
                res.message = (f"找到 {len(all_cands)} 条匹配「{intent.locator_value}」的记录：\n"
                               + "\n".join(lines)
                               + self._column_confidence_note(tgt_match))
                for cv in cell_data:
                    self._add_result_row(res, tgt_match.index, tgt_match.column, new_value=cv)
                res.multi_rows = multi_rows
                res.final = CLICallResult(ok=True, data={"rows": res.multi_rows, "count": len(all_cands)})
                return res
        row = row_match.row
        res.add("locate_row", True,
                f"row={row} value={row_match.value!r} conf={row_match.confidence:.2f} "
                f"method={row_match.method}")
        self._fill_row_evidence(res, row_match, intent.locator_value or "",
                                path, sheet, loc_match.index)

        # 4. 读取单元格
        r = self.cli.read_cell(path, sheet, row, tgt_match.index)
        if r.ok:
            val = r.data
            res.add("read_cell", True, f"读取 [{sheet}!{row},{tgt_match.index}] = {val!r}")
            res.message = (f"查询结果：{sheet} 行{row} [{intent.locator_value}] "
                           f"列{tgt_match.column} = {val}"
                            + self._row_confidence_note(row_match)
                            + self._column_confidence_note(tgt_match))
            self._add_result_row(res, tgt_match.index, tgt_match.column, new_value=val)
            # 回填 dict 形态 data，使响应 data.value 可被程序化读取
            # （chat/dry_run 仅当 final.data 为 dict 时才填充响应 data 字段）
            res.final = CLICallResult(
                ok=True,
                data={"value": val, "row": row,
                      "col": tgt_match.index, "column": tgt_match.column})
        else:
            res.final = r
            res.add("read_cell", False, r.error or "")
            res.message = "读取失败"
        return res

    @staticmethod
    def _unresolved_failed_fields(failed_by_index: dict[int, tuple[str, str]],
                                  values: dict[int, any]) -> list[tuple[int, str, str]]:
        return [
            (idx, col, err)
            for idx, (col, err) in (failed_by_index or {}).items()
            if idx not in (values or {})
        ]

    @staticmethod
    def _norm_field_key(k: str) -> str:
        """字段名规范化：去类型后缀/空格/下划线/连字符 + 小写（供同列判等）。"""
        return re.sub(r"[\s_\-]", "", str(k).split(":")[0]).strip().lower()

    def _drop_resolved_stale_fields(self, intent, fields: dict, res) -> dict:
        """剔除 Step2 修正后仍残留在 fields 里的旧值（修复「已修正仍报失败」）。

        Step2 ask / 自动推断把 fields[col] 改写为新值后，原解析产出的同列字段可能
        以另一形态键（中文表头 / 英文规范名）残留旧值，或修正键与残留键并存。
        Step3 再次 coerce 该旧值必然硬失败（如 int 列的「节日」）→ 行已按新值写
        成功，steps 却留下 coerce_value 失败 + coerce_failed，Step3 据此标 partial，
        Step4 汇总报「1 项失败未解决」（实际已解决）。命中台账 (列名, 旧值) 即剔除，
        并以成功态记录「原值已在 Step2 改为 X」，不再污染失败态。
        """
        resolved = (getattr(intent, "extras", None) or {}).get("user_resolved_fields")
        if not isinstance(fields, dict) or not isinstance(resolved, dict) or not resolved:
            return fields
        by_key = {}
        for _c, _r in resolved.items():
            if isinstance(_r, dict):
                by_key[self._norm_field_key(_c)] = _r
        if not by_key:
            return fields
        vals_now: dict[str, int] = {}
        for _v in fields.values():
            vals_now[str(_v).strip()] = vals_now.get(str(_v).strip(), 0) + 1
        kept: dict = {}
        for _k, _v in fields.items():
            _vs = str(_v).strip()
            _r = by_key.get(self._norm_field_key(_k))
            _stale = False
            if _r is not None and _vs and _vs == str(_r.get("old", "")).strip():
                # 同键（或规范化同列）上的旧值残留
                _stale = True
            else:
                # 异键残留：fields 里同时存在"被改掉的旧值"与"修正后的新值"
                for _r2 in by_key.values():
                    _old = str(_r2.get("old", "")).strip()
                    _new = str(_r2.get("new", "")).strip()
                    if _old and _new and _old != _new and _vs == _old \
                            and vals_now.get(_new, 0) > 0:
                        _stale = True
                        _r = _r2
                        break
            if not _stale:
                kept[_k] = _v
                continue
            _src = "用户修正" if (_r or {}).get("source") != "auto" else "自动推断"
            try:
                res.add("coerce_value", True,
                        f"列[{_k}]原值「{_v}」已在 Step2 经{_src}改为"
                        f"「{(_r or {}).get('new', '')}」，跳过旧值残留")
            except Exception:
                logger.debug("记录已修正旧值跳过说明失败", exc_info=True)
        return kept if kept else fields

    def _run_add(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """执行"新增"操作：从自然语言中提取列值 → 追加新行。

        流程：
            1. 若 LLM 已提供 fields 字典（intent.extras["fields"]）→ 列名转列号直接写入
               （推荐路径，绕过脆弱的别名文本扫描）
            2. 否则降级到规则扫描：扫描所有列别名在文本中的出现位置提取值
            3. 构建 {列索引: 值} 字典
            4. 调用 cli.append_row 追加行
        """
        headers = self.cli.read_header(path, sheet)
        stem = path.stem
        matcher = self._make_matcher(headers, stem, sheet, path)

        # ── 路径1：LLM 提供的 fields 字典（列名→值），列名转列号 ──
        fields = intent.extras.get("fields")
        _narr_skipped: list[str] = []  # §叙述值跳过兜底（共享，分支内外都可见）
        if isinstance(fields, dict) and fields:
            # §已修正旧值剔除：Step2 交互修正后残留的同列旧值不再参与写盘
            fields = self._drop_resolved_stale_fields(intent, fields, res)
            # D10: 写前枚举预转换（int 列+非 int 值命中枚举→转 int，减少硬错误）
            fields = self._precoerce_enum_fields(fields, stem, sheet)
            # effect.* 点分键 → 真实表头翻译（splitter 产出的 effect.key/effect.data.N.*）
            fields = self._pretranslate_effect_fields(fields, headers)
            # 通用嵌套点路径键 → 末段作列名（aptitude_base.StrPotCon → StrPotCon）
            # G11: 别名优先，原 key 在 column_aliases 有配置则保留原 key（精确命中）
            # 原则9（R8）：type_aliases（row2 规范名）补全 splitter 点分规范键
            fields = self._translate_dotted_keys(
                fields, headers, set(matcher.yaml_aliases.keys()),
                self._type_aliases(path, sheet, headers))
            values: dict[int, any] = {}
            failed: list[str] = []
            failed_by_index: dict[int, tuple[str, str]] = {}
            _auto_cols: list[str] = []  # 快赢3:<auto> 批量收敛,循环后一次性 add
            for col_name, val in fields.items():
                # §P0 数字索引键兜底：col_name 为纯数字 = LLM 退化把列序号当键
                # （fields 键约定为列名，纯数字绝非合法列名）。无法映射真实列，
                # matcher.match 必失败 → 原逻辑 res.add("match_field",False) 翻转
                # 整条 intent 为失败（即使其余列合法可写）。跳过该退化键（不写不
                # 判失败），让行其余列 + 自增主键正常落库。通用判据（键形式），
                # 不绑列名/表/测例。
                if str(col_name).strip().isdigit():
                    _narr_skipped.append(f"{col_name}（纯数字=列序号索引键,退化,跳过）")
                    continue
                # §叙述值跳过兜底（泛化）：仅对 int/float/bool 数字列跳过「含中文标点
                # 的整段叙述」（如「30010，叫焚天赤龙…」灌 int 列）。str/string 列
                # （名称/描述）中文值正常保留；int 枚举列的单个中文标签（如「节日」，
                # 无标点）也保留，交 Step2 报 TYPE_MISMATCH + ask 用户填数字码。
                _col_type_hint = self._get_col_type(stem, sheet, str(col_name)) or ""
                _ct_low = str(_col_type_hint).lower()
                _is_num_col = bool(_ct_low) and ("int" in _ct_low or "float" in _ct_low
                                                  or "bool" in _ct_low
                                                  or "number" in _ct_low
                                                  or "long" in _ct_low
                                                  or "double" in _ct_low)
                _vs = str(val).strip() if val is not None else ""
                if _vs and not (_vs.startswith("<") and _vs.endswith(">")):
                    _stripped = _vs.replace(",", "").replace("，", "")
                    _is_pure_num = _stripped.lstrip("-").isdigit()
                    if not _is_pure_num:
                        try:
                            float(_stripped)
                            _is_pure_num = True
                        except ValueError:
                            pass
                    if (not _is_pure_num
                            and not _vs.startswith("[")
                            and not _vs.startswith("{")):
                        import re as _re_ch
                        _has_cn = bool(_re_ch.search(r"[\u4e00-\u9fff]", _vs))
                        _has_punct = bool(_re_ch.search(
                            r"[，。；、！？（）【】…：]", _vs))
                        if _has_cn and _is_num_col and _has_punct:
                            _narr_skipped.append(
                                f"{col_name}（数字列值含中文标点似叙述，跳过）")
                            continue
                # 多 id 消歧：泛「id/编号」且 sheet 有多个 id 列 → 中止提示重试
                ambig, cands = self._check_id_ambiguity(headers, str(col_name))
                if ambig:
                    names = "、".join(c for _, c in cands)
                    res.add("id_ambiguous", False, f"列[{col_name}]命中多个id列: {names}")
                    res.message = (f"「{col_name}」可匹配多个 id 列：{names}。"
                                   f"请用具体列名（如「{cands[0][1]}」）重试。")
                    return res
                # 精确 match 优先（LLM 给的多为精确列名），避免 match_best 分词出短 id 子候选
                # 与精确列名同分时误取（如「神通id」被分出「id」→误命中「技能id」）
                m = matcher.match(col_name) or matcher.match_best(col_name)
                if m is None:
                    res.add("match_field", False, f"未找到列[{col_name}]")
                    continue
                col_type = self._get_col_type(stem, sheet, m.column)
                coerced, warn, error = self._coerce_value(col_type, val, stem, sheet, m.column)
                if warn:
                    # 快赢3:<auto> 列收集,循环后批量 add(避免逐列刷屏)
                    if str(val).strip() == "<auto>":
                        _auto_cols.append(m.column)
                        res.needs_user_fill.append({
                            "col": m.column, "table": stem, "sheet": sheet,
                            "reason": f"列[{m.column}]输入未提及，标 <auto> 留空待补",
                        })
                        res.partial = True
                        continue
                    res.add("coerce_value", True, warn)
                if error:
                    # Step5 AI 增强：类型硬失败时让 LLM 判断"列映射错误"还是"值需转换"
                    # LLM 可建议把值移到正确的列（如"要不要"是选项文本，不该塞进 int 列）
                    # §P0-4 零LLM gate：Step3 (execute_no_llm=True) 禁止发 LLM。
                    # 类型硬失败交回 Step2（应已拦 TYPE_MISMATCH + ask），Step3 不偷偷发 LLM。
                    if (self._ai_enhancer is not None
                            and not getattr(self, "_ai_coerce_retried", False)
                            and not getattr(self, "execute_no_llm", False)):
                        try:
                            headers = self.cli.read_header(path, sheet)
                            headers_clean = [(h or "").split(":")[0] for h in headers if h]
                            # §Step1 列名信号：从 intent.extras 取 ColumnExtractor 信号，注入诊断 prompt
                            col_sig = intent.extras.get("extracted_columns_signal") if intent and intent.extras else None
                            # §枚举提示：若该列类型是 int 且值是中文，提示 LLM 走枚举解析而非改列
                            col_type = self._get_col_type(stem, sheet, m.column)
                            enum_hint = ""
                            if col_type and "int" in str(col_type).lower() and isinstance(val, str) and val:
                                # 粗判中文标签：含非 ASCII 字符
                                if any(ord(c) > 127 for c in val):
                                    enum_hint = (f"列[{m.column}]类型为{col_type}，当前值「{val}」是中文标签。"
                                                 "该列很可能是枚举码列（存数字，显示中文），"
                                                 "中文标签应保留原列名，由下游枚举解析器转数字码，不要改列。")
                            ai_fix = self._ai_enhancer.ai_fix_field_mapping(
                                error_msg=error, table_stem=stem, sheet=sheet,
                                columns=headers_clean, wrong_col=m.column,
                                wrong_value=val, all_fields=dict(intent.extras.get("fields", {})),
                                column_signal=col_sig, enum_hint=enum_hint)
                            if ai_fix and ai_fix.get("correct_col"):
                                correct_col = ai_fix["correct_col"]
                                # 校验 AI 建议的列确实存在
                                if correct_col in headers_clean:
                                    new_m = matcher.match(correct_col) or matcher.match_best(correct_col)
                                    if new_m is not None:
                                        new_type = self._get_col_type(stem, sheet, new_m.column)
                                        new_val = ai_fix.get("correct_value", val)
                                        new_coerced, _, new_error = self._coerce_value(
                                            new_type, new_val, stem, sheet, new_m.column)
                                        if new_error is None:
                                            res.add_thinking("校验",
                                                             f"AI 修正字段映射：[{m.column}]='{val}' → [{new_m.column}]='{new_val}'")
                                            values[new_m.index] = new_coerced
                                            # 更新 intent.fields 供重试复用
                                            intent.extras["fields"][correct_col] = new_val
                                            if m.column in intent.extras.get("fields", {}):
                                                del intent.extras["fields"][m.column]
                                            continue
                        except Exception:
                            logger.warning("Step5 AI 字段映射修正失败，降级走原失败路径", exc_info=True)
                    failed_by_index[m.index] = (str(col_name), str(error))
                    continue
                # ID 列段校验（越界 → 跳过该字段）
                if self._is_id_column(m.column) and coerced is not None:
                    ok, reason = self._validate_id_scope(stem, sheet, m.column, coerced)
                    if not ok:
                        failed_by_index[m.index] = (str(col_name), str(reason))
                        continue
                values[m.index] = coerced
            # 快赢3:<auto> 列批量收敛为一次 add(避免逐列刷屏)
            if _auto_cols:
                res.add("coerce_value", True,
                        f"以下列标 <auto>（用户未提及，留空）：{_auto_cols}")
            if values:
                unresolved_failed = self._unresolved_failed_fields(failed_by_index, values)
                for _idx, _col, _err in unresolved_failed:
                    res.add("coerce_value", False, _err)
                failed = [col for _idx, col, _err in unresolved_failed]
                res.add("add_values", True, f"提取到 {len(values)} 个列值: {values}")
                if failed:
                    res.add("coerce_failed", True,
                            f"{len(failed)}个字段类型转换失败被跳过: {failed}")
                return self._do_append(path, sheet, values, res)
            for _col, _err in failed_by_index.values():
                res.add("coerce_value", False, _err)
            res.add("add_values", False, "fields 中所有列名均无法匹配表头或类型转换失败")
            res.message = "无法匹配任何目标列或类型转换全部失败"
            return res

        # ── 路径2（降级）：从自然语言中扫描所有"列别名 + 值"对 ──
        aliases = self.column_cfg.all_aliases(stem, sheet)
        text = intent.raw
        values = {}

        alias_positions: list[tuple[int, str, str]] = []  # (文本位置, 别名, 列名)
        for alias, col_name in aliases.items():
            pos = text.find(alias)
            if pos < 0:
                continue
            alias_positions.append((pos, alias, col_name))

        alias_positions.sort(key=lambda x: (x[0], -len(x[1])))  # pos升序，同位置长别名优先

        # 区间去重叠：移除与已保留别名区间重叠的短别名匹配
        # （避免 building 表单字别名"名"与"建筑名称"重叠导致脏值）
        filtered: list[tuple[int, str, str]] = []
        last_end = -1
        for pos, alias, col_name in alias_positions:
            if pos < last_end:
                continue  # 与前一个保留区间重叠，跳过
            filtered.append((pos, alias, col_name))
            last_end = pos + len(alias)
        alias_positions = filtered

        for i, (pos, alias, col_name) in enumerate(alias_positions):
            # 当前别名到下一个别名之间的文本即为对应的值
            next_pos = alias_positions[i + 1][0] if i + 1 < len(alias_positions) else len(text)
            tail = text[pos + len(alias):next_pos].strip()
            tail = _strip_separators(tail)
            # §P0 叙述碎片硬拦（path2 碎片根治）：原 >50 字阈放过短碎片（如
            # 「包也建一下，」14 字灌 reward 名称 str 列 → 写盘垃圾行 {42:'包也建一下'}）。
            # 兜底产 fields 空时 path2 在整段 raw 上裸扫，别名间 tail 常是跨子任务叙述。
            # 判据：tail 含中文句读（，。；：、）且非纯数字/括号列表 → 叙述碎片，跳过。
            # 合法标量值（数字/日期/坐标/列表）不含中文句读，不会被误拦。
            if any(_p in tail for _p in "，。；：、！？"):
                _tail_s = tail.replace(",", "").replace("，", "")
                if (not _tail_s.lstrip("-").isdigit()
                        and not tail.startswith("[") and not tail.startswith("{")):
                    if _narr_skipped is not None:
                        _narr_skipped.append(f"{col_name}（值含中文句读似叙述碎片，跳过）")
                    continue
            # §P1 长叙述值跳过（原 >50 字守卫，保留作双保险）
            if len(tail) > 50 and any(_p in tail for _p in "，。；：、！？"):
                _tail_s = tail.replace(",", "").replace("，", "")
                if (not _tail_s.lstrip("-").isdigit()
                        and not tail.startswith("[") and not tail.startswith("{")):
                    if _narr_skipped is not None:
                        _narr_skipped.append(f"{col_name}（规则扫描值过长似叙述，跳过）")
                    continue
            if tail:
                m = matcher.match(col_name)
                if m:
                    # D10: 写前枚举预转换（int 列+非 int 值命中枚举→转 int）
                    tail = self._precoerce_enum_value(col_name, tail, stem, sheet)
                    col_type = self._get_col_type(stem, sheet, m.column)
                    coerced, warn, error = self._coerce_value(col_type, tail, stem, sheet, m.column)
                    if warn:
                        res.add("coerce_value", True, warn)
                    if error:
                        # §P0 叙述碎片跳过（path2 coerce 报错点，补 path1 已有守卫的盲区）：
                        # path2 的前置 >50 字阈放过短碎片（如 40 字「1099，技能列表 9001，
                        # AI 用 aggressive_ai，」灌 reward_id int 列）→ coerce 硬错 →
                        # res.add(False) → res.ok=False → execute_failed_no_llm 假失败。
                        # 值含中文叙述特征 + 非纯数字/列表 + coerce 失败 = 叙述碎片灌错类型列，
                        # 跳过该字段不写（不判失败）。合法 str 列中文不会 coerce 报错，故只拦
                        # 真正类型不符的灌值。通用判据（值特征+类型转换结果），不绑业务词/表/测例。
                        _ev = str(tail)
                        _ev_s = _ev.replace(",", "").replace("，", "")
                        _ev_num = _ev_s.lstrip("-").isdigit()
                        if not _ev_num:
                            try:
                                float(_ev_s)
                                _ev_num = True
                            except ValueError:
                                pass
                        import re as _re_ch2
                        if (not _ev_num and not _ev.startswith("[")
                                and not _ev.startswith("{")
                                and _re_ch2.search(
                                    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", _ev)):
                            if _narr_skipped is not None:
                                _narr_skipped.append(
                                    f"{col_name}（含中文致类型转换失败,叙述碎片,跳过）")
                            continue
                        res.add("coerce_value", False, error)
                        continue
                    # ID 列段校验（越界 → 跳过该字段，不写入）
                    if self._is_id_column(m.column) and coerced is not None:
                        ok, reason = self._validate_id_scope(stem, sheet, m.column, coerced)
                        if not ok:
                            res.add("id_scope", False, reason)
                            continue
                    values[m.index] = coerced

        if not values:
            # ── 路径3（兜底）：仅对象名新增 ──
            # 文本里没有任何列别名+值对（如"增加灵兽子鼠"）→ 剥掉类别前缀
            # ("灵兽"/"宠物"...)，把剩余对象名写入该表的主定位列
            # （row_aliases 的 locator_column，如 灵兽名称/建筑名称）。
            obj = _strip_lead_verbs(text).strip()
            obj = _strip_separators(obj)  # 去掉开头的"增加"/"新增"等动词
            for pfx in self._ENTITY_PREFIXES:
                if obj.startswith(pfx) and len(obj) > len(pfx):
                    cand = obj[len(pfx):].strip()
                    if cand:
                        obj = cand
                        break
            if obj:
                # §P1 叙述值跳过（路径3 兜底）：obj 是整段文本（>80 字符 + 含中文标点 +
                # 非纯数字/列表）时，是别名稀疏的复杂跨表段被当对象名，写进 locator 列必
                # 失败/污染。跳过不写，让该 intent 走"无法解析"软失败而非污染行。
                _obj_s = str(obj)
                if len(_obj_s) > 80 and any(_p in _obj_s for _p in "，。；：、！？"):
                    _obj_stripped = _obj_s.replace(",", "").replace("，", "")
                    if (not _obj_stripped.lstrip("-").isdigit()
                            and not _obj_s.startswith("[") and not _obj_s.startswith("{")):
                        if _narr_skipped is not None:
                            _narr_skipped.append(f"对象名兜底（整段文本过长似叙述，跳过）")
                        obj = ""
            if obj:
                loc_match, _mode, _matcher, _stem, loc_col_name = self._resolve_locator_and_mode(path, sheet, intent)
                if loc_match is not None:
                    values[loc_match.index] = obj
                    res.add("add_values", True,
                            f"仅对象名新增：[{loc_match.column}] = {obj}")
                    return self._do_append(path, sheet, values, res)

            res.add("add_values", False, "未能从语句中提取到列值")
            res.message = "无法解析新增内容"
            return res

        if _narr_skipped:
            res.add_thinking("执行",
                f"跳过 {len(_narr_skipped)} 个叙述值列：{', '.join(_narr_skipped[:4])}"
                f"（值过长似整段叙述，写进 type 列必失败，已跳过避免污染行）")
        res.add("add_values", True, f"提取到 {len(values)} 个列值: {values}")
        return self._do_append(path, sheet, values, res)

    def _verify_write_back(self, path: Path, sheet: str, row_no: int,
                           expected_fields: dict[int, Any]) -> dict:
        """D1 写后读回验证：读目标行，比对落盘值与期望值。

        复用已加载 cli.read_cell（不重新 open xlsx）。比对用 _values_equal 容差。
        返回:
          - 成功: {"ok": True}
          - 值不符: {"ok": False, "mismatched_fields": {col: {expected, actual}}, "actual_values": {...}}
          - 读回失败: {"ok": False, "error": "read_back_failed"}
        首列主键为空时，expected_fields 不含首列即可（按非首列字段比对，不依赖 pk 定位）。
        """
        mismatched = {}
        actual = {}
        for col_idx, expected in expected_fields.items():
            try:
                cr = self.cli.read_cell(path, sheet, row_no, col_idx)
                actual_val = cr.data if cr.ok else None
            except Exception:
                logger.warning("写后读回失败（row=%s col=%s IO 错误）", row_no, col_idx, exc_info=True)
                return {"ok": False, "error": "read_back_failed"}
            actual[col_idx] = actual_val
            if not _values_equal(expected, actual_val):
                mismatched[col_idx] = {"expected": expected, "actual": actual_val}
        if mismatched:
            return {"ok": False, "mismatched_fields": mismatched, "actual_values": actual}
        return {"ok": True}

    def _handle_cli_hold_events(self, res, cli_result, sheet: str = "") -> None:
        """A2/AD1/AD2：消费 CLICallResult.hold_events → 转 #40 软失败追加 res.failures
        （保 D6 上报不静默吞）+ 经 _agent_subtask_sink 推 pre_commit_hold SSE 事件（前端接）。

        CLI 层（_save_with_cache_check）已构造 PreCommitHoldEvent + record_hold_audit 留痕；
        本方法负责 agent 层：① hold 事件不静默吞 → 软失败入 res.failures（_phase_summarize 上报）
        ② SSE 推送（若 sink 注入）。opt-in：CODEMAKER_FORMULA_GATE=hold 触发 formula_loss，
        CODEMAKER_COMMENT_GUARD=on（默认）触发 comment_loss（still_lost>0 时）。
        """
        hold_events = getattr(cli_result, "hold_events", None) or []
        if not hold_events:
            return
        _sink = getattr(self, "_agent_subtask_sink", None)
        for ev in hold_events:
            ev_dict = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
            kind = ev_dict.get("kind", "unknown")
            # #40 形状软失败 dict（P23 通道，_phase_summarize 聚合）
            res.failures.append({
                "code": 40,
                "kind": kind,
                "severity": ev_dict.get("severity", "hold"),
                "message": ev_dict.get("message", ""),
                "sheet": sheet,
                "recommendation": ev_dict.get("recommendation", ""),
            })
            if _sink is not None:
                try:
                    _sink("pre_commit_hold", ev_dict)
                except Exception:
                    logger.debug("pre_commit_hold SSE 推送失败 kind=%s", kind, exc_info=True)

    def _write_cell_and_verify(self, path: Path, sheet: str, row: int, col: int,
                               value: Any, number_format=None) -> dict:
        """D1 write_cell 写盘 + 读回验证封装。

        CLI 层 write_cell 保持"未抛异常"语义（返回 CLICallResult.ok），
        本方法在其成功后追加 _verify_write_back，返回验证结果 dict。
        失败时返回 {"ok": False, "error": ...}。
        """
        r = self.cli.write_cell(path, sheet, row, col, value, number_format=number_format)
        if not r.ok:
            return {"ok": False, "error": r.error or "write_cell 失败", "cli_result": r}
        # A2：始终透出 cli_result（含 needs_manual_fix/hold_events），供 _run_set 消费。
        verify = self._verify_write_back(path, sheet, row, {col: value})
        verify["cli_result"] = r
        return verify

    def _locate_pk_col(self, path: Path, sheet: str) -> Optional[int]:
        """定位 PK 列(1-based)。项目所有表主键在第 1 列,直接返 1。
        若首列表头含非 id 字样则扫类型行找首个 _id:int 列。
        """
        try:
            hdrs = self.cli.read_header(path, sheet)
        except Exception:
            return 1
        if not hdrs:
            return 1
        first = str(hdrs[0] or "").lower()
        if "id" in first or first == "":
            return 1
        for i, h in enumerate(hdrs, 1):
            if "id" in str(h or "").lower():
                return i
        return 1

    def _real_pk_col_name(self, path: Path, sheet: str, failed_col: str,
                          intent=None) -> str:
        """核心4辅助:读表头找真实 PK 列名(reward_id 而非泛化"ID")。

        classifier 常把 failed_col 泛化为"ID",但 intent fields 键是真实列名
        (如 reward_id)。value_coerce 需用真实列名才能匹配 intent fields。
        策略:先从 intent fields 找含 id 的键,无则读表头首列。
        """
        # 1) 从 intent fields 找含 id 的键(最准,与写入键一致)
        if intent is not None:
            _fields = (getattr(intent, "extras", None) or {}).get("fields") or {}
            for k in _fields:
                if k and "id" in str(k).lower():
                    return str(k).split(":")[0].strip()
        # 2) 读表头找 id 列
        try:
            hdrs = self.cli.read_header(path, sheet) if self.cli else []
            if hdrs:
                for h in hdrs:
                    if h and "id" in str(h).lower():
                        return str(h).split(":")[0].strip()
                return str(hdrs[0] or failed_col or "id").split(":")[0].strip()
        except Exception:
            pass
        return failed_col or "id"

    def _allocate_pk(self, path: Path, sheet: str, pk_col: int,
                     exclude: Optional[set] = None) -> Optional[int]:
        """为新增行分配主键：取该列当前最大值 +1（空表返回 1）。

        这些表（interaction/entity_prefab/spawn）主键需显式分配，非自增。
        读列所有数值取 max，+1 得新 id。非数值或空列返回 1。

        Args:
            exclude: verify_repair_loop 维护的本轮已试/已写 ID 集，
                防止 id_reallocate 多轮自撞（轮1 写 100603 未回滚 → 轮2
                读表仍见 100603 但 max+1 算出同值 → 自撞）。
        """
        try:
            rows = self.cli.read_sheet(path, sheet)
        except Exception:
            return None
        used: set = set()
        max_id = 0
        for r in rows:
            if not r or len(r) < pk_col:
                continue
            v = r[pk_col - 1]
            if v is None or v == "":
                continue
            try:
                n = int(v)
                used.add(n)
                if n > max_id:
                    max_id = n
            except (ValueError, TypeError):
                continue
        if exclude:
            used |= set(exclude)
        n = max_id + 1
        while n in used:
            n += 1
        return n

    def _is_misplaced_pk(self, pk_str: str, values: dict[int, any],
                         path: Path, sheet: str) -> bool:
        """检测主键值是否为 LLM 误塞的非主键语义值（应清空走自增）。

        判定规则（任一命中即视为误塞）：
        1. 值在 values 其他列也出现 → 说明是某列的值被复制到主键
        2. 值是字符串非纯数字 → 主键应为 int，字符串必为误塞（名字/描述等）
        3. 值是纯数字但 < 10000，且表存在"交互效果编号"列 → 疑似效果码（3001/3002/...）
           交互表主键通常是大数（>10000），小数字疑似效果码误填

        Args:
            pk_str: 主键列的值（字符串形式）
            values: 全部列值字典 {列号: 值}
            path/sheet: 目标表（用于读表头判断是否有"交互效果编号"列）
        Returns: True=误塞应自增，False=真主键冲突
        """
        # 规则1：值在其他列重复出现
        other_vals = [str(v).strip() for c, v in values.items() if c != 1 and v is not None]
        if pk_str in other_vals:
            return True
        # 规则2：非纯数字字符串 → 主键应为 int，字符串必为误塞
        if not pk_str.isdigit():
            return True
        # 规则3：纯数字但疑似效果码（小数字 + 表有交互效果编号列）
        try:
            n = int(pk_str)
            if n < 10000:
                try:
                    headers = self.cli.read_header(path, sheet)
                    if any("效果编号" in str(h or "") or "交互效果" in str(h or "")
                           for h in headers):
                        return True
                except Exception:
                    pass
        except (ValueError, TypeError):
            pass
        return False

    def _do_append(self, path: Path, sheet: str, values: dict[int, any],
                   res: AgentResult) -> AgentResult:
        """实际调用 cli.append_row 追加行并填充结果。

        写入前校验主键（第一列）唯一性：新增主键若已被占用 → 检测是否为
        LLM 误塞的非主键语义值（效果码/名字/描述等），若是则清空主键走自增；
        真主键冲突（用户显式指定且非语义误填）才阻止写入。
        values 字典 key 为 1-based 列号（与 list_columns/append_row 一致）；
        read_sheet 返回行 list 为 0-based，第一列 r[0] 即主键。
        """
        PK_COL = 1
        # §P0 虚假碎片行守卫（根治 Step1 切分碎片）：Step1 偶把他表片段（如奖励
        # 引用「奖励包给 100600」、任务组名「封魔录，主线」）误切成独立 add intent，
        # 经 path2 别名扫描抓到零散碎片 → 写出仅含碎片、无有效标识的垃圾行
        # （如 reward 行 ={2:'封魔录，主线',42:'包给 100600。'}）。
        # 通用判据：合法实体创建必有 ≥1 个"干净锚点"——数字，或不含中文句读
        # （，。；、！？（）等）的字符串（名称/枚举/英文标识）。当主键为自增
        # （未显式指定）且待写值全为"无锚点碎片"（每个字符串都含中文句读）时，
        # 判为 Step1 误切片段，跳过不写（走既有"无法解析新增内容"干净软跳过通道，
        # 不计失败）。合法行必含干净名称/编号，故不误伤；含标点的正文描述只要
        # 同行有干净锚点即保留。不绑业务词/表/测例。
        _pk_in = values.get(PK_COL)
        if _pk_in is None or not str(_pk_in).strip():
            _sent_punct = "，。；、！？（）【】…‘’"""
            # 区分三类值：干净名称锚点（非数字、无中文句读 = 名称/枚举/英文标识）、
            # 数字锚点、中文碎片（含句读的残段）。合法实体创建须有 PK 或"干净名称锚点"；
            # 仅有数字锚点又混着中文碎片（如 Step1 误切 + path2 尾巴清洗后 {42:'包也建一下',
            # 9:'800'}）判为碎片行——数字孤值不足以构成实体，跳过不写。
            _has_name_anchor = False
            _has_num_anchor = False
            _has_fragment = False
            for _c, _v in values.items():
                if _c == PK_COL:
                    continue
                if isinstance(_v, (int, float)):
                    _has_num_anchor = True
                    continue
                _vs = str(_v).strip()
                if not _vs:
                    continue
                if _vs.lstrip("-").replace(".", "", 1).isdigit():
                    _has_num_anchor = True
                    continue
                if any(_p in _vs for _p in _sent_punct):
                    _has_fragment = True
                    continue
                _has_name_anchor = True
            # 跳过条件：无 PK 且无干净名称锚点，且（存在中文碎片 或 连数字锚点都没有）。
            # → 仅数字锚点且无碎片（纯数值行，如属性权重行）不误伤。
            _is_fragment_row = (values and not _has_name_anchor
                                and (_has_fragment or not _has_num_anchor))
            if _is_fragment_row:
                # 不记 res.add(False) 失败步骤（否则 eval 逐步渲染出 ❌
                # spurious_fragment_row，且残留失败步骤污染 aggregated_message）。
                # 直接置 res.ok=False + 走"无法解析新增内容"文案 → 下游
                # execute_no_llm 空内容干净软跳过通道（agent.py:6996）识别该
                # marker，翻回 res.ok=True 并标 skipped，不计失败清单。既跳过
                # 垃圾碎片行、又不产生失败标记。add_thinking 留痕供诊断。
                res.ok = False
                res.add_thinking("执行",
                    f"{sheet} 待写值均为无锚点碎片，疑 Step1 误切他表片段，"
                    f"干净跳过不写: {values}")
                res.message = (
                    f"无法解析新增内容（{sheet} 仅含无锚点碎片、无有效标识，"
                    f"疑 Step1 误切他表片段，已跳过不写）")
                return res
        # §复合主键写盘唯一性校验：读 rules primary_key 声明，组合重复才阻止写入，
        # 单列重复但组合不同（如 FabaoLevel (法宝id=5, 法宝等级=1/2/3)）作为合法不同
        # 行放行，根治单列 _do_append 误拦同一实体的多等级行。无声明时退回下方
        # 单列 PK 检查，保持旧行为。
        _comp_pk_cols: list = []
        if hasattr(self, "_load_composite_pk_for_sheet"):
            try:
                _comp_pk_cols = self._load_composite_pk_for_sheet(path, sheet)
            except Exception:
                _comp_pk_cols = []
        if len(_comp_pk_cols) >= 2:
            _check_fn = getattr(self, "_check_composite_pk_conflict", None)
            _comp_conflict = _check_fn(
                path, sheet, values, _comp_pk_cols) if callable(_check_fn) else None
            if _comp_conflict is not None:
                _combo_desc = _comp_conflict
                res.add("pk_conflict", False,
                        f"复合主键组合已存在：{_combo_desc}")
                res.message = (f"新增失败：复合主键组合「{_combo_desc}」已存在，"
                               f"请改用其他组合或修改已有行")
                return res
            # 复合键判定通过（组合未占用）→ 跳过单列 pk 检查，直接写盘
            # （单列重复在复合键里合法，不应拦）
            _dw = getattr(self, "_do_append_write", None)
            if callable(_dw):
                return _dw(path, sheet, values, res)
            return TableAgent._do_append_write.__get__(self)(path, sheet, values, res)
        pk_val = values.get(PK_COL)
        if pk_val is not None and str(pk_val).strip():
            pk_str = str(pk_val).strip()
            try:
                existing_rows = self.cli.read_sheet(path, sheet)
            except Exception:
                existing_rows = []
            existing_pks = {
                str(r[0]).strip() for r in existing_rows
                if r and len(r) > 0 and r[0] is not None
                and str(r[0]).strip()
            }
            if pk_str in existing_pks:
                # 智能回退：检测 pk_val 是否为 LLM 误塞的非主键语义值
                # 场景：LLM 把效果码(3002)/名字/描述塞进主键列 → 冲突
                # 判定：值在 values 其他列也出现（说明是某列的值被误复制到主键），
                #       或值是字符串非纯数字（主键应为 int），或值疑似效果码（<10000 且
                #       同时存在"交互效果编号"列）→ 清空主键走自增，不报错
                should_auto = self._is_misplaced_pk(pk_str, values, path, sheet)
                if should_auto:
                    res.add_thinking("执行",
                        f"主键值[{pk_str}]疑似非主键语义值被误塞入第1列，清空走自增")
                    values.pop(PK_COL, None)
                    pk_val = None
                else:
                    res.add("pk_conflict", False, f"ID [{pk_str}] 已被占用")
                    res.message = f"新增失败：ID [{pk_str}] 已存在，请更换 ID 后重试"
                    return res
        # 写盘 + 写后验证（单列/复合主键共用）。getattr 兜底：self 可能是
        # SimpleNamespace mock（未绑定 _do_append_write），此时直接用类方法 __get__ 调
        _dw = getattr(self, "_do_append_write", None)
        if callable(_dw):
            return _dw(path, sheet, values, res)
        return TableAgent._do_append_write.__get__(self)(path, sheet, values, res)

    def _do_append_write(self, path: Path, sheet: str, values: dict[int, any],
                        res: AgentResult) -> AgentResult:
        """实际调 cli.append_row + 写后验证（单列/复合主键共用）。从 _do_append 拆出。"""
        PK_COL = 1
        if values:
            values = {ci: _serialize_complex_cell_value(v) for ci, v in values.items()}
        r = self.cli.append_row(path, sheet, values)
        res.final = r
        # A2/AD1/AD2：消费 cli_result.hold_events → 软失败 + SSE（CLI 构造事件，agent 上报）。
        # 兼容轻量 mock（SimpleNamespace 等）：无 _handle_cli_hold_events 时跳过。
        _hold_handler = getattr(self, "_handle_cli_hold_events", None)
        if _hold_handler is not None:
            _hold_handler(res, r, sheet)
        if r.ok:
            new_row = r.data.get('row')
            # 记录写入行号供 Step5 前向引用 backfill 复用（conv→option 等循环依赖链）
            res._written_row = new_row
            # D1 写后读回验证：比对落盘值与期望值
            verify = self._verify_write_back(path, sheet, new_row, values)
            if verify.get("ok"):
                # §P0 状态一致性：coerce 失败字段被跳过但行写入成功时，整体应标成功
                # （partial）。原 res.add("coerce_value",False) 已置 res.ok=False 不可逆，
                # 导致 Step6 汇总判失败但行实际已落库（状态不一致：写成功报失败）。
                # 写后验证通过 = 行已正确落库 → 恢复 res.ok=True 并标 partial 让前端知
                # 有字段被跳过。failed 列已在 steps 的 coerce_failed 显示。
                _had_coerce_fail = any(
                    getattr(s, "name", "") == "coerce_value" and not getattr(s, "ok", True)
                    for s in res.steps)
                if _had_coerce_fail:
                    res.ok = True
                    res.partial = True
                res.add("append_row", True, f"新增行 row={new_row} 值={values}（写后验证通过）")
                res.message = f"已新增：{sheet} 行{new_row} 内容={values}"
            else:
                mismatch_desc = verify.get("mismatched_fields") or verify.get("error", "未知")
                res.add("append_row", False, f"新增行 row={new_row} 写后验证失败：{mismatch_desc}")
                res.message = f"新增失败：写后验证不符 {mismatch_desc}"
            # 表体：生成行的列名+值（按列号升序，便于直观阅读）
            try:
                headers = self.cli.read_header(path, sheet)
            except Exception:
                headers = []
            for ci in sorted(values.keys()):
                col_name = ""
                if headers and 1 <= ci <= len(headers):
                    h = headers[ci - 1]
                    col_name = str(h).split(":")[0].strip() if h else ""
                res.result_rows.append({
                    "col": ci,
                    "col_name": col_name,
                    "old_value": None,
                    "new_value": _serialize_complex_cell_value(values[ci]),
                })
            # P2 编排器依赖：若主键（第一列）未被显式写入，自动分配 max+1 并回读，
            # 供 OperationOrchestrator._capture_produced 提取新 ID 传递给后续意图。
            # 这些表（interaction/entity_prefab/spawn）主键需显式分配，非自增。
            pk_name = ""
            if headers and 1 <= PK_COL <= len(headers):
                h = headers[PK_COL - 1]
                pk_name = str(h).split(":")[0].strip() if h else ""
            if PK_COL not in values and new_row is not None:
                try:
                    pk_cr = self.cli.read_cell(path, sheet, new_row, PK_COL)
                    pk_val = pk_cr.data if pk_cr.ok else None
                except Exception:
                    pk_val = None
                # 主键为空 → 自动分配当前列 max+1，回写单元格
                if pk_val in (None, ""):
                    pk_val = self._allocate_pk(path, sheet, PK_COL)
                    if pk_val is not None:
                        try:
                            self.cli.write_cell(path, sheet, new_row, PK_COL, pk_val)
                            values[PK_COL] = pk_val
                            res.add_thinking("执行", f"主键[{pk_name}]未填，自动分配 {pk_val}")
                        except Exception:
                            pass
                if pk_val not in (None, ""):
                    res.result_rows.append({
                        "col": PK_COL,
                        "col_name": pk_name,
                        "old_value": None,
                        "new_value": pk_val,
                    })
            # §P2-12 auto_sort 后行号失效修复：auto_sort 按第1列升序重排，
            # 原 _written_row 是排序前行号，后续 backfill/前向引用用它补写会错行。
            # 排序后用 PK 值重新定位真实行号，更新 _written_row。
            self._auto_sort_after_write(path, sheet, res)
            if new_row is not None and hasattr(res, "_written_row"):
                # 用 PK 值(若已分配)重定位行号
                _reloc_pk = values.get(PK_COL)
                if _reloc_pk is not None:
                    try:
                        _rows = self.cli.read_sheet(path, sheet)
                        for _ri, _r in enumerate(_rows, start=1):
                            if _r and len(_r) > 0 and str(_r[0]).strip() == str(_reloc_pk).strip():
                                res._written_row = _ri
                                break
                    except Exception:
                        pass
            if not self._refresh_index_after_write(path):
                res.index_dirty = True
            self._check_missing_required_after_add(path, sheet, headers, values, res, getattr(res, "intent", None))
        else:
            res.add("append_row", False, r.error or "")
            res.message = "新增行失败"
        return res

    def _load_composite_pk_for_sheet(self, path: Path, sheet: str) -> list:
        """读 rules primary_key overlay 取本 (stem, sheet) 的复合主键列名列表。

        _do_append 写盘时调用，无声明返回空列表（退回单列 PKCOL=1 检查）。
        缓存按 (path, sheet) 懒加载，写盘热路径不重复解析 yaml。
        """
        if not hasattr(self, "_composite_pk_cache"):
            self._composite_pk_cache = {}
        key = (str(path), sheet)
        cached = self._composite_pk_cache.get(key)
        if cached is not None:
            return cached
        cols: list = []
        try:
            from ..rules_loader import get_primary_key_overlay
            overlay = get_primary_key_overlay() or {}
            stem = path.stem.lower()
            sheets_map = overlay.get(stem)
            if isinstance(sheets_map, dict):
                cols = list(sheets_map.get(sheet) or [])
                if not cols:
                    for sn, cs in sheets_map.items():
                        if sn and sn.lower() == sheet.lower():
                            cols = list(cs or [])
                            break
        except Exception:
            logger.debug("composite pk overlay 加载失败 %s/%s", path, sheet, exc_info=True)
            cols = []
        self._composite_pk_cache[key] = cols
        return cols

    def _check_composite_pk_conflict(self, path: Path, sheet: str,
                                      values: dict[int, any],
                                      pk_cols: list) -> Optional[str]:
        """复合主键写盘冲突检测：组合值已存在返回组合描述串，否则 None。

        values: {1-based col_idx: val}（写盘格式）。
        pk_cols: 列名列表（rules primary_key 声明）。
        按 headers 把列名归一回 1-based idx，从 values 取各列值组成组合，
        与现有数据行的同列组合比对，组合重复即冲突。
        """
        if not pk_cols or len(pk_cols) < 2 or not values:
            return None
        try:
            headers = self.cli.read_header(path, sheet) if self.cli else []
        except Exception:
            headers = []
        if not headers:
            return None
        # 列名归一(小写+去后缀) -> 1-based col_idx
        def _nl(h):
            return (str(h or "").split(":")[0].strip().lower())
        idx_of = {}
        for i, h in enumerate(headers, 1):
            nl = _nl(h)
            if nl and nl not in idx_of:
                idx_of[nl] = i
        pick_idx = []
        for c in pk_cols:
            nl = _nl(c)
            if not nl or nl not in idx_of:
                return None  # 任一 PK 列不在表头 → 无法判组合，放行交单列兜底
            pick_idx.append(idx_of[nl])
        combo = []
        for ci in pick_idx:
            v = values.get(ci)
            sv = str(v).strip() if v is not None else ""
            if not sv:
                # 复合键某列缺值 → 无法定夺组合，放行（必填逻辑另拦）
                return None
            combo.append(sv)
        combo_t = tuple(combo)
        try:
            rows = self.cli.read_sheet(path, sheet)
        except Exception:
            rows = []
        for r in rows:
            if not r:
                continue
            vals = []
            ok = True
            for ci in pick_idx:
                cv = r[ci - 1] if ci - 1 < len(r) else None
                sv = str(cv).strip() if cv is not None else ""
                if not sv:
                    ok = False
                    break
                vals.append(sv)
            if ok and tuple(vals) == combo_t:
                # 组合重复 → 冲突
                names = [c.split(":")[0].strip() for c in pk_cols]
                return ",".join(f"{n}={v}" for n, v in zip(names, combo))
        return None

    def _check_missing_required_after_add(self, path, sheet, headers, values, res,
                                          intent=None) -> None:
        """写库后业务必填列 schema-grounding 检查。

        启发式：当 user_text 含引号(说明用户显式给了名字/描述值)且列名含「名称/描述/名」
        字样的 string 列未落盘 → res.missing_required + schema_grounding 失败 step +
        res.ok=False（让 Step3 failures 透传到 Step4 Conclude induce_anti_patterns）。
        对应"看似成功的失败"——项目里大量 case LLM 漏产名称/描述，写库 ok 但缺必填列，
        原 dialog_logger 按 ok=True 算 excellent 入 examples，学习链路缺种子。
        PK 自动分配列豁免。
        """
        if not headers or not values:
            return
        try:
            written_cols = {ci for ci in values.keys()
                            if isinstance(ci, int) and ci > 0}
            required_kws = ("名称", "描述", "名")
            quoted = any(q in ((intent.raw if intent else "") or "")
                         for q in ("'", '"', "「", "」")) or \
                any(kw in ((getattr(intent, "raw", "") if intent else "") or "")
                    for kw in ("活动描述", "活动名称", "描述为", "名称为"))
            if not quoted:
                return
            missing: list[dict] = []
            for idx0, h in enumerate(headers, start=1):
                if not h or idx0 in written_cols:
                    continue
                name = str(h).split(":")[0].strip()
                if not name or not any(kw in name for kw in required_kws):
                    continue
                v = values.get(idx0, "")
                if v in (None, ""):
                    missing.append({"col": idx0, "col_name": name,
                                    "col_type": "str"})
            if not missing:
                return
            res.missing_required = missing
            names = ", ".join(m["col_name"] for m in missing)
            res.add("schema_grounding", False,
                    f"业务必填列未填: {names}（LLM 漏产/字段缺失——行已落盘但缺列，"
                    "按失败记录喂 induce_anti_patterns）")
            if res.ok:
                res.ok = False
                _m = res.message or f"新增 {sheet} 行已写但缺必填列"
                res.message = (f"新增行部分成功但缺业务必填列：{names}。"
                               f"建议补列重跑或直接 ask 让用户补值。原始：{_m}")
        except Exception:
            logger.warning("schema-grounding check 失败(非致命)", exc_info=True)

    # ── 列级操作 ──
    def _clean_col_hint(self, hint: str | None) -> str:
        """清理列名提示：取最后一个"的"之后部分，去首尾空白。

        用于剥离自然语言中残留的表名/定语前缀（如"灵兽表的饱食度" → "饱食度"）。
        """
        if not hint:
            return ""
        h = hint.strip()
        if "的" in h:
            h = h.rsplit("的", 1)[-1].strip()
        return h

    def _run_col(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """列级操作分发：col_list/col_add/col_delete/col_rename。"""
        col_op = intent.extras.get("col_op", "")
        if col_op == "col_list":
            return self._run_col_list(intent, path, sheet, res)
        if col_op == "col_add":
            return self._run_col_add(intent, path, sheet, res)
        if col_op == "col_delete":
            return self._run_col_delete(intent, path, sheet, res)
        if col_op == "col_rename":
            return self._run_col_rename(intent, path, sheet, res)
        res.add("col_op", False, f"未知列操作: {col_op}")
        res.message = f"未知列操作: {col_op}"
        return res

    def _run_col_list(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """查看列：列出 sheet 所有列。"""
        cols = self.cli.list_columns(path, sheet)
        if not cols:
            res.add("list_columns", False, "无列或读取失败")
            res.message = "无法读取列信息"
            return res
        lines = [f"  [{ci}] {name}" for ci, name in cols]
        res.add("list_columns", True, f"{len(cols)} 列")
        res.message = f"{sheet} 共 {len(cols)} 列：\n" + "\n".join(lines)
        res.final = CLICallResult(
            ok=True,
            data={"columns": [{"col": ci, "name": name} for ci, name in cols]},
        )
        return res

    def _run_col_add(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """新增列：默认追加末尾，可选类型标注与插入位置。"""
        name = self._clean_col_hint(intent.extras.get("col_name"))
        # 剥离残留的类型标注片段（parser 可能把"类型为int"并入列名）
        name = re.sub(r"[，,。]?\s*类型(?:为|是)?\s*\S*$", "", name).strip()
        if not name:
            res.add("col_name", False, "未识别到新列名")
            res.message = "无法识别新列名"
            return res

        # 可选类型标注：从原文提取"类型为X"
        type_str = None
        m = re.search(r"类型(?:为|是)?\s*([A-Za-z\u4e00-\u9fff]+)", intent.raw or "")
        if m:
            type_str = m.group(1).strip()

        # 可选插入位置：在X列后
        after = None
        m = re.search(r"在(.+?)(?:列|字段)(?:之后|后面|后)", intent.raw or "")
        if m:
            after_name = m.group(1).strip()
            headers = self.cli.read_header(path, sheet)
            matcher = self._make_matcher(headers, path.stem, sheet, path)
            am = matcher.match(after_name)
            if am:
                after = am.index

        r = self.cli.insert_column(path, sheet, name, after=after, type_str=type_str)
        res.final = r
        if r.ok:
            pos = f"在列{after}后" if after else "末尾"
            res.add("insert_column", True, f"新增列 col{r.data['col']}={name} @ {pos}")
            extra = f" 类型={type_str}" if type_str else ""
            res.message = f"已新增列：{sheet} {pos} 列{r.data['col']}({name}){extra}"
        else:
            res.add("insert_column", False, r.error or "")
            res.message = f"新增列失败: {r.error}"
        return res

    def _run_col_delete(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """删除列：先匹配列名，破坏性操作走确认流程。"""
        hint = self._clean_col_hint(intent.extras.get("col_name"))
        if not hint:
            res.add("col_name", False, "未识别到待删列名")
            res.message = "无法识别待删列名"
            return res
        headers = self.cli.read_header(path, sheet)
        matcher = self._make_matcher(headers, path.stem, sheet, path)
        m = matcher.match_best(hint)
        if m is None:
            res.add("match_col", False, f"未找到列: {hint}")
            res.message = f"未找到列: {hint}"
            return res
        res.add("match_col", True, f"列{m.column}({m.index})")

        # 确认流程：首次预览，回传 confirm_token 后执行
        confirmed = intent.extras.get("__col_delete_confirmed__") is True
        if not confirmed:
            rows = self.cli.read_sheet(path, sheet)
            non_empty = sum(1 for r in rows
                            if m.index - 1 < len(r) and r[m.index - 1] is not None)
            res.needs_confirm = True
            res.confirm_token = f"col_delete:{path.stem}:{sheet}:{m.index}"
            res.add("col_delete_preview", True,
                    f"列{m.index}({m.column}) {non_empty}个非空值")
            res.message = (
                f"将删除列 {m.column}(列{m.index})，含 {non_empty} 个非空数据，"
                f"此操作不可逆，确认？"
            )
            return res

        r = self.cli.delete_column(path, sheet, m.index)
        res.final = r
        if r.ok:
            res.add("delete_column", True, f"删除列 col{m.index}={m.column}")
            res.message = f"已删除列：{sheet} 列{m.index}({m.column})"
        else:
            res.add("delete_column", False, r.error or "")
            res.message = f"删除列失败: {r.error}"
        return res

    def _run_col_rename(self, intent: NLIntent, path: Path, sheet: str, res: AgentResult) -> AgentResult:
        """重命名列：匹配旧列名，写入新列名。"""
        old_hint = self._clean_col_hint(intent.extras.get("col_name"))
        new_name = self._clean_col_hint(intent.extras.get("col_new_name"))
        if not old_hint or not new_name:
            res.add("col_name", False, f"未识别列名 old={old_hint} new={new_name}")
            res.message = "无法识别重命名的旧/新列名"
            return res
        headers = self.cli.read_header(path, sheet)
        matcher = self._make_matcher(headers, path.stem, sheet, path)
        m = matcher.match_best(old_hint)
        if m is None:
            res.add("match_col", False, f"未找到列: {old_hint}")
            res.message = f"未找到列: {old_hint}"
            return res
        r = self.cli.rename_column(path, sheet, m.index, new_name)
        res.final = r
        if r.ok:
            res.add("rename_column", True, f"列{m.index}: {m.column} -> {new_name}")
            res.message = f"已重命名列：{sheet} 列{m.index} {m.column} → {new_name}"
        else:
            res.add("rename_column", False, r.error or "")
            res.message = f"重命名列失败: {r.error}"
        return res

    def _wire_sinks(self, res: AgentResult) -> AgentResult:
        """把实例级流式 sink 挂到 res 上，思考/步骤实时推送。"""
        if self._agent_thinking_sink is not None:
            res.on_thinking = self._agent_thinking_sink
        if self._agent_step_sink is not None:
            res.on_step = self._agent_step_sink
        return res

    def run_as_writer(self, fragment, context: dict = None,
                      session_id: str = "") -> AgentResult:
        """管道 Step6 写库执行器:接收 AgentFragment,执行其 sql_or_ops。

        复用现有 _run_single / _verify_write_back 能力,降级为管道末端执行单元。
        旧路径径 run() 保留不变(向后兼容)。

        Args:
            fragment: AgentFragment(含 sql_or_ops 列表)
            context: 上下文(produced 映射等)
            session_id: 会话 id

        Returns:
            AgentResult,ok 汇总各 op 写后验证结果
        """
        res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="add", raw=fragment.agent_name or "")))
        res.add_thinking("执行", f"Step6 写库: {fragment.agent_name} ({len(fragment.sql_or_ops)} ops)")
        produced = (context or {}).get("produced", {})
        for op in fragment.sql_or_ops:
            if not isinstance(op, dict):
                continue
            # op → NLIntent 转换(复用 _run_single)
            try:
                it = NLIntent(
                    action=op.get("action", "add"),
                    table_hint=fragment.target_table or op.get("table_hint"),
                    sheet_hint=fragment.target_sheet or op.get("sheet_hint"),
                    locator_field=op.get("locator_field"),
                    locator_value=op.get("locator_value"),
                    target_field=op.get("target_field"),
                    value=op.get("value"),
                    raw=op.get("raw", ""),
                )
                fields = op.get("fields", {})
                if isinstance(fields, dict) and fields:
                    it.extras["fields"] = fields
                # produced 占位符替换(复用 orchestrator 的 placeholder 机制)
                for k, v in (it.extras.get("fields", {}) or {}).items():
                    if isinstance(v, str):
                        for ph, rid in produced.items():
                            if ph in v:
                                it.extras["fields"][k] = v.replace(ph, rid)
                single = self._run_single(it, None, session_id)
                if single is None or single.ok is not True:
                    res.ok = False
                    res.add("write_op", False, f"op failed: {op.get('action')} {fragment.target_table}")
                else:
                    res.add("write_op", True, f"op ok: {op.get('action')} {fragment.target_table}")
                    # 合并 single 的 result_rows 供上层 _capture_produced 提取新 ID
                    for r in getattr(single, "result_rows", None) or []:
                        res.result_rows.append(r)
                    if single.message:
                        res.message = single.message
            except Exception as e:
                res.ok = False
                res.add("write_op", False, f"op exception: {e}")
        res.add_thinking("执行", f"Step6 写库完成: ok={res.ok}")
        return res

    def _step2_validate_intents(self, intents: list, _stream_res: "AgentResult",
                                session_id: str, locator_result=None) -> list:
        """Step2 最终校验：对解析+AI校验后的【最终 intents】跑 validate_two_layer。

        关键：必须在 _apply_ai_intent_check 等 Step1 重拆之后再调，否则
        Core4 PK 改写会被后续 parse_multi 重拆丢弃（Step3 仍用原 99001）。
        Step2 = 指令最终确认点：检测 PK 冲突 → ask → accept → 改写 intent，
        Step3 直接用改写后的 intent 干净写入。
        """
        if self._validator_agent is None or not intents:
            return intents
        try:
            from ..schema_bundle import (
                build_data_getter, _stem_to_path, _resolve_sheet, _resolve_path)

            def _sg(intent):
                stem = getattr(intent, "table_hint", "") or ""
                path = _stem_to_path(self, stem)
                if path is None and stem:
                    path = _resolve_path(self, stem)
                sheet = getattr(intent, "sheet_hint", "") or ""
                # §sheet 一致性：sheet_hint 空时复用与 Step3 _phase_partition 同源的
                # _resolve_sheet(path, intent)（agent 自有方法，sheet 消歧用 raw 关键词 +
                # row_aliases + LLM 兜底），保证校验 schema 读到的 sheet 与执行写盘 sheet
                # 一致，根治"校验判 Fabao → 执行写 FabaoLevel"错读漏检 PK 冲突。
                if path is not None and not sheet and stem:
                    try:
                        sheet = self._resolve_sheet(path, intent) or ""
                    except Exception:
                        sheet = ""
                if path is None or not sheet:
                    return [], []
                try:
                    headers = self.cli.read_header(path, sheet) if hasattr(self.cli, "read_header") else []
                    type_row = self.cli.read_type_row(path, sheet) if hasattr(self.cli, "read_type_row") else []
                    return list(headers or []), list(type_row or [])
                except Exception:
                    return [], []

            _dg = build_data_getter(
                self, intents,
                sheet_resolver=lambda p, i: self._resolve_sheet(p, i))
            # §FK 名→id 自动解析（V2 Step2 前置，0 LLM）：DecomposeAgent 常把被引用
            # 实体名直接填进 int/编号 FK 列（如 SchoolSpirit.神通id=裂空斩、灵根id=
            # 金灵根）。enum_resolver 只覆盖本表枚举映射，跨表 FK 名无映射 → 落
            # TYPE_MISMATCH ask，建议值仅 min(existing) 占位（非真解析），accept 也
            # 写错值（裂空斩≠该列最小值101，应是能力表里裂空斩的真实 id）。此处跨表
            # 精确名匹配出唯一行的 PK → 改写 fields[col]=PK，让 validate 直接判通过。
            self._auto_resolve_fk_names(intents, _stream_res)
            _vr = self._validator_agent.validate_two_layer(
                intents, schema_getter=_sg, data_getter=_dg,
                locator_result=locator_result,
                dry_run=bool(getattr(self, "_dry_run_flag", False)))
            if _vr.get("tips"):
                _stream_res.add_thinking("校验",
                    f"Step2 ValidateAgent: {len(_vr['tips'])} issues")
                try:
                    from ..subagent.validator_agent import attach_tips_as_soft_failures
                    _n_soft = attach_tips_as_soft_failures(intents, _vr.get("tips") or [])
                    if _n_soft:
                        _stream_res.add_thinking("校验",
                            f"P23 {_n_soft} 条 tips 转软失败上报")
                except Exception:
                    logger.debug("attach_tips_as_soft_failures 失败", exc_info=True)
            if not _vr.get("ok"):
                _stream_res.add_thinking("校验",
                    f"Step2 ValidateAgent 未通过,用户回复={_vr.get('user_reply')}")
                _skipped = [it for it in intents
                            if getattr(it, "validation", None) and it.validation.skipped]
                if _skipped:
                    # §交互增强：交互模式下用户主动 skip 的 intent 真正过滤掉，
                    # 不复位放行进 Step3（避免写冲突/幻觉数据）。
                    # 非交互模式（无 _ask_callback）保留原"复位放行交 Step5"行为。
                    # §dry_run 放行：dry_run 预览模式硬 issue 复位放行不真过滤，
                    # 保链路完整走通（用户要求默认接受建议，不跳过子任务）。
                    _has_ask_cb = getattr(self, "_ask_callback", None) is not None
                    _is_dry = bool(getattr(self, "_dry_run_flag", False))
                    if _has_ask_cb and not _is_dry:
                        _skip_desc = ", ".join(
                            f"{getattr(it,'table_hint','') or ''}/"
                            f"{getattr(it,'sheet_hint','') or ''}" for it in _skipped)
                        _stream_res.add_thinking("校验",
                            f"{len(_skipped)} 条子任务 Step2 未解决,交 Step3 显式标记为跳过: {_skip_desc}")
                    else:
                        _skip_desc = ", ".join(
                            f"{getattr(it,'table_hint','') or ''}/"
                            f"{getattr(it,'sheet_hint','') or ''}" for it in _skipped)
                        for it in _skipped:
                            v = getattr(it, "validation", None)
                            if v is not None:
                                v.skipped = False
                        _stream_res.add_thinking("校验",
                            f"{len(_skipped)} 条子任务有硬 issue，保留待 Step5 修复"
                            f"（不丢弃）: {_skip_desc}")
            try:
                self._save_nl_checkpoint(session_id, "post_validate", intents)
            except Exception:
                pass
        except Exception:
            logger.warning("Step2 validate_two_layer 失败", exc_info=True)
        return intents

    def _auto_resolve_fk_names(self, intents: list,
                               _stream_res: "AgentResult" = None) -> int:
        """跨表 FK 名→id 自动解析（V2 Step2 前置，0 LLM）。

        DecomposeAgent 常把被引用实体名直接填进 int/编号 FK 列（SchoolSpirit.神通id
        =「裂空斩」、灵根id=「金灵根」）。enum_resolver 仅覆盖本表枚举映射，跨表 FK
        名无映射 → 落 TYPE_MISMATCH ask，建议值仅是 min(existing_values) 占位（非真
        解析），accept 也写错值。此处对 add 的 int/编号类列填中文值做跨表精确名匹配：
        唯一命中行 + 该行有 PK → 改写 fields[col]=PK，让 validate 直接见 int 过校验。

        保守条件：仅 add；值含中文且非数字；仅 id/编号类列；跨表 exact 全表唯一 +
        单行唯一（多命中/无命中不动，交 ask 不误填）。
        """
        n = 0
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields")
            if not isinstance(fields, dict) or not fields:
                continue
            stem = (getattr(it, "table_hint", "") or "").strip()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            for col, val in list(fields.items()):
                if not isinstance(val, str):
                    continue
                vs = val.strip()
                if not vs or vs.lstrip("-").isdigit():
                    continue
                if vs.startswith("<") and vs.endswith(">"):
                    continue
                if not any(ord(c) > 127 for c in vs):
                    continue
                _col_clean = str(col).split(":")[0].strip()
                if "id" not in _col_clean.lower() and "编号" not in _col_clean:
                    continue
                _new_id = self._resolve_fk_name_to_id(vs, exclude_stem=stem)
                if _new_id is not None:
                    fields[col] = _new_id
                    n += 1
                    try:
                        if _stream_res is not None:
                            _stream_res.add_thinking("校验",
                                f"FK 名→id 自动解析：{stem}/{sheet} 列[{_col_clean}] "
                                f"「{vs}」→ {_new_id}")
                    except Exception:
                        pass
        return n

    def _resolve_fk_name_to_id(self, name: str,
                              exclude_stem: str = "") -> Optional[int]:
        """跨表精确名匹配 → 唯一引用行的 PK。多命中/无命中返回 None（不误填）。

        注：_cross_table_search 的 match_type=="exact" 实为 contains（v in k or k in v），
        需再按 cell 值精确相等收紧，否则「裂空」会命中「裂空斩·改」等超集。
        """
        try:
            _cands = self._cross_table_search(
                name, exclude_stem=exclude_stem, top_k_tables=10)
        except Exception:
            return None
        _exact_row: Optional[tuple] = None
        _near_row: Optional[tuple] = None
        _hit_tables = 0
        _near_hit_tables = 0
        _ambiguous = False
        for _c in (_cands or []):
            if _c.get("match_type") != "exact":
                continue
            _ms = [m for m in (_c.get("matches") or [])
                   if str(m.get("value", "")).strip() == str(name).strip()]
            if _ms:
                _hit_tables += 1
                if _hit_tables > 1:
                    return None  # 跨多表同名，不自动填
                if len(_ms) > 1:
                    _ambiguous = True  # 同表多行同名
                    continue
                _exact_row = (_c.get("table_stem"), _c.get("sheet"), _ms[0].get("row"))
                continue
            _near = []
            for m in (_c.get("matches") or []):
                cell = str(m.get("value", "")).strip()
                query = str(name).strip()
                if not cell or cell == query:
                    continue
                if len(cell) <= 2 and len(query) > len(cell) and cell in query:
                    _near.append(m)
            if not _near:
                continue
            _near_hit_tables += 1
            if _near_hit_tables > 1:
                return None
            if len(_near) > 1:
                _ambiguous = True
                continue
            _near_row = (_c.get("table_stem"), _c.get("sheet"), _near[0].get("row"))
        if _exact_row is None or _ambiguous:
            if _near_row is None or _ambiguous:
                return None
            _exact_row = _near_row
        _t_stem, _t_sheet, _row = _exact_row
        _path = self._find_table_by_stem(_t_stem)
        if _path is None:
            return None
        try:
            _pk_idx = self._locate_pk_col(_path, _t_sheet)
        except Exception:
            _pk_idx = None
        if not _pk_idx:
            return None
        try:
            _rv = self.cli.read_cell(_path, _t_sheet, _row, _pk_idx)
            if getattr(_rv, "ok", False) and _rv.data is not None:
                return int(_rv.data)
        except (ValueError, TypeError):
            return None
        except Exception:
            return None
        return None

    def _apply_ai_intent_check(self, intents: list, text: str,
                               _stream_res: "AgentResult",
                               _4step_parsed: bool = False) -> list:
        """Step1 AI 意图校验：对规则拆分产出的 intents 做完整性/列映射校验。

        §优化④：去掉 `len(intents) < 2 就 short-circuit` 盲点（单条意图也可能漏）。
        段级覆盖对账已在 ParseAgent._parse_segments 内做（每段 ≥1 条，0 条重跑），
        此处 AI 校验改为对【最终 intents】做字段层完整性复核，漏 → 仅对漏的那段
        重跑 decompose（便宜），而非整句重跑 parse_multi（贵）。

        - AI 通过 → 保持 intents。
        - AI 发现「主线意图遗漏」（missing 非空）→ 4-step 已分段时按段重跑
          decompose_segment；非 4-step 才回退 parse_multi。
        - AI 发现字段映射建议（corrections）→ 仅记录，不阻断（规则结果优先）。
        - AI 失败/超时 → 保持规则结果。
        """
        if self._ai_enhancer is None:
            return intents
        try:
            rule_summary = [
                {"action": i.action, "table_hint": i.table_hint,
                 "locator_value": i.locator_value,
                 "fields_keys": list((i.extras or {}).get("fields", {}).keys())}
                for i in intents
            ]
            ai_check = self._ai_enhancer.ai_verify_intents(text, rule_summary)
            if not ai_check:
                return intents
            if ai_check.get("ok"):
                _stream_res.add_thinking("解析", "AI 意图校验通过：规则拆分合理")
                return intents
            missing = ai_check.get("missing", [])
            corr = ai_check.get("corrections", [])
            # §边界修复（问题B）：幻觉意图过滤。原 ai_verify_intents 只查漏(missing)
            # 不查多，DecomposeAgent 幻产意图（如用户没要 guild/Library 却产一条）直接
            # 进 Step2/3，到写盘才发现必填列缺失或写入脏数据。现扩展 prompt 产 extra
            # 字段（幻觉意图下标列表），此处对照用户原文过滤掉。
            extra_idxs = ai_check.get("extra", []) or []
            if extra_idxs:
                # 校验下标合法性 + 对照原文二次确认（防 AI 误判）
                _valid_extra: list[int] = []
                for _ei in extra_idxs:
                    if isinstance(_ei, int) and 0 <= _ei < len(intents):
                        _valid_extra.append(_ei)
                if _valid_extra:
                    _extra_desc = ", ".join(
                        f"#{_ei}({getattr(intents[_ei],'table_hint','') or ''}/"
                        f"{getattr(intents[_ei],'sheet_hint','') or ''})"
                        for _ei in _valid_extra)
                    _stream_res.add_thinking("解析",
                        f"AI 幻觉检测：过滤 {_valid_extra} 个无原文依据意图: {_extra_desc}")
                    intents = [it for _idx, it in enumerate(intents)
                               if _idx not in set(_valid_extra)]
            # §优化④：4-step 已分段 → 按段重跑 decompose_segment（便宜），不跑 parse_multi
            if (missing and _4step_parsed
                    and hasattr(self, '_decompose_agent')
                    and self._decompose_agent is not None):
                try:
                    from ..parser.multi_intent_splitter import split_multi_intent
                    segs = split_multi_intent(text)
                    if segs and len(segs) > 1:
                        re_split: list = []
                        existing_stems = {i.table_hint for i in intents if i.table_hint}
                        for seg in segs:
                            seg_text = getattr(seg, "text", seg) if not isinstance(seg, str) else seg
                            seg_stems = {i.table_hint for i in intents
                                         if i.table_hint and i.raw and seg_text
                                         and seg_text[:15] in i.raw}
                            # 段已覆盖（有对应 intent）则跳过；未覆盖则重跑
                            if seg_stems & existing_stems:
                                continue
                            try:
                                lr = self._locator_agent.locate(seg_text)
                                if lr and lr.candidates:
                                    ri = self._decompose_agent.decompose_segment(
                                        seg_text, lr)
                                    re_split.extend(ri)
                            except Exception:
                                logger.warning("段重跑失败(seg=%s)", seg_text[:30],
                                               exc_info=True)
                        if re_split:
                            from ..parse_agent import ParseAgent as _PA
                            _pa = _PA(parser=self.parser,
                                      thinking_sink=self._agent_thinking_sink,
                                      cli=self.cli, locator_agent=self._locator_agent,
                                      decompose_agent=self._decompose_agent)
                            re_nl = [_pa._split_to_nl(si, text) for si in re_split]
                            try:
                                from .produces_inference import infer_produces_consumes
                                infer_produces_consumes(re_nl)
                            except Exception:
                                pass
                            _stream_res.add_thinking("解析",
                                f"AI 校验指出漏段,段级重跑补 {len(re_nl)} 条")
                            return list(intents) + re_nl
                except Exception:
                    logger.warning("段级重跑失败,降级 parse_multi", exc_info=True)
            # 非 4-step 或段重跑失败 → 回退 parse_multi（原逻辑）
            if missing and self.parser is not None and hasattr(self.parser, "parse_multi"):
                try:
                    pm_intents = self.parser.parse_multi(text)
                    if pm_intents and len(pm_intents) >= 1:
                        _stream_res.add_thinking("解析",
                            f"AI 校验指出规则拆分遗漏主线意图 {missing}，"
                            f"回退 LLM parse_multi 重新分解得 {len(pm_intents)} 条")
                        return pm_intents
                except Exception:
                    logger.warning("parse_multi 回退失败，保留规则拆分", exc_info=True)
            # parse_multi 未触发/失败 → 降级记建议不阻断
            if missing:
                _stream_res.add_thinking("解析",
                    f"AI 校验建议补充 {len(missing)} 条意图：{missing}")
                _stream_res.ai_suggestions.extend(str(m) for m in missing)
            if corr:
                _stream_res.add_thinking("解析",
                    f"AI 校验发现字段映射建议：{corr}（已记录，规则结果优先）")
                _stream_res.ai_suggestions.extend(f"字段映射：{c}" for c in corr)
            return intents
        except Exception:
            logger.warning("Step1 AI 意图校验失败，保持规则拆分", exc_info=True)
            return intents

    def run_v2(self, text: str, confirm_token: Optional[str] = None,
               context: str = "", confirm_cascade: bool = True,
               session_id: str = "") -> AgentResult:
        """4-Step V2 入口（CODEMAKER_EXCEL_PIPELINE_V2 默认 ON）。

        步间隔离（过渡期尽力保证，详见 core/pipeline/contracts.py）：
        Step1 解析 → Step2 校验 → Step3 执行（零 LLM）→ Step4 汇总。
        每步错误固定归属本步 step_id，不跨步冒泡、不吞。

        边界约定：
          - Step1 只解析（分段+段级对账）
          - Step2 只校验+冲突处理（validate_two_layer + ask + 修正）
          - Step3 零 LLM（no_llm=True 透传到 _run_single，替代原 env 突变）
          - Step4 只汇总+反模式归纳（内联 induce_anti_patterns，不补建写入）

        Returns:
            AgentResult（聚合 V2 StepResult[]，兼容旧下游）。
        """
        from .pipeline import (
            ExcelAgentPipeline, ExcelAgentServices,
            Step1ParseSubAgent, Step2ValidateSubAgent,
            Step3ExecuteSubAgent, Step4ConcludeSubAgent,
            StepContext, SSE, STEP1_PARSE, STEP3_EXECUTE, STEP4_CONCLUDE,
        )
        _stream_res = self._wire_sinks(
            AgentResult(ok=True, intent=NLIntent(raw=text)))
        _stream_res.add_thinking("V2", "▶ run_v2 入口（4-Step 硬隔离）")
        orig_text = text
        text = _clean_quotes(text)
        if text != orig_text:
            _stream_res.add_thinking("解析", f"清洗引号后：「{text}」")
        if self._ai_enhancer is not None:
            try:
                self._ai_enhancer.reset_circuit()
            except Exception:
                pass
        # §确认令牌短路（V2）：复用 legacy _run_confirmed 的	delete/col_delete/
        # search: token 直接执行已确认操作（跳过 4-Step 重解析，避免重定位漂移）。
        # _run_confirmed 不识别的 token（ap:/cascade_set_pk:）返回 None → 继续走 V2
        # 流水线，由 ctx.confirm_token 透传给 Step3 → _run_single 设 extras 确认标记。
        if confirm_token:
            _stream_res.add_thinking("执行",
                f"检测到确认令牌「{confirm_token}」，跳过 4-Step 重解析，直接执行已确认操作")
            _confirmed = self._run_confirmed(confirm_token, text, confirm_cascade)
            if _confirmed is not None:
                if self._agent_thinking_sink is not None and _confirmed.thinking_steps:
                    for _ts in _confirmed.thinking_steps:
                        self._agent_thinking_sink(
                            _ts.get("phase", ""), _ts.get("detail", ""))
                _stream_res.thinking_steps.extend(_confirmed.thinking_steps)
                _confirmed.thinking_steps = _stream_res.thinking_steps
                return _confirmed
        # §低危修复：per-run 新建 LLMCounter（替代 reset 共享实例）。
        # 原入口 reset() 清 _instance_stats → 单实例跨请求（agent_service 单例 agent）
        # 串行 reset 互踩（请求 A 计数被请求 B reset 清零）+ 并发同实例 reset 互踩。
        # 新建 per-run counter 赋值 self._llm_counter（覆盖 __init__ 的共享实例），
        # 隔离本次 run 打点；旧实例被 GC，不还原（下次 run_v2 入口再 new 覆盖）。
        # 并发同实例仍互踩 self._llm_counter（需 ctx 传递彻底解，改动大暂不引入；
        # server 场景请求串行处理 + skill_ab_test 多 agent 实例，此修法足够）。
        from ...llm_counter import LLMCounter as _LC
        self._llm_counter = _LC()
        # cancel_event / llm_counter 下传（与 run() 一致）
        _run_cancel = getattr(self, "_cancel_event", None)
        try:
            if self.parser is not None:
                self.parser._cancel_event = _run_cancel
                self.parser._llm_counter = self._llm_counter
        except Exception:
            pass

        ctx = StepContext(
            session_id=session_id or "default", user_text=text,
            legacy_agent=self, thinking_sink=self._agent_thinking_sink,
            cancel_event=_run_cancel, confirm_token=confirm_token)
        # 服务对象收口 SubAgent 所需的 legacy 接口（替代原 agent=self 散播私态）。
        # services 内部委托 legacy 私有方法（S4 提取为纯服务后去 agent 句柄）。
        services = ExcelAgentServices(legacy_agent=self)
        step1 = Step1ParseSubAgent(
            parser=self.parser, thinking_sink=self._agent_thinking_sink,
            cli=self.cli, locator_agent=self._locator_agent,
            decompose_agent=self._decompose_agent)
        pipeline = ExcelAgentPipeline(
            step1=step1, step2=Step2ValidateSubAgent(services=services),
            step3=Step3ExecuteSubAgent(services=services),
            step4=Step4ConcludeSubAgent(services=services))

        all_steps: list[AgentStep] = []
        all_failures: list[dict] = []
        for ev in pipeline.run(ctx):
            etype = ev.get("type")
            if etype == "stage_start":
                _stream_res.add_thinking(ev.get("title", ""),
                    f"{ev.get('title','')} 开始")
            elif etype == "stage_end":
                sid = ev.get("step_id", "")
                _ok = ev.get("ok", False)
                errs = ev.get("errors", []) or []
                msg = f"{ev.get('title', sid)} {'通过' if _ok else '未通过'}"
                if errs:
                    msg += f"（{len(errs)} 个问题）"
                _stream_res.add_thinking(sid, msg)
                # §中危 6 修复：仅聚合 hard error 到 all_failures（hard 通常无对应
                # s3.failures 项，如 Step1 parse_empty）；soft failures 不在此重复聚合，
                # 由 s3.artifacts["failures"] 单一源取（保真 table/sheet/col 形状，
                # 避免同一 failure 双记 + 形状不一导致前端 ?/undefined）。
                for e in errs:
                    if e.get("is_hard"):
                        all_failures.append({
                            "type": e.get("error_type", ""),
                            "step_id": sid,
                            "message": e.get("message", ""),
                            "root_cause": e.get("root_cause", ""),
                            "suggestion": e.get("suggestion"),
                            "status": "hard_error",
                        })
            elif etype == "done":
                _stream_res.ok = ev.get("ok", False)
                _stream_res.message = ev.get("message", "")

        s3 = ctx.get_result(STEP3_EXECUTE)
        s4 = ctx.get_result(STEP4_CONCLUDE)
        sub_tasks = (s3.artifacts.get("subtasks") if s3 else []) or []
        all_result_rows = (s3.artifacts.get("results") if s3 else []) or []
        steps = (s3.artifacts.get("steps") if s3 else []) or []
        if s3:
            all_steps.extend(steps)
        if s4 and isinstance(s4.artifacts.get("failures"), list):
            all_failures.extend(s4.artifacts.get("failures") or [])
        elif s3:
            all_failures.extend(s3.artifacts.get("failures", []) or [])
        # §确认信号回流：Step3 把行未找到/级联删除/反模式等 needs_confirm 信号落入
        # s3.failures（每条带 confirm_token/confirm_kind/pending_search）。run_v2 需
        # 回填顶层 AgentResult 的对应字段，否则 _finalize_crud 的确认分支永远不触发
        # （needs_confirm=False/confirm_token=None → 不暂存 pending → 前端拿不到
        # 确认按钮 + confirm_token 无法续传 → 行定位值找不到的跨表搜索确认链断裂）。
        _confirm_failure = None
        for _f in (s3.artifacts.get("failures", []) if s3 else []) or []:
            if isinstance(_f, dict) and _f.get("confirm_token"):
                _confirm_failure = _f
                break
        if _confirm_failure:
            _stream_res.needs_confirm = True
            _stream_res.confirm_token = _confirm_failure.get("confirm_token")
            _stream_res.confirm_kind = _confirm_failure.get("confirm_kind") or ""
            if _confirm_failure.get("pending_search"):
                _stream_res.pending_search = _confirm_failure["pending_search"]
            # 行歧义删除候选行回填：_finalize_crud 据此映射 row_alternatives 给前端渲染。
            if _confirm_failure.get("row_evidence"):
                _stream_res.row_evidence = _confirm_failure["row_evidence"]
        if s4:
            if s4.artifacts.get("summary"):
                _stream_res.message = s4.artifacts["summary"]
        _stream_res.steps = all_steps
        _stream_res.result_rows = all_result_rows
        _stream_res.sub_tasks = sub_tasks
        _stream_res.failures = all_failures
        s1 = ctx.get_result(STEP1_PARSE)
        if s1 and s1.artifacts.get("intents"):
            _stream_res.intent = s1.artifacts["intents"][0]
        return _stream_res

    def run(self, text: str, confirm_token: Optional[str] = None,
            context: str = "", confirm_cascade: bool = True,
            session_id: str = "") -> AgentResult:
        """主入口：接收自然语言文本，解析意图并执行对应的增删查改操作。

        执行流程：
            1. 用 parse_multi 解析自然语言 → 多条 NLIntent
            2. 若只有单条意图，直接执行
            3. 若有多条意图，逐条执行并汇总结果

        Args:
            text: 用户的自然语言指令
            confirm_token: 二次确认令牌。当上一次返回 needs_confirm=True 时，
                调用方回传 confirm_token 表示确认，跳过 dry-run 直接执行
                （如级联删除）。

        Returns:
            AgentResult，包含执行步骤、最终结果和面向用户的消息。
        """
        # V2 流水线分流（CODEMAKER_EXCEL_PIPELINE_V2 默认 ON）：走 4-Step 硬隔离 orchestrator
        # 单一开关：=0 显式降级到旧 run() 6 步路径；默认 V2 接管。
        # 废弃 CODEMAKER_4STEP_LOOP/enable_4step_loop（旧合并分支，s1_parse 命名与 V2 冲突）。
        if os.getenv("CODEMAKER_EXCEL_PIPELINE_V2", "1") != "0":
            return self.run_v2(text, confirm_token=confirm_token,
                               context=context, confirm_cascade=confirm_cascade,
                               session_id=session_id)
        # 临时 res 用于流式推送确认短路阶段的思考（_run_confirmed 内部有自己的 res）
        _stream_res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="get", raw=text)))
        _stream_res.add_thinking("解析", f"接收指令：「{text}」。开始理解意图。")
        # §低危修复：per-run 新建 LLMCounter（与 run_v2 一致，替代 reset 共享实例）。
        from ...llm_counter import LLMCounter as _LC
        self._llm_counter = _LC()
        # 取消事件下传：parser（parse/parse_multi/subagent 共享同一实例）与
        # ai_enhancer（Step3/4/5/汇总）各自持有 client，需各自读到 cancel_event
        # 才能在单次 LLM 调用内被中断（而非仅循环边界）。
        _run_cancel = getattr(self, "_cancel_event", None)
        try:
            if self.parser is not None:
                self.parser._cancel_event = _run_cancel
                # LLM 计数器下传：parser.parse/parse_multi 与 subagent(base._call_llm*)
                # 经此读 agent._llm_counter，inc+merge 后心跳 peek_total 才可见。
                self.parser._llm_counter = self._llm_counter
        except Exception:
            pass
        # 要求 D：R7 serve 健康门控。run 入口预检 codemaker serve 可达性，
        # 不可达 → 直接降级返错（不进 LLM 链路卡死 90-180s）。
        # 门控级别 1：health_check（GET /api/health，5s 超时，仅测可达）
        # 门控级别 2（可选）：R7 膨胀探针（env CODEMAKER_R7_PROBE=1，发极小 prompt
        #   测 token 膨胀，本身耗时 <30s healthy / >90s confirmed）。
        # 默认仅跑级别 1（成本低），级别 2 需显式开 env。
        # 容错：health_check 误判（auth/网络抖动）不应阻断正常流程，只记 warning。
        # 仅 env CODEMAKER_STRICT_HEALTH_GATE=1 时严格阻断（默认宽松，避免误杀健康 serve）。
        try:
            _client = getattr(self.parser, "client", None) if self.parser else None
            if _client is not None and hasattr(_client, "health_check"):
                if not _client.health_check():
                    if os.getenv("CODEMAKER_STRICT_HEALTH_GATE", "0") == "1":
                        _stream_res.add_thinking("健康门控",
                            "codemaker serve 不可达，严格门控降级返错")
                        _stream_res.ok = False
                        _stream_res.message = "AI 服务暂时不可用，请稍后重试。"
                        return _stream_res
                    else:
                        _stream_res.add_thinking("健康门控",
                            "health_check 误判（serve 可能仍可用），宽松放行进 LLM 链路")
        except Exception:
            pass  # 门控失败不阻断主流程（容错）
        try:
            _enh = getattr(self, "_ai_enhancer", None)
            if _enh is not None:
                _enh._cancel_event = _run_cancel
        except Exception:
            pass
        # 清洗输入中的引号（用户常输入「名为"朱雀"」等带引号文本）
        # 注意：cross_table_splitter 的 regex 依赖引号定界对话/选项/分支内容，
        # 清洗后引号被剥会导致 _BRANCH_OPT_RE 等漏匹配 → branch 丢失 → 误走奖励
        # 对话路径。故保留原文 orig_text 供 splitter 使用，清洗后 text 供其余路径。
        orig_text = text
        text = _clean_quotes(text)
        _stream_res.add_thinking("解析", f"清洗引号后：「{text}」")

        # 新指令开始：重置 AI 熔断计数（上条指令的连续失败不波及本条）
        if self._ai_enhancer is not None:
            self._ai_enhancer.reset_circuit()

        # ── 二次确认短路 ──
        # confirm_token 已编码首轮定位出的确切目标（stem/sheet/row 或 col），
        # 直接按 token 执行删除，不再走 NL 重解析——LLM 重解析非确定性，
        # 可能把同一句话定位到另一张表（如"删除灵兽鬼剑"重解析命中 guild 表）。
        if confirm_token:
            _stream_res.add_thinking("执行", f"检测到确认令牌「{confirm_token}」，跳过 NL 重解析，直接执行已确认操作")
            confirmed = self._run_confirmed(confirm_token, text, confirm_cascade)
            if confirmed is not None:
                # 合并确认执行产生的思考到 stream
                if self._agent_thinking_sink is not None and confirmed.thinking_steps:
                    for ts in confirmed.thinking_steps:
                        self._agent_thinking_sink(ts.get("phase", ""), ts.get("detail", ""))
                # 合并 res 的 thinking_steps 字段供 service 收集
                _stream_res.thinking_steps.extend(confirmed.thinking_steps)
                confirmed.thinking_steps = _stream_res.thinking_steps
                return confirmed

        # 6.6 巨型指令延迟墙 + 原则11/R8h:三 agent 串行链替代硬编码模板。
        #   - 全输入走 LocatorAgent 探候选表 + FK 边(无关键词闸门,真正泛化)
        #   - ≥2 候选表或含 FK 边 → DecomposeAgent 产 SplitIntent[] + ValidatorAgent 校验
        #   - 三 agent 失败/单表 → 回退 detect_cross_table_action + splitter 11 模式(规则安全网)
        #   - 开关 CODEMAKER_AGENT_CHAIN=0 可回退原 detect 闸门路径
        cross_intents_nl: list[NLIntent] = []
        cross_action = None
        agent_chain_enabled = os.getenv("CODEMAKER_AGENT_CHAIN", "1") != "0"
        covered_stems: set[str] = set()
        # === 6 步降级路径 ParseAgent 主导（合并老 s1/s2/s3 为 s1_parse）===
        # 仅 CODEMAKER_EXCEL_PIPELINE_V2=0 显式降级到旧 run() 时才跑到此。
        # V2 默认 ON 时 run() 在 4436 已分流到 run_v2，这段为降级通道的 ParseAgent 入口。
        # 废弃原 CODEMAKER_4STEP_LOOP gate（与 V2 开关重复，造成两套 step_id 命名混叠）。
        _4step_parsed = False
        # O14 P27 resume：显式 env 触发（CODEMAKER_4STEP_RESUME=<session_id>）。
        # stall/crash 后重启设此 env → run() 入口调 _resume_from_checkpoint 取中间态
        # → skip Step1 parse + post_parse save，跳过 completed_op_keys 中的已成功 Step5 op。
        # e2e 阻 R7（serve 侧），证据用确定性单测。
        _resumed_intents: Optional[list] = None
        _resumed_completed: set = set()
        _resumed_stage: Optional[str] = None
        if os.getenv("CODEMAKER_4STEP_RESUME", "") and session_id:
            _ri, _rs, _rc = self._resume_from_checkpoint(session_id)
            if _ri is not None:
                _resumed_intents = _ri
                _resumed_stage = _rs
                _resumed_completed = set(_rc or [])
                _4step_parsed = True
                cross_intents_nl = list(_ri)
                cross_action = "4step_resume"
                covered_stems = {i.table_hint for i in _ri if i.table_hint}
                _stream_res.add_thinking("续跑",
                    f"P27 resume from {_resumed_stage}: {len(_ri)} 条意图，"
                    f"{len(_resumed_completed)} 个 op 已成功跳过")
        if True:  # 6 步降级路径默认走 ParseAgent 合并（原 CODEMAKER_4STEP_LOOP 已废弃，统一 V2 开关）
            # O14：resume 已置 _4step_parsed=True + cross_intents_nl → skip Step1 parse。
            if _4step_parsed:
                _4step_nl = cross_intents_nl
            else:
                from ..parse_agent import ParseAgent as _ParseAgent
                _pa = _ParseAgent(
                    parser=self.parser, thinking_sink=self._agent_thinking_sink,
                    cli=self.cli, locator_agent=self._locator_agent,
                    decompose_agent=self._decompose_agent)
                try:
                    _4step_nl = _pa.parse(orig_text)
                except Exception:
                    logger.warning("4-Step ParseAgent 失败,回退原流程", exc_info=True)
                    _4step_nl = []
            if _4step_nl and not _resumed_intents:
                cross_intents_nl = _4step_nl
                cross_action = "4step_parse_agent"
                covered_stems = {i.table_hint for i in _4step_nl if i.table_hint}
                _4step_parsed = True
                # P27：post_parse checkpoint（opt-in）。stall 续跑可免 Step1 重 LLM decompose。
                self._save_nl_checkpoint(session_id, "post_parse", _4step_nl)
                _stream_res.add_thinking("解析",
                    f"4-Step ParseAgent 产出 {len(_4step_nl)} 条意图"
                    f"(source={_4step_nl[0].source},覆盖 {len(covered_stems)} 表)")
                # §4 ValidateAgent 接入已后移：原在此（Step1 parse 阶段）跑 validate
                # 会被后续 _apply_ai_intent_check 的 parse_multi 重拆丢弃 Core4 PK 改写
                # → Step3 仍用原 99001 → 冲突落 Step3。现移到 _apply_ai_intent_check
                # 之后对【最终 intents】校验（见 _step2_validate_intents 调用点），
                # Step2 才是指令最终确认点。
        # 1. 规则模板先跑已知模式(npc_dialogue/item/...)的精确结构 op(dialog/option/spawn/
        #    prefab 产 produces/consumes 连线)作基线,保证已知模式稳覆盖。
        if not _4step_parsed:
            detect_action = detect_cross_table_action(orig_text)
            if detect_action:
                splitter = CrossTableIntentSplitter()
                for si in splitter.split(orig_text):
                    extras_tpl: dict = {"fields": si.fields, "source": "splitter"}
                    if si.produces:
                        extras_tpl["produces"] = si.produces
                    cross_intents_nl.append(NLIntent(
                        action=si.action, table_hint=si.table_hint, sheet_hint=si.sheet_hint,
                        locator_value=si.locator_value, locator_field=si.locator_field,
                        raw=si.text, extras=extras_tpl))
                    if si.table_hint:
                        covered_stems.add(si.table_hint)
                cross_action = detect_action
                if cross_intents_nl:
                    _stream_res.add_thinking("解析",
                        f"规则模板产出 {len(cross_intents_nl)} 条意图"
                        f"(模式={detect_action},覆盖 {len(covered_stems)} 表)")
        # 2. LLM 三 agent 链:仅分解"模板未覆盖"的候选表(本用例 quest/reward),
        #    缩小 LLM 调用范围→更快更可靠;detect=None(未知模式)→全候选 LLM 兜底,真正泛化。
        #    4-Step §2.10: _4step_parsed=True 时 ParseAgent 已含完整 LLM 拆分,跳过避免重复。
        if not _4step_parsed and agent_chain_enabled and self._locator_agent and self._decompose_agent:
            try:
                locator_result = self._locator_agent.locate(orig_text)
                if locator_result and locator_result.is_cross_table:
                    uncovered = [c for c in locator_result.candidates
                                if c.stem not in covered_stems]
                    if uncovered:
                        from ..subagent.locator_agent import LocatorResult as _LR
                        sub_lr = _LR(candidates=uncovered,
                                       fk_edges=locator_result.fk_edges,
                                       ambiguous=False)
                        split_intents = self._decompose_agent.decompose(orig_text, sub_lr)
                        if split_intents:
                            if self._validator_agent:
                                self._validator_agent.validate(split_intents, sub_lr)
                            chain_nl = self._split_intents_to_nl(split_intents, orig_text)
                            existing = {(getattr(i, "table_hint", None),
                                         getattr(i, "sheet_hint", None))
                                        for i in cross_intents_nl}
                            _added = 0
                            for i in chain_nl:
                                k = (getattr(i, "table_hint", None),
                                     getattr(i, "sheet_hint", None))
                                if k in existing:
                                    continue
                                cross_intents_nl.append(i)
                                existing.add(k)
                                _added += 1
                            if _added:
                                cross_action = cross_action or "agent_chain"
                                _stream_res.add_thinking("解析",
                                    f"LLM 链补 {_added} 条意图"
                                    f"(模板已覆盖 {len(covered_stems)} 表后,LLM 分解 {len(uncovered)} 候选)"
                                    f"→ cross 共 {len(cross_intents_nl)} 条")
            except Exception:
                if os.getenv("CODEMAKER_AGENT_CHAIN_RAISE", "0") == "1":
                    raise
                logger.warning("三 agent 链失败,降级仅用规则模板", exc_info=True)

        fast_path = (bool(cross_intents_nl)
                     and os.getenv("CODEMAKER_SPLITTER_FAST_PATH", "1") != "0")
        # 原则11/R8g：splitter 模板不完整(产 <2 intent,如 pet 缺 PetEvolveData/mail 产空)
        # 时,走 LLM 链分解(schema 注入+产每表一 op+produces/consumes)取代 per-template 手写。
        # splitter 产 ≥2(quest_npc/item)保留 fast-path;LLM 为主、规则为安全网。
        # 阈值 env 可调（4-Step §1.2）：CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD 默认 2（现状），
        # 调到 99 即强制 DecomposeAgent 接管所有命中 fast-path 的输入（schema-driven 灰度推前置）。
        _decompose_threshold = int(os.getenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "2"))
        # 4-Step §2.10: _4step_parsed=True 时 ParseAgent 已含 LLM 拆分,跳过 _llm_chain_decompose 避免重复。
        if not _4step_parsed and cross_action and len(cross_intents_nl) < _decompose_threshold:
            hint_stem = (cross_intents_nl[0].table_hint if cross_intents_nl
                         else cross_action)
            try:
                llm_intents = self._llm_chain_decompose(orig_text, hint_stem)
                if len(llm_intents) >= 2:
                    cross_intents_nl = llm_intents
                    _stream_res.add_thinking(
                        "解析",
                        f"LLM 链分解 {len(cross_intents_nl)} 条意图(splitter 模板不完整,LLM 为主)")
            except Exception:
                logger.warning("LLM 链分解失败,保留 splitter 结果", exc_info=True)
        # 模板产出仅作 baseline，不短路 LLM 拆分。
        # 撤掉 fast-path 命中即跳过 parse_multi 的硬编码（combat_reward 等模板字段写死
        # 会漏子任务，如「封印魔龙」只产 2 条丢 8 条）。现一律让 LLM parse_multi
        # 补全：模板已覆盖的表 LLM 重拆也无害（同表去重），未覆盖的表 LLM 才是主力。
        # 模板产出仅在 parse_multi 产空时作兜底 baseline（见下方 not intents 分支）。
        if cross_intents_nl:
            _stream_res.add_thinking("解析",
                f"模板/规则 baseline 产出 {len(cross_intents_nl)} 条意图"
                f"(模式={cross_action},覆盖 {len(covered_stems)} 表),交 LLM 补全")
        # Step1 AI 校验 + LLM 补全：parse_multi 对整句重拆，补模板漏掉的子任务。
        # 命中跨表模式也不再跳过——准确率优先于避免超时（用户明确：不少指令是关键）。
        # D5: 注入 relation graph 关联表提示辅助 LLM 拆表
        try:
            from .table_relations import RelationGraph
            rg = RelationGraph.load()
            hint_stem = ""
            if cross_intents_nl:
                hint_stem = cross_intents_nl[0].table_hint or ""
            if hint_stem:
                related = rg.get_related_tables(hint_stem)
                if related:
                    rel_desc = "、".join(f"{s}({rt})" for s, rt in related)
                    context = (context + "\n" if context else "") + \
                              f"## 关联表提示（{hint_stem} 的关联表：{rel_desc}，可据此拆分多表新增）"
        except Exception:
            logger.warning("relation graph 提示注入失败（降级回退无提示）", exc_info=True)
        # §优化③：4-step 已做段级拆分覆盖时短路冗余 parse_multi（省 ~180s）。
        # 保留为 fallback：仅当 ParseAgent 产 <2 条时才跑 parse_multi 补全。
        if (not _4step_parsed) and hasattr(self.parser, 'parse_multi'):
            _llm_intents = self.parser.parse_multi(text, context)
            # LLM 计数：parse_multi 是一次 LLM 往返（capability: llm-call-instrumentation）
            try:
                if self._llm_counter is not None:
                    from .llm_context import estimate_tokens
                    self._llm_counter.inc("parse", tokens=estimate_tokens(text))
                    self._llm_counter.merge_to_instance()
            except Exception:
                pass
            if _llm_intents:
                # 合并：LLM 产出为主，模板产出的表若 LLM 未覆盖则保留（防 LLM 漏拆）
                _llm_stems = {i.table_hint for i in _llm_intents if i.table_hint}
                _merged = list(_llm_intents)
                for ci in cross_intents_nl:
                    if ci.table_hint not in _llm_stems:
                        _merged.append(ci)
                        _llm_stems.add(ci.table_hint)
                intents = _merged
                _stream_res.add_thinking("解析",
                    f"LLM parse_multi 补全得 {len(_llm_intents)} 条"
                    f" + 模板补 {len(intents) - len(_llm_intents)} 条 = {len(intents)} 条")
            else:
                intents = list(cross_intents_nl)
                _stream_res.add_thinking("解析",
                    f"LLM parse_multi 产空,回退模板 baseline {len(intents)} 条")
        else:
            intents = list(cross_intents_nl)
        # Step1 AI 校验：发现「主线意图遗漏」→ 回退 parse_multi 重拆（Test2 修复）；
        # 其余仅记录建议。
        intents = self._apply_ai_intent_check(intents, text, _stream_res,
                                               _4step_parsed=_4step_parsed)
        # parse_multi 产空且无模板 baseline 时的二级兜底（逐段 parse）。
        # 新逻辑上方已优先走 parse_multi，此处仅在 intents 仍空时兜底。
        if not intents:
            from ..parser.multi_intent_splitter import split_multi_intent
            try:
                segs = split_multi_intent(text)
            except Exception:
                segs = []
            fallback_intents: list[NLIntent] = []
            for seg in segs:
                if len(fallback_intents) >= 8:
                    break
                try:
                    fi = self.parser.parse(seg.text, context)
                    if fi:
                        fallback_intents.append(fi)
                except Exception:
                    continue
            if len(fallback_intents) >= 1:
                intents = fallback_intents
                _stream_res.add_thinking(
                    "解析",
                    f"parse_multi 产空，回退逐条 parse 成功 {len(intents)} 条意图")
            if not intents:
                err_type = getattr(self.parser, "_last_error_type", "") or "parse_failed"
                _stream_res.ok = False
                _stream_res.add("parse", False, f"解析失败（{err_type}）")
                _stream_res.message = "指令解析失败（LLM 超时或不可用），请重试或简化指令。"
                _stream_res.add_thinking("解析", "解析失败，快速返回错误提示（不再重试浪费时间）")
                return _stream_res
        if intents:
            _stream_res.add_thinking("解析", f"解析为 {len(intents)} 条意图：" + "、".join(
                (i.action if i else "?") for i in intents[:3]))

        # §4 Step2 最终校验：在 Step1 解析+AI校验（_apply_ai_intent_check 含
        # parse_multi 重拆）之后再跑 validate_two_layer，对【最终 intents】做
        # PK 冲突前移检测 + ask + 改写。要求 A：解除 _4step_parsed gate，
        # 所有路径（4-step / 非 4-step 回退 / 跨表多意图）统一走 Step2 校验，
        # Step3 拿到已确认/改写的 intent 干净写入。6-step env 块（下方 4593）
        # 默认关，仅作逃生口（env 显式开时双跑，PK 已 _pk_resolved 不重复 ask）。
        intents = self._step2_validate_intents(
            intents, _stream_res, session_id,
            getattr(locals().get("_pa"), "_last_locator_result", None))

        if len(intents) <= 1:
            return self._run_single(intents[0], confirm_token, session_id)

        # 多条指令：全局分阶段批量执行（Step2 所有→Step3 所有→...→Step6 所有）
        # 保证前端阶段顺序严格 Step1→Step2→Step3→Step4→Step5→Step6，
        # 避免逐任务执行导致任务1的 Step6 汇总先于任务2的 Step2 分区。
        # 各 _phase_* 方法的 thinking/step 事件正常推送（不抑制），由外层阶段顺序保证时序。
        from .operation_orchestrator import OperationOrchestrator

        # §4 ValidateAgent 接入（6 步降级路径,V2 已在 Step2 SubAgent 内接入）
        # 字段层(6项)+FK 拓扑层+ask_user 交互反问；skipped 子任务过滤后再建 partitions
        # env 开关 CODEMAKER_6STEP_VALIDATE（默认关）控制 6 步路径接入,避免破坏 6 步现状
        # V2 路径（CODEMAKER_EXCEL_PIPELINE_V2=1）不受此开关影响（Step2 内接入）
        if (not _4step_parsed
                and os.getenv("CODEMAKER_6STEP_VALIDATE", "0") != "0"
                and self._validator_agent is not None
                and len(intents) > 1):
            try:
                from ..schema_bundle import build_data_getter, _stem_to_path
                def _6step_sg(intent):
                    stem = getattr(intent, "table_hint", "") or ""
                    path = _stem_to_path(self, stem)
                    sheet = getattr(intent, "sheet_hint", "") or ""
                    # §sheet 一致性（同 _sg）：sheet_hint 空时与执行同源 _resolve_sheet
                    if path is not None and not sheet and stem:
                        try:
                            sheet = self._resolve_sheet(path, intent) or ""
                        except Exception:
                            sheet = ""
                    if path is None or not sheet:
                        return [], []
                    try:
                        headers = self.cli.read_header(path, sheet) if hasattr(self.cli, "read_header") else []
                        type_row = self.cli.read_type_row(path, sheet) if hasattr(self.cli, "read_type_row") else []
                        return list(headers or []), list(type_row or [])
                    except Exception:
                        return [], []
                _6step_dg = build_data_getter(
                    self, intents,
                    sheet_resolver=lambda p, i: self._resolve_sheet(p, i))
                _vr6 = self._validator_agent.validate_two_layer(
                    intents, schema_getter=_6step_sg,
                    data_getter=_6step_dg, locator_result=None)
                if _vr6.get("tips"):
                    _stream_res.add_thinking("校验",
                        f"6-Step ValidateAgent: {len(_vr6['tips'])} issues")
                    # P23：遗留 tips → intent.failures 软失败，保 D6 上报
                    try:
                        from ..subagent.validator_agent import attach_tips_as_soft_failures
                        _n_soft6 = attach_tips_as_soft_failures(intents, _vr6.get("tips") or [])
                        if _n_soft6:
                            _stream_res.add_thinking("校验",
                                f"P23 {_n_soft6} 条 tips 转软失败上报")
                    except Exception:
                        logger.debug("attach_tips_as_soft_failures 失败(6-step)", exc_info=True)
                if not _vr6.get("ok"):
                    _stream_res.add_thinking("校验",
                        f"6-Step ValidateAgent 未通过,用户回复={_vr6.get('user_reply')}")
                    # 不静默丢弃 skipped 子任务（用户要求不少指令），保留待 Step5 修复。
                    # 清除 skipped 标记让占位符 gate / ask / ReplanAgent 就地修复。
                    _skipped6 = [it for it in intents
                                 if getattr(it, "validation", None) and it.validation.skipped]
                    if _skipped6:
                        _skip_desc6 = ", ".join(
                            f"{getattr(it,'table_hint','') or ''}/"
                            f"{getattr(it,'sheet_hint','') or ''}"
                            for it in _skipped6)
                        for it in _skipped6:
                            v = getattr(it, "validation", None)
                            if v is not None:
                                v.skipped = False
                        _stream_res.add_thinking("校验",
                            f"{len(_skipped6)} 条子任务有硬 issue，保留待 Step5 修复"
                            f"（不丢弃）: {_skip_desc6}")
            except Exception:
                logger.warning("6-Step validate_two_layer 失败", exc_info=True)

        all_ok = True
        all_steps: list[AgentStep] = []
        all_messages: list[str] = []
        all_result_rows: list[dict] = []
        sub_tasks: list[dict] = []
        main_intent = intents[0]
        main_final = None
        main_stem = ""
        main_sheet = ""

        # 每个子任务一个独立 res（挂流式 sink），阶段事件正常推送
        # partitions[i] 对齐 intents[i]（原序）；Step5 执行按 topo_order 重排
        # 核心7:拓扑排序前移到 partition 构建前(原在 Step5 执行阶段),
        # 让 Step2 校验时依赖序已知,Step3 执行直接按序写不重排。
        from .produces_inference import infer_produces_consumes
        infer_produces_consumes(intents)
        ordered_idx = OperationOrchestrator._topo_order(intents)
        # 子任务数供 verify-repair 自适应轮数（_adaptive_rounds）引用
        self._n_subtasks = len(ordered_idx)
        partitions: list[dict] = []
        # 按 topo 序构建 partitions(执行序=partitions 顺序,Step5 无需再重排)
        for _orig_i in ordered_idx:
            intent = intents[_orig_i] if _orig_i < len(intents) else None
            if intent is None:
                continue
            res = self._wire_sinks(AgentResult(intent=intent))
            res.session_id = session_id
            # P23：pre-validate 遗留 tips 软失败 transfer 到 res.failures，
            # 让 all_failures 聚合（4601）+ _phase_summarize（6854）上报，保 D6
            _intent_failures = getattr(intent, "failures", None)
            if _intent_failures:
                res.failures.extend(_intent_failures)
            partitions.append({
                "idx": _orig_i, "intent": intent, "res": res,
                "path": None, "sheet": None,
                "executed": False, "out": None, "skipped": False,
            })

        # 按拓扑顺序排序（有占位符依赖的排后面）；无依赖时为原序
        # 原则9/R8：关系图驱动 produces 推断——对无 splitter 模板的跨表链
        # （pet/mail/...）自动挂 produces + <new_X> 占位符，让通用 topo 引擎闭环，
        # 替代 per-template 硬编码 produces（splitter 已标注的保留）
        # 核心7:infer_produces_consumes + _topo_order 已前移到 partition 构建前

        # Step2: 所有子任务分区（表/sheet 定位）——各子任务独立，可并行
        _stream_res.add_thinking("分区", f"Step2: 开始 {len(partitions)} 个子任务的表/sheet 定位")
        max_w = max(1, int(os.getenv("CODEMAKER_PHASE_MAX_WORKERS", "3") or "3"))
        if max_w > 1 and len(partitions) > 1:
            with ThreadPoolExecutor(max_workers=min(max_w, len(partitions))) as pool:
                fut_to_p = {
                    pool.submit(self._phase_partition, p["intent"], p["res"]): p
                    for p in partitions
                }
                for fut in as_completed(fut_to_p):
                    p = fut_to_p[fut]
                    try:
                        path, sheet = fut.result()
                        p["path"], p["sheet"] = path, sheet
                    except Exception:
                        p["path"], p["sheet"] = None, None
        else:
            for p in partitions:
                path, sheet = self._phase_partition(p["intent"], p["res"])
                p["path"], p["sheet"] = path, sheet

        # Step3+Step4: 计划+校验合并并行——同子任务 Step3 后 Step4，跨子任务可并行
        # 各子任务读表头+LLM，无写操作，线程安全（res 独立、cli 只读）
        ready_p = [p for p in partitions if p["path"] and p["sheet"]]
        _stream_res.add_thinking("计划", f"Step3: 开始 {len(ready_p)} 个子任务的操作计划")
        _stream_res.add_thinking("校验", f"Step4: 开始 {len(ready_p)} 个子任务的写前校验")

        def _plan_and_validate(p):
            intent = p["intent"]
            if self._merge_applicable(intent):
                p["path"], p["sheet"] = self._phase_plan_validate_merged(
                    intent, p["path"], p["sheet"], p["res"])
            else:
                self._phase_plan(intent, p["path"], p["sheet"], p["res"])
                self._phase_validate(intent, p["path"], p["sheet"], p["res"])

        if max_w > 1 and len(ready_p) > 1:
            with ThreadPoolExecutor(max_workers=min(max_w, len(ready_p))) as pool:
                futs = [pool.submit(_plan_and_validate, p) for p in ready_p]
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception:
                        logger.warning("Step3/4 并行执行异常", exc_info=True)
        else:
            for p in ready_p:
                _plan_and_validate(p)

        # Step5: 按拓扑顺序执行写库（含占位符替换 + 产出ID累积）
        _stream_res.add_thinking("执行", f"Step5: 开始按依赖顺序执行 {len(partitions)} 个子任务")
        if OperationOrchestrator.has_dependencies(intents):
            _stream_res.add_thinking("执行", "检测到跨表依赖（占位符引用前序新增 ID），按依赖顺序编排执行")
        produced: dict[str, str] = {}
        seq_counter: dict[str, int] = {}
        # O14 P27 resume：从 checkpoint 的 execution 重算 produced（跳过已成功 op，
        # 其行已写盘 PK 已在 checkpoint execution.result_rows），供后续未完成 op 占位符
        # 替换。filter ordered_idx 去掉 completed_op_keys。若非 resume 则 completed 空集
        # → ordered_idx 不变（向后兼容）。重算用 OperationOrchestrator._capture_produced
        # 逐个跑（与主循环同一捕获逻辑，确保 produced key 命名一致）。
        if _resumed_completed:
            for _ci in list(_resumed_completed):
                if _ci >= len(partitions):
                    continue
                _cp = partitions[_ci]
                _cres = _cp.get("res")
                _cintent = _cp.get("intent")
                _cexec = getattr(_cintent, "execution", None) if _cintent else None
                # 执行态回填到 res（_capture_produced 读 res.result_rows，
                # checkpoint 已序列化 execution 但 res 是新对象 → 从 execution 回填）
                if _cexec is not None and _cres is not None:
                    try:
                        _rows = getattr(_cexec, "result_rows", None) or []
                        if _rows and not getattr(_cres, "result_rows", None):
                            _cres.result_rows = list(_rows)
                        OperationOrchestrator._capture_produced(
                            _cres, _cintent, produced, seq_counter)
                        _cp["executed"] = True
                        _cp["out"] = _cexec
                    except Exception:
                        logger.debug("O14 resume 重算 produced 失败 idx=%s", _ci, exc_info=True)
            ordered_idx = [i for i in ordered_idx if i not in _resumed_completed]
            if ordered_idx:
                _stream_res.add_thinking("续跑",
                    f"Step5 跳过 {len(_resumed_completed)} 已成功 op，"
                    f"剩余 {len(ordered_idx)} 个待执行")
        # 复杂链事务语义（D2 修订）：
        # - ok=True：成功，捕获 produced，继续。
        # - ok=None（验证未结论/重试中）：软失败——行可能已写入，捕获 produced，
        #   不中断事务，继续后续子任务。原 D2 把 None 视为失败，复杂指令嵌套字段
        #   （effect.data.3006.conv_id / options[0] 等）验证常不结论 → 整链卡死。
        # - ok=False（硬失败，非 partial）：标 broken_producers，仅跳过依赖它的
        #   后续子任务；独立子任务继续。G8 回滚仅回滚失败步直接依赖的前序 op。
        broken_producers: set[int] = set()
        failed_tables: list[str] = []
        # 依赖图：deps[i] = i 依赖的 producer 下标集合
        _deps_map = OperationOrchestrator._compute_deps(intents)
        # Step5 执行追踪：print + logger 双通道（不推 SSE，避免加重前端流速）。
        # 服务端 stdout 实时可见每步进度，便于定位卡死在哪步。
        import time as _t, os as _os
        _step5_trace = bool(_os.getenv("CODEMAKER_STEP5_TRACE", "1") not in ("0", "", "false", "False"))
        def _step5_log(msg: str) -> None:
            if _step5_trace:
                ts = _t.strftime("%H:%M:%S")
                line = f"[Step5 {ts}] {msg}"
                print(line, flush=True)
                logger.info(line)
        _step5_log(f"开始执行 {len(ordered_idx)} 个子任务（依赖顺序），跨表依赖={OperationOrchestrator.has_dependencies(intents)}")
        # 子任务级 SSE：经 agent_service 注入的 _agent_subtask_sink 推送 subtask_start/done，
        # 前端据此增量渲染卡片骨架（消除"空白转圈"）。sink 为空时静默降级（CLI/eval 路径）。
        _subtask_sink = getattr(self, "_agent_subtask_sink", None)
        def _emit_subtask(event: str, data: dict) -> None:
            if _subtask_sink is None:
                return
            try:
                _subtask_sink(event, data)
            except Exception:
                logger.debug("subtask sink 推送失败", exc_info=True)
        def _llm_calls() -> int:
            c = getattr(self, "_llm_counter", None)
            return c.peek_total() if c is not None else 0
        _cancelled_flag = False
        for _step5_i, orig_idx in enumerate(ordered_idx):
            _ce = getattr(self, "_cancel_event", None)
            if _ce is not None and _ce.is_set():
                _step5_log(f"  → 用户取消：中断子任务循环（已完成 {_step5_i}/{len(ordered_idx)}）")
                all_messages.append(f"⚠️ 用户已中断：仅完成 {_step5_i}/{len(ordered_idx)} 个子任务")
                _cancelled_flag = True
                break
            p = partitions[orig_idx]
            intent, res = p["intent"], p["res"]
            path, sheet = p["path"], p["sheet"]
            _step5_log(f"[{_step5_i+1}/{len(ordered_idx)}] idx={orig_idx} table={getattr(intent,'table_hint','')} sheet={sheet} action={intent.action} path={path}")
            _emit_subtask("subtask_start", {
                "idx": _step5_i + 1, "total": len(ordered_idx),
                "table": getattr(intent, "table_hint", "") or "",
                "action": intent.action if intent else "",
                "llm_calls": _llm_calls(),
            })
            if not path or not sheet:
                _step5_log(f"  → 跳过：path/sheet 为空（Step2 已失败）")
                _emit_subtask("subtask_done", {
                    "idx": _step5_i + 1, "total": len(ordered_idx),
                    "table": getattr(intent, "table_hint", "") or "",
                    "ok": False, "skipped": True, "llm_calls": _llm_calls(),
                    "message": "path/sheet 为空（Step2 已失败）",
                })
                continue  # Step2 已失败，跳过执行（不中断后续无关任务）
            # Step4 hard gate：缺行定位值等早失败，不进 Step5（避免 verify-repair 浪费 LLM）
            if getattr(res, "_hard_blocked", False):
                res.message = res.message or f"{intent.action} 操作缺少行定位值，已拒绝"
                _step5_log(f"  → 跳过：Step4 hard gate 拒绝（{intent.action} 缺行定位值）")
                _emit_subtask("subtask_done", {
                    "idx": _step5_i + 1, "total": len(ordered_idx),
                    "table": getattr(intent, "table_hint", "") or "",
                    "ok": False, "skipped": True, "llm_calls": _llm_calls(),
                    "message": "Step4 拒绝：缺少行定位值",
                })
                continue
            # 仅跳过依赖了已失败 producer 的子任务；独立子任务继续执行
            _blocked_by = _deps_map.get(orig_idx, set()) & broken_producers
            if _blocked_by:
                res.ok = False
                res.message = "依赖的前序产出失败，此步骤被跳过"
                res.add("transaction_rollback", False,
                        f"跳过：依赖的前序 producer {_blocked_by} 已失败（独立任务不受影响）")
                p["skipped"] = True
                _step5_log(f"  → 跳过：依赖 producer {_blocked_by} 已失败（独立任务不受影响）")
                _emit_subtask("subtask_done", {
                    "idx": _step5_i + 1, "total": len(ordered_idx),
                    "table": getattr(intent, "table_hint", "") or "",
                    "ok": False, "skipped": True, "llm_calls": _llm_calls(),
                    "message": "依赖的前序产出失败，此步骤被跳过",
                })
                continue
            # 占位符替换：用前序 produced 替换 intent 中的 <xxx>
            if produced:
                _before_fields = dict((intent.extras or {}).get("fields", {}))
                OperationOrchestrator._resolve_placeholders(intent, produced)
                _after_fields = (intent.extras or {}).get("fields", {})
                _replaced = {k: v for k, v in _after_fields.items()
                             if k in _before_fields and _before_fields[k] != v}
                if _replaced:
                    _step5_log(f"  → 占位符替换：{list(_replaced.keys())} consumed from produced={list(produced.keys())}")
            _step5_t0 = _t.perf_counter()
            # O21 健壮性：单 op 异常不中断后续 op。_phase_execute 内部 try/except 仅
            # rollback 后 raise（agent.py:6046），异常会逃出 Step5 循环致后续 op 全弃。
            # 包 try/except 兜异常 → failure 上报（保 D6）+ broken_producers 标记 +
            # continue 下一 op（独立任务不受影响）。
            try:
                out = self._phase_execute(intent, path, sheet, res, confirm_token)
            except Exception as _dispatch_exc:
                _step5_log(f"  → ✗ 执行异常：{type(_dispatch_exc).__name__}: {_dispatch_exc}")
                logger.warning("Step5 子任务执行异常 idx=%s table=%s",
                               orig_idx, getattr(intent, "table_hint", ""), exc_info=True)
                out = None
                res.ok = False
                res.message = f"执行异常：{type(_dispatch_exc).__name__}: {_dispatch_exc}"
                try:
                    res.failures.append({
                        "type": "dispatch_exception",
                        "table": getattr(intent, "table_hint", "") or (path.stem if path else ""),
                        "sheet": sheet or "",
                        "col": "",
                        "root_cause": f"{type(_dispatch_exc).__name__}: {_dispatch_exc}",
                        "attempted_strategies": ["direct_dispatch"],
                        "suggestion": "检查指令/表结构；重试或联系排查",
                        "status": "unresolved", "user_reply": None,
                    })
                except Exception:
                    pass
                broken_producers.add(orig_idx)
                tbl = getattr(intent, "table_hint", "") or f"intent_{orig_idx}"
                if tbl not in failed_tables:
                    failed_tables.append(tbl)
                if hasattr(res, "dirty_data"):
                    res.dirty_data = True
                res.add("transaction_rollback", False,
                        f"子任务执行异常：{tbl}（独立任务不受影响，继续后续）")
                _emit_subtask("subtask_done", {
                    "idx": _step5_i + 1, "total": len(ordered_idx),
                    "table": getattr(intent, "table_hint", "") or "",
                    "ok": False, "skipped": False, "llm_calls": _llm_calls(),
                    "message": f"执行异常：{type(_dispatch_exc).__name__}",
                })
                continue
            _step5_dur = (_t.perf_counter() - _step5_t0) * 1000
            p["executed"] = True
            p["out"] = out
            _out_ok = getattr(out, "ok", None)
            _out_msg = (out.message if out is not None else None) or ""
            _step5_log(f"  → 执行完成 耗时={_step5_dur:.0f}ms ok={_out_ok} msg={_out_msg[:120]}")
            # 收集成功 op 的 backup（供链失败时回滚前序已 commit 行）
            if _out_ok is True:
                p["backup"] = getattr(res, "_commit_backup", None)
            # partial=True（行已写入但缺值列待补）：软成功，捕获 produced，不中断
            _is_partial = bool(getattr(res, "partial", False))
            # 硬失败：ok is False 且非 partial → 标 broken + G8 回滚直接依赖的前序
            if _out_ok is False and not _is_partial:
                broken_producers.add(orig_idx)
                tbl = getattr(intent, "table_hint", "") or f"intent_{orig_idx}"
                failed_tables.append(tbl)
                if hasattr(res, "dirty_data"):
                    res.dirty_data = True
                res.add("transaction_rollback", False,
                        f"子任务硬失败：{tbl} 写后验证未通过，仅跳过依赖它的后续意图")
                _step5_log(f"  → ✗ 硬失败：table={tbl} dirty_data={getattr(res,'dirty_data',False)}，依赖它的后续将跳过")
                # G8 链回滚：仅回滚失败步直接依赖的前序已 commit op（避免半成品孤儿行）。
                # 原"回滚全部前序"会牵连无关独立 op，复杂链下误伤严重。
                # P26 strict（CODEMAKER_BATCH_TRANSACTIONAL=1）：回滚整批前序已 commit op，
                # 批级原子（任一失败全回滚，免重跑 UNIQUE_VIOLATION）。
                _txn_mode, _rollback_targets = self._compute_rollback_targets(
                    orig_idx, partitions, _deps_map, self.batch_transactional,
                    OperationOrchestrator.has_dependencies(intents))
                rolled = []
                for _prod_idx in _rollback_targets:
                    prev_p = partitions[_prod_idx]
                    bk = prev_p.get("backup")
                    if bk and not prev_p.get("rolled_back"):
                        try:
                            self._rollback_write(Path(bk[0]), bk[1], prev_p["res"])
                            prev_p["rolled_back"] = True
                            rolled.append(prev_p["intent"].table_hint or "")
                        except Exception:
                            logger.warning("G8 链回滚失败 %s", bk[0], exc_info=True)
                if rolled:
                    res.add_thinking("回滚",
                                     f"{_txn_mode} 已回滚前序 op：{rolled}（避免半成品残留）")
            else:
                # ok=True / ok=None / partial：行可能已写入 → 捕获 produced 供后续消费
                _before_keys = set(produced.keys())
                OperationOrchestrator._capture_produced(res, intent, produced, seq_counter)
                _new_keys = set(produced.keys()) - _before_keys
                if _new_keys:
                    _step5_log(f"  → ✓ 产出ID：{dict((k, produced[k]) for k in _new_keys)}")
                elif _out_ok is None:
                    _step5_log(f"  → ◐ 验证未结论（ok=None），软通过未中断事务")
                else:
                    _step5_log(f"  → ✓ 成功（无新产出ID）")
                # O14：Step5 增量回写 checkpoint（每成功 op 后），stall 后 resume 可
                # 跳过更多已成功 op。回写 intents 含 execution（to_checkpoint_dict 序列化）
                # + 累积的 completed_op_keys（resume 已跳过 + 本轮已成功）。opt-in 默认 off。
                if _out_ok is not False and session_id:
                    try:
                        # execution 回填到 intent（_capture_produced 写 res.result_rows，
                        # checkpoint 序列化读 intent.execution → 需把 res 执行态映射到 intent）
                        from ..parser.nl_parser import ExecutionResult
                        if getattr(intent, "execution", None) is None:
                            intent.execution = ExecutionResult(
                                ok=_out_ok, row=getattr(res, "row", None),
                                written_fields=getattr(res, "written_fields", None),
                                new_row_pk=getattr(res, "new_row_pk", None),
                                failure=getattr(res, "failure", None),
                                raw=getattr(res, "raw", None))
                        intent.execution.result_rows = list(getattr(res, "result_rows", None) or [])
                        _all_completed = set(_resumed_completed) | {orig_idx}
                        # 当前轮已成功 op 累积（resume_completed + 本轮走到此处的成功 op）
                        _cur_completed = [i for i in range(len(intents))
                                          if i in _all_completed or
                                          (partitions[i].get("executed") and
                                           partitions[i].get("out") is not None and
                                           getattr(partitions[i]["out"], "ok", None) is not False)]
                        self._save_nl_progress(
                            session_id, intents, _cur_completed)
                    except Exception:
                        logger.debug("O14 _save_nl_progress 失败 idx=%s", orig_idx, exc_info=True)
            _emit_subtask("subtask_done", {
                "idx": _step5_i + 1, "total": len(ordered_idx),
                "table": getattr(intent, "table_hint", "") or "",
                "ok": _out_ok, "skipped": False, "llm_calls": _llm_calls(),
                "dur_ms": int(_step5_dur), "message": _out_msg[:200],
            })
        _step5_log(f"Step5 结束：共 {len(ordered_idx)} 个子任务，硬失败={len(failed_tables)} failed_tables={failed_tables}")

        # Step5.5: 前向引用 backfill —— 循环依赖链（conv→option→conv）在主循环中
        # 因拓扑环回退原序，conv 行的 options[0]/options[1] 引用尚未产出的 option_id，
        # 写入时占位符未解析被跳过。主循环结束后 produced 已齐全，回扫各 add 行补写。
        self._backfill_forward_refs(partitions, produced, _step5_log, confirm_token)

        # O22 §9.1 replan-on-failure：Step5 + backfill 后、Step6 前扫前。
        # 批级失败聚合 → ReplanAgent LLM 重规划剩余 op → 重跑 Step5（补建/改字段）。
        # 门控 CODEMAKER_REPLAN_ON_FAILURE=0 默认关（与 §D4 一致，增量 LLM 触发点默认关）。
        # 上限 replan_max_rounds()=2 防 LLM 死循环。replan 产空/失败 → 降级走原 Step6 上报。
        self._run_replan_phase(partitions, intents, produced, ordered_idx,
                               broken_producers, failed_tables, text, session_id,
                               confirm_token, _step5_log, _emit_subtask, _llm_calls,
                               all_messages, all_result_rows, all_steps)

        # Step6 前扫：result_rows 残留占位符 = 引用未解析 = 功能未接通（如实计入 summary）
        # _backfill 之后仍残留 <...> 说明前向引用未解析成功，功能链未闭环（如 reward/quest/conv_id 空引）
        _broken_links: dict[tuple[str, str], list[dict]] = {}
        for _pi, p in enumerate(partitions):
            if not p.get("executed"):
                continue
            _intent = p.get("intent")
            _res = p.get("res")
            _rows = getattr(_res, "result_rows", None) or []
            _tk = (getattr(_res, "table_stem", "") or "",
                   getattr(_res, "table_sheet", "") or "")
            for r in _rows:
                if not isinstance(r, dict):
                    continue
                nv = r.get("new_value")
                sv = "" if nv is None else str(nv).strip()
                if sv and sv.startswith("<") and sv.endswith(">"):
                    _broken_links.setdefault(_tk, []).append({
                        "idx": _pi + 1,
                        "col": r.get("col_name") or r.get("col", ""),
                        "placeholder": sv,
                    })
                    break
        if _broken_links:
            _link_lines = []
            for (_bt, _bs), _items in _broken_links.items():
                for _it in _items:
                    _link_lines.append(f"- 子任务#{_it['idx']} 表{_bt}/{_bs} 列{_it['col']} 占位符残留 {_it['placeholder']}")
            _link_msg = "⚠️ 以下引用未解析（功能未接通，已写盘但功能链未闭环）：\n" + "\n".join(_link_lines)
            all_messages.append(_link_msg)
            _stream_res.add_thinking("汇总", f"检测到 {len(_broken_links)} 个子任务功能未接通（占位符残留）")

        # 第三层B 深度校验：FK 列已解析为具值，但指向的目标行可能并不存在
        # （如 reward_id=10088 引用了一个未建/已删的行）。占位符残留是强信号，此处补
        # "指向行存在性"深度校验：produced 本批产出 ∪ 写盘 read-back 双路核验，避免误报。
        # O21 影响结果才阻塞：悬空 FK（指向不存在的行）影响结果 → 默认 on 阻塞 ask 用户
        # 补建。原 opt-in（CODEMAKER_CONNECTIVITY_DEEP_CHECK=0 关）致影响结果的悬空 FK
        # 不检测不阻塞。改默认 on，env=off 显式关闭（向后兼容降级路径）。
        _dangling_fk: dict[tuple[str, str], list[dict]] = {}
        if os.environ.get("CODEMAKER_CONNECTIVITY_DEEP_CHECK", "1") != "off":
            try:
                _dangling_fk = self._check_dangling_fk_refs(partitions, produced)
            except Exception:
                logger.warning("指向行存在性深度校验异常（已降级跳过）", exc_info=True)
                _dangling_fk = {}
            if _dangling_fk:
                _dlines = []
                _dcnt = 0
                for (_bt, _bs), _items in _dangling_fk.items():
                    for _it in _items:
                        _dcnt += 1
                        _dlines.append(f"- 子任务#{_it['idx']} 表{_bt}/{_bs} 列{_it['col']}={_it['value']} 指向 {_it['target']} 行不存在")
                all_messages.append("⚠️ 以下外键引用指向不存在的行（功能未接通）：\n" + "\n".join(_dlines))
                _stream_res.add_thinking("汇总", f"深度校验检测到 {_dcnt} 个悬空外键引用")
                # 中断反问：悬空 FK，让用户补建目标行续跑
                _ask = getattr(self, "_ask_callback", None)
                if _ask is not None:
                    _fr = _ask({
                        "reason": "深度校验发现外键指向不存在的行，已暂停",
                        "table": "", "sheet": "",
                        "failed_col": "", "failed_val": "",
                        "root_cause": "\n".join(_dlines),
                        "attempted_strategies": "已尝试：产出结果 + 回读 双路核验",
                        # O21 表格交互：suggestion 引导走 field 模式填表格（补建目标
                        # 行的 PK 值），而非补自然语言指令。failed_col/failed_val
                        # 已携带待补建的列/值结构化数据，用户填表即可。
                        "suggestion": (
                            "在下方表格填入需补建的目标行的主键值"
                            f"（列「{_it['col']}」待指向值「{_it['value']}」）；"
                            "或点「跳过」放弃此项继续后续任务。"
                        ),
                        "example": f"{_it['col']}填「（此处填目标主键值）」",
                        "snip": (getattr(main_intent, "raw", "") or "")[:120],
                        # 要求 B：大白话 reason + action
                        "user_friendly": {
                            "reason": (f"你引用的内容在对应表里找不到"
                                       f"（列「{_it['col']}」指向「{_it['value']}」，但那张表没这行）。"),
                            "action": ("请先去建被引用的那行数据，或换一个已经存在的编号；"
                                       "也可点「跳过」放弃此项。"),
                        },
                    }) or {}
                    if _fr.get("mode") == "nl":
                        _ft = (_fr.get("text") or "").strip()
                        if _ft and self.parser is not None:
                            try:
                                _fis = (self.parser.parse_multi(_ft)
                                        if hasattr(self.parser, "parse_multi")
                                        else [self.parser.parse(_ft)]) or []
                            except Exception:
                                _fis = []
                            for _fi in _fis:
                                try:
                                    _sr = self._run_single(_fi, None, session_id)
                                    if _sr is not None:
                                        all_messages.append(f"✅ 已补建：{_sr.message or ''}")
                                        if getattr(_sr, "result_rows", None):
                                            all_result_rows.extend(_sr.result_rows)
                                except Exception:
                                    logger.warning("悬空 FK 补建执行失败", exc_info=True)
                                    all_messages.append("❌ 补建执行异常")

        # Step6: 汇总所有子任务结果
        _stream_res.add_thinking("汇总", f"Step6: 开始汇总 {len(partitions)} 个子任务结果")
        # 各子任务 per-task 汇总（事件正常推送 + res.message 置 AI 总结）
        for p in partitions:
            if p["executed"] and p["path"] and p["sheet"]:
                self._phase_summarize(p["intent"], p["path"], p["sheet"], p["res"], p["out"])
        # 全局 AI 汇总：所有子任务结果 → 一段整体总结
        global_summary = None
        if self._ai_enhancer is not None:
            results_for_ai = []
            for p in partitions:
                if not p["executed"]:
                    continue
                out = p["out"]
                res = p["res"]
                _tk = (res.table_stem or (p["path"].stem if p["path"] else ""),
                       res.table_sheet or (p["sheet"] or ""))
                _broken = _broken_links.get(_tk) or _dangling_fk.get(_tk)
                _detail = (out.message if out is not None else res.message) or "操作完成"
                if _broken:
                    _bparts = []
                    for it in _broken:
                        if "placeholder" in it:
                            _bparts.append(f"列{it['col']}={it['placeholder']}")
                        else:
                            _bparts.append(f"列{it['col']}={it['value']}→指向{it['target']}不存在")
                    _detail = _detail + " ⚠️功能未接通：" + "；".join(_bparts)
                results_for_ai.append({
                    "table": res.table_stem or (p["path"].stem if p["path"] else ""),
                    "sheet": res.table_sheet or (p["sheet"] or ""),
                    "action": p["intent"].action if p["intent"] else "",
                    "ok": bool(getattr(out, "ok", False)) and not _broken,
                    "detail": _detail,
                    "row": getattr(out, "row", "") if out is not None else "",
                })
            if results_for_ai:
                try:
                    global_summary = self._ai_enhancer.ai_summarize(
                        results=results_for_ai, user_text=text)
                    if global_summary:
                        _stream_res.add_thinking("汇总", f"AI 全局总结：{global_summary[:80]}")
                except Exception:
                    logger.warning("Step6 AI 全局汇总失败，降级走各任务模板", exc_info=True)
        # D5 ConcludeAgent：批量级自学习闭环（聚合全 failure → AI 归纳反模式）
        self._phase_conclude(partitions, text, _stream_res)
        # 证据写盘（保留原 _run_single finally 行为：非事务跳过的子任务都写）
        for p in partitions:
            if p["skipped"]:
                continue
            try:
                self._log_evidence(p["res"], p["intent"],
                                   user_text=p["intent"].raw if p["intent"] else "")
            except Exception:
                logger.warning("子任务证据写盘失败（不影响主流程）", exc_info=True)

        # 聚合结果（按原序输出 sub_tasks，对齐 intents）
        all_needs_user_fill: list[dict] = []
        any_partial = False
        all_failures: list[dict] = []
        for p in partitions:
            orig_idx, intent, res = p["idx"], p["intent"], p["res"]
            all_steps.extend(res.steps)
            all_messages.append(f"[{orig_idx + 1}] {res.message}")
            all_result_rows.extend(res.result_rows)
            # C 方案：聚合子任务 needs_user_fill 到顶层
            if getattr(res, "needs_user_fill", None):
                all_needs_user_fill.extend(res.needs_user_fill)
            # #40：聚合子任务结构化失败清单到顶层（供 done payload + 前端渲染）
            if getattr(res, "failures", None):
                all_failures.extend(res.failures)
            if getattr(res, "partial", False):
                any_partial = True
            # 保留子任务分组：steps/result_rows/table 仍各属其主，前端可分段渲染
            sub_tasks.append({
                "index": orig_idx + 1,
                "intent_action": intent.action if intent else "",
                "ok": res.ok,
                "message": res.message,
                "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail}
                          for s in res.steps],
                "result_rows": list(res.result_rows),
                "table_stem": res.table_stem,
                "table_sheet": res.table_sheet,
                "needs_user_fill": list(getattr(res, "needs_user_fill", [])),
                "partial": getattr(res, "partial", False),
            })
            if not main_stem and res.table_stem:
                main_stem = res.table_stem
            if not main_sheet and res.table_sheet:
                main_sheet = res.table_sheet
            if res.ok is not True:
                all_ok = False
            if res.final is not None:
                main_final = res.final

        msg = global_summary if global_summary else "\n".join(all_messages)
        # #38 失败清单：global AI 总结偏叙事、常吞掉具体失败原因；有结构化失败时
        # 显式拼一段失败清单附在 summary 末尾，确保用户能看到「哪张表/哪列/为何/试过啥」。
        if all_failures:
            _flines = []
            for _i, _f in enumerate(all_failures, 1):
                _floc = f"{_f.get('table','')}/{_f.get('sheet','')}"
                _fcol = f" 列[{_f.get('col')}]" if _f.get("col") else ""
                _frc = _f.get("root_cause") or "未知"
                _fstr = ""
                if _f.get("attempted_strategies"):
                    _fstr = f"（已试：{_f['attempted_strategies']}）"
                _freply = ""
                if _f.get("user_reply"):
                    _freply = f"；用户回复={_f['user_reply']}"
                _flines.append(
                    f"{_i}. {_floc}{_fcol} 失败：{_frc}{_fstr}{_freply}")
            _fblock = "❌ 失败清单（" + str(len(all_failures)) + " 项未解决）：\n" + "\n".join(_flines)
            msg = (msg + "\n\n" if msg else "") + _fblock
        # F1: AI 意图校验非阻断建议如实列入 summary（用户下一轮补描述）
        if _stream_res.ai_suggestions:
            _sug = "；".join(dict.fromkeys(_stream_res.ai_suggestions))
            msg = (msg + "\n\n" if msg else "") + f"💡 AI 校验建议补充：{_sug}（可在下一轮补描述）"
        # C 方案：partial 时 message 追加缺值列提示，供前端展示
        if all_needs_user_fill:
            fill_cols = "、".join(sorted({f["col"] for f in all_needs_user_fill}))
            msg = f"{msg}\n\n⚠️ 以下列已写入行但缺值待补：{fill_cols}"
        if _cancelled_flag:
            msg = (msg + "\n\n" if msg else "") + "⚠️ 执行已由用户中断（部分子任务未完成）"
        return AgentResult(
            ok=all_ok,
            intent=main_intent,
            steps=all_steps,
            final=main_final,
            message=msg,
            result_rows=all_result_rows,
            table_stem=main_stem,
            table_sheet=main_sheet,
            sub_tasks=sub_tasks,
            needs_user_fill=all_needs_user_fill,
            partial=any_partial,
            failures=all_failures,
            ai_suggestions=list(_stream_res.ai_suggestions),
        )

    def _run_confirmed(self, confirm_token: str, text: str = "",
                       cascade: bool = True) -> Optional[AgentResult]:
        """按 confirm_token 直接执行已确认的危险操作，跳过 NL 重解析。

        token 形如 `delete:{stem}:{sheet}:{row}` 或 `col_delete:{stem}:{sheet}:{col}`，
        编码了首轮定位出的确切目标。无法识别的 token 返回 None（交回常规流程）。

        cascade: 仅对整行删除有效。True=确认级联删除关联数据；False=仅删当前行。

        search 类 token：`search:{stem}:{sheet}:{col_idx}:{value}`（value 可能含冒号，
        故按 4 段 split 后剩余合并）。表示用户确认跨表搜索 value。
        """
        parts = confirm_token.split(":")
        if len(parts) < 4:
            return None
        kind = parts[0]
        # search token：value 可能含冒号，合并第4段之后
        if kind == "search":
            stem = parts[1]
            sheet = parts[2]
            try:
                col_idx = int(parts[3])
            except (TypeError, ValueError):
                return None
            value = ":".join(parts[4:]) if len(parts) > 4 else ""
            return self._execute_confirmed_cross_search(stem, sheet, col_idx, value, text)
        # ambig_delete token：删除歧义命中的多行（第4段为逗号分隔行号列表）
        # 用户确认 = 删除全部候选行；降序删以保持行号稳定。
        if kind == "ambig_delete":
            stem = parts[1]
            sheet = parts[2]
            rows_csv = parts[3] if len(parts) > 3 else ""
            _amb_rows: list[int] = []
            for _x in str(rows_csv).split(","):
                _x = _x.strip()
                if _x.isdigit():
                    _amb_rows.append(int(_x))
            if not _amb_rows:
                return None
            return self._execute_confirmed_ambiguous_delete(stem, sheet, _amb_rows, text)
        if len(parts) != 4:
            return None
        stem, sheet, target = parts[1], parts[2], parts[3]
        try:
            target_idx = int(target)
        except (TypeError, ValueError):
            return None

        path = self._find_table_by_stem(stem)
        if path is None:
            res = self._wire_sinks(AgentResult(ok=False, intent=NLIntent(action="delete", raw=text)))
            res.add("resolve_table", False, f"确认令牌指向的表「{stem}」不存在")
            res.message = f"确认失败：找不到表「{stem}」（可能已变更）"
            return res

        if kind == "delete":
            return self._execute_confirmed_row_delete(path, sheet, target_idx, text, cascade)
        if kind == "col_delete":
            return self._execute_confirmed_col_delete(path, sheet, target_idx, text)
        return None

    def _execute_confirmed_cross_search(self, stem: str, sheet: str,
                                       col_idx: int, value: str,
                                       text: str = "") -> AgentResult:
        """用户确认跨表搜索 → 在 top5 候选表找 value。

        流程：
          1. 跨表搜索（排除原 stem）
          2. 有完全匹配 → 返回命中位置 + 该行信息（思考记"找到正确位置"）
          3. 无完全匹配但有相近项 → 返回各表相近项汇总（思考记候选）
          4. 全空 → 返回未找到（思考记"跨表搜索无结果"）
        """
        res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="get", raw=text)))
        res.add_thinking("跨表探索", f"用户确认跨表搜索「{value}」")
        res.add_thinking("跨表探索", f"原表 {stem}/{sheet} 已排除，遍历所有其他表的名称列做 contains 匹配")
        candidates = self._cross_table_search(value, exclude_stem=stem, top_k_tables=5)
        res.cross_table_candidates = candidates
        exact_hits = [c for c in candidates if c.get("match_type") == "exact"]
        if exact_hits:
            # 完全匹配 = 正确位置，读该行返回
            hit = exact_hits[0]
            hit_stem = hit["table_stem"]
            hit_sheet = hit["sheet"]
            hit_row = hit["matches"][0]["row"]
            res.add_thinking("跨表探索",
                             f"找到完全匹配：{hit_stem}/{hit_sheet} 行{hit_row}「{value}」 — 这就是正确位置")
            res.add_thinking("执行", f"读取 {hit_stem}/{hit_sheet} 行{hit_row} 的全部列返回")
            path = self._find_table_by_stem(hit_stem)
            if path is not None:
                header = self.cli.read_header(path, hit_sheet)
                row_data = self._read_row_data(path, hit_sheet, hit_row)
                pairs = []
                for ci in range(1, len(header) + 1):
                    name = (header[ci - 1] or "").split(":")[0] if ci - 1 < len(header) else ""
                    val = row_data.get(ci)
                    if val is not None and str(val).strip() != "":
                        pairs.append((name, val))
                        self._add_result_row(res, ci, name, new_value=val)
                res.table_stem = hit_stem
                res.table_sheet = hit_sheet
                lines = "\n".join(f"  {n} = {v}" for n, v in pairs)
                res.message = (f"在 {hit_stem}/{hit_sheet} 行{hit_row} 找到「{value}」：\n{lines}")
                res.final = CLICallResult(
                    ok=True, data={"row": hit_row, "values": [{"col": n, "value": v} for n, v in pairs]})
                return res
        # 无完全匹配 → 汇总相近项
        if candidates:
            lines = []
            for c in candidates[:5]:
                mt = "完全匹配" if c["match_type"] == "exact" else "相近"
                ms = "、".join(f"{m['value']}(行{m['row']})" for m in c["matches"][:3])
                lines.append(f"  {c['table_stem']}/{c['sheet']} [{mt}]: {ms}")
            res.add_thinking("跨表探索", f"无完全匹配，相近候选：{lines}")
            res.message = (f"跨表搜索「{value}」未找到完全匹配，相近项：\n" + "\n".join(lines))
            res.ok = False
            return res
        res.add_thinking("跨表探索", "跨表搜索无任何结果")
        res.message = f"跨表搜索「{value}」未找到任何匹配项"
        res.ok = False
        return res

    def _find_table_by_stem(self, stem: str) -> Optional[Path]:
        """按 stem 精确查找表格路径（确认执行用，不做模糊/LLM 推断）。"""
        for p in self.cli.list_tables():
            if p.stem == stem:
                return p
        return None

    def _execute_confirmed_row_delete(self, path: Path, sheet: str, row: int,
                                      text: str = "", cascade: bool = True) -> AgentResult:
        """确定性执行整行删除，目标由 confirm_token 指定。

        cascade=True 时一并删除关联数据；False 时仅删当前行（用户选"取消级联"）。
        """
        res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="delete", raw=text)))
        res.table_stem = path.stem
        res.table_sheet = sheet
        res.add("resolve_table", True, str(path))
        res.add("resolve_sheet", True, sheet)

        stem = path.stem
        header = self.cli.read_header(path, sheet)
        row_data = self._read_row_data(path, sheet, row)
        if not row_data:
            res.ok = False
            res.add("delete_row", False, f"行 {row} 无数据")
            res.message = f"确认失败：{sheet} 行{row} 无数据（表格可能已变更），请重新发起删除"
            return res

        r = self.cli.delete_row(path, sheet, row)
        res.final = r
        if r.ok:
            res.add("delete_row", True, f"删除行 row={row}")
            for ci in sorted(row_data.keys()):
                self._add_result_row(res, ci, self._col_name(header, ci),
                                     old_value=row_data[ci])
            if cascade:
                self._do_cascade_delete(path, sheet, header, row_data, stem)
                res.message = f"已删除：{sheet} 行{row}（含关联数据）"
            else:
                res.message = f"已删除：{sheet} 行{row}（仅当前行，未级联）"
            self._refresh_index_after_write(path)
        else:
            res.ok = False
            res.add("delete_row", False, r.error or "")
            res.message = "删除行失败"
        return res

    def _execute_confirmed_ambiguous_delete(self, stem: str, sheet: str,
                                            rows: list[int],
                                            text: str = "") -> AgentResult:
        """确定性删除多行歧义命中（由 confirm_token=ambig_delete:... 指定）。

        契合「删除…名称为 X 的行」语义=删除全部匹配行。降序删除以保持行号稳定
        （先删大行号，小行号不受位移影响）。行可能已被变更 → 跳过无数据行。
        每行记录被删内容到 result_rows，供前端展示 + 反查。
        """
        res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="delete", raw=text)))
        res.table_stem = stem
        res.table_sheet = sheet
        path = self._find_table_by_stem(stem)
        if path is None:
            res.ok = False
            res.add("resolve_table", False, f"确认令牌指向的表「{stem}」不存在")
            res.message = f"确认失败：找不到表「{stem}」（可能已变更）"
            return res
        res.add("resolve_table", True, str(path))
        res.add("resolve_sheet", True, sheet)

        header = self.cli.read_header(path, sheet)
        deleted: list[int] = []
        last_r = None
        # 降序删除：删大行号在前，避免删小行号后下方行号位移
        for _r in sorted({ri for ri in rows if ri}, reverse=True):
            _row_data = self._read_row_data(path, sheet, _r)
            if not _row_data:
                res.add("delete_row", False, f"行 {_r} 无数据（已变更），跳过")
                continue
            _rr = self.cli.delete_row(path, sheet, _r)
            last_r = _rr if _rr is not None else last_r
            if _rr is not None and _rr.ok:
                res.add("delete_row", True, f"删除行 row={_r}")
                for ci in sorted(_row_data.keys()):
                    self._add_result_row(res, ci, self._col_name(header, ci),
                                         old_value=_row_data[ci])
                deleted.append(_r)
            else:
                _err = (_rr.error if _rr is not None else "删除失败")
                res.add("delete_row", False, f"删除行 {_r} 失败：{_err}")
        if deleted:
            res.final = last_r
            _dl = "、".join(str(r) for r in sorted(deleted))
            res.message = f"已删除：{sheet} 行 {_dl}（共 {len(deleted)} 行，歧义命中全部删除）"
            res.ok = True
            self._refresh_index_after_write(path)
        else:
            res.ok = False
            res.message = "确认失败：未删除任何行（候选行可能已被变更），请重新发起删除"
        return res

    def _execute_confirmed_col_delete(self, path: Path, sheet: str, col: int,
                                      text: str = "") -> AgentResult:
        """确定性执行整列删除，目标由 confirm_token 指定。"""
        res = self._wire_sinks(AgentResult(ok=True, intent=NLIntent(action="col", raw=text)))
        res.table_stem = path.stem
        res.table_sheet = sheet
        res.add("resolve_table", True, str(path))
        res.add("resolve_sheet", True, sheet)

        header = self.cli.read_header(path, sheet)
        col_name = self._col_name(header, col)
        r = self.cli.delete_column(path, sheet, col)
        res.final = r
        if r.ok:
            res.add("delete_column", True, f"删除列 col{col}={col_name}")
            res.message = f"已删除列：{sheet} 列{col}({col_name})"
            self._refresh_index_after_write(path)
        else:
            res.ok = False
            res.add("delete_column", False, r.error or "")
            res.message = f"删除列失败: {r.error}"
        return res

    # ── T6 证据层 helper ──
    def _fill_col_evidence(self, res: AgentResult, loc_match: Optional[ColumnMatch],
                           query: str) -> None:
        """把定位列匹配结果填入 res.col_evidence。loc_match 为 None 时跳过。"""
        if loc_match is None:
            return
        res.col_evidence = {
            "query": query or "",
            "resolved": loc_match.column,
            "index": loc_match.index,
            "source": loc_match.source,
            "score": loc_match.score,
            "candidates": [],
        }

    def _row_summary(self, path: Path, sheet: str, row: int,
                     skip_col: int = 0, limit: int = 3) -> dict[str, object]:
        """读取指定行的非空数据列前 limit 个，返回 {col_name: value}。
        skip_col 为定位列索引，跳过避免重复展示定位值。失败返回空 dict。"""
        try:
            header = self.cli.read_header(path, sheet)
            row_data = self._read_row_data(path, sheet, row)
            summary: dict[str, object] = {}
            for ci, val in row_data.items():
                if skip_col and ci == skip_col:
                    continue
                name = header[ci - 1] if 1 <= ci <= len(header) else f"col{ci}"
                name = str(name).split(":")[0].strip() if name else f"col{ci}"
                summary[name] = val
                if len(summary) >= limit:
                    break
            return summary
        except Exception:
            return {}

    def _fill_row_evidence(self, res: AgentResult, row_match, query: str,
                           path: Path = None, sheet: str = "",
                           loc_col_idx: int = 0) -> None:
        """把行定位结果填入 res.row_evidence。row_match 为 None 时跳过。

        path/sheet/loc_col_idx 给定时为 alternatives 填 summary（前 3 个非空数据列，
        跳过定位列），供前端 R15 候选卡片展示。
        """
        if row_match is None:
            return
        alts: list[dict] = []
        can_summary = path is not None and sheet != ""
        # 当前行 + 候选行合并去重
        all_rows: list[tuple[int, str, bool]] = [(row_match.row, row_match.value, True)]
        for r, v in row_match.alternatives:
            all_rows.append((r, v, False))
        seen: set[int] = set()
        for r, v, is_current in all_rows:
            if r in seen:
                continue
            seen.add(r)
            entry: dict = {"row": r, "value": v, "current": is_current}
            if can_summary:
                entry["summary"] = self._row_summary(path, sheet, r, skip_col=loc_col_idx)
            alts.append(entry)
        res.row_evidence = {
            "query": query or "",
            "row": row_match.row,
            "resolved": row_match.value,
            "method": row_match.method,
            "confidence": row_match.confidence,
            "ambiguous": row_match.ambiguous,
            "alternatives": alts,
        }

    def _log_evidence(self, res: AgentResult, intent: NLIntent,
                      dry_run: bool = False, user_text: str = "") -> None:
        """组装证据 + 对话记录写盘（旁路，失败不阻断主流程）。

        - dialog 记录：始终写（带 skill_enabled 标记），供质量评分与自动留优
        - evidence + skill_updater：仅 enable_skill=True 且非 dry-run 副本时写
          （A/B 对照 off 轮不学习，避免污染 skill）
        """
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        base = {
            "ts": ts,
            "session_id": res.session_id,
            "table_stem": res.table_stem,
            "sheet": res.table_sheet,
            "intent_action": intent.action if intent else "",
            "dry_run": dry_run,
            "col": res.col_evidence,
            "row": res.row_evidence,
            "ok": res.ok,
            "needs_confirm": res.needs_confirm,
            "user_corrected": res.user_corrected,
            "corrected_to": res.corrected_to,
            "skill_enabled": self.enable_skill,
            "missing_required": list(getattr(res, "missing_required", []) or []),
        }
        # 对话记录：完整对话 + 质量评分 + 自动留优（始终写，独立于 skill 开关）
        dialog_record = {
            **base,
            "user_text": user_text or (intent.raw if intent else ""),
            "agent_message": res.message,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail}
                      for s in res.steps],
        }
        try:
            get_dialog_logger().log(dialog_record)
        except Exception:
            logger.warning("对话记录写盘失败（不影响主流程）", exc_info=True)
        # evidence + skill 学习：仅 skill 开启且非 dry-run 副本时写
        if not self.enable_skill:
            return
        if not getattr(self, "enable_evidence", True):
            return  # dry-run 临时副本不写证据，避免污染真实 evidence
        if not res.table_stem:
            return  # 表格未定位到，无证据价值
        try:
            get_evidence_logger().log(base)
            # T7/T8: 证据消费 → 候选生成 + 反模式信号 + 跨表关系（旁路，失败只 warn）
            get_skill_updater().ingest_evidence(base)
        except Exception:
            logger.warning("证据/skill 学习写入失败（不影响主流程）", exc_info=True)

    def _check_anti_pattern(self, table_stem: str, sheet: str,
                            column: str = "", operation: str = "",
                            input_text: str = "") -> Optional[dict]:
        """T8: 查 L3 反模式（status=active）。命中返回条目 dict，否则 None。

        调用方据此改变定位行为：
          - action=force_exact       → match_mode 强制 "exact"，不回退 contains
          - action=require_confirm   → set 操作写非数字前需确认
          - action=block_dry_run     → 删除等高危操作直接拒绝
          - action=warn_only         → 仅提示（semantic_pattern 常用）
        input_text 非空时额外匹配 semantic_pattern 的 trigger_pattern 关键词。
        """
        if not getattr(self, "anti_pattern_cfg", None):
            return None
        return self.anti_pattern_cfg.lookup(table_stem, sheet, column, operation, input_text)

    def _apply_anti_pattern_fix_filter(self, table_stem: str, sheet: str,
                                       intent, semantic_issues: list) -> list:
        """方案2 修正学习 apply：命中 anti-pattern 的 fix.skip_outlier_check →
        滤掉离群 issue（值合法但被语义离群误报，如新增实体大编号远超历史 median），
        预防复发——学到的「正确修法」写前生效，失败不发生。

        仅 status=active 的 anti-pattern 生效（pending_review 不 apply，安全阀）。
        """
        if not semantic_issues:
            return semantic_issues
        _ap = self._check_anti_pattern(
            table_stem, sheet, input_text=(intent.raw if intent else ""))
        if not _ap or not (_ap.get("fix") or {}).get("skip_outlier_check"):
            return semantic_issues
        _outlier_kws = ("离群", "分布", "median", "MAD", "远高于", "远超", "偏离")
        filtered = [si for si in semantic_issues
                    if not any(k in (si.get("reason") or "") for k in _outlier_kws)]
        if len(filtered) < len(semantic_issues):
            logger.debug("anti-pattern fix.skip_outlier_check 命中，滤除 %d 条离群 issue",
                         len(semantic_issues) - len(filtered))
        return filtered

    def _run_single(self, intent: NLIntent, confirm_token: Optional[str] = None,
                    session_id: str = "",
                    suppress_phase_thinking: bool = False,
                    no_llm: bool = False) -> AgentResult:
        res = self._wire_sinks(AgentResult(intent=intent))  # ok 默认 None，由写后验证/add 驱动
        res.session_id = session_id
        # 多指令聚合模式：抑制内部 Step2-6 阶段 thinking，由外层统一发阶段标记
        res._suppress_phase_thinking = suppress_phase_thinking
        # Step3 零 LLM 透传：用实例属性临时短路越界 LLM 路径（_phase_execute:6420 读取），
        # 替代原 os.environ["CODEMAKER_EXECUTE_NO_LLM"] 进程级突变（污染并发 + 触发 P19）。
        # 作用域 = 本次 _run_single 调用，finally 还原旧值。
        _prev_no_llm = getattr(self, "execute_no_llm", False)
        if no_llm:
            self.execute_no_llm = True
        try:
            return self._run_single_impl(intent, confirm_token, res)
        finally:
            self.execute_no_llm = _prev_no_llm
            self._log_evidence(res, intent, user_text=intent.raw if intent else "")

    def _collect_error_feedback(self, res: AgentResult, intent: NLIntent,
                                path: Path, sheet: str) -> str:
        """D3/D4 收集错误上下文，供 retry-loop 的 error_feedback prompt 用。

        D4 增强：从失败 step 解析失败列+错误值+期望列类型，拼 D2 列类型 schema 块。
        文案格式："上次 fields 中 [列名]='值' 失败，该列类型 int 不接受字符串。
                  <D2 列类型 schema 块>。请重新产出 fields。"
        """
        import re
        stem = path.stem
        parts: list[str] = []
        failed_col = None
        failed_val = None

        # 结构化错误分类（capability: error-classification-repair）：用 error_classifier 归类，
        # 供 LLM retry prompt 知晓错误类型与定向修复方向。失败时降级跳过，不影响原文本堆栈。
        try:
            from ..repair.error_classifier import classify as _classify, VerifyResult
            classified = _classify(None, res, VerifyResult(), context={"table_stem": stem, "sheet": sheet})
            parts.append(f"错误类型：{classified.error_type.value}（置信度 {classified.confidence:.2f}）"
                         f"；根因：{classified.root_cause}")
            if classified.failed_col:
                failed_col = classified.failed_col
                failed_val = classified.failed_val
        except Exception:
            pass
        if failed_col:
            col_type = self._get_col_type(stem, sheet, failed_col)
            type_hint = f"，该列类型 {col_type} 不接受该值" if col_type else ""
            parts.append(f"上次 fields 中 [{failed_col}]='{failed_val}' 失败{type_hint}")

        # 失败步骤明细
        failed_details = [f"[{s.name}] {s.detail}" for s in res.steps if not s.ok]
        if failed_details:
            parts.append("失败步骤：" + "；".join(failed_details))

        # D2: 列类型 schema 块（列名: 类型 [枚举/主名称列标记]）
        try:
            from .skill_context import _format_column_types_block
            schema = _format_column_types_block([stem])
            if schema:
                parts.append(schema)
        except Exception:
            pass

        # 目标列/上次值（保留原基础文案）
        if intent.target_field:
            parts.append(f"目标列：{intent.target_field}")
        if intent.value is not None:
            parts.append(f"上次值：{intent.value!r}（若类型不符请改用正确类型/列名）")

        # 列名候选（供 LLM 重新选列）
        try:
            headers = self.cli.read_header(path, sheet)
            if headers:
                parts.append(f"表列名候选：{headers}")
        except Exception:
            pass

        if failed_col:
            parts.append("请重新产出 fields。")
        return "\n".join(parts)

    def _retry_with_error_feedback(self, intent: NLIntent, path: Path, sheet: str,
                                    res: AgentResult, error_feedback: str) -> Optional[AgentResult]:
        """D3 带错误反馈重试 LLM 解析一次，重新执行 _dispatch。

        重试仅触发一次（不递归）。返回新 out 或 None（解析失败）。
        重试 steps 追加到同一 res（保留首次失败历史）。
        """
        if not hasattr(self, "parser") or not hasattr(self.parser, "parse_multi"):
            return None
        res.add_thinking("重试", f"写操作失败，带错误反馈重试 LLM 解析一次：{error_feedback[:200]}")
        try:
            new_intents = self.parser.parse_multi(intent.raw, error_feedback=error_feedback)
        except Exception:
            logger.warning("retry parse_multi 调用失败", exc_info=True)
            return None
        if not new_intents:
            return None
        new_intent = new_intents[0]
        new_intent.extras.update(intent.extras)  # 保留 confirm_token 等上下文
        # 重置 ok 为 None：首次失败 ok=False 会阻止重试 add(True) 置 True（D2 语义：
        # 成功不覆盖失败）。重试是全新尝试，重置为 None 让重试 steps 重新驱动 ok。
        res.ok = None
        # 用新 intent 重新 _dispatch（文件已回滚到操作前状态）
        def _retry_dispatch() -> AgentResult:
            return _dispatch_action(self, new_intent, path, sheet, res)
        try:
            return _retry_dispatch()
        except Exception:
            logger.warning("retry _dispatch 执行异常", exc_info=True)
            return None

    def _run_single_impl(self, intent: NLIntent, confirm_token: Optional[str],
                         res: AgentResult) -> AgentResult:
        """_run_single 的实现体（res 由包装方法创建，便于 finally 写证据）。

        分阶段拆分：Step2 分区 → Step3 计划 → Step4 校验 → Step5 执行 → Step6 汇总，
        各阶段由独立 _phase_* 方法承载，便于多指令路径全局分阶段批量调用
        （Step2 所有任务→Step3 所有任务→...→Step6 所有任务），保证前端阶段时序。
        """
        # 回传确认令牌 → 标记 delete 已确认，跳过级联 dry-run
        if confirm_token:
            intent.extras["__delete_confirmed__"] = True
            intent.extras["__col_delete_confirmed__"] = True
            # T8: anti_pattern require_confirm 也通过 confirm_token 回传确认
            intent.extras["__anti_pattern_confirmed__"] = True

        # Step2 分区
        path, sheet = self._phase_partition(intent, res)
        if path is None or sheet is None:
            return res
        # Step3+4 计划+校验(合并路径:单次 LLM;否则串行两次)
        # §V2 P1-1/P1-2 短路：execute_no_llm=1 时跳过所有 plan/validate LLM
        # （_phase_plan/_phase_validate 内部已各加 execute_no_llm gate；合并路径在此短路）。
        if self._merge_applicable(intent) and not self.execute_no_llm:
            path, sheet = self._phase_plan_validate_merged(intent, path, sheet, res)
        elif self._merge_applicable(intent) and self.execute_no_llm:
            # 合并路径但 V2 零 LLM：跳过 ai_pipeline_merge，走纯规则
            pass
        else:
            self._phase_plan(intent, path, sheet, res)
            self._phase_validate(intent, path, sheet, res)
        # Step5 执行
        out = self._phase_execute(intent, path, sheet, res, confirm_token)
        # Step6 汇总
        self._phase_summarize(intent, path, sheet, res, out)
        return out if out is not None else res

    def _phase_partition(self, intent: NLIntent, res: AgentResult
                        ) -> tuple[Optional[Path], Optional[str]]:
        """Step2 分区：resolve_table + resolve_sheet + AI表推断。

        成功返回 (path, sheet)；任一失败返回 (None, None)（res.message/ok 已置）。
        """
        # 1. 定位表格（Step2 分区：规则优先，未命中时 AI 推断）
        res.add_thinking("路由", f"开始定位表格。意图 action={intent.action}，"
                         f"table_hint={intent.table_hint or '无'}，"
                         f"locator_value={intent.locator_value or '无'}，"
                         f"target_field={intent.target_field or '无'}")
        # §中危 8：V2 路径（execute_no_llm）下复用 Step1 locator_results 的 candidates，
        # 跳过 _resolve_table 重跑——Step1 已粗路由 + decompose 已基于 candidates 选定
        # table_hint，_resolve_table 行索引策略1（用 locator_value 命中）可能误命中它表
        # 覆盖正确 table_hint（如 reward_id 命中 item 表）。candidates 是表级全局信号，
        # 仅当 table_hint 在候选集内才信任，否则回退 _resolve_table 全策略保 fallback。
        path: Optional[Path] = None
        if self.execute_no_llm:
            _cands = ((intent.extras or {}).get("locator_candidates")) or []
            _hint = (intent.table_hint or "").strip()
            if _hint and _hint in _cands:
                try:
                    _p = next((p for p in self.cli.list_tables()
                               if p.stem == _hint), None)
                except Exception:
                    _p = None
                if _p is not None:
                    path = _p
                    res.add_thinking("路由",
                                     f"V2 复用 Step1 候选：{_hint}（跳过 _resolve_table）")
        if path is None:
            path, _ = self._resolve_table(intent)
        if path is None:
            # Step2 AI 增强：规则未命中 → LLM 推断目标表
            # §V2 P1-1 短路：execute_no_llm=1（V2 Step3 透传）时跳过 ai_resolve_table，
            # Step1 locator 已做表路由，partition 内 AI 推断在 V2 路径下被短路。
            if (self._ai_enhancer is not None and not self.execute_no_llm):
                all_stems = [p.stem for p in self.cli.list_tables()]
                ai_stem = self._ai_enhancer.ai_resolve_table(
                    intent.raw or intent.table_hint or "", [], all_stems)
                if ai_stem:
                    _p = next((p for p in self.cli.list_tables() if p.stem == ai_stem), None)
                    if _p is not None:
                        path = _p
                        res.add_thinking("路由", f"AI 推断目标表：{ai_stem}（{path}）")
                        intent.table_hint = ai_stem
        if path is None:
            sug = self._fuzzy_suggest(
                intent.table_hint or intent.locator_value or intent.raw,
                [p.stem for p in self.cli.list_tables()])
            hint = self._fmt_simple(sug)
            res.add_thinking("路由", f"未找到匹配表格。相近表：{hint or '无'}")
            res.add("resolve_table", False,
                    f"未找到匹配表格 (hint={intent.table_hint}){hint}")
            res.message = (f"无法定位表格文件：未找到匹配「{intent.table_hint or intent.raw}」"
                           f"的表格{hint}")
            return None, None
        res.add_thinking("路由", f"定位到表格文件：{path.stem}（{path}）")
        res.add("resolve_table", True, str(path))
        res.table_stem = path.stem

        # Step2 AI 二次确认：规则命中后跑一次轻量 LLM 确认，避免纯规则错误
        # （如关键词歧义把"灵兽"误路由到 guild）。AI 失败/超时 → 保持规则结果。
        # 合并路径(CODEMAKER_LLM_PIPELINE_MERGE)把 confirm 并入单次合并调用,此处跳过。
        # splitter 源意图 table_hint 由规则模板显式设定（高置信），AI confirm 不覆盖——
        # 否则"刷新 寻宝老人"等描述性 raw 会被 AI 误判回 entity_prefab，spawn_world_entity
        # /spawn_quest_entity 全路由错（quest_npc 用例 step7/9 硬失败根因）。
        _is_splitter_src = (intent.extras or {}).get("source") == "splitter"
        # §V2 P1-1 短路：execute_no_llm=1 时跳过 ai_confirm_table（二次确认 LLM）
        if (self._ai_enhancer is not None
                and not self.execute_no_llm
                and not self._merge_applicable(intent)
                and not _is_splitter_src):
            try:
                all_stems = [p.stem for p in self.cli.list_tables()]
                ai_confirm = self._ai_enhancer.ai_confirm_table(
                    intent.raw or intent.table_hint or "", path.stem, all_stems)
                if ai_confirm and ai_confirm != path.stem:
                    _p = next((p for p in self.cli.list_tables() if p.stem == ai_confirm), None)
                    if _p is not None:
                        res.add_thinking("路由",
                                         f"AI 二次确认纠正：规则路由「{path.stem}」→「{ai_confirm}」（{_p}）")
                        path = _p
                        intent.table_hint = ai_confirm
                        res.table_stem = path.stem
                        res.add("resolve_table", True, str(path))
                elif ai_confirm:
                    res.add_thinking("路由", f"AI 二次确认：路由「{path.stem}」正确")
            except Exception:
                logger.warning("Step2 AI 二次确认失败，保持规则路由", exc_info=True)

        # 2. 定位 sheet
        sheet = self._resolve_sheet(path, intent)
        if sheet is None:
            res.add_thinking("路由", f"在 {path.stem} 中未找到可用 sheet")
            res.add("resolve_sheet", False, "未找到可用 sheet")
            res.message = "无法定位 sheet"
            return None, None
        res.add_thinking("路由", f"定位到 sheet：{sheet}")
        res.add("resolve_sheet", True, sheet)
        res.table_sheet = sheet
        # Q1 守卫：列举/参考 sheet（「当前可用的...」「...列表」等）是只读目录，
        # 非业务数据表——往里 add/set/delete 会写到合并单元格或破坏目录结构，
        # 报 MergedCell/类型错乱且分类器无匹配模式 → 落 UNKNOWN 兜底。
        # 写操作一律拒，给清晰错误而非走到底撞 MergedCell。
        if intent.action in ("add", "set", "delete") and _is_listing_sheet(sheet):
            res.add_thinking("路由",
                f"拒绝写操作：sheet「{sheet}」是列举/参考 sheet（只读目录），不可写入业务数据")
            res.add("resolve_sheet", False,
                    f"sheet「{sheet}」为列举/参考目录，不可执行 {intent.action}")
            res.message = (f"目标 sheet「{sheet}」是只读列举目录（如可用资源清单），"
                           f"不能执行写操作 {intent.action}。请在对应业务数据 sheet 落值，"
                           f"或改指令指向可写 sheet。")
            res.ok = False
            return None, None
        return path, sheet

    def _phase_plan(self, intent: NLIntent, path: Path, sheet: str,
                    res: AgentResult) -> None:
        """Step3 计划：AI 增强字段映射（add/set 时 LLM 校准列名+值转换）。"""
        plan_fields = intent.extras.get("fields", {})
        # #22 R5：splitter 规则模板产出的 intent——保留 AI 列名映射(校准列名到表头),
        # 但锁定全部原值(含串/枚举)不被 AI 改 + 不采纳 AI 新增字段(防多余写入)。
        # 原 #19 仅保护数字/占位符,非数字被 AI 幻觉改致字段错位;v6 验证全跳 AI 致
        # 列名失准精准度回归,故改为 surgical:AI 仅按值匹配重映射列名,值与字段集不变。
        protect_all = intent.extras.get("source") == "splitter"
        # §V2 P1-2 短路：execute_no_llm=1 时跳过 ai_plan_operation（字段映射 LLM）。
        # 写前 LLM 校验职责归 Step2（V2 路径已在 Step2 SubAgent 跑 validate_two_layer）。
        if (self._ai_enhancer is not None and not self.execute_no_llm
                and intent.action in ("add", "set") and plan_fields):
            try:
                headers = self.cli.read_header(path, sheet)
                headers_clean = [(h or "").split(":")[0] for h in headers if h]
                col_types = _col_types_by_header(path.stem, sheet, headers_clean)
                ai_plan = self._ai_enhancer.ai_plan_operation(
                    intent_desc=intent.raw or "", table_stem=path.stem,
                    sheet=sheet, columns=headers_clean, intent_fields=plan_fields,
                    col_types=col_types)
                if ai_plan and ai_plan.get("fields"):
                    ai_fields = ai_plan["fields"]
                    if protect_all:
                        # splitter:surgical 合并——AI 仅重映射列名(按值匹配),不加字段、不改值
                        remapped: dict = {}
                        used_ai_keys: set = set()
                        for k, v in plan_fields.items():
                            new_k = k
                            if v is not None:
                                sv = str(v).strip()
                                for ak, av in ai_fields.items():
                                    if ak in used_ai_keys:
                                        continue
                                    if str(av).strip() == sv:
                                        new_k = ak  # AI 把此值映射到新列名
                                        used_ai_keys.add(ak)
                                        break
                            remapped[new_k] = v
                        intent.extras["fields"] = remapped
                        res.add_thinking("计划",
                            f"splitter 字段保护:AI 仅重映射列名,{len(remapped)} 字段值锁定")
                    else:
                        # 非 splitter:合并 AI 修正,保护显式数字编号/占位符(见 _apply_plan_fields)
                        self._apply_plan_fields(intent, ai_fields, plan_fields,
                                                res, ai_plan.get('notes', ''))
            except Exception:
                logger.warning("Step3 AI 计划增强失败，降级用原 fields", exc_info=True)
        res.add_thinking("计划", f"构造操作计划——单表指令 {intent.action}，目标 {path.stem}/{sheet}")
        res.add("写入计划", True, f"操作计划：{intent.action} {path.stem}/{sheet}")

    def _phase_validate(self, intent: NLIntent, path: Path, sheet: str,
                        res: AgentResult) -> None:
        """Step4 校验：AI 语义校验（之上叠加业务逻辑/命名/引用校验）。"""
        # Hard gate: delete/set 必须有行定位值（locator_value 或 row_override），
        # 否则 Step5 locate_row 必失败 → 进 verify-repair 浪费 LLM。在此早失败。
        if intent.action in ("delete", "set"):
            _has_loc = bool((intent.locator_value or "").strip())
            _has_row_override = bool(getattr(intent, "row_override", None))
            if not _has_loc and not _has_row_override:
                res.ok = False
                res._hard_blocked = True
                res.add("写前校验", False,
                        f"缺少行定位值：{intent.action} 操作需 locator_value 或 row_override")
                res.add_thinking("校验", f"❌ Hard gate 拒绝：{intent.action} 缺行定位值（早失败，不进 Step5）")
                return
        # #25: splitter 规则模板产出的 intent 跳过 AI 校验（与 #22 Step3 同源）。
        # Step3 已 surgical 锁定字段值/字段集，AI validate 仍跑会因幻觉改字段；
        # 降为纯规则校验（分支内部硬规则门仍生效），省 1 次 LLM 往返。
        # §V2 P1-2 短路：execute_no_llm=1 时跳过 ai_validate_plan（语义校验 LLM）。
        # V2 路径 Step2 已做 validate_two_layer，此处 LLM 校验职责归 Step2 不重复。
        if (self._ai_enhancer is not None and not self.execute_no_llm
                and intent.action in ("add", "set")
                and intent.extras.get("source") != "splitter"):
            try:
                headers = self.cli.read_header(path, sheet)
                headers_clean = [(h or "").split(":")[0] for h in headers if h]
                cur_fields = intent.extras.get("fields", {})
                ai_val = self._ai_enhancer.ai_validate_plan(
                    table_stem=path.stem, sheet=sheet, columns=headers_clean,
                    plan_fields=cur_fields, action=intent.action)
                if ai_val:
                    if ai_val.get("ok"):
                        res.add_thinking("校验", f"AI 语义校验通过：{ai_val.get('issues') or '无问题'}")
                    else:
                        issues = ai_val.get("issues", [])
                        suggestions = ai_val.get("suggestions", [])
                        res.add_thinking("校验", f"AI 语义校验发现问题：{issues}；建议：{suggestions}")
                        # 校验失败但不阻塞，由分支内部硬规则二次拦截
            except Exception:
                logger.warning("Step4 AI 语义校验失败，降级走硬规则", exc_info=True)
        elif intent.extras.get("source") == "splitter":
            res.add_thinking("校验", "splitter intent 跳过 AI 校验（#25），走规则校验")
        res.add_thinking("校验", "写前校验——硬规则类型/约束校验由分支内部执行")
        res.add("写前校验", True, "写前校验通过")

    def _merge_applicable(self, intent: NLIntent) -> bool:
        """是否走 LLM 合并路径(confirm+plan+validate 单次调用)。

        仅普通 add/set(非 splitter、非 whitelist-skip、有 fields、AI 助手开启)
        走合并;其余仍走串行。splitter/whitelist-skip 有各自跳过语义,不并入合并。
        """
        if self._ai_enhancer is None:
            return False
        if not self._ai_enhancer.pipeline_merge_enabled():
            return False
        if os.getenv("CODEMAKER_AI_ASSIST", "1") == "0":
            return False
        if intent.action not in ("add", "set"):
            return False
        if not (intent.extras or {}).get("fields"):
            return False
        if (intent.extras or {}).get("source") == "splitter":
            return False
        if self._ai_enhancer._should_skip_ai("validate_plan", intent):
            return False
        return True

    def _apply_plan_fields(self, intent: NLIntent, ai_fields: dict,
                           plan_fields: dict, res: AgentResult, notes: str) -> None:
        """合并 AI 修正字段到 intent.extras['fields'](非 splitter 路径)。

        保护显式数字编号/占位符不被 LLM 幻觉改值;仅允许 AI 修正非数字值
        (列名映射/枚举标签等)。AI 把数字值映射到新列名时保留原值。
        """
        merged = dict(ai_fields)  # AI 输出为基底
        for k, v in plan_fields.items():
            if v is None:
                continue
            sv = str(v).strip()
            # 显式数字编号：纯数字（含负号/小数）→ 保护，不被 AI 改
            # 但允许 AI 把数字值映射到正确列名（key 可变，value 保留）
            is_numeric = sv and re.match(r'^-?\d+(\.\d+)?$', sv)
            # 占位符 <xxx> 同样保护（前序产出 ID）
            is_placeholder = sv.startswith("<") and sv.endswith(">")
            if is_numeric or is_placeholder:
                # AI 把这个值改了？回退保护原值
                if k in merged and str(merged[k]).strip() != sv:
                    merged[k] = v  # 强制保留原显式值
                # AI 可能用不同 key（列名修正），尝试同值查找
                for ak, av in list(ai_fields.items()):
                    if str(av).strip() == sv and ak != k:
                        merged[ak] = sv  # 保留占位符/编号到新列名
        # Core4 PK 强制保护：Step2 validate 改写过的 PK（_pk_resolved 标记），
        # AI merge 不得「按用户指令」回退成原冲突值（如 100603→99001）。
        _pk_r = (intent.extras or {}).get("_pk_resolved")
        if isinstance(_pk_r, dict) and _pk_r.get("col") and _pk_r.get("value") is not None:
            merged[str(_pk_r["col"])] = _pk_r["value"]
            intent.extras["pk_value"] = _pk_r["value"]
        intent.extras["fields"] = merged
        res.add_thinking("计划", f"AI 字段映射修正：{notes}")

    def _phase_plan_validate_merged(self, intent: NLIntent, path: Path,
                                    sheet: str, res: AgentResult
                                    ) -> tuple[Path, str]:
        """Step3+4 合并路径:单次 LLM 同时做表确认+计划+校验。

        合并成功且高置信/表一致 → 直接采用合并结果(1 次 LLM)。
        合并失败/歧义/低置信/表纠正 → 拆分回独立 plan+validate(表纠正时重定位)。
        返回 (path, sheet)——表纠正时可能更新。
        """
        user_text = intent.raw or intent.table_hint or ""
        all_stems = [p.stem for p in self.cli.list_tables()]
        headers = self.cli.read_header(path, sheet)
        headers_clean = [(h or "").split(":")[0] for h in headers if h]
        plan_fields = (intent.extras or {}).get("fields", {})
        col_types = _col_types_by_header(path.stem, sheet, headers_clean)

        merged = None
        try:
            merged = self._ai_enhancer.ai_pipeline_merge(
                user_text=user_text, rule_stem=path.stem, all_tables=all_stems,
                table_stem=path.stem, sheet=sheet, columns=headers_clean,
                intent_fields=plan_fields, action=intent.action,
                col_types=col_types)
        except Exception:
            logger.warning("合并 LLM 调用异常，拆分回独立 plan+validate", exc_info=True)

        # 表纠正:合并确认指出 rule 表错误 → 重定位到正确表(拆分跑独立 plan+validate)
        if merged is not None:
            confirm_stem = merged.get("confirm_stem") or path.stem
            if confirm_stem and confirm_stem != path.stem:
                _p = next((p for p in self.cli.list_tables()
                           if p.stem == confirm_stem), None)
                if _p is not None:
                    new_sheet = self._resolve_sheet(_p, intent)
                    if new_sheet:
                        res.add_thinking("路由",
                            f"合并确认纠正表：「{path.stem}」→「{confirm_stem}」，拆分重定位")
                        path, sheet = _p, new_sheet
                        intent.table_hint = confirm_stem
                        res.table_stem = path.stem
                        res.add("resolve_table", True, str(path))
                        res.add("resolve_sheet", True, sheet)
                        merged = None  # 表已变,合并的 plan/validate 作废 → 拆分

        # 拆分判定:LLM 失败 / 歧义 / 低置信
        split = (merged is None
                 or merged.get("ambiguous")
                 or merged.get("confidence", 1.0) < 0.6)
        if split:
            # 独立 plan+validate(confirm 已在合并调用内完成,不重复)
            self._phase_plan(intent, path, sheet, res)
            self._phase_validate(intent, path, sheet, res)
            return path, sheet

        # happy path:采用合并结果
        ai_fields = merged.get("fields") or {}
        if ai_fields:
            self._apply_plan_fields(intent, ai_fields, plan_fields,
                                    res, merged.get("notes", ""))
        if merged.get("ok"):
            res.add_thinking("校验",
                f"AI 语义校验通过(合并)：{merged.get('issues') or '无问题'}")
        else:
            res.add_thinking("校验",
                f"AI 语义校验发现问题(合并)：{merged.get('issues', [])}；"
                f"建议：{merged.get('suggestions', [])}")
        res.add_thinking("计划",
            f"构造操作计划(合并)——单表指令 {intent.action}，目标 {path.stem}/{sheet}")
        res.add("Step3计划", True, f"操作计划：{intent.action} {path.stem}/{sheet}")
        res.add_thinking("校验", "写前校验——硬规则类型/约束校验由分支内部执行")
        res.add("Step4校验", True, "写前校验通过")
        return path, sheet

    def _phase_execute(self, intent: NLIntent, path: Path, sheet: str,
                       res: AgentResult, confirm_token: Optional[str]) -> Optional[AgentResult]:
        """Step5 执行：backup + _dispatch + 重试 + AI故障诊断。返回 out。

        confirm_token 透传保留（单指令路径在 _run_single_impl 顶部已置 extras 标记，
        多指令路径同样依赖 extras 标记，此参数为签名占位/未来扩展）。
        """
        # §4 ExecuteAgent 跳 skipped：validation.skipped=True 的子任务跳写盘
        # （4-Step 路径 validate_two_layer 产出,用户 skip 不执行）
        # P24/P25：O3 后 validate_two_layer 非阻断（ok=True 恒）不标 skipped →
        # 此分支 dormant（死代码）。保留供未来 O9 引入 partial 态时复用
        # （_mark_validation_skipped + 此早返）。当前 placeholder-gate 失败
        # 走 failure 分支（5531+）非 skip，故此分支不可达。
        _val = getattr(intent, "validation", None)
        if _val is not None and getattr(_val, "skipped", False):
            res.add_thinking("执行",
                f"跳过 skipped 子任务: {getattr(intent, 'table_hint', '') or ''}/"
                f"{getattr(intent, 'sheet_hint', '') or ''}")
            res.ok = True  # skipped 视为 ok,不阻塞下游拓扑依赖
            res.message = "用户跳过此项"
            return res
        # Step4.5 占位符断言：执行前检查 fields 是否仍含未替换 <label> 占位符。
        # 区分两类占位：
        #   <auto>            = LLM 显式标「留空待补」（用户没提的可选列）→ 静默软跳过，不弹问
        #   <new_xxx_id> 等   = 跨表引用/必填占位，前序产出没对上 → 才弹问用户补值
        # 目的：避免对可选列频繁弹「无法确定」打扰用户（用户没提的可选列默认留空）。
        _fields_pre = (intent.extras or {}).get("fields")
        if isinstance(_fields_pre, dict):
            import re as _re_ph
            # 拆分：auto（可选留空）vs 必填/跨表引用（需补值）
            _auto_cols, _required_cols = _classify_placeholder_fields(_fields_pre)
            # 可选 <auto> 列：静默留空，仅记 thinking 供审计
            if _auto_cols:
                res.add_thinking("执行",
                    f"以下列标 <auto>（用户未提及，留空）：{_auto_cols}")
                logger.info("Step5 占位列标 <auto> 留空 %s (table=%s sheet=%s)",
                            _auto_cols, path.stem, sheet)
            # 必填/跨表引用占位未解 → 才弹问
            if _required_cols:
                # §主键列占位豁免：add 意图主键列（表头首列）填 <new_xxx> 占位符 =
                # 主键自动分配（_do_append 自增），无需引用任何 produces。LLM 产
                # produces 标签名与占位符名常不一致（<new_entity_id> vs
                # new_gate_prefab_id），仅靠 label 精确匹配会漏 → 用「首列位置」判主键。
                _pk_header = ""
                try:
                    _hdrs = (self.cli.read_header(path, sheet)
                             if hasattr(self.cli, "read_header") else [])
                    if _hdrs:
                        _pk_header = str(_hdrs[0] or "").split(":")[0].strip()
                except Exception:
                    _pk_header = ""
                # §自引用豁免：<new_xxx_id> 且 label == 本 intent produces_label
                _self_prod = (getattr(intent, "produces_label", None)
                              or (intent.extras or {}).get("produces"))
                if _self_prod:
                    _self_prod = str(_self_prod).strip()
                _filtered: list[str] = []
                for _c in _required_cols:
                    _v = str(_fields_pre.get(_c) or "").strip()
                    _lbl = (_v[1:-1].strip()
                            if (_v.startswith("<") and _v.endswith(">")) else "")
                    _c_clean = (_c or "").split(":")[0].strip()
                    # 主键列（表头首列）占位符 → 删除字段走自增（勿清空成 ''，
                    # 否则 _run_add 对 '' 做 int coerce 报「无法转整数」）
                    if _pk_header and _c_clean == _pk_header:
                        _fields_pre.pop(_c, None)
                        res.add_thinking("执行",
                            f"列[{_c}] 为主键列，占位 {_v} 删除走自增分配")
                        continue
                    # 自引用（label 精确匹配 produces）→ 删除字段走自增
                    if _lbl and _self_prod and _lbl == _self_prod:
                        _fields_pre.pop(_c, None)
                        res.add_thinking("执行",
                            f"列[{_c}] 自引用占位 {_v}（本步 produces），已删除走自增分配")
                        continue
                    _filtered.append(_c)
                _required_cols = _filtered
            if _required_cols:
                logger.warning("Step5 执行前发现未替换占位符字段 %s (table=%s sheet=%s)",
                               _required_cols, path.stem, sheet)
                res.add_thinking("执行",
                    f"⚠ 必填/引用占位未替换：{_required_cols}（拓扑/backfill 未解析）")
                intent.extras["_has_unresolved_placeholder"] = _required_cols
                # 清洗列名（去 \n 转义/换行/压空白），供 ask 展示 + 失败清单共用。
                def _clean_col(_n: str) -> str:
                    return _re_ph.sub(r"\s+", " ",
                                      _n.replace("\\n", " ").replace("\n", " ")).strip()
                _unresolved_clean = [_clean_col(k) for k in _required_cols]
                _unresolved_disp = "、".join(f"「{c}」" for c in _unresolved_clean)
                _col_disp = "、".join(_unresolved_clean)
                # 提取每个未解占位列引用的「依赖标记」，明确告诉用户缺的是哪个前置项。
                _dep_labels: list[str] = []
                for _c in _required_cols:
                    for _m in _PLACEHOLDER_RE.finditer(str(_fields_pre.get(_c) or "")):
                        _lbl = _m.group(1)
                        if _lbl and _lbl not in _dep_labels:
                            _dep_labels.append(_lbl)
                _dep_hint = ("、".join(_humanize_dep_label(_l) for _l in _dep_labels)
                             if _dep_labels else "前置数据")
                _example = "；".join(f"{c}填「（此处填具体值）」" for c in _unresolved_clean)
                _ask_rc = (
                    f"列 {_col_disp} 要引用的前置数据（{_dep_hint}）还没被前面的步骤创建出来，"
                    f"系统也没能从前序结果自动回填。继续写入会残留 <列名> 占位文本污染数据，故暂停。"
                )
                _ask_strats = "已尝试：按依赖顺序自动回填（未找到前序产出）"
                # O21 表格交互：suggestion 引导走 field 模式填表格，而非补自然语言句子。
                # mode=field 路径已支持（5967 读 fix_payload.fields），但原文本说"补一句
                # 自然语言"与 example（字段填法）矛盾 → 统一引导用户填字段值表格。
                _ask_sug = (
                    "这些列无法自动生成，请在下方表格按列填入具体值"
                    "（数字/枚举列填数字或编号，不能填中文名）；"
                    "或点「跳过」放弃此项继续后续任务（会记入失败清单）。"
                )
                # 计算建议值：单占位列且能定位本表 PK 列 → 用下一可用 ID 作建议
                _suggested_id = None
                if len(_required_cols) == 1:
                    try:
                        _pk_col_idx = self._locate_pk_col(path, sheet)
                        if _pk_col_idx:
                            _suggested_id = self._allocate_pk(path, sheet, _pk_col_idx)
                    except Exception:
                        _suggested_id = None
                # 中断反问：让用户填字段值或重描述，清占位符后续跑
                _ask = getattr(self, "_ask_callback", None)
                _user_mode = None
                if _ask is not None:
                    _ask_payload = {
                        "reason": f"无法确定 {_unresolved_disp} 的取值，已暂停写入",
                        "table": path.stem, "sheet": sheet,
                        "failed_col": _col_disp, "failed_val": "",
                        "root_cause": _ask_rc,
                        "attempted_strategies": _ask_strats,
                        "suggestion": _ask_sug,
                        "example": _example,
                        "snip": (getattr(intent, "raw", "") or "")[:120],
                        # 要求 B：大白话 reason + action
                        "user_friendly": {
                            "reason": (f"这项「{_col_disp}」要引用的前置数据（{_dep_hint}）"
                                       f"还没被前面的步骤创建出来，所以现在填不了。"),
                            "action": (f"正常应由系统建好『{_dep_hint}』后自动回填。你可以："
                                       "①检查那条前置数据是否漏配、补上它 "
                                       "②手动填一个已存在的编号 ③点「跳过」放弃此项。"),
                        },
                    }
                    # §取值不确定统一交互：单占位列 + 有建议值 → 前端走「建议ID+文字框」
                    # （不填=接受建议，填了=按自定义），与 PK 冲突同模式（id_suggest）。
                    if _suggested_id is not None:
                        _ask_payload["mode_hint"] = "id_suggest"
                        _ask_payload["suggested_id"] = _suggested_id
                        _ask_payload["suggestion"] = (
                            f"「{_col_disp}」需要的前置值不存在，建议填 {_suggested_id}，"
                            "或输入其他已存在的编号")
                        _ask_payload["user_friendly"]["action"] = (
                            f"系统没找到『{_dep_hint}』，建议 {_col_disp} 填 {_suggested_id}。"
                            "不填＝接受建议，填了＝按你输入的编号写入。")
                    _pr = _ask(_ask_payload) or {}
                    _user_mode = _pr.get("mode")
                    if _user_mode == "field":
                        # §id_suggest：accept_suggest/custom_id → 填建议/自定义值到占位列
                        if (_pr.get("accept_suggest") or _pr.get("custom_id")) and _required_cols:
                            _new_id = (_pr.get("custom_id")
                                       if _pr.get("custom_id") else _suggested_id)
                            if _new_id is not None:
                                for _c in _required_cols:
                                    _fields_pre[_c] = _new_id
                                intent.extras["_has_unresolved_placeholder"] = None
                        else:
                            _fill = ((_pr.get("fix_payload") or {}).get("fields") or {})
                            for _c, _v in _fill.items():
                                if _c in _fields_pre:
                                    _fields_pre[_c] = _v
                            if _fill:
                                intent.extras["_has_unresolved_placeholder"] = None
                    elif _user_mode == "nl":
                        _nt = (_pr.get("text") or "").strip()
                        if _nt and self.parser is not None:
                            try:
                                _ni = self.parser.parse(_nt)
                                if _ni is not None and (_ni.extras or {}).get("fields"):
                                    intent.extras["fields"] = _ni.extras["fields"]
                                    intent.raw = _nt
                                    intent.extras["_has_unresolved_placeholder"] = None
                            except Exception:
                                pass
                # 占位符仍残留（用户跳过 / 无 callback / 补值未对上）→ 记 failure
                # 让 Step6 总结归纳显式列出，避免「行部分写入 verify 过即判 ok」的静默半成品。
                if intent.extras.get("_has_unresolved_placeholder"):
                    try:
                        res.failures.append({
                            "type": "placeholder_unresolved",
                            "table": path.stem, "sheet": sheet,
                            "col": _col_disp,
                            "root_cause": _ask_rc,
                            "attempted_strategies": _ask_strats,
                            "suggestion": _ask_sug,
                            "snip": (getattr(intent, "raw", "") or "")[:120],
                            "status": "unresolved",
                            "user_reply": _user_mode,
                        })
                    except Exception:
                        pass
                    # O20d：占位符残留 → 已记 failure，跳写库避免 <列名> 占位文本污染数据。
                    # 保 D6 上报不静默吞 + 不留半成品（res.failures 已聚合→_phase_summarize 上报）。
                    res.ok = False
                    res.add_thinking("执行",
                        f"占位符未解，跳过写库 {path.stem}/{sheet}（已记 placeholder_unresolved failure）")
                    return res
        # 3. 按动作分支分发
        res.add_thinking("执行", f"意图动作「{intent.action}」，分发到对应处理分支")
        # 4.3：写操作前快照文件，处理返回不 ok 时回滚，保证失败不留半成品。
        col_op = intent.extras.get("col_op", intent.action)
        is_read = (intent.action == "get"
                   or intent.action == "col_list"
                   or (intent.action == "col" and col_op == "col_list"))
        is_write = not is_read
        backup_file = None
        if is_write and getattr(self, "auditor", None) is not None:
            try:
                backup_file = self.auditor.backup_and_record(
                    operation=f"pre_{intent.action}", path=str(path),
                    sheet=sheet, extra={"locator": intent.locator_value or ""})
                backup_file = backup_file.backup_file if backup_file else None
            except Exception:
                logger.warning("auditor 快照失败（写操作将无回滚备份，失败时无法回滚）", exc_info=True)
                backup_file = None

        def _dispatch() -> AgentResult:
            return _dispatch_action(self, intent, path, sheet, res)

        try:
            out = _dispatch()
        except Exception:
            if is_write and backup_file:
                self._rollback_write(path, backup_file, res)
            raise

        # §确认暂停短路：needs_confirm（行未命中跨表搜索 / 行歧义删除 / 级联删除预览 /
        # 低置信度 / 列删除 / 反模式）是「待用户确认」语义（ok 默认 None），非失败。
        # V2 execute_no_llm 下，下方把 ok=None 当失败回滚 + 改写为 execute_failed_no_llm
        # + 覆盖 message，会污染 needs_confirm 信号（activity 春节活动歧义删除用例即被
        # 判成 execute_failed_no_llm + direct_dispatch 而无法选择删除）。此处直接返回
        # out，保留 needs_confirm/confirm_token/pending_search/row_evidence 原样供上层聚合。
        # _phase_summarize 仍会跑（else 分支保留原 message），与 legacy 级联删除预览
        # needs_confirm 的 ok=False + needs_confirm=True 口径一致。
        if out is not None and getattr(out, "needs_confirm", False):
            return out

        # §3 ExecuteAgent 去 LLM（CODEMAKER_EXECUTE_NO_LLM=1）：失败直接结构化进
        # res.failures（#40），跳过 verify-repair loop + D3 retry-loop 的 LLM 诊断/重试，
        # 诊断 + 反模式归纳交 §5 ConcludeAgent。默认关（保持现状含 LLM 诊断+重试+修复）。
        if self.execute_no_llm and is_write and out is not None \
                and not getattr(out, "ok", False):
            # §P1 空内容子任务干净跳过：失败文案属"无可写内容"类（baseline 对弱命中
            # 候选产的空壳 fields，或字段全被叙述跳过后 values 空）→ ask 也无从填起
            # （用户看到"未指明列"无法修）。标 skipped 不计 failure，res.ok=True 不阻塞
            # 下游拓扑，让流水线以干净状态正确结束。区别于类型/PK/列名冲突（有内容需
            # 用户修，走下方软 ask 循环）。通用判据（错误文案类别），不绑业务词/表/测例。
            _fail_msg = (getattr(out, "message", "") or res.message or "")
            _EMPTY_CONTENT_MARKERS = (
                "无法解析新增内容",
                "未能从语句中提取到列值",
                "无法匹配任何目标列",
                "所有列名均无法匹配表头",
            )
            if any(_mk in _fail_msg for _mk in _EMPTY_CONTENT_MARKERS):
                if backup_file:
                    self._rollback_write(path, backup_file, res)
                res.ok = True
                res.add_thinking("执行",
                    f"跳过无写入内容的子任务 {path.stem}/{sheet}"
                    f"（{_fail_msg[:32]}），不计入失败清单")
                return res
            # §C Step3 软 ask 收窄（workflow 纯化）：原 if 块"类型不符/字段缺失写失败软 ask
            # 救援循环（_ask_callback + fix_payload 重试）"已禁用——这类属 Step2 校验范畴
            #（B 升 TYPE_MISMATCH/COL_NOT_FOUND 硬阻断，Step2 漏过的属非交互放行带病 intent，
            # 不该 Step3 越界补校验）。Step3 职责=执行非校验，写失败恒走下方 else"如实记
            # failed 进 res.failures（Step4 汇总呈现，用户看汇总重跑）"。
            # 保留：占位符悬空 rescue（_phase_execute 前置 gate 7054，直接 self._ask_callback
            # + suggested_id）——"跨表引用未对上"属数据冲突=Step3 职责，非校验越界。
            _ask_cb = None  # 收窄：禁用软 ask rescue（行为退到 else 记 failed）。死代码待后续清理。
            if _ask_cb is not None:
                # 拉 headers/type_row 供提交值预检（按列查 col_type）
                _hdrs = []
                _trow = []
                try:
                    _hdrs = self.cli.read_header(path, sheet) if hasattr(self.cli, "read_header") else []
                    _trow = self.cli.read_type_row(path, sheet) if hasattr(self.cli, "read_type_row") else []
                except Exception:
                    _hdrs, _trow = [], []

                def _col_type(_c: str) -> str:
                    _cn = (_c or "").split(":")[0].strip().lower()
                    for _h, _t in zip(_hdrs, _trow):
                        if _h and (_h or "").split(":")[0].strip().lower() == _cn:
                            return str(_t or "")
                    return ""

                def _ct_label(_t: str) -> str:
                    _tl = (_t or "").lower()
                    if "int" in _tl or "long" in _tl:
                        return "数字"
                    if "float" in _tl or "double" in _tl or "number" in _tl:
                        return "数字"
                    if "bool" in _tl:
                        return "0 或 1"
                    return "正确的值"

                def _first_failure(_src_out, _src_res):
                    _f = None
                    for _x in (getattr(_src_out, "failures", None) or []):
                        if isinstance(_x, dict) and (_x.get("col") or _x.get("root_cause")):
                            _f = _x
                            break
                    if _f is None:
                        for _x in (getattr(_src_res, "failures", None) or []):
                            if isinstance(_x, dict) and (_x.get("col") or _x.get("root_cause")):
                                _f = _x
                                break
                    return _f

                import re as _re_fv
                _MAX_RESCUE_ROUNDS = 3
                _retried_ok = False
                _final_user_reply = None
                _cur_err = (getattr(out, "message", "") or res.message
                            or "首次写操作失败")
                _cur_ff = _first_failure(out, res)
                _cur_fail_col = (_cur_ff.get("col") if _cur_ff else "") or ""
                for _round in range(_MAX_RESCUE_ROUNDS):
                    _ff_rc = (_cur_ff.get("root_cause") if _cur_ff else "") or _cur_err
                    _fail_val = ""
                    _m_fv = _re_fv.search(r"[「『]([^」』]+)[」』]", _ff_rc)
                    if _m_fv:
                        _fail_val = _m_fv.group(1)
                    _disp_col = _cur_fail_col or ""
                    # 取该列当前值（优先 intent.fields 真实值，回退根因里裁出的值）
                    _cur_fields_ref = (getattr(intent, "extras", None) or {})
                    _cur_fields_ref = (_cur_fields_ref.get("fields")
                                        if isinstance(_cur_fields_ref, dict) else None) or {}
                    _cur_val_disp = _cur_fields_ref.get(_cur_fail_col)
                    if _cur_val_disp in (None, "") and _fail_val:
                        _cur_val_disp = _fail_val
                    _cur_val_disp_s = ("" if _cur_val_disp in (None, "")
                                       else str(_cur_val_disp))
                    _ask_rc = (f"「{_disp_col}」列的值「{_cur_val_disp_s}」"
                               f"格式不正确：{_cur_err}"
                               if _cur_fail_col
                               else f"{path.stem}/{sheet} 存在格式不正确的字段值：{_cur_err}")
                    # 列类型 + 形态标签（数字 / 0 或 1 / 正确的值）+ 类型适用建议值
                    _exp_label = ""
                    _sugg_id = None
                    if _cur_fail_col:
                        _ct = _col_type(_cur_fail_col)
                        if _ct:
                            _exp_label = _ct_label(_ct)
                            _ctl = str(_ct).lower()
                            if "int" in _ctl or "long" in _ctl:
                                _sugg_id = 1
                            elif "float" in _ctl or "double" in _ctl or "number" in _ctl:
                                _sugg_id = 1
                            elif "bool" in _ctl:
                                _sugg_id = 0
                    _intent_snip = (getattr(intent, "raw", "") or "")[:60]
                    # 文案：点明"哪条意图 → 哪列 → 当前值X → 该列要形态Y → 建议 Z"
                    # 比"存在格式不正确的字段值"具体可懂，用户一眼知道改哪。
                    if _cur_fail_col:
                        _reason = (f"列「{_disp_col}」值「{_cur_val_disp_s}」"
                                   f"格式有误，需{_exp_label or '正确格式'}")
                        _uf_reason = (f"意图「{_intent_snip}」中，列「{_disp_col}」"
                                      f"填的「{_cur_val_disp_s}」无法转成"
                                      f"{_exp_label or '该列要求的类型'}：{_cur_err}")
                    else:
                        _reason = "数据写入失败，请修正后重试"
                        _uf_reason = "存在格式不正确的字段值，系统未能完整写入。"
                    _uf_action_parts = []
                    if _sugg_id is not None:
                        _uf_action_parts.append(
                            f"建议填「{_sugg_id}」可直接点「接受」"
                            f"（该列要 {_exp_label or '该类型'}）")
                    _uf_action_parts.append("或在输入框填写你自己的值后点「提交修正」")
                    _uf_action_parts.append("不需要就点「跳过」")
                    _uf_action = "；".join(_uf_action_parts)
                    _ask_pr = _ask_cb({
                        "reason": _reason,
                        "table": path.stem, "sheet": sheet,
                        "failed_col": _cur_fail_col, "failed_val": _cur_val_disp or _fail_val,
                        "current_val": _cur_val_disp_s,
                        "root_cause": _ask_rc,
                        "expected_type": _exp_label,
                        "attempted_strategies": (f"{_round + 1} 次，均未成功"
                                                 if _round else "1 次，未成功"),
                        "suggestion": _uf_action,
                        "snip": _intent_snip,
                        "mode_hint": "value_input" if _sugg_id is not None else None,
                        "suggested_id": _sugg_id,
                        "user_friendly": {
                            "reason": _uf_reason,
                            "action": _uf_action,
                        },
                    }) or {}
                    _final_user_reply = _ask_pr.get("mode")
                    # 统一解析回复为 fix_payload.fields（修复原 bug：_ask_callback 回
                    # {mode,accept_suggest,value,custom_id} 无 fix_payload.fields，
                    # 致 7377 _fill 恒空→ break → execute_failed_no_llm，用户点接受/
                    # 输入值均无效）。现按 failed_col 单列组装 fields，让 7377 取得值。
                    if _ask_pr.get("mode") == "field" and _cur_fail_col:
                        _uv = None
                        if _ask_pr.get("accept_suggest"):
                            _uv = (_ask_pr.get("custom_id")
                                   or _ask_pr.get("value")
                                   or _ask_pr.get("text")
                                   or _sugg_id)
                        else:
                            _uv = (_ask_pr.get("custom_id")
                                   or _ask_pr.get("value")
                                   or _ask_pr.get("text"))
                        if _uv is not None and str(_uv).strip() != "":
                            _fp = _ask_pr.setdefault("fix_payload", {})
                            _fp.setdefault("fields", {})[_cur_fail_col] = _uv
                    if _ask_pr.get("mode") != "field":
                        break  # 用户跳过/无回复 → 退出循环走 fail
                    _fill = ((_ask_pr.get("fix_payload") or {}).get("fields")
                             or _ask_pr.get("fields") or {})
                    if not isinstance(_fill, dict) or not _fill:
                        break
                    # 提交值预检：每个填值 _coerce_value 看能否强转（不写盘）
                    _bad = []  # (col, val, error)
                    for _c, _v in _fill.items():
                        _ct = _col_type(_c)
                        if not _ct:
                            continue  # 无类型信息不预检（交写盘真校验）
                        try:
                            _, _w, _e = self._coerce_value(
                                _ct, _v, stem=path.stem, sheet=sheet, col_name=_c)
                        except Exception as _e_cv:
                            _e, _w = str(_e_cv), None
                        if _e:
                            _bad.append((_c, _v, _e))
                    if _bad:
                        # 预检不过 → 重新 ask，把错误方向并入根因
                        _cur_err = "；".join(f"列「{c}」的值「{v}」：{e}" for c, v, e in _bad)
                        _cur_fail_col = _bad[0][0]
                        _cur_ff = {"col": _cur_fail_col, "root_cause": _cur_err}
                        res.add_thinking("执行",
                            f"用户提交值预检未过（第{_round+1}轮），重新 ask：{_cur_err[:120]}")
                        continue  # 重新 ask 带错误方向
                    # 预检通过 → 改写 fields + 回滚 + 重试写盘
                    _cur_fields = intent.extras.setdefault("fields", {})
                    for _c, _v in _fill.items():
                        _cur_fields[_c] = _v
                    if backup_file:
                        self._rollback_write(path, backup_file, res)
                    try:
                        _retry_out = _dispatch()
                    except Exception:
                        _retry_out = None
                    if _retry_out is not None and getattr(_retry_out, "ok", False):
                        out = _retry_out
                        _retried_ok = True
                        res.add_thinking("执行",
                            f"用户修正「{_disp_col}」后重试成功（第{_round+1}轮）")
                        break
                    # 写盘重试仍失败 → 把写盘错误并入下轮 ask 根因
                    _cur_err = (getattr(_retry_out, "message", "")
                                if _retry_out else "") or res.message or "重试写盘失败"
                    _nf = _first_failure(_retry_out, res) if _retry_out else None
                    if _nf:
                        _cur_fail_col = _nf.get("col") or _cur_fail_col
                        _cur_ff = _nf
                    else:
                        _cur_ff = {"col": _cur_fail_col, "root_cause": _cur_err}
                    res.add_thinking("执行",
                        f"重试写盘仍失败（第{_round+1}轮），并入下轮 ask：{_cur_err[:120]}")
                    continue
                if _retried_ok:
                    pass  # 走正常后续（verify/summarize）
                else:
                    if backup_file:
                        self._rollback_write(path, backup_file, res)
                    res.ok = False
                    res.message = (f"执行失败（ExecuteAgent 去 LLM 模式,经 "
                                   f"{_MAX_RESCUE_ROUNDS} 轮 ask+校验仍未通过）：{_cur_err}")
                    res.failures.append({
                        "type": "execute_failed_no_llm",
                        "table": path.stem, "sheet": sheet, "col": _cur_fail_col,
                        "root_cause": _cur_err,
                        "attempted_strategies": ["direct_dispatch", "user_ask_retry_with_validate"],
                        "suggestion": "待 ConcludeAgent(§5) 诊断/反模式归纳",
                        "status": "failed", "user_reply": _final_user_reply,
                    })
                    return res
            else:
                if backup_file:
                    self._rollback_write(path, backup_file, res)
                _first_err = (getattr(out, "message", "") or res.message
                              or "首次写操作失败")
                res.ok = False
                res.message = f"执行失败（ExecuteAgent 去 LLM 模式,跳过 LLM 诊断/重试）：{_first_err}"
                res.failures.append({
                    "type": "execute_failed_no_llm",
                    "table": path.stem, "sheet": sheet, "col": "",
                    "root_cause": _first_err,
                    "attempted_strategies": ["direct_dispatch"],
                    "suggestion": "待 ConcludeAgent(§5) 诊断/反模式归纳",
                    "status": "failed", "user_reply": None,
                })
                return res

        # verify-repair 迭代环（enable 时接管写操作 verify 门控 + 失败修复；快路径优先）
        # §V2 零 LLM 守卫：execute_no_llm=1（V2 Step3 透传）时短路 verify-repair loop，
        # 否则写成功但 _verify_write 校验失败会进 repair loop 调 LLM（绕过 Step3 零 LLM 不变量）。
        # 失败结构化进 res.failures，不做 LLM 修复（交复盘模式或 Step4 汇总）。
        if (is_write and self.enable_verify_repair_loop
                and self.repair_playbook is not None
                and not self.execute_no_llm):
            loop_out = self._run_verify_repair_loop(intent, path, sheet, res, out, backup_file, is_write)
            if loop_out is not None:
                if getattr(loop_out, "ok", False):
                    if is_write and backup_file:
                        res._commit_backup = (str(path), backup_file)
                    return loop_out
                # 迭代环失败：回滚 + 返回结构化失败 res（已由 loop 设置 res.extras["repair_failure"]）
                if backup_file:
                    self._rollback_write(path, backup_file, res)
                return loop_out
            # loop 返回 None（不应发生，防御）→ 落到原 retry

        # D3 retry-loop：写操作失败时带错误反馈重试 LLM 一次（不递归）
        # 仅 enable_verify_repair_loop=False 时走此分支（迭代环关闭时退回原行为）
        # Step5 AI 增强：失败时先用 LLM 诊断根因 + 给修正方案，再进入重试
        if is_write and out is not None and not out.ok:
            first_error = res.message or "首次写操作失败"
            # Step5 AI 故障诊断：LLM 分析根因 + 修正 fields
            ai_diagnosis = None
            if self._ai_enhancer is not None:
                try:
                    headers = self.cli.read_header(path, sheet)
                    headers_clean = [(h or "").split(":")[0] for h in headers if h]
                    failed_fields = intent.extras.get("fields", {})
                    ai_diagnosis = self._ai_enhancer.ai_analyze_failure(
                        error_msg=first_error, table_stem=path.stem, sheet=sheet,
                        columns=headers_clean, attempted_fields=failed_fields,
                        action=intent.action)
                    if ai_diagnosis:
                        res.add_thinking("执行",
                                         f"AI 故障诊断——根因：{ai_diagnosis.get('root_cause', '')}；"
                                         f"策略：{ai_diagnosis.get('strategy', '')}")
                        # AI 给出修正 fields 时直接采用（覆盖原 fields）
                        fix_fields = ai_diagnosis.get("fix_fields")
                        if fix_fields and isinstance(fix_fields, dict):
                            intent.extras["fields"] = fix_fields
                except Exception:
                    logger.warning("Step5 AI 故障诊断失败，降级走原 error_feedback", exc_info=True)
            # 首次失败 → 回滚清半成品（文件恢复操作前状态）
            if backup_file:
                self._rollback_write(path, backup_file, res)
            error_feedback = self._collect_error_feedback(res, intent, path, sheet)
            # AI 诊断有修正 fields 时，error_feedback 追加 AI 根因（供 LLM 重试参考）
            if ai_diagnosis and ai_diagnosis.get("root_cause"):
                error_feedback = (error_feedback + "\n" if error_feedback else "") + \
                                 f"AI 诊断根因：{ai_diagnosis.get('root_cause')}"
            retry_out = self._retry_with_error_feedback(intent, path, sheet, res, error_feedback)
            if retry_out is not None and retry_out.ok:
                # 重试成功：显式置 ok=True（覆盖首次 False）
                res.ok = True
                res.message = f"重试成功：{retry_out.message}"
                # 重试成功路径不走 Step6 汇总（保留原 _run_single_impl 早返回行为）
                res._skip_summarize = True
                return retry_out
            # 重试仍失败 → 回滚重试写 + ok=False + 两次失败描述
            if backup_file:
                self._rollback_write(path, backup_file, res)
            retry_error = (retry_out.message if retry_out else "重试解析未产出意图") or "重试失败"
            res.ok = False
            res.message = f"首次失败：{first_error}；重试失败：{retry_error}"
            return res

        if is_write and backup_file and out is not None and not out.ok:
            self._rollback_write(path, backup_file, res)
        # G8 链回滚：成功 op 记录 backup_file 供 run 多指令路径失败时回滚前序已 commit 行。
        # 写读失败/重试失败路径 out.ok 非 True，backup 已回滚（上面分支），不记录。
        if is_write and backup_file and out is not None and getattr(out, "ok", None) is True:
            res._commit_backup = (str(path), backup_file)
        return out

    # ── verify-repair 迭代环（capability: verify-repair-loop / error-classification-repair / skill-executor-tools）──

    def _verify_write(self, intent: NLIntent, path: Path, sheet: str,
                      out: Optional[AgentResult]) -> "VerifyResult":
        """写后规则校验门控：类型约束 + id_scope + anti_pattern + 本地 ref_integrity。零 LLM。

        成功路径调用（out.ok=True），失败则驱动 error_classifier 走 repair。全程防御：
        任何校验异常 → passed=True（不阻断，交由后续 read-back 兜底）。
        """
        from ..repair.error_classifier import ErrorType, VerifyResult
        try:
            if out is None or not getattr(out, "ok", False):
                return VerifyResult()  # 未成功写入不校验，由失败分支处理
            table_stem = path.stem
            vc = _load_value_constraints().get(table_stem, {}).get(sheet, {}).get("columns", {})
            headers = self.cli.read_header(path, sheet) if self.cli else []
            headers_clean = [(h or "").split(":")[0] for h in headers if h]
            result_rows = getattr(out, "result_rows", []) or []
            # Level 0: 值语义合理性门（在类型/id/anti_pattern 校验前，纯代码零 LLM）
            try:
                from .semantic_gate import run_semantic_gate
                semantic_issues = run_semantic_gate(
                    table_stem, sheet, path, headers_clean, result_rows, self.cli, vc,
                    action=intent.action,
                )
            except Exception:
                logger.debug("semantic_gate 异常降级", exc_info=True)
                semantic_issues = []
            # 方案2 修正学习：命中 anti-pattern fix.skip_outlier_check → 滤离群 issue
            semantic_issues = self._apply_anti_pattern_fix_filter(
                table_stem, sheet, intent, semantic_issues)
            if semantic_issues:
                # Level 0 命中离群：优先返回 SEMANTIC_OUTLIER，进 repair 修值后再 verify
                return VerifyResult(
                    passed=False, failed_kind=ErrorType.SEMANTIC_OUTLIER,
                    semantic_issues=semantic_issues,
                    checked=len(result_rows),
                )
            type_issues: list[dict] = []
            id_issues: list[dict] = []
            anti_pattern_hits: list[dict] = []
            for rr in result_rows:
                col_name = (rr.get("col_name") or "").split(":")[0]
                new_val = rr.get("new_value")
                col_idx = rr.get("col")
                # 类型约束检查
                col_type = vc.get(col_name, {}).get("type", "")
                if col_type and new_val is not None:
                    ok, reason = self._check_type_constraint(col_type, new_val)
                    if not ok:
                        type_issues.append({"column": col_name, "value": new_val, "expected_type": col_type, "reason": reason})
                # anti_pattern 命中：仅 block_dry_run 类阻断 verify（failed_operation 历史
                # 失败拦截）；ambiguous_column/force_exact 是定位阶段精确性提示，verify
                # 已有具体 col_name 说明列已定位成功，不应判写操作失败
                if self.anti_pattern_cfg and col_name:
                    ap = self.anti_pattern_cfg.lookup(table_stem, sheet, col_name, intent.action)
                    if ap and ap.get("status") == "active" and ap.get("action") == "block_dry_run":
                        anti_pattern_hits.append({"column": col_name, "pattern": ap})
                # id_scope 主键检查（首列或显式 id 列）
                if col_idx == 1 or (col_name and "id" in col_name.lower() and new_val is not None):
                    try:
                        from engine.id_scope import get_id_scope_validator
                        v = get_id_scope_validator()
                        ok, reason = v.validate_value(table_stem, new_val)
                        if not ok:
                            id_issues.append({"column": col_name, "value": new_val, "reason": reason})
                    except Exception:
                        pass
            # 本地 ref_integrity（仅 add 操作跑，set/modify 保持快路径；单 sheet 引用完整性）
            dangling: list[dict] = []
            if intent.action == "add":
                try:
                    if result_rows and self.cli:
                        from engine.ref_integrity import validate_sheet_references
                        rows = self.cli.read_sheet(path, sheet)
                        vr = validate_sheet_references(rows, headers_clean, table_stem, sheet, {})
                        dangling = vr.get("dangling", []) or []
                except Exception:
                    pass
            # 优先级汇总 failed_kind
            # 注：anti_pattern_hits 仅作 warning signal（进 VerifyResult 供下游 repair 参考），
            # 不直接判 verify 失败——反模式命中不等于列不存在/值错（verify 已有具体 col_name
            # 说明列已定位，需进一步看 type_issues/dangling 等真实问题）
            failed_kind = None
            if dangling:
                failed_kind = ErrorType.CROSS_REF_BROKEN
            elif id_issues:
                failed_kind = ErrorType.ID_CONFLICT
            elif type_issues:
                failed_kind = ErrorType.TYPE_MISMATCH
            passed = failed_kind is None
            return VerifyResult(
                passed=passed, failed_kind=failed_kind,
                dangling_refs=dangling, id_issues=id_issues,
                type_issues=type_issues, anti_pattern_hits=anti_pattern_hits,
                checked=len(result_rows),
            )
        except Exception:
            logger.warning("verify 门控异常，降级放行", exc_info=True)
            from ..repair.error_classifier import VerifyResult as _VR
            return _VR()

    def _check_type_constraint(self, col_type: str, value: Any) -> tuple[bool, str]:
        """轻量类型校验。§3.2 薄转发到 verify_repair_loop.check_type_constraint（纯函数抽离）。"""
        from ..repair.verify_repair_loop import check_type_constraint
        return check_type_constraint(col_type, value)

    def _apply_repair_fix(self, intent: NLIntent, path: Path, sheet: str,
                          fix_payload: dict, exclude: Optional[set] = None) -> bool:
        """把 Level 1 fix_payload 应用到 intent，返回是否成功应用。

        Args:
            exclude: verify_repair_loop 维护的本轮已试/已写 ID 集，
                透传给 _allocate_pk 防 id_reallocate 多轮自撞。
        """
        try:
            fields = intent.extras.setdefault("fields", {})
            applied = False
            if "column_remap" in fix_payload:
                remap = fix_payload["column_remap"]
                new_fields = {}
                for k, v in fields.items():
                    new_fields[remap.get(k, k)] = v
                intent.extras["fields"] = new_fields
                applied = True
            if "value_coerce" in fix_payload:
                for col, val in fix_payload["value_coerce"].items():
                    fields[col] = val
                applied = True
            if fix_payload.get("clear_pk"):
                intent.locator_value = None
                intent.extras["pk_value"] = None
                applied = True
            if fix_payload.get("allocate_new_id"):
                pk_col = 1
                # 用真实 PK 列名（reward_id 等）写入 fields，避免泛 "id" 键
                # 经 matcher 别名回退才命中首列（脆弱，且 round 间易自撞）。
                _real_col = self._real_pk_col_name(
                    path, sheet, "id", intent=intent)
                new_id = self._allocate_pk(path, sheet, pk_col, exclude=exclude)
                if new_id is not None:
                    intent.extras["pk_value"] = new_id
                    fields[_real_col or "id"] = new_id
                    applied = True
            if "row_re_resolve_candidates" in fix_payload:
                cands = fix_payload["row_re_resolve_candidates"]
                if cands:
                    intent.locator_value = str(cands[0])
                    intent.extras["row_candidates"] = cands
                    applied = True
            return applied
        except Exception:
            logger.warning("应用 repair fix 异常", exc_info=True)
            return False

    def _llm_call(self, prompt: str, site: str = "react") -> str:
        """调用 codemaker LLM（单段文本 prompt），返回响应文本。供 Level 2 ReAct 循环复用。

        site：LLM 调用站点标签（react/diagnose/parse 等），供 LLMCounter by_site 统计。
        """
        client = getattr(self.parser, "client", None)
        if client is None:
            return ""
        # LLM 调用计数（capability: llm-call-instrumentation）
        try:
            if self._llm_counter is not None:
                from .llm_context import estimate_tokens
                self._llm_counter.inc(site, tokens=estimate_tokens(prompt))
        except Exception:
            pass
        try:
            sess = getattr(self.parser, "_session_id", "") or ""
            if not sess:
                sr = client.create_session(directory=getattr(self.parser, "directory", "") or "",
                                           model=getattr(self.parser, "model", "") or "")
                if not sr.ok:
                    return ""
                sess = sr.session_id
                self.parser._session_id = sess
            if not client.health_check():
                return ""
            resp = client.prompt(sess, prompt, model=getattr(self.parser, "model", "") or "",
                                 cancel_event=getattr(self, "_cancel_event", None))
            if not resp.ok:
                return ""
            return resp.response_text or ""
        except Exception:
            logger.warning("Level 2 LLM 调用失败", exc_info=True)
            return ""
        finally:
            # 汇总线程本地到实例（run 结束 snapshot 读实例）
            try:
                if self._llm_counter is not None:
                    self._llm_counter.merge_to_instance()
            except Exception:
                pass

    def _split_intents_to_nl(self, split_intents: list, text: str) -> list[NLIntent]:
        """SplitIntent[] → NLIntent[] 适配(agent 链产出转内部格式)。

        split_intents 来自 cross_table_splitter.SplitIntent 结构,
        含 text/table_hint/sheet_hint/action/fields/produces/locator_*。
        """
        out: list[NLIntent] = []
        for si in split_intents:
            extras: dict = {"fields": getattr(si, "fields", {}) or {},
                            "source": "agent_chain"}
            produces = getattr(si, "produces", None)
            if produces:
                extras["produces"] = produces
            out.append(NLIntent(
                action=getattr(si, "action", "add"),
                table_hint=getattr(si, "table_hint", None),
                sheet_hint=getattr(si, "sheet_hint", None),
                locator_value=getattr(si, "locator_value", None),
                locator_field=getattr(si, "locator_field", None),
                raw=getattr(si, "text", text) or text,
                extras=extras,
            ))
        return out

    def _llm_chain_decompose(self, text: str, hint_stem: str) -> list[NLIntent]:
        """LLM 跨表链分解器（泛化引擎,原则11）：schema 注入 + LLM 产每表一 op。

        当 splitter 模板不完整(pet/mail/新链型)时,取代 per-template 手写。
        流程:
          1. 候选表 = hint_stem + relation graph 关联表(referencing + dependencies)
          2. 读每表所有业务 sheet 的 row1+row2 表头,构 schema 块
          3. relation graph FK 链注入(哪表引用哪表)
          4. LLM 产 JSON 数组,每元素 {table,sheet,action,fields,produces,consumes}
             fields 用真实表头列名;新主键 produces="new_<stem>_id";引用他表新ID 用 <label> 占位符
          5. 解析为 NLIntent 列表,produces-inference 安全网(已 topo 前接入)补漏

        LLM 为主、规则为安全网——真正泛化,非 per-pattern 手写。
        """
        if not text or not self._ai_enhancer:
            return []
        # 1. 候选表
        from .table_relations import RelationGraph
        try:
            rg = RelationGraph.load()
            rels = rg.relations
        except Exception:
            rels = []
        cand_stems = {hint_stem} if hint_stem else set()
        for r in rels:
            fs = Path(r.from_path.replace("\\", "/")).stem
            ts = Path(r.to_path.replace("\\", "/")).stem
            if fs == hint_stem or ts == hint_stem:
                cand_stems.add(fs)
                cand_stems.add(ts)
        cand_stems.discard("")
        if not cand_stems:
            return []
        # 2. 候选表路径
        all_tables = {p.stem: p for p in self.cli.list_tables()}
        cand_paths = [(s, all_tables[s]) for s in cand_stems if s in all_tables]
        if not cand_paths:
            return []
        # 3. schema 块 + FK 链
        schema_lines = []
        for stem, p in cand_paths:
            try:
                sheets = self.cli.get_sheets(p)
            except Exception:
                continue
            biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s]
            for sh in biz[:4]:  # 限每表4 sheet 防膨胀
                try:
                    hdrs = self.cli.read_header(p, sh)
                    trow = self.cli.read_type_row(p, sh)
                except Exception:
                    continue
                cols = []
                for h, t in zip(hdrs, trow):
                    if h:
                        cols.append(str(h) + (f"（{t}）" if t and str(t) != str(h) else ""))
                if cols:
                    schema_lines.append(f"- {stem}/{sh}: " + " | ".join(cols[:18]))
        fk_lines = []
        for r in rels:
            fs = Path(r.from_path.replace("\\", "/")).stem
            ts = Path(r.to_path.replace("\\", "/")).stem
            if fs in cand_stems and ts in cand_stems and fs != ts:
                fk_lines.append(f"  {fs}.{r.from_sheet}.{r.from_column} → {ts}.{r.to_sheet}.{r.to_column}")
        schema_block = "\n".join(schema_lines) or "（无 schema）"
        fk_block = "\n".join(fk_lines) or "（无显式 FK）"
        # 4. LLM prompt
        prompt = (
            "你是配表跨表链分解器。一条指令可能涉及多张表(经外键关联)。"
            "请分解为每张表一个原子操作,用真实表头列名。\n\n"
            f"## 候选表 schema(row1 显示名,row2 规范名)\n{schema_block}\n\n"
            f"## 外键关联(决定 produces/consumes)\n{fk_block}\n\n"
            f"## 指令\n{text}\n\n"
            "## 输出 fenced JSON 数组,每元素一个原子操作:\n"
            "```json\n[{\"table\":\"<stem>\",\"sheet\":\"<sheet名>\",\"action\":\"add|set\","
            "\"fields\":{<真实表头列名>:<值>},\"produces\":\"new_<stem>_id 或空\","
            "\"consumes\":{<列名>:\"<produces_label>\"}}]\n```\n"
            "规则:\n"
            "- fields 键必须用上面 schema 的真实表头列名(row1 显示名)\n"
            "- 新增行若主键自动(未在指令给)→ produces=\"new_<stem>_id\"\n"
            "- 引用他表新产出的 ID → 该字段值用 \"<produces_label>\" 占位符,并在 consumes 标注\n"
            "- 仅输出 JSON,不要解释"
        )
        llm_out = self._llm_call(prompt)
        if not llm_out:
            return []
        import json as _json
        import re as _re
        m = _re.search(r"```json\s*(\[.*?\])\s*```", llm_out, _re.DOTALL)
        if not m:
            m = _re.search(r"\[\s*\{.*\}\s*\]", llm_out, _re.DOTALL)
        if not m:
            return []
        try:
            arr = _json.loads(m.group(1))
        except ValueError:
            return []
        # 5. 解析为 NLIntent
        intents: list[NLIntent] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            stem = str(item.get("table") or "").strip()
            sheet = str(item.get("sheet") or "").strip() or None
            act = str(item.get("action") or "add").strip().lower()
            if act not in ("add", "set", "delete", "get"):
                act = "add"
            fields = item.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            produces = str(item.get("produces") or "").strip() or None
            consumes = item.get("consumes") or {}
            # consumes: 字段值替换为 <label> 占位符
            if isinstance(consumes, dict):
                for k, label in consumes.items():
                    if k in fields and label:
                        fields[k] = f"<{str(label).strip()}>"
            extras = {"fields": fields, "source": "llm_chain"}
            if produces:
                extras["produces"] = produces
            intents.append(NLIntent(
                action=act, table_hint=stem, sheet_hint=sheet,
                raw=text, extras=extras,
            ))
        return intents

    def _coerce_fix_fields_to_headers(self, fix_fields, reason: str,
                                       path: Path, sheet: str) -> dict:
        """post-LLM validator：强制 fix_fields keys 映射进真实表头（原则9 part2）。

        LLM reason 常识别出语义等价列（"存在语义等价列「选项功能」"）但 fix_fields
        未真替换原失败 key → 重跑仍 "未找到列[X]"。本方法对每个 key：
          1. 跑 _translate_dotted_keys(type_aliases) 翻译点分键
          2. matcher.match 校验——能匹配则用真实表头列名作 key
          3. 仍不匹配 → 从 reason 解析「X」/"X"/'X' 候选列名，matcher 验证后替换
          4. 都失败 → 保留翻译后 key（让失败清晰可见，不静默丢）
        返回 new dict（不污染原 fix_fields）。
        """
        if not isinstance(fix_fields, dict) or not fix_fields:
            return fix_fields
        try:
            headers = self.cli.read_header(path, sheet) or []
        except Exception:
            return fix_fields
        if not headers:
            return fix_fields
        matcher = self._make_matcher(headers, path.stem, sheet, path)
        type_aliases = self._type_aliases(path, sheet, headers)
        alias_keys = set(matcher.yaml_aliases.keys())

        import re as _re
        reason_candidates = _re.findall(r'[「"\']([^」"\']{1,30})[」"\']', reason or "")

        def _match_col(name: str):
            if not name:
                return None
            m = matcher.match(name) or matcher.match_best(name)
            return m.column if m else None

        coerced: dict = {}
        for k, v in fix_fields.items():
            translated = self._translate_dotted_keys({k: v}, headers, alias_keys, type_aliases)
            tk, tv = next(iter(translated.items()))
            col = _match_col(tk)
            if col:
                coerced[col] = tv
                continue
            # 翻译后仍不匹配 → 从 reason 提取候选列
            for cand in reason_candidates:
                c = _match_col(cand)
                if c:
                    coerced[c] = tv
                    break
            else:
                coerced[tk] = tv  # 保留，让失败清晰
        return coerced

    def _run_react_repair(self, intent: NLIntent, path: Path, sheet: str,
                          classified_err, res: AgentResult) -> Optional[NLIntent]:
        """Level 2：手写文本协议 ReAct 循环。LLM 输出 fenced JSON tool_call → 调 skill_executor
        → 结果回灌 prompt → 循环至产出修正 intent。单次 skill tool 调用上限 skill_tool_call_limit。

        codemaker 无原生 function-calling，故走文本协议（design 已确认）。
        """
        if not self.enable_skill_tools_recovery or self.skill_executor is None:
            # 未启用 skill tools：退回纯 LLM 诊断（基于结构化反馈产出修正 fields）
            return self._llm_diagnose_only(intent, path, sheet, classified_err, res)
        from ...tools import make_skill_tools
        tool_map = {t.name: t for t in make_skill_tools(self.skill_executor)}
        tool_desc = "\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in tool_map.values())
        feedback = classified_err.as_feedback()
        sys_prompt = (
            "你是表格修复助手。当前写操作失败，需调用 skill 工具探查后产出修正意图。\n"
            f"失败信息：{feedback}\n目标表：{path.stem} sheet={sheet} 动作={intent.action}\n"
            f"可用工具：\n{tool_desc}\n\n"
            "每步输出 EITHER（二选一）：\n"
            "1. 调用工具：```json\n{\"tool\": \"工具名\", \"args\": {参数}}\n```\n"
            "2. 产出修正：```json\n{\"fix_fields\": {列名: 值}, \"reason\": \"...\"}\n```\n"
            "先探查（如 get_table_structure/fuzzy_search_value），再产出修正。"
        )
        messages = [sys_prompt]
        import json as _json
        import re as _re
        for _ in range(self.skill_tool_call_limit):
            _ce = getattr(self, "_cancel_event", None)
            if _ce is not None and _ce.is_set():
                return None
            llm_out = self._llm_call("\n\n".join(messages))
            if not llm_out:
                return None
            m = _re.search(r"```json\s*(\{.*?\})\s*```", llm_out, _re.DOTALL)
            if not m:
                messages.append(f"【助手】\n{llm_out}\n（未识别为工具调用或修正，请用 fenced JSON 输出）")
                continue
            try:
                parsed = _json.loads(m.group(1))
            except ValueError:
                messages.append("【助手】\nJSON 解析失败，请重试")
                continue
            if "fix_fields" in parsed:
                intent.extras["fields"] = self._coerce_fix_fields_to_headers(
                    parsed["fix_fields"], parsed.get("reason", ""), path, sheet)
                res.add_thinking("修复", f"Level 2 ReAct 修正 fields：{parsed.get('reason','')}")
                return intent
            if "tool" in parsed:
                tname = parsed["tool"]
                targs = parsed.get("args", {})
                tool = tool_map.get(tname)
                if tool is None:
                    messages.append(f"【助手】\n未知工具 {tname}")
                    continue
                try:
                    result = tool.invoke(targs)
                except Exception as e:
                    result = f"工具调用失败：{e}"
                messages.append(f"【助手】\n调用 {tname}({targs})")
                messages.append(f"【工具】\n{result}")
                continue
            messages.append("【助手】\n输出缺少 tool/fix_fields 字段")
        return None

    def _llm_diagnose_only(self, intent: NLIntent, path: Path, sheet: str,
                           classified_err, res: AgentResult) -> Optional[NLIntent]:
        """Level 2 退回纯 LLM 诊断（未启用 skill tools）：基于结构化反馈产出修正 fields。

        N1：注入同表历史失败案例，供 LLM 参考"该表常见错误与修正"。
        """
        feedback = classified_err.as_feedback()
        # N1: 同表失败案例参考（同表历史上类似的错误怎么修的）
        case_hint = ""
        try:
            from .dialog_logger import get_dialog_logger
            dl = get_dialog_logger()
            failures = dl.query_examples(path.stem, limit=2, grade="failure")
            if failures:
                lines = ["## 同表历史失败案例（参考常见错误与修正方向，勿照搬值）"]
                for fa in failures:
                    if not isinstance(fa, dict):
                        continue
                    ut = (fa.get("user_text") or "").strip()
                    msg = (fa.get("agent_message") or "").strip().split("\n")[0][:100]
                    if ut:
                        lines.append(f"-「{ut}」失败：{msg}")
                if len(lines) > 1:
                    case_hint = "\n".join(lines) + "\n\n"
        except Exception:
            logger.debug("repair失败案例注入异常（已降级）", exc_info=True)
        prompt = (
            "你是表格修复助手。写操作失败，请基于失败信息产出修正字段映射（JSON）。\n"
            f"{case_hint}失败信息：{feedback}\n目标表：{path.stem} sheet={sheet} 动作={intent.action}\n"
            f"原 fields：{intent.extras.get('fields', {})}\n"
            "输出 ```json\n{\"fix_fields\": {列名: 值}, \"reason\": \"...\"}\n```"
        )
        llm_out = self._llm_call(prompt)
        if not llm_out:
            return None
        import json as _json
        import re as _re
        m = _re.search(r"```json\s*(\{.*?\})\s*```", llm_out, _re.DOTALL)
        if not m:
            return None
        try:
            parsed = _json.loads(m.group(1))
        except ValueError:
            return None
        if "fix_fields" in parsed and isinstance(parsed["fix_fields"], dict):
            intent.extras["fields"] = self._coerce_fix_fields_to_headers(
                parsed["fix_fields"], parsed.get("reason", ""), path, sheet)
            res.add_thinking("修复", f"Level 2 LLM 诊断修正：{parsed.get('reason','')}")
            return intent
        return None

    def _adaptive_rounds(self) -> int:
        """verify-repair 轮数按子任务数自适应：N≥6 降为 1 轮，N≥4 降为 2 轮，其余保持。
        避免 9 子任务 × 3 轮 × 多次 LLM 的最坏组合（方案第三层）。"""
        base = self.verify_repair_max_rounds
        n = getattr(self, "_n_subtasks", 1) or 1
        if n >= 6:
            return 1
        if n >= 4:
            return min(base, 2)
        return base

    def _run_verify_repair_loop(self, intent: NLIntent, path: Path, sheet: str,
                                res: AgentResult, out: Optional[AgentResult],
                                backup_file, is_write: bool) -> Optional[AgentResult]:
        """verify→repair→execute 迭代环（最多 verify_repair_max_rounds 轮）。快路径优先。

        成功路径（首次 out.ok 且 verify 通过）直接返回，零 LLM 往返。
        失败路径：classify→playbook 选策略→Level 1 fix→re-dispatch→verify；
        Level 1 失败升级 Level 2 ReAct（LLM+skill tools）。
        """
        from ..repair.error_classifier import classify as _classify, ErrorType, VerifyResult
        from ..repair.repair_context import RepairContext
        from ..repair.repair_playbook import RepairPlaybook, RepairActionKind, RepairTaskCtx
        from .skill_context import _format_column_types_block

        # 快路径：首次成功且 verify 通过 → 直接返回
        if out is not None and getattr(out, "ok", False):
            vr = self._verify_write(intent, path, sheet, out)
            if vr.passed:
                return out
            # 成功写入但 verify 失败（如 cross_ref_broken）→ 进入 repair
            verify_output = vr
            first_ok = True
        else:
            verify_output = VerifyResult()
            first_ok = False

        if not self.enable_verify_repair_loop or self.repair_playbook is None:
            return out  # 开关关闭：交回原 retry 逻辑处理

        rctx = RepairContext()
        # id_reallocate 自撞防护：本轮已分配/已写 ID 集，透传给 _apply_repair_fix
        # → _allocate_pk(exclude=...)。轮1 写 100603 未回滚时，轮2 不会再算出同值。
        _attempted_ids: set = set()
        headers = self.cli.read_header(path, sheet) if self.cli else []
        headers_clean = [(h or "").split(":")[0] for h in headers if h]
        vc = _load_value_constraints().get(path.stem, {}).get(sheet, {}).get("columns", {})
        col_types = {c: v.get("type", "") for c, v in vc.items()}
        column_aliases = self.column_cfg.all_aliases(path.stem, sheet) if self.column_cfg else {}
        # RowAliasConfig 无 all_aliases（用 rules_for），repair_playbook 的 row_not_found
        # handler 仅在 row_aliases 非空时用候选；此处传空让零命中升级 LLM（符合设计）。
        row_aliases: dict[str, list[str]] = {}

        current_out = out
        current_ok = first_ok
        for _round in range(self._adaptive_rounds()):
            _ce = getattr(self, "_cancel_event", None)
            if _ce is not None and _ce.is_set():
                break
            # 分类
            classified = _classify(None, res if not current_ok else res, verify_output, context={
                "table_stem": path.stem, "sheet": sheet, "headers": headers_clean,
            })
            # #41 repair 分级：语义类错误（需人工判断，自动修复徒劳）+ 交互模式
            # → 直接 break 走 post-exhaustion ask 反问用户，不浪费 LLM 轮次。
            # 规则可自修类（type_mismatch/column_not_found/id_conflict 等）继续自动 loop。
            if (getattr(self, "_ask_callback", None) is not None
                    and classified.error_type in (ErrorType.SEMANTIC_OUTLIER, ErrorType.UNKNOWN)):
                res.add_thinking("修复",
                    f"语义类错误 {classified.error_type.value}，跳过自动修复，直接反问用户")
                break
            # repair 成功捕获快路径：先查 committed recipe，命中直接 apply 已验证 fix
            # （经验真值层，非 LLM 猜），跳 playbook/LLM。失败回落正常 playbook。
            _fast = self._try_recipe_fast_path(
                classified, intent, path, sheet, res, backup_file, is_write)
            if _fast is not None:
                return _fast
            strat = self.repair_playbook.select(classified.error_type)
            # 重复策略检测
            is_repeat = rctx.is_repeat_strategy(classified.error_type, strat.name)
            # 分类信号回流学习（置信度低或重复时记录反模式候选）
            self._record_repair_signal(classified, path.stem, sheet, is_repeat)
            if is_repeat:
                res.add_thinking("修复", f"重复策略 {strat.name}，中止迭代环")
                break
            # 失败回滚（写操作）
            if is_write and backup_file and current_out is not None and not current_ok:
                self._rollback_write(path, backup_file, res)
            task_ctx = RepairTaskCtx(
                table_stem=path.stem, sheet=sheet, path=str(path),
                headers=headers_clean, col_types=col_types,
                column_aliases=column_aliases, row_aliases=row_aliases,
                intent=intent,
                llm_caller=(getattr(self, "_ai_enhancer", None)._call_llm
                            if getattr(self, "_ai_enhancer", None) is not None else None),
            )
            action = self.repair_playbook.apply(classified.error_type, classified, task_ctx)
            res.add_thinking("修复", f"轮{_round+1}：{strat.name} → {action.kind.value}（{action.reason}）")
            rctx.record_attempt(classified.error_type, strat.name,
                                failed_col=classified.failed_col, failed_val=classified.failed_val,
                                detail=action.reason)
            if action.kind == RepairActionKind.ABORT:
                break
            if action.kind == RepairActionKind.RE_EXECUTE:
                applied = self._apply_repair_fix(intent, path, sheet, action.fix_payload,
                                                 exclude=_attempted_ids)
                # 记录本轮分配的 PK（id_reallocate），供下一轮 exclude 防自撞
                _pv = (getattr(intent, "extras", None) or {}).get("pk_value")
                if _pv is not None:
                    try:
                        _attempted_ids.add(int(_pv))
                    except (ValueError, TypeError):
                        pass
                if not applied:
                    res.add_thinking("修复", "Level 1 fix 应用失败，升级 Level 2")
                    action_kind = "escalate"
                else:
                    new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
                    vr2 = self._verify_write(intent, path, sheet, new_out)
                    current_out = new_out
                    current_ok = getattr(new_out, "ok", False) and vr2.passed
                    verify_output = vr2
                    if current_ok:
                        res.ok = True
                        res.message = f"迭代环修复成功（轮{_round+1}）：{action.reason}"
                        res._skip_summarize = True
                        self._capture_repair_recipe(classified, path.stem, sheet,
                                                    action.fix_payload, "level1")
                        return new_out
                    action_kind = "done"
                    continue
            else:
                action_kind = "escalate"
            # 升级 Level 2（opt-in：CODEMAKER_REPAIR_LLM_SUBTYPE=1 时先试 LLM 细分修复产
            # 结构化 fix_payload，失败再走 ReAct。与 ValidatorAgent 复用同一 LLM 通道）
            if action_kind == "escalate":
                if (os.getenv("CODEMAKER_REPAIR_LLM_SUBTYPE", "0") == "1"
                        and self._ai_enhancer is not None):
                    llm_action = self.repair_playbook.apply_llm_subtype(classified, task_ctx)
                    if llm_action.kind == RepairActionKind.RE_EXECUTE:
                        res.add_thinking("修复", f"轮{_round+1}：LLM 细分修复 → {llm_action.reason}")
                        applied = self._apply_repair_fix(intent, path, sheet, llm_action.fix_payload,
                                                         exclude=_attempted_ids)
                        _pv = (getattr(intent, "extras", None) or {}).get("pk_value")
                        if _pv is not None:
                            try:
                                _attempted_ids.add(int(_pv))
                            except (ValueError, TypeError):
                                pass
                        if applied:
                            new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
                            vr2 = self._verify_write(intent, path, sheet, new_out)
                            current_out = new_out
                            current_ok = getattr(new_out, "ok", False) and vr2.passed
                            verify_output = vr2
                            if current_ok:
                                res.ok = True
                                res.message = f"LLM 细分修复成功（轮{_round+1}）：{llm_action.reason}"
                                res._skip_summarize = True
                                self._capture_repair_recipe(classified, path.stem, sheet,
                                                            llm_action.fix_payload, "llm_subtype")
                                return new_out
                            continue  # 修了但仍 verify 失败 → 下一轮重新分类
                        res.add_thinking("修复", "LLM 细分修复 fix 应用失败，回落 ReAct")
                    else:
                        res.add_thinking("修复",
                            f"LLM 细分修复未产出有效 fix（{llm_action.reason}），回落 ReAct")
                fixed_intent = self._run_react_repair(intent, path, sheet, classified, res)
                if fixed_intent is not None:
                    new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
                    vr2 = self._verify_write(intent, path, sheet, new_out)
                    current_out = new_out
                    current_ok = getattr(new_out, "ok", False) and vr2.passed
                    verify_output = vr2
                    if current_ok:
                        res.ok = True
                        res.message = f"Level 2 ReAct 修复成功（轮{_round+1}）"
                        res._skip_summarize = True
                        return new_out
        # 中断反问：达上限仍失败时，经 ask_callback 问用户是否修改续跑。
        # 仅 chat_stream 路径注入 _ask_callback；非流 /chat 路径为 None 走原逻辑。
        _ask = getattr(self, "_ask_callback", None)
        if _ask is not None:
            _has_cls = 'classified' in locals()
            _failed_col = getattr(classified, "failed_col", "") if _has_cls else ""
            _failed_val = getattr(classified, "failed_val", "") if _has_cls else ""
            _root_cause = classified.root_cause if _has_cls else "迭代环达上限"
            _is_semantic_break = ('classified' in locals()
                                  and classified.error_type in (ErrorType.SEMANTIC_OUTLIER, ErrorType.UNKNOWN))
            _strategies_list = rctx.summarized_strategies()
            _strategies_str = "已尝试：" + " | ".join(_strategies_list) if _strategies_list else ""
            # PK/ID 冲突:预计算建议新 ID,前端渲染"接受/输入"简化交互
            _is_pk_conflict = ('classified' in locals()
                              and classified.error_type == ErrorType.ID_CONFLICT)
            _suggested_id = None
            if _is_pk_conflict:
                try:
                    _pk_col_idx = self._locate_pk_col(path, sheet)
                    if _pk_col_idx:
                        _suggested_id = self._allocate_pk(
                            path, sheet, _pk_col_idx, exclude=_attempted_ids)
                except Exception:
                    _suggested_id = None
            _ask_payload = {
                "reason": ("自动修复多次仍未通过，疑似语义类问题，已暂停"
                           if _is_semantic_break else "自动修复达到上限仍未通过，已暂停等你处理"),
                "error_type": (rctx.last_error_type() or ErrorType.UNKNOWN).value,
                "root_cause": _root_cause,
                "table": path.stem, "sheet": sheet,
                "failed_col": _failed_col, "failed_val": _failed_val,
                "attempted_strategies": (_strategies_str if _strategies_str
                                         else "已尝试多轮自动修复"),
                "snip": (getattr(intent, "raw", "") or "")[:120],
            }
            # 要求 B：挂 user_friendly 大白话（前端优先渲染）
            try:
                from ..repair.error_classifier import build_user_friendly as _buf
                _ask_payload["user_friendly"] = _buf(
                    (rctx.last_error_type() or ErrorType.UNKNOWN).value,
                    root_cause=_root_cause, table=path.stem, sheet=sheet,
                    failed_col=_failed_col, failed_val=_failed_val,
                    suggested_id=_suggested_id)
            except Exception:
                pass
            if _is_pk_conflict and _suggested_id is not None:
                # PK 冲突:简化文案 + 带建议值,前端走"接受/输入"交互
                _ask_payload["suggested_id"] = _suggested_id
                _ask_payload["mode_hint"] = "pk_conflict"
                _ask_payload["suggestion"] = (
                    f"「{_failed_col or 'ID'}」值「{_failed_val}」已被占用,"
                    f"建议改为「{_suggested_id}」"
                )
                _ask_payload["example"] = ""
            else:
                # 非 PK 冲突:保留原 field 表格填值引导
                _ask_payload["suggestion"] = (
                    "请在下方表格按失败列填入正确字段值"
                    f"（失败列「{_failed_col or '未知'}」原值「{_failed_val or '空'}」）；"
                    "或点「跳过」放弃此项继续后续任务。"
                )
                _ask_payload["example"] = (f"{_failed_col or '失败列'}填「（此处填正确值）」"
                                          if _failed_col else "")
            _reply = _ask(_ask_payload) or {}
            _mode = _reply.get("mode", "skip")
            if _mode == "field":
                _fp = _reply.get("fix_payload") or {}
                # PK 冲突"接受建议":前端只回传 mode=field + accept_suggest=true
                # → 后端用预算的 suggested_id 填入实际 PK 列名(非泛化 failed_col="ID")
                # _failed_col 常被 classifier 泛化为"ID",intent fields 里键是 reward_id
                # 故用 _locate_pk_col 读真实列名匹配,否则 value_coerce 键不匹配 → 不生效
                if _is_pk_conflict and (_reply.get("accept_suggest") or _reply.get("custom_id")):
                    _new_pk = (_reply.get("custom_id")
                               if _reply.get("custom_id") else _suggested_id)
                    if _new_pk is not None:
                        _real_pk_col = self._real_pk_col_name(path, sheet, _failed_col, intent)
                        _fp = {"value_coerce": {_real_pk_col: _new_pk}}
                applied = self._apply_repair_fix(intent, path, sheet, _fp)
                if applied:
                    new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
                    vr2 = self._verify_write(intent, path, sheet, new_out)
                    if getattr(new_out, "ok", False) and vr2.passed:
                        res.ok = True
                        res.message = f"用户中断修复成功：{_root_cause}"
                        res._skip_summarize = True
                        return new_out
                    res.add_thinking("修复", "用户 field 修复后仍 verify 失败，记 failure 继续")
                else:
                    res.add_thinking("修复", "用户 field 修复 fix_payload 应用失败，记 failure 继续")
            elif _mode == "nl":
                _nl_text = (_reply.get("text") or "").strip()
                if _nl_text and self.parser is not None:
                    try:
                        ni = self.parser.parse(_nl_text)
                    except Exception:
                        ni = None
                    if ni is not None:
                        intent.action = ni.action
                        intent.table_hint = ni.table_hint or intent.table_hint
                        intent.extras = ni.extras
                        intent.raw = _nl_text
                        new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
                        vr2 = self._verify_write(intent, path, sheet, new_out)
                        if getattr(new_out, "ok", False) and vr2.passed:
                            res.ok = True
                            res.message = f"用户重描述修复成功：{_root_cause}"
                            res._skip_summarize = True
                            return new_out
                        res.add_thinking("修复", "用户 nl 重描述后仍 verify 失败，记 failure 继续")
                    else:
                        res.add_thinking("修复", "用户 nl 重描述解析失败，记 failure 继续")
                else:
                    res.add_thinking("修复", "用户 nl 回复为空或 parser 不可用，记 failure 继续")
            # mode=skip: 直落 failure
        # 达上限仍失败
        rctx.set_final_failure({
            "error_type": (rctx.last_error_type() or ErrorType.UNKNOWN).value,
            "root_cause": classified.root_cause if 'classified' in locals() else "迭代环达上限",
            "attempted_strategies": rctx.summarized_strategies(),
            "error_type_history": [e.value for e in rctx.error_type_history],
        })
        res.ok = False
        res.message = f"verify-repair 迭代环达上限（{self._adaptive_rounds()}轮）仍失败：" \
                      f"{rctx.final_failure.get('root_cause','')}"
        # 结构化失败挂在 thinking_steps（AgentResult 无 extras 字段，thinking_steps 为 list[dict] 必存在）
        try:
            res.thinking_steps.append({
                "phase": "修复失败",
                "detail": "迭代环达上限",
                "repair_failure": rctx.final_failure,
            })
        except Exception:
            pass
        # #38/#40 结构化失败清单（供 Step6 显式失败总结 + 前端渲染失败块）
        try:
            res.failures.append({
                "type": "verify_repair_exhausted",
                "table": path.stem, "sheet": sheet,
                "col": _failed_col if '_failed_col' in locals() else "",
                "root_cause": rctx.final_failure.get("root_cause", ""),
                "attempted_strategies": rctx.summarized_strategies(),
                "suggestion": "改字段值/重描述/补建",
                "snip": (getattr(intent, "raw", "") or "")[:120],
                "status": "unresolved",
                "user_reply": (_reply.get("mode")
                               if '_reply' in locals() and isinstance(_reply, dict) else None),
            })
        except Exception:
            pass
        # N2: 迭代环最终失败时，聚合本 session 失败 trace 喂 AI 归纳反模式。
        # 生产路径接入 induce_anti_patterns（之前只在 table_case_eval 调）。
        # env CODEMAKER_INDUCE_PROD=1 才在生产跑（默认关 → 回 eval/手动触发，
        # 避免每次失败 run +1 LLM 归纳 + mini 回归拖慢）。条件：enable_skill + failed_ops 非空。
        if (os.getenv("CODEMAKER_INDUCE_PROD", "0") == "1"
                and self.enable_skill and rctx.failed_ops):
            try:
                failed_traces = [{
                    "input": (getattr(intent, "raw", "") or "")[:120],
                    "error_type": (fo.error_type.value if fo.error_type else ""),
                    "error_detail": (fo.detail or "")[:200],
                    "entries_summary": f"表={path.stem} sheet={sheet} col={fo.failed_col or ''} val={fo.failed_val or ''}",
                } for fo in rctx.failed_ops]
                enhancer = getattr(self, "_ai_enhancer", None)
                if enhancer is not None:
                    produced = get_skill_updater().induce_anti_patterns(
                        failed_traces, enhancer=enhancer)
                    if produced:
                        res.add_thinking("归纳",
                            f"AI 反模式归纳产出 {len(produced)} 条候选（pending_review，"
                            f"待 promote_pending_anti_patterns 升级）")
            except Exception:
                logger.debug("AI 反模式归纳接入失败（已降级）", exc_info=True)
        return res

    def _record_repair_signal(self, classified, table_stem: str, sheet: str,
                              is_repeat: bool) -> None:
        """capability: error-classification-repair —— 分类置信度低或同类型重复时记录反模式候选信号。

        信号类型映射：type_mismatch→frequent_type_mismatch、column_not_found→
        frequent_column_mapping_error、row_not_found→frequent_row_match_failure、
        cross_ref_broken→missed_cross_table_dep。仅 enable_skill 时写（复用 evidence 门控）。
        """
        if not self.enable_skill:
            return
        from ..repair.error_classifier import ErrorType
        signal_map = {
            ErrorType.TYPE_MISMATCH: "frequent_type_mismatch",
            ErrorType.COLUMN_NOT_FOUND: "frequent_column_mapping_error",
            ErrorType.ROW_NOT_FOUND: "frequent_row_match_failure",
            ErrorType.CROSS_REF_BROKEN: "missed_cross_table_dep",
        }
        signal_type = signal_map.get(classified.error_type)
        if not signal_type:
            return
        # 仅在置信度低或重复出现时记录（避免高频噪声）
        if classified.confidence >= 0.5 and not is_repeat:
            return
        try:
            get_skill_updater()._ingest_anti_pattern_signal({
                "signal_type": signal_type,
                "table_stem": table_stem,
                "sheet": sheet,
                "col": {"resolved": classified.failed_col or ""},
                "intent_action": getattr(classified, "failed_step", "") or "",
                "reason": classified.root_cause,
            })
        except Exception:
            logger.warning("repair 信号回流失败（不阻断）", exc_info=True)

    def _capture_repair_recipe(self, classified, table_stem: str, sheet: str,
                               fix_payload: dict, source: str) -> None:
        """repair 成功 → 捕获经验真值 fix → staging jsonl。
        同 error_signature 累积 N 次 → promote committed repair_recipes.yaml(active)。
        这是「失败→正确做法」的经验真值源（re-verify 验过，非 LLM 猜）。
        """
        if not self.enable_skill:
            return
        try:
            et = classified.error_type.value if classified.error_type else ""
            get_skill_updater().ingest_repair_success(
                et, table_stem, sheet, classified.failed_col or "",
                fix_payload or {}, source)
        except Exception:
            logger.debug("repair recipe 捕获失败（不阻断）", exc_info=True)

    def _try_recipe_fast_path(self, classified, intent: NLIntent, path: Path,
                              sheet: str, res: AgentResult, backup_file,
                              is_write: bool) -> Optional[AgentResult]:
        """repair 快路径：查 committed repair_recipes.yaml 的 active recipe，
        同 error_signature 有已验证 fix → 直接 apply + re-verify，跳 playbook/LLM。
        通过返回 new_out；无 recipe / apply 失败 / re-verify 仍败 → None（回落 playbook）。
        """
        if not self.enable_skill:
            return None
        try:
            et = classified.error_type.value if classified.error_type else ""
            recipe = get_skill_updater().lookup_repair_recipe(
                et, path.stem, sheet, classified.failed_col or "")
            if not recipe:
                return None
            fp = recipe.get("fix_payload") or {}
            if not fp:
                return None
            if is_write and backup_file and not getattr(res, "ok", False):
                self._rollback_write(path, backup_file, res)
            applied = self._apply_repair_fix(intent, path, sheet, fp)
            if not applied:
                return None
            new_out = self._safe_redispatch(intent, path, sheet, res, backup_file, is_write)
            vr2 = self._verify_write(intent, path, sheet, new_out)
            if getattr(new_out, "ok", False) and vr2.passed:
                res.ok = True
                res.message = f"recipe 快路径修复成功（{recipe.get('id')}，{recipe.get('fix_kind')}）"
                res._skip_summarize = True
                res.add_thinking("修复",
                    f"recipe 快路径命中 {recipe.get('id')}：apply 已验证 fix {list(fp.keys())}，跳 playbook/LLM")
                return new_out
            return None
        except Exception:
            logger.debug("recipe 快路径异常，回落 playbook", exc_info=True)
            return None

    def _safe_redispatch(self, intent: NLIntent, path: Path, sheet: str,
                         res: AgentResult, backup_file, is_write: bool) -> Optional[AgentResult]:
        """repair 后重新执行（_dispatch 复用），异常时回滚并返回失败 res。"""
        try:
            if intent.action == "add":
                return self._run_add(intent, path, sheet, res)
            elif intent.action == "delete":
                return self._run_delete(intent, path, sheet, res)
            elif intent.action == "get":
                return self._run_get(intent, path, sheet, res)
            elif intent.action == "col" or intent.action in ("col_add", "col_delete", "col_rename", "col_list"):
                return self._run_col(intent, path, sheet, res)
            else:
                return self._run_set(intent, path, sheet, res)
        except Exception:
            if is_write and backup_file:
                self._rollback_write(path, backup_file, res)
            logger.warning("repair 重派发异常", exc_info=True)
            res.add("repair_redispatch", False, "重派发异常")
            return res

    def _check_dangling_fk_refs(self, partitions: list,
                                produced: dict[str, str]) -> dict[tuple[str, str], list[dict]]:
        """第三层B 深度校验：FK 列已解析为具值时，核验指向的目标行是否存在。

        占位符残留检查（run 内 Step6 前扫）只看 <...> 未解析这一强信号；本方法补
        "指向行存在性"：对每个 partition result_rows 中具值（非占位符）的 FK 列，
        按 table_relations 关系图找目标表/sheet/PK 列，produced 本批产出 ∪ 写盘
        read-back 双路核验。任一存在即视为有效（避免对预存行/跨表 PK 误报）；
        均不存在才记悬空引用。

        Returns:
            {(table_stem, table_sheet): [{"idx","col","value","target"}]} 悬空引用清单。
        """
        from pathlib import Path as _P
        try:
            from .table_relations import RelationGraph
            graph = RelationGraph.load()
        except Exception:
            logger.warning("深度校验：关系图加载失败，跳过", exc_info=True)
            return {}
        if not graph.relations:
            return {}

        def _norm(s):
            return str(s or "").split(":")[0].strip().lower()

        produced_ids = {str(v).strip() for v in produced.values() if v not in (None, "")}

        # 按 (from_stem, from_sheet) 索引出向 FK 边，避免每行全表扫
        edges_by_src: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
        for r in graph.relations:
            if r.relation_type and r.relation_type not in ("foreign_key", "co_occur"):
                continue
            src = (_P(r.from_path).stem, r.from_sheet)
            edges_by_src.setdefault(src, []).append(
                (r.from_column, r.to_path, r.to_sheet, r.to_column))

        dangling: dict[tuple[str, str], list[dict]] = {}
        for _pi, p in enumerate(partitions):
            if not p.get("executed"):
                continue
            _res = p.get("res")
            if _res is None:
                continue
            rows = getattr(_res, "result_rows", None) or []
            stem = getattr(_res, "table_stem", "") or ""
            sheet = getattr(_res, "table_sheet", "") or ""
            if not stem or not sheet:
                continue
            edges = edges_by_src.get((stem, sheet))
            if not edges:
                continue
            edge_map: dict[str, list[tuple[str, str, str]]] = {}
            for from_col, to_path, to_sheet, to_col in edges:
                edge_map.setdefault(_norm(from_col), []).append((to_path, to_sheet, to_col))
            for r in rows:
                if not isinstance(r, dict):
                    continue
                nv = r.get("new_value")
                sv = "" if nv is None else str(nv).strip()
                if not sv or (sv.startswith("<") and sv.endswith(">")):
                    continue  # 占位符残留已由强信号检查覆盖
                col_name = r.get("col_name") or r.get("col", "")
                tgts = edge_map.get(_norm(col_name))
                if not tgts:
                    continue
                for (to_path, to_sheet, to_col) in tgts:
                    if sv in produced_ids:
                        continue  # 本批产出，有效
                    if self._fk_target_row_exists(to_path, to_sheet, to_col, sv):
                        continue  # 写盘存在，有效
                    dangling.setdefault((stem, sheet), []).append({
                        "idx": _pi + 1,
                        "col": col_name,
                        "value": sv,
                        "target": f"{_P(to_path).stem}/{to_sheet}.{to_col}",
                    })
        return dangling

    def _fk_target_row_exists(self, to_path: str, to_sheet: str,
                              to_col: str, value: str) -> bool:
        """read-back 核验：目标表 sheet 的 to_col 列是否存在值==value 的行。

        保守策略：文件/sheet/表头不可解析时返回 True（不报悬空，避免误报）。
        只有能确认扫到目标列且无匹配行时才返回 False。
        """
        if self.cli is None:
            return True
        from pathlib import Path as _P
        target = None
        ws_root = getattr(self.cli, "workspace", None)
        if ws_root is not None:
            cand = _P(ws_root) / to_path
            if cand.exists():
                target = cand
        if target is None:
            stem = _P(to_path).stem
            for tp in self.cli.list_tables():
                if tp.stem == stem:
                    target = tp
                    break
        if target is None:
            return True  # 找不到目标文件，不报悬空（保守）
        try:
            if to_sheet not in self.cli.get_sheets(target):
                return True
            headers = self.cli.read_header(target, to_sheet) or []
        except Exception:
            return True

        def _norm(s):
            return str(s or "").split(":")[0].strip().lower()

        tc_norm = _norm(to_col)
        ci = None
        for i, h in enumerate(headers):
            if _norm(h) == tc_norm:
                ci = i
                break
        if ci is None:
            return True  # 表头无该列，不报悬空（保守）
        try:
            rows = self.cli.read_sheet(target, to_sheet) or []
        except Exception:
            return True
        vstr = str(value).strip()
        for row in rows:
            if ci < len(row):
                cv = row[ci]
                if cv is not None and str(cv).strip() == vstr:
                    return True
        return False

    def _run_replan_phase(self, partitions: list, intents: list,
                           produced: dict[str, str], ordered_idx: list,
                           broken_producers: set, failed_tables: list,
                           text: str, session_id, confirm_token,
                           _step5_log, _emit_subtask, _llm_calls,
                           all_messages: list, all_result_rows: list,
                           all_steps: list) -> None:
        """O22 §9.1 replan-on-failure：批级失败聚合 → ReplanAgent 重规划 → 重跑 Step5。

        门控 CODEMAKER_REPLAN_ON_FAILURE=0 默认关。replan 产空/失败 → 降级走原 Step6 上报。
        上限 replan_max_rounds()=2 防 LLM 死循环。
        """
        from ..subagent.replan_agent import replan_enabled, replan_max_rounds
        from .operation_orchestrator import OperationOrchestrator
        if not replan_enabled():
            return
        if self._replan_agent is None:
            return
        if not failed_tables and not broken_producers:
            return  # 无失败不 replan

        # 收集失败 partition 的 failures（结构化 root_cause/col/table/sheet）
        failures: list[dict] = []
        for p in partitions:
            _res = p.get("res")
            if _res is None:
                continue
            _fs = getattr(_res, "failures", None) or []
            if not _fs:
                continue
            # 只收未成功 op 的 failures（已成功 op 的 failures 是软失败不重规划）
            _out = p.get("out")
            if _out is not None and getattr(_out, "ok", None) is True:
                continue
            failures.extend(f for f in _fs if isinstance(f, dict))
        if not failures:
            return

        # 收集 remaining（未成功/未执行）的 NLIntent
        remaining: list = []
        for p in partitions:
            _out = p.get("out")
            if _out is not None and getattr(_out, "ok", None) is True:
                continue  # 已成功跳过
            _intent = p.get("intent")
            if _intent is not None:
                remaining.append(_intent)
        if not remaining:
            return

        _step5_log(f"replan-on-failure：{len(failures)} 失败 + {len(remaining)} 剩余 op，触发重规划")
        _emit_subtask("subtask_start", {
            "idx": 0, "total": len(remaining),
            "table": "replan", "action": "replan", "llm_calls": _llm_calls(),
        })

        max_rounds = replan_max_rounds()
        for _round in range(max_rounds):
            _ce = getattr(self, "_cancel_event", None)
            if _ce is not None and _ce.is_set():
                _step5_log("  → replan 取消：用户中断")
                break
            try:
                replan_intents = self._replan_agent.replan(
                    failures, remaining, produced, text, cli=self.cli)
            except Exception:
                logger.warning("ReplanAgent 异常（降级走原 Step6 上报）", exc_info=True)
                replan_intents = []
            if not replan_intents:
                _step5_log(f"  → replan 轮{_round+1} 无修订 op，结束重规划")
                break
            _step5_log(f"  → replan 轮{_round+1} 产出 {len(replan_intents)} 条修订 op，重跑 Step5")
            # 重跑修订 op：复用 _phase_execute 单 op 执行逻辑
            _new_seq = 0
            _seq_counter = {"seq": _new_seq}
            for _ri, _rint in enumerate(replan_intents):
                _ce = getattr(self, "_cancel_event", None)
                if _ce is not None and _ce.is_set():
                    break
                _path, _sheet = None, None
                try:
                    _path, _sheet = self._resolve_table(_rint)
                    if _path is not None and _sheet is None:
                        _sheet = self._resolve_sheet(_path, _rint)
                except Exception:
                    _path, _sheet = None, None
                if not _path or not _sheet:
                    _step5_log(f"  → replan op{_ri} 表解析失败：table={_rint.table_hint}")
                    all_messages.append(f"[replan op{_ri}] 表解析失败：{_rint.table_hint}")
                    continue
                # 占位符替换（用已 produced）
                if produced:
                    OperationOrchestrator._resolve_placeholders(_rint, produced)
                _rres = AgentResult()
                _rres.table_stem = _path.stem
                _rres.table_sheet = _sheet
                try:
                    _rout = self._phase_execute(_rint, _path, _sheet, _rres, confirm_token)
                except Exception as _re_exc:
                    logger.warning("replan op 执行异常", exc_info=True)
                    _rout = None
                    _rres.ok = False
                    _rres.message = f"replan op 执行异常：{type(_re_exc).__name__}"
                    try:
                        _rres.failures.append({
                            "type": "replan_dispatch_exception",
                            "table": _rint.table_hint or _path.stem,
                            "sheet": _sheet or "",
                            "col": "",
                            "root_cause": f"{type(_re_exc).__name__}: {_re_exc}",
                            "attempted_strategies": ["replan_dispatch"],
                            "suggestion": "检查 replan op 指令/表结构",
                            "status": "unresolved", "user_reply": None,
                        })
                    except Exception:
                        pass
                _rok = getattr(_rout, "ok", None) if _rout else None
                if _rok is True:
                    OperationOrchestrator._capture_produced(_rres, _rint, produced, _seq_counter)
                    _step5_log(f"  → replan op{_ri} 成功 table={_rint.table_hint}")
                    all_messages.append(f"[replan op{_ri}] 成功：{_rres.message or ''}")
                else:
                    _step5_log(f"  → replan op{_ri} 失败 ok={_rok}")
                    all_messages.append(f"[replan op{_ri}] 失败：{_rres.message or ''}")
                if _rout is not None and getattr(_rout, "result_rows", None):
                    all_result_rows.extend(_rout.result_rows)
                all_steps.extend(_rres.steps)
                _emit_subtask("subtask_done", {
                    "idx": _ri + 1, "total": len(replan_intents),
                    "table": _rint.table_hint or "",
                    "action": _rint.action, "ok": _rok,
                    "skipped": False, "llm_calls": _llm_calls(),
                    "message": (_rres.message or "")[:200],
                })
            # 重跑后重新评估是否还需 replan（仍失败 → 下一轮，上限 max_rounds）
            _still_failed = any(
                getattr(p.get("out"), "ok", None) is False
                for p in partitions if p.get("out") is not None)
            if not _still_failed:
                _step5_log(f"  → replan 轮{_round+1} 后无失败，结束重规划")
                break
            # 更新 remaining 为仍失败 op（下一轮 replan 输入）
            remaining = [p["intent"] for p in partitions
                         if p.get("out") is not None
                         and getattr(p["out"], "ok", None) is False
                         and p.get("intent") is not None]
            if not remaining:
                break
        _emit_subtask("subtask_done", {
            "idx": 0, "total": 0, "table": "replan",
            "action": "replan_done", "ok": True, "skipped": False,
            "llm_calls": _llm_calls(), "message": "replan phase 结束",
        })

    def _backfill_forward_refs(self, partitions: list, produced: dict[str, str],
                                _step5_log, confirm_token) -> None:
        """Step5.5 前向引用 backfill：循环依赖链（conv↔option）主循环中占位符
        未解析被跳过的字段，主循环结束后 produced 已齐全，回扫补写。

        对每个已执行 add 的 partition：
        1. 重跑 _resolve_placeholders（现在更多占位符可解析）
        2. 找出从 <...> 变为真实值的字段
        3. 用写入时记录的行号 write_cell 补写
        """
        import re as _re
        _PH = _re.compile(r"<([^>]+)>")
        backfilled = 0
        backfilled_cols: list[str] = []
        for p in partitions:
            if not p.get("executed"):
                continue
            intent = p["intent"]
            if intent.action != "add":
                continue
            res = p["res"]
            row = getattr(res, "_written_row", None)
            if row is None:
                continue
            fields = (intent.extras or {}).get("fields")
            if not isinstance(fields, dict) or not fields:
                continue
            # 检测仍含占位符的字段
            deferred = {k: v for k, v in fields.items()
                        if isinstance(v, str) and "<" in v and _PH.search(v)}
            if not deferred:
                continue
            # 在副本上重解析（不污染原 intent 语义）
            tmp_fields = dict(deferred)
            tmp_intent_fields = {"fields": tmp_fields}
            # 借用 orchestrator 的 _resolve_placeholders 逻辑：直接 sub
            from .operation_orchestrator import OperationOrchestrator as _OO
            def _sub(v):
                if not isinstance(v, str) or "<" not in v:
                    return v
                def _repl(m):
                    val = _OO._lookup(m.group(1), produced)
                    return str(val) if val is not None else m.group(0)
                return _PH.sub(_repl, v)
            resolved = {}
            for k, v in tmp_fields.items():
                nv = _sub(v)
                if nv != v and "<" not in nv:
                    resolved[k] = nv
            if not resolved:
                continue
            # 逐字段写回
            path = p["path"]
            sheet = p["sheet"]
            stem = path.stem if hasattr(path, "stem") else ""
            try:
                headers = self.cli.read_header(path, sheet)
            except Exception:
                headers = []
            matcher = self._make_matcher(headers, stem, sheet, path)
            for col_name, val in resolved.items():
                # 点分键翻译（与 _run_add 一致）
                eff_fields = self._pretranslate_effect_fields({col_name: val}, headers)
                dot_fields = self._translate_dotted_keys(
                    eff_fields, headers, set(matcher.yaml_aliases.keys()),
                    self._type_aliases(path, sheet, headers))
                for cn, cv in dot_fields.items():
                    m = matcher.match(cn) or matcher.match_best(cn)
                    if m is None:
                        _step5_log(f"  → backfill 跳过：列[{cn}]未匹配")
                        continue
                    col_type = self._get_col_type(stem, sheet, m.column)
                    coerced, _warn, error = self._coerce_value(col_type, cv, stem, sheet, m.column)
                    if error:
                        _step5_log(f"  → backfill 跳过：列[{cn}]类型错误 {error}")
                        continue
                    try:
                        self.cli.write_cell(path, sheet, row, m.index, coerced)
                        fields[col_name] = cv  # 同步回 intent 供汇总
                        backfilled += 1
                        backfilled_cols.append(f"{p['intent'].table_hint}/{cn}")
                        _step5_log(f"  → backfill [{cn}]={coerced} → row{row} col{m.index} ({p['intent'].table_hint})")
                    except Exception:
                        logger.warning("backfill write_cell 失败 row=%s col=%s", row, m.index, exc_info=True)
        if backfilled:
            _step5_log(f"Step5.5 backfill 完成：补写 {backfilled} 个前向引用字段，涉及列：{backfilled_cols}")

    def _phase_summarize(self, intent: NLIntent, path: Path, sheet: str,
                         res: AgentResult, out: Optional[AgentResult]) -> None:
        """Step6 汇总：AI 生成自然语言总结（失败降级走模板拼接）。

        重试成功路径（res._skip_summarize=True）跳过汇总，保留原早返回行为；
        重试失败/读失败路径 out.ok 非 True 自然跳过。
        """
        # 重试成功路径已标记跳过汇总
        if getattr(res, "_skip_summarize", False):
            return
        # Step6 汇总：AI 生成自然语言总结（失败降级走模板拼接）
        # O5:成功路径跳 per-intent LLM 汇总(改模板直出),依赖全局 _stream_res
        # 聚合时调一次 ai_summarize(见 _run_pipeline 末 results_for_ai 分支),
        # 避免每成功子任务 1 次 LLM(N 子任务 N→0,仅全局 1 次)。
        # 失败路径仍需 per-intent failures 聚合(无 LLM,纯模板),保持原行为。
        if out is not None and out.ok:
            res.add_thinking("汇总",
                f"操作完成——{intent.action} {path.stem}/{sheet} 已应用")
            res.message = out.message or f"{intent.action} {path.stem}/{sheet} 已应用"
            res.add("写入汇总", True, "操作完成")
        else:
            # 失败/未完成：汇总全量 failures（#40 结构）+ success_list（§5.1 整合）。
            # 原仅取 failures[-1] 单条,4-Step Loop 多失败子任务需全量聚合供前端失败块。
            _failures = list(res.failures)
            if not _failures:
                # 回退：从 thinking_steps 取 repair_failure（旧路径未填 failures 时）
                _fs = [t for t in (res.thinking_steps or [])
                       if isinstance(t, dict) and t.get("repair_failure")]
                if _fs:
                    _rf = _fs[-1].get("repair_failure", {})
                    _failures = [{"col": "",
                              "root_cause": _rf.get("root_cause") or _rf.get("error_type") or "未知",
                              "attempted_strategies": _rf.get("attempted_strategies", "")}]
            if _failures:
                _parts = []
                for _f in _failures:
                    _loc = f"{_f.get('table') or path.stem}/{_f.get('sheet') or sheet}"
                    _col_s = f" 列[{_f.get('col')}]" if _f.get("col") else ""
                    _rc = _f.get("root_cause") or "未知"
                    _strats = _f.get("attempted_strategies", "")
                    if isinstance(_strats, (list, tuple)):
                        _strats = ", ".join(str(s) for s in _strats)
                    _parts.append(f"{_loc}{_col_s} 原因：{_rc}"
                                  + (f"；已试策略：{_strats}" if _strats else ""))
                res.message = "失败：" + " | ".join(_parts)
                res.add_thinking("汇总",
                    f"失败总结（{len(_failures)} 项）：{res.message[:120]}")
            else:
                res.message = res.message or f"操作未完成：{path.stem}/{sheet}"
                res.add_thinking("汇总", f"未完成：{path.stem}/{sheet}")
            res.add("写入汇总", False, res.message)

    # ── 反模式归纳共享 helper（V2 Step4 + legacy _phase_conclude 共用，消除双份漂移）──
    @staticmethod
    def _collect_failed_traces(failures: list, table_stem: str = "",
                                table_sheet: str = "") -> list[dict]:
        """从 failures list 构造 failed_traces（统一 V2 Step4 与 legacy _phase_conclude）。

        failures 项 shape（res.failures 元素）：{snip/suggestion, type/root_cause,
        root_cause, col, status}。table_stem/sheet 从 res 或 partition 取后传入。
        """
        traces: list[dict] = []
        for _f in (failures or []):
            if not isinstance(_f, dict):
                continue
            traces.append({
                "input": (_f.get("snip") or _f.get("suggestion") or "")[:120],
                "error_type": _f.get("type") or _f.get("root_cause") or "",
                "error_detail": (_f.get("root_cause") or "")[:200],
                "entries_summary": (
                    f"表={table_stem} sheet={table_sheet} col={_f.get('col') or ''}"
                    f" status={_f.get('status') or ''}"),
            })
        return traces

    @staticmethod
    def _induce_anti_patterns_via(failed_traces: list, enhancer: Any,
                                   stream_res: Any = None) -> int:
        """调 induce_anti_patterns 并可选推送 thinking（统一 V2 Step4 + legacy）。

        返回产出候选数。失败降级返回 0。
        """
        if not failed_traces:
            return 0
        try:
            produced = get_skill_updater().induce_anti_patterns(
                failed_traces, enhancer=enhancer)
            if produced and stream_res is not None and hasattr(stream_res, "add_thinking"):
                stream_res.add_thinking("归纳",
                    f"ConcludeAgent: AI 反模式归纳产出 {len(produced)} 条候选"
                    f"（pending_review，待 promote 升级 active）")
            return len(produced) if produced else 0
        except Exception:
            logger.debug("ConcludeAgent 反模式归纳失败（已降级）", exc_info=True)
            return 0

    def _phase_conclude(self, partitions: list, text: str,
                        _stream_res: "AgentResult") -> None:
        """Step6+ ConcludeAgent（D5 自学习闭环）：批量级聚合本批全量 failures
        → AI 归纳反模式 → 写 anti_patterns.yaml（pending_review）→ 下次运行受益。

        与 _run_verify_repair_loop 内 per-intent 归纳（agent.py:6435，gated
        CODEMAKER_INDUCE_PROD=0 默认关）的区别：本阶段是批量级、覆盖全 failure
        类型（placeholder_unresolved / execute_no_llm / verify_repair_exhausted），
        非 verify-repair 耗尽独占。设计 §5 ConcludeAgent 主路径落地；

        门控：enable_skill（skill 体系总开关，默认 True）+ _ai_enhancer 可用 +
        failures 非空。失败不阻断主流程（try/except 降级）。
        """
        if not self.enable_skill:
            return
        enhancer = getattr(self, "_ai_enhancer", None)
        if enhancer is None:
            return
        # 用共享 helper 构造 traces（统一 V2 Step4 + legacy，消除双份漂移）
        failed_traces: list[dict] = []
        for p in partitions:
            if not p.get("executed"):
                continue
            _res = p.get("res")
            if _res is None:
                continue
            _stem = getattr(_res, "table_stem", "") or (
                p["path"].stem if p.get("path") else "")
            _sheet = getattr(_res, "table_sheet", "") or (p.get("sheet") or "")
            _f = getattr(_res, "failures", None) or []
            failed_traces.extend(
                TableAgent._collect_failed_traces(_f, _stem, _sheet))
        if not failed_traces:
            return
        TableAgent._induce_anti_patterns_via(failed_traces, enhancer, _stream_res)

    def _rollback_write(self, path: Path, backup_file: str, res: AgentResult) -> None:
        """4.3：写操作失败回滚到快照，并在思考流标注。"""
        try:
            ok = self.auditor.rollback_to_backup(backup_file, target_path=str(path))
            if ok:
                res.add_thinking("回滚", f"写操作失败，已回滚 {path.stem} 到操作前快照")
                self._refresh_index_after_write(path)
        except Exception:
            logger.error("回滚失败（半成品数据残留，需人工核查 %s）", path, exc_info=True)
            res.dirty_data = True

