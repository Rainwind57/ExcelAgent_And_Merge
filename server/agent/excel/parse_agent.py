"""4-Step Loop Step1: ParseAgent（§3.1 设计文档）。

合并老 Step1/Step2/Step3：定位 → 拉真实列约束 → schema-driven LLM 拆分 →
produces 推断 → 组装 NLIntent（SubTask 超集，路线 A 扩 NLIntent）。

复用现有三组件（不重写，遵循 §五代码映射表）：
  - LocatorAgent.locate：粗路由 + FK 扩表（零 LLM 主路径，歧义 LLM 裁决）
  - DecomposeAgent.decompose：schema 注入 + 每表单 prompt 并发 LLM 拆分 +
    隔离 session + fail-fast + 指数退避（_build_schema_block 内嵌 schema 拉取，
    复用 cli.read_header/read_type_row 本地读 row1+row2 表头）
  - produces_inference.infer_produces_consumes：关系图驱动 produces 推断 +
    显式 PK 字面代换（producer_pk_values 内联逻辑，等价 §2.7 _explicit_pk_literal_substitute）

兜底（§3.1 step 4d / §2.8）：DecomposeAgent 产空 → parse() 返回 []，
调用方回退 cross_table_splitter 11 模板 baseline，再调 parse_baseline() 适配为
NLIntent[]（source="splitter_baseline", ai_check_skipped=True 走 #25 跳 LLM validate）。

接入：core/agent.py run() 在 CODEMAKER_EXCEL_PIPELINE_V2=0（显式降级到旧 6 步）
时默认走 ParseAgent.parse；V2 默认 ON 时 run() 分流到 run_v2，不经此。原
CODEMAKER_4STEP_LOOP/enable_4step_loop 已废弃（统一到 V2 单一开关）。

注：§2.2 lazy schema 拉取（HTTP /api/tables?include_columns=1 + ThreadPool +
schema_bundle）与 §2.3 _suggest_cache 复用——当前 DecomposeAgent 用本地
cli.read_header（非 HTTP），schema 拉取内嵌于 _build_schema_block。HTTP 化与
独立 schema_bundle 精细化待 R21 接口落地后做，本版复用 DecomposeAgent 现状。
§2.6 列名校验（column_matcher 重映射）归 Step2 ValidateAgent 字段层（§4.1 ①列存在性）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .core.produces_inference import infer_produces_consumes
from .parser.multi_intent_splitter import split_multi_intent
from .parser.nl_parser import NLIntent
from .subagent.decompose_agent import DecomposeAgent
from .subagent.locator_agent import LocatorAgent, LocatorResult
from .subagent.column_extractor import ColumnExtractor

logger = logging.getLogger(__name__)


class ParseAgent:
    """4-Step Loop Step1：ParseAgent（§3.1）。

    入口：parse(text) -> list[NLIntent]

    §Step1 列名提取前置改造：parse 入口先调 ColumnExtractor，从用户输入确定性
    提取列名 token → 反向索引 topK 反查候选表，产出 candidate_stems 注入每条
    intent.extras["extracted_columns"]，供 LocatorAgent 收敛候选表 + DecomposeAgent
    schema 注入优先命中 sheet。修复三个失败案例的"列名未在 Step1 前置提取"根因。

    每条 NLIntent 已 schema-grounded（列名由 LLM 基于 DecomposeAgent 注入的真实
    表头产）+ produces/consumes 占位符已标注（DecomposeAgent 内部 + produces_inference
    后置补）+ source="llm_decompose"（下游 Step2/Step3 据此走完整字段层 + FK 层）。

    失败返回空列表，调用方回退 splitter_baseline（parse_baseline 适配）。
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None,
                 locator_agent: Optional[LocatorAgent] = None,
                 decompose_agent: Optional[DecomposeAgent] = None):
        self._parser = parser
        self._thinking_sink = thinking_sink
        self._cli = cli
        self._locator_agent = locator_agent or LocatorAgent(
            parser=parser, thinking_sink=thinking_sink, cli=cli)
        self._decompose_agent = decompose_agent or DecomposeAgent(
            parser=parser, thinking_sink=thinking_sink, cli=cli)
        self._column_extractor = ColumnExtractor()
        # §4 FK 字面值引用校验：parse 后存 locator_result 供 validate_two_layer 用
        self._last_locator_result: Optional[LocatorResult] = None
        # 全段 locator_results 收集（替代单值 _last_locator_result 的末段覆盖问题）。
        # Step1 SubAgent 读此写入 s1.artifacts["locator_results"]，Step2 改读 artifacts
        # 而非探 _last_locator_result 私态（消除 contracts.py:16 步间隔离违反）。
        self._last_locator_results: list = []
        # 分段结果缓存（供 Step1 复用，消除 Step1 重复调 split_multi_intent）。
        self._last_segments: list = []
        # 列名提取结果缓存（供 Step1 注入 intent.extras）
        self._last_column_extraction = None

    def parse(self, text: str) -> list[NLIntent]:
        """主入口：text → list[NLIntent]（SubTask 超集）。

        §优化①：入口先规则分段（split_multi_intent，0 LLM）。单段走原 single-prompt
        路径；多段每段独立 locate + decompose_segment（段内文本短/候选少/schema 小，
        LLM 不漏且快）。段间默认串行，CODEMAKER_PARSE_SEGMENT_CONCURRENCY=1 开并发。
        段级覆盖：每段产出与原段列表对账，某段 0 条 → 仅对该段重跑（便宜），
        而非整句重跑 parse_multi（贵）。

        §Step1 列名提取前置（LLM 主导，规则信号为辅）：parse 入口先跑
        ColumnExtractor（0 LLM），从用户输入提取列名 token → 反向索引 topK 反查，
        产出列名命中信号（候选表/sheet + 命中列名）。信号不直接决策塞候选表，
        而是透传给 DecomposeAgent 注入 prompt，让 LLM 看着信号选表选列。
        修复三个失败案例根因（列名信号缺失致 LLM 黑盒选错）。

        流程（§3.1 step 2-6，分段化）：
          0. ColumnExtractor.extract(text) → 列名信号（新增，0 LLM，供 LLM 参考）
          1. split_multi_intent(text) → N 段（单指令 N=1）
          2. 每段：粗路由（LocatorAgent.locate）+ schema-driven LLM 拆分
             （decompose_segment，段内单 prompt 主路径）
          3. produces 推断 + NLIntent 组装
          4. 段级覆盖对账：某段 0 条 → 该段重跑 decompose_segment

        失败返回 []，调用方回退 splitter_baseline。
        """
        if not text or not text.strip():
            return []
        # 每次 parse 重置全段 locator_results 收集
        self._last_locator_results = []
        self._last_column_extraction = None
        # §优化①：入口先分段（0 LLM）。segments 存到实例属性供 Step1 复用（消除
        # Step1 重复调 split_multi_intent 的冗余——同函数同 text 结果一致）。
        try:
            segs = split_multi_intent(text)
            self._last_segments = segs
        except Exception:
            logger.warning("ParseAgent 分段失败,走整段路径", exc_info=True)
            segs = []
            self._last_segments = []
        if not segs or len(segs) <= 1:
            # 单指令走原整段 single-prompt 路径
            return self._parse_whole(text)
        # 多段：每段独立 locate + decompose_segment
        return self._parse_segments(segs, text)

    def _parse_whole(self, text: str) -> list[NLIntent]:
        """原整段路径（单指令或分段失败兜底）。"""
        # §3.1 step 2: 粗路由（零 LLM 主路径，内置 ColumnExtractor 列名信号补候选）
        locator_result: Optional[LocatorResult] = None
        try:
            locator_result = self._locator_agent.locate(text)
        except Exception:
            logger.warning("ParseAgent 粗路由失败", exc_info=True)
            self._think("ParseAgent 粗路由异常,回退")
            return []
        if not locator_result or not locator_result.candidates:
            self._think("ParseAgent 粗路由无候选,回退")
            return []
        # §Step1 列名信号：locate 已内置 ColumnExtractor，直接从 result 取
        self._last_column_extraction = getattr(locator_result, "column_signal", None)
        # 存 locator_result 供 4-Step 路径 validate_two_layer FK 字面值引用校验用
        self._last_locator_result = locator_result
        # 同步收集到全段 list（单段路径也走 list 收集，统一 Step1 读取入口）
        self._last_locator_results = [locator_result]
        # §3.1 step 3+4: schema 拉取 + LLM 拆分（DecomposeAgent 内嵌）
        try:
            split_intents = self._decompose_agent.decompose(text, locator_result)
        except Exception:
            logger.warning("ParseAgent DecomposeAgent 失败", exc_info=True)
            self._think("ParseAgent DecomposeAgent 异常,回退 splitter_baseline")
            return []
        if not split_intents:
            self._think(
                f"ParseAgent DecomposeAgent 产空"
                f"({len(locator_result.candidates)} 候选/{len(locator_result.fk_edges)} FK 边)"
                f",回退 splitter_baseline")
            return []
        return self._assemble(split_intents, text, locator_result)

    def _parse_segments(self, segs: list, text: str) -> list[NLIntent]:
        """多段路径：每段独立 locate + decompose_segment。

        §优化①：段间默认串行；CODEMAKER_PARSE_SEGMENT_CONCURRENCY=1 开并发。
        段级覆盖对账：某段 0 条 → 该段重跑一次（便宜），仍空则记 warning。
        """
        concurrency = os.getenv("CODEMAKER_PARSE_SEGMENT_CONCURRENCY", "0") == "1"

        def _do_one(seg):
            seg_text = getattr(seg, "text", seg) if not isinstance(seg, str) else seg
            if not seg_text or not seg_text.strip():
                return []
            try:
                lr = self._locator_agent.locate(seg_text)
            except Exception:
                logger.warning("ParseAgent 段粗路由失败(seg=%s)", seg_text[:30],
                               exc_info=True)
                return []
            if not lr or not lr.candidates:
                return []
            # §P1-10 多段列名信号隔离：多段时不设全局 _last_column_extraction（首段信号
            # 会污染后续段 intent.extras，如 quest 段信号注入 item 段导致 Step2/3 错配）。
            # 各段 decompose_segment 内部已用段自己的 lr.column_signal 注入 LLM prompt
            # （decompose_agent 已对），_split_to_nl 多段时不注入跨段信号，Step2/3 以 LLM
            # 产的 locator_field/fields 为主。仅单段路径(_parse_whole)设全局信号供 extras 注入。
            seg_signal = getattr(lr, "column_signal", None)
            # 收集全段 locator_result（替代单值末段覆盖）
            self._last_locator_results.append(lr)
            try:
                intents = self._decompose_agent.decompose_segment(seg_text, lr)
            except Exception:
                logger.warning("ParseAgent 段分解失败(seg=%s)", seg_text[:30],
                               exc_info=True)
                return []
            # §P1-10 段级信号注入：该段产出的 intent 挂段自己信号到 extras
            # （供 _assemble 后 _split_to_nl 不再覆盖，避免跨段污染）
            if seg_signal and seg_signal.has_signal:
                for it in intents:
                    try:
                        it_extras = getattr(it, "extras", None) or {}
                        it_extras["_seg_column_signal"] = seg_signal
                        it.extras = it_extras
                    except Exception:
                        pass
            # 段级覆盖对账：0 条 → 该段重跑一次
            if not intents:
                self._think(
                    f"ParseAgent 段「{seg_text[:30]}」产空,重跑一次 decompose_segment")
                try:
                    intents = self._decompose_agent.decompose_segment(seg_text, lr)
                except Exception:
                    logger.warning("ParseAgent 段重跑失败", exc_info=True)
                    intents = []
            if not intents:
                self._think(
                    f"ParseAgent 段「{seg_text[:30]}」重跑仍空,该段漏覆盖")
            return intents

        all_split: list = []
        if concurrency and len(segs) >= 3:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(
                    max_workers=min(3, len(segs))) as ex:
                for r in ex.map(_do_one, segs):
                    all_split.extend(r)
        else:
            for seg in segs:
                all_split.extend(_do_one(seg))
        if not all_split:
            self._think("ParseAgent 多段全产空,回退 splitter_baseline")
            return []
        # locator_result：取首段供 validate_two_layer FK 校验用（段间 FK 边
        # 已在 _do_one 内逐段 locate 收集到 _last_locator_results；此处取首段，
        # 不再重复 locate 首段——消除原 line 194-197 的冗余 locate + 末段覆盖问题）。
        self._last_locator_result = (
            self._last_locator_results[0] if self._last_locator_results else None)
        self._think(
            f"ParseAgent 多段分解产出 {len(all_split)} 条 SplitIntent"
            f"({len(segs)} 段,{'并发' if concurrency and len(segs) >= 3 else '串行'})")
        return self._assemble(all_split, text, self._last_locator_result)

    def _assemble(self, split_intents: list, text: str,
                  locator_result: Optional[LocatorResult]) -> list[NLIntent]:
        """SplitIntent[] → NLIntent[] + produces 推断（公共尾部）。"""
        # §3.1 step 6a: SplitIntent → NLIntent 适配（SubTask 超集）
        nl_intents = [self._split_to_nl(si, text) for si in split_intents]
        # §3.1 step 5: produces 推断（关系图驱动,原地补 produces_label/consumes 占位）
        try:
            infer_produces_consumes(nl_intents)
        except Exception:
            logger.warning("ParseAgent produces_inference 失败,保留原 intent", exc_info=True)
        # 同步 NLIntent.produces_label 字段（从 extras["produces"] 同步,供 Step3 拓扑用）
        for it in nl_intents:
            prod = it.extras.get("produces") if it.extras else None
            if prod and not it.produces_label:
                it.produces_label = str(prod)
        self._think(f"ParseAgent 产出 {len(nl_intents)} 条 NLIntent(source=llm_decompose)")
        return nl_intents


    def parse_baseline(self, text: str, splitter_intents: list) -> list[NLIntent]:
        """splitter_baseline 兜底适配（§3.1 step 4d / §2.8）。

        当 parse() 返回空时，调用方用 cross_table_splitter 11 模板产出
        SplitIntent[] 后调本方法适配为 NLIntent[]：
          - source="splitter_baseline"（下游 Step2 走 #25 跳 LLM validate 快路径，
            但仍跑规则字段层 §4.1）
          - ai_check_skipped=True（#22/#25 字段已模板锁定,不进 AI 重映射）
          - extras["source"]="splitter"（兼容旧下游读 extras["source"] 的代码）

        仍跑 produces_inference 补漏（splitter 模板 produces 标签精确但新链型可能缺）。
        """
        if not splitter_intents:
            return []
        nl_intents = [
            self._split_to_nl(si, text, source="splitter_baseline",
                              ai_check_skipped=True)
            for si in splitter_intents
        ]
        try:
            infer_produces_consumes(nl_intents)
        except Exception:
            logger.warning("ParseAgent baseline produces_inference 失败", exc_info=True)
        for it in nl_intents:
            prod = it.extras.get("produces") if it.extras else None
            if prod and not it.produces_label:
                it.produces_label = str(prod)
        self._think(f"ParseAgent baseline 产出 {len(nl_intents)} 条(source=splitter_baseline)")
        return nl_intents

    # ── 内部 ───────────────────────────────────────────────────

    def _split_to_nl(self, si, text: str, *,
                     source: str = "llm_decompose",
                     ai_check_skipped: bool = False) -> NLIntent:
        """SplitIntent → NLIntent 适配（SubTask 超集，路线 A）。

        字段映射：
          si.fields(dict)            → extras["fields"]
          si.produces                → extras["produces"] + NLIntent.produces_label
          si.locator_field/value     → NLIntent.locator_field/locator_value
          si.table_hint/sheet_hint   → NLIntent.table_hint/sheet_hint
          si.action/text             → NLIntent.action/raw

        extras["source"] 兼容旧下游读 extras["source"] 的代码：
          "llm_chain"（llm_decompose）/ "splitter"（splitter_baseline），
        与 _llm_chain_decompose:5871 现状一致。
        """
        fields = getattr(si, "fields", None) or {}
        produces = getattr(si, "produces", None)
        extras: dict = {"fields": fields}
        if produces:
            extras["produces"] = produces
        # extras["source"] 兼容旧下游（_llm_chain_decompose 现状用 "llm_chain"）
        extras["source"] = "llm_chain" if source == "llm_decompose" else "splitter"
        # §Step1 列名提取前置：把 ColumnExtractor 的列名 token + 候选表注入 extras，
        # 供 Step2/Step3 直接消费（替代 Step3 从 raw 重新猜列）。
        # §P1-10 优先段级信号：多段时 _do_one 已给 si 挂 _seg_column_signal（该段自己的），
        # 避免首段信号污染后续段。单段走全局 _last_column_extraction。
        # §修复：SplitIntent 无 extras 属性，用 getattr 安全访问
        _si_extras = getattr(si, "extras", None) or {}
        _ce = _si_extras.get("_seg_column_signal") if isinstance(_si_extras, dict) else None
        if _ce is None:
            _ce = self._last_column_extraction
        if _ce and getattr(_ce, "has_signal", False):
            extras["extracted_columns"] = list(
                _ce.extracted_terms)
            extras["column_extract_stems"] = list(
                _ce.candidate_stems)
            extras["extracted_columns_signal"] = _ce
            extras["column_extract_hits"] = [
                {"column": h.column, "stem": h.stem, "sheet": h.sheet,
                 "score": h.score, "source": h.source}
                for h in self._last_column_extraction.hits
            ]
        return NLIntent(
            action=getattr(si, "action", "add") or "add",
            table_hint=getattr(si, "table_hint", None),
            sheet_hint=getattr(si, "sheet_hint", None),
            locator_field=getattr(si, "locator_field", None),
            locator_value=getattr(si, "locator_value", None),
            locator_fields=list(getattr(si, "locator_fields", []) or []),
            locator_values=list(getattr(si, "locator_values", []) or []),
            raw=getattr(si, "text", text) or text,
            extras=extras,
            produces_label=produces,
            source=source,
            ai_check_skipped=ai_check_skipped,
        )

    def _think(self, msg: str) -> None:
        """推送 thinking 事件（复用 thinking_sink,与现有 SubAgent 一致）。"""
        if self._thinking_sink:
            try:
                # thinking_sink 接口: .add(stage, msg) 或 __call__(stage, msg)
                add = getattr(self._thinking_sink, "add", None) \
                    or getattr(self._thinking_sink, "__call__", None)
                if add is not None:
                    add("解析", msg)
            except Exception:
                pass


__all__ = ["ParseAgent"]
