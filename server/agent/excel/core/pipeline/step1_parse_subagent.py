"""4-Step V2 Step1 Parse SubAgent（§设计 S2）。

职责（严格限定）：
  - 输入分析、匹配表格、指令初形成。
  - split_multi_intent 分段（0 LLM）→ 每段独立 locate + decompose_segment（段小→快+可靠）
  - produces 推断（0 LLM）
  - 段级覆盖率对账：每段 ≥1 intent，0 条段重跑（便宜），仍空报 StepError（soft）

严禁：
  - AI 校验/字段校验/冲突处理（属 Step2）
  - 执行/写入（属 Step3）
  - 汇总/反模式归纳（属 Step4）

复用现有 ParseAgent（已含分段 + 段级对账），包装为统一 StepResult。
S1 阶段只做"包装 + 错误归属固定到 step1_parse"，ParseAgent 内部逻辑不动。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ...parse_agent import ParseAgent
from .contracts import STEP1_PARSE, StepContext, StepError, StepHardError, StepResult

logger = logging.getLogger(__name__)

_ACTION_CN = {"add": "新增", "set": "修改", "delete": "删除", "get": "查询",
              "col": "列操作"}


def _format_intent_human(it: Any) -> str:
    """把 NLIntent 转成人类可读中文描述，供 Step1 结束打印对照。"""
    act = _ACTION_CN.get(getattr(it, "action", ""), getattr(it, "action", "?"))
    tbl = getattr(it, "table_hint", "") or "?"
    sheet = getattr(it, "sheet_hint", "") or ""
    loc = f"{tbl}.{sheet}" if sheet else tbl
    parts = [f"{act} {loc}"]
    # 单主键定位
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        parts.append(f"定位 {lf}={lv}")
    # 复合主键定位
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs and len(lfs) == len(lvs):
        kv = ", ".join(f"{f}={v}" for f, v in zip(lfs, lvs))
        parts.append(f"定位 {kv}")
    # set 目标字段
    if getattr(it, "action", "") == "set" and getattr(it, "target_field", None):
        parts.append(f"{it.target_field}→{getattr(it, 'value', None)}")
    # add 写入字段
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if getattr(it, "action", "") == "add" and isinstance(fields, dict) and fields:
        kv = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:12])
        if len(fields) > 12:
            kv += f", …(共{len(fields)}列)"
        parts.append(f"写入 {kv}")
    # produces/consumes 依赖标注
    if getattr(it, "produces_label", None):
        parts.append(f"产出 <{it.produces_label}>")
    if getattr(it, "consumes_labels", None):
        parts.append("消费 " + ", ".join(f"<{c}>" for c in it.consumes_labels))
    return "，".join(parts)


def _jsonable(o: Any, depth: int = 0) -> Any:
    """递归把不可 JSON 序列化对象转成可序列化形式（截断深度/长度防爆）。

    extras 里可能嵌套 ColumnLocateResult 等非 dataclass 对象（浅拷贝残留），
    json.dumps 会抛 TypeError。本函数逐层转换：
      dict/list/set/tuple → 递归；有 to_dict → 展开；有 __dict__ → 展开；
      其余 → str 截断。
    """
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if depth > 6:
        try:
            return str(o)[:80]
        except Exception:
            return "<unserializable>"
    if isinstance(o, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in o]
    if hasattr(o, "to_dict") and callable(o.to_dict):
        try:
            return _jsonable(o.to_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(o, "__dict__"):
        try:
            return _jsonable(o.__dict__, depth + 1)
        except Exception:
            pass
    try:
        return str(o)[:120]
    except Exception:
        return f"<{type(o).__name__}>"


def _intent_to_json(it: Any) -> dict:
    """NLIntent → 精简 JSON（只含意图核心字段）。

    剔除 to_checkpoint_dict 里的调试数据（extracted_columns_signal / hits /
    validation / execution 等），只保留用户关心的意图本体：
      action/table/sheet + 定位 + fields(写入列) + set 目标 + produces/consumes。
    """
    d: dict = {
        "action": getattr(it, "action", ""),
        "table": getattr(it, "table_hint", "") or None,
        "sheet": getattr(it, "sheet_hint", "") or None,
    }
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        d["定位"] = {lf: lv}
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs and len(lfs) == len(lvs):
        d["定位"] = dict(zip(lfs, lvs))
    tf = getattr(it, "target_field", None)
    if tf:
        d["set"] = {tf: getattr(it, "value", None)}
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if isinstance(fields, dict) and fields:
        d["fields"] = {str(k): _jsonable(v) for k, v in fields.items()}
    pl = getattr(it, "produces_label", None)
    if pl:
        d["produces"] = pl
    cl = getattr(it, "consumes_labels", None)
    if cl:
        d["consumes"] = list(cl)
    return d


class Step1ParseSubAgent:
    """Step1：输入分析、匹配表格、指令初形成。"""

    def __init__(self, parser=None, thinking_sink=None, cli=None,
                 locator_agent=None, decompose_agent=None):
        self._parser = parser
        self._thinking_sink = thinking_sink
        self._cli = cli
        # 复用现有 ParseAgent（已含 split_multi_intent 分段 + 段级对账 + decompose_segment）
        self._parse_agent = ParseAgent(
            parser=parser, thinking_sink=thinking_sink, cli=cli,
            locator_agent=locator_agent, decompose_agent=decompose_agent)
        # metrics
        self._llm_calls = 0

    def execute(self, ctx: StepContext) -> StepResult:
        """Step1 执行：text → list[NLIntent]（装进 artifacts）。

        错误归属：所有错误 step_id=STEP1_PARSE。
        - 全空 + 兜底也空 → hard error（后续步无法跑）
        - 某段 0 intent → soft error（segment_idx 标注，不阻断）
        - 内部异常 → soft error（legacy fallback 仍可尝试）

        §段级对账：本层调 split_multi_intent 取 segments（parse 内部同源调用，结果一致），
        用于段级覆盖对账；产空走 splitter_baseline 兜底。
        """
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []
        intents: list = []
        segments: list = []
        # §中危 4 修复：execute 前后读 counter 差值 = 本步 LLM 调用数（替代硬编码 0）。
        # Step1 的 decompose/locate LLM 经 parser._llm_counter 累计（共享 counter），
        # 差值法隔离出本步调用，避免 Step3 metrics 被本步累计污染。
        _cnt = getattr(self._parser, "_llm_counter", None)
        _cnt_before = 0
        try:
            _cnt_before = int(_cnt.peek_total()) if _cnt else 0
        except Exception:
            _cnt_before = 0

        try:
            # §split 复用：parse 内部已调 split_multi_intent 并缓存到 _last_segments，
            # Step1 读它做段级对账，不再重复调 split（消除冗余 + 双源风险）。
            intents = self._parse_agent.parse(ctx.user_text)
            segments = getattr(self._parse_agent, "_last_segments", []) or []
            ctx.segments = segments

            # §增强：产空 → splitter_baseline 兜底（本层直接接，少一层 run() 重来）
            if not intents and segments:
                warnings.append("ParseAgent 产空,尝试 splitter_baseline 兜底")
                try:
                    from ...core.cross_table_splitter import (
                        CrossTableIntentSplitter, detect_cross_table_action)
                    if detect_cross_table_action(ctx.user_text):
                        splitter = CrossTableIntentSplitter()
                        split_intents = splitter.split(ctx.user_text)
                        if split_intents:
                            intents = self._parse_agent.parse_baseline(
                                ctx.user_text, split_intents)
                            if intents:
                                warnings.append(
                                    f"splitter_baseline 兜底成功,产 {len(intents)} 条")
                except Exception:  # noqa: BLE001
                    logger.warning("Step1 splitter_baseline 兜底失败",
                                   exc_info=True)
        except StepHardError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Step1 ParseAgent 异常", exc_info=True)
            errors.append(StepError(
                step_id=STEP1_PARSE, error_type="parse_internal",
                message="指令解析失败",
                root_cause=f"{type(e).__name__}: {e}", is_hard=False))

        # §增强：段级覆盖对账（精确全文匹配，非前缀15）
        # §P2-11 原对账 seg in raw 恒真（raw=seg全文），无区分度。补充动作数校验：
        # 段含 N 个动作词（新增/修改/删除/查看）应产 ≥ N 条意图，少于则报段内漏产 soft warning。
        if segments and len(segments) > 1 and intents:
            covered = set()
            # 段→产意图数映射（按 raw 文本包含归属）
            seg_intent_count: dict[int, int] = {}
            for it in intents:
                raw = (getattr(it, "raw", "") or "").strip()
                if not raw:
                    continue
                for i, seg in enumerate(segments):
                    if i in covered:
                        # 仍计数（一个段可能多 intent）
                        pass
                    seg_text = (getattr(seg, "text", seg)
                                if not isinstance(seg, str) else seg).strip()
                    # 双向包含：段文本在 raw 内 或 raw 在段文本内（段被 LLM 扩写）
                    if seg_text and (seg_text in raw or raw in seg_text):
                        covered.add(i)
                        seg_intent_count[i] = seg_intent_count.get(i, 0) + 1
            # 动作数校验：段含的动作词数应 ≤ 产意图数
            import re as _re
            _action_re = _re.compile(r'(?:新增|增加|添加|修改|改成|改为|删除|去掉|移除|清除|查看|查询|配一个|建一个|造一个|给一个)')
            for i, seg in enumerate(segments):
                seg_text = (getattr(seg, "text", seg)
                            if not isinstance(seg, str) else seg).strip()
                if i not in covered:
                    errors.append(StepError(
                        step_id=STEP1_PARSE, error_type="segment_no_intent",
                        message=f"第{i+1}段「{(seg_text or '')[:20]}」未能解析出意图",
                        is_hard=False, segment_idx=i))
                    continue
                # 段内动作数 vs 产意图数
                n_actions = len(_action_re.findall(seg_text))
                n_intents = seg_intent_count.get(i, 0)
                if n_actions > 1 and n_intents < n_actions:
                    errors.append(StepError(
                        step_id=STEP1_PARSE, error_type="segment_partial_coverage",
                        message=f"第{i+1}段含{n_actions}个动作但仅产{n_intents}条意图，"
                                f"可能有子句漏解析",
                        is_hard=False, segment_idx=i))

        # 全空 → hard（后续步无法跑）
        if not intents:
            errors.append(StepError(
                step_id=STEP1_PARSE, error_type="parse_empty",
                message="未解析出任何可执行意图",
                suggestion="请简化指令或检查表格是否存在",
                is_hard=True))

        # 产出存入 ctx 供后续步只读
        # intents 适配为 NLIntent[]（ParseAgent.parse 已返回 NLIntent[]）
        # locator_results 显式产出（替代 Step2 探 _last_locator_result 私态）：
        # Step1 持有 parse_agent 句柄，读其 _last_locator_results 全段收集，
        # 写入 s1.artifacts["locator_results"]，Step2 改读 artifacts。
        locator_results = getattr(self._parse_agent, "_last_locator_results", []) or []
        # §中危 8：把全段 candidates stems 合并去重，注入每条 intent.extras
        # ["locator_candidates"]。V2 Step3 路径（execute_no_llm）下 _phase_partition
        # 读此短路 _resolve_table 重跑（行索引策略1 可能用 locator_value 误命中它表，
        # 覆盖 decompose 已选定的 table_hint）。candidates 是表级全局信号，多段
        # 时合并去重后作"Step1 已探测合法候选表集合"供 partition 校验。
        _cand_stems: list[str] = []
        _seen: set = set()
        for _lr in locator_results:
            for _c in (getattr(_lr, "candidates", None) or []):
                _s = getattr(_c, "stem", None)
                if _s and _s not in _seen:
                    _seen.add(_s)
                    _cand_stems.append(_s)
        for _it in intents:
            try:
                if _it.extras is None:
                    _it.extras = {}
                _it.extras["locator_candidates"] = list(_cand_stems)
            except Exception:
                pass
        # 本步 LLM 调用数（差值法）
        _llm_calls = 0
        try:
            _llm_calls = max(0, int(_cnt.peek_total()) - _cnt_before) if _cnt else 0
        except Exception:
            _llm_calls = 0
        # Step1 结束：打印意图清单（中文描述 + JSON 形态），便于后续 Step2 校验对照
        if intents:
            _lines = []
            for _i, _it in enumerate(intents, start=1):
                _human = _format_intent_human(_it)
                _jd = json.dumps(_intent_to_json(_it), ensure_ascii=False, default=str)
                _lines.append(f"[{_i}] {_human}\n    JSON: {_jd}")
            _summary = "\n".join(_lines)
            logger.info("Step1 解析意图清单（%d 条）:\n%s", len(intents), _summary)
            # 推 thinking 事件（前端 Step1 气泡 Thinking 区逐条显示）。
            # 单行格式：前端 thinking_steps 用 Vue 插值渲染，\n 会折叠成空格，
            # 故每条意图推一个独立事件，phase=意图序号，detail=中文 | 紧凑 JSON。
            if self._thinking_sink is not None:
                for _i, _it in enumerate(intents, start=1):
                    _human = _format_intent_human(_it)
                    _jd = json.dumps(_intent_to_json(_it), ensure_ascii=False, default=str)
                    try:
                        self._thinking_sink(f"意图{_i}", f"{_human} | {_jd}")
                    except Exception:  # noqa: BLE001
                        pass
        ok = bool(intents)
        return StepResult(
            step_id=STEP1_PARSE, ok=ok,
            errors=errors, warnings=warnings,
            metrics={
                "dur_ms": int((time.time() - t0) * 1000),
                "segments": len(segments),
                "intents": len(intents),
                "llm_calls": _llm_calls,
            },
            artifacts={"intents": intents, "segments": segments,
                       "locator_results": locator_results})


__all__ = ["Step1ParseSubAgent"]
