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
import re
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
        # §P1-2.1 Step1 全局 deadline：超时立即冻结当前产出 + 走 _splitter_baseline，
        # 不再叠 LLM。默认 60s（env CODEMAKER_STEP1_DEADLINE_S 可调）。
        import time as _time_p
        import os as _os_p
        _dl = int(_os_p.getenv("CODEMAKER_STEP1_DEADLINE_S", "60"))
        self._step1_deadline = _time_p.monotonic() + _dl
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
        # §P0 单任务链：_last_segments 为单段 cross_table 时（split_multi_intent
        # 判定为单条跨表任务链，未按句切段），force_single=True 让 decompose 无视
        # 候选数阈值走单 prompt 全候选路径，保留跨表 produces/consumes 全链上下文。
        _force_single = (
            len(self._last_segments) == 1
            and getattr(self._last_segments[0], "action", "") == "cross_table")
        try:
            split_intents = self._decompose_agent.decompose(
                text, locator_result, force_single=_force_single)
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
        # §优化①：段间默认并发（原默认串行,12 段 = 12×LLM RTT 串行等待,Step1 慢根因）。
        # 设 CODEMAKER_PARSE_SEGMENT_CONCURRENCY=0 可显式退回串行。
        concurrency = os.getenv("CODEMAKER_PARSE_SEGMENT_CONCURRENCY", "1") == "1"

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
        if concurrency and len(segs) >= 2:
            from concurrent.futures import ThreadPoolExecutor
            # §P1-2.1 deadline 检查：并发前若已超 Step1 deadline，冻结产出走 baseline
            import time as _t_dl
            _dl = getattr(self, "_step1_deadline", None)
            if _dl is not None and _t_dl.monotonic() > _dl:
                self._think(f"ParseAgent Step1 deadline 超时，冻结并发段路径走 baseline")
                return []  # 调用方走 _splitter_baseline
            with ThreadPoolExecutor(
                    max_workers=min(5, len(segs))) as ex:
                for r in ex.map(_do_one, segs):
                    all_split.extend(r)
        else:
            import time as _t_dl2
            _dl = getattr(self, "_step1_deadline", None)
            for seg in segs:
                # §P1-2.1 每段前查 deadline，超时冻结剩余段
                if _dl is not None and _t_dl2.monotonic() > _dl:
                    self._think(f"ParseAgent Step1 deadline 超时，剩余 {len(segs)-segs.index(seg)} 段冻结")
                    break
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
            f"({len(segs)} 段,{'并发' if concurrency and len(segs) >= 2 else '串行'})")
        return self._assemble(all_split, text, self._last_locator_result,
                              do_backfill=True)

    def _scrub_narrative_scalar(self, nl_intents: list) -> None:
        """§P1 缺陷A 修复：灌值守卫 choke point。

        所有 Step1 子路径（DecomposeAgent LLM / _splitter_baseline / CrossTableIntentSplitter）
        产出的 NLIntent 在汇合点（_assemble / parse_baseline）统一过此闸。按列类型精准判：
          - 数字/布尔标量列填入含中文（标点或汉字）且值
            → 灌值（数字列不该含叙述）→ 清空该字段（置空待 Step2 补），不丢整条 intent
          - 落在 str/描述/text 列 → 合法保留（活动描述/对话文本本就长）
        列类型查 decompose_agent._col_type_for（已修缺陷B，正确比对 row1/row2 + 取冒号后类型）。
        通用判据（列类型 + 值特征），不绑业务词/表/测例/长度阈——原 >30 字阈放过短碎片
        （如「包也建一下，」7 字灌进 int 列），现按列类型+中文特征统一拦。
        """
        if not nl_intents:
            return
        _da = self._decompose_agent
        # 中文字符范围（含标点/汉字），与 _coerce_field_simple 多值校验同源
        import re as _re_ch
        _cn_char_re = _re_ch.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
        for it in nl_intents:
            fields = (it.extras or {}).get("fields")
            if not isinstance(fields, dict) or not fields:
                continue
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            for _fk in list(fields.keys()):
                _fv = fields[_fk]
                _fvs = str(_fv).strip() if _fv is not None else ""
                if not _fvs:
                    continue
                # 占位符/纯数字/list/dict 合法放行
                if (_fvs.startswith("<") and _fvs.endswith(">")) or _fvs == "<auto>":
                    continue
                _stripped = _fvs.replace(",", "").replace("，", "")
                if _stripped.lstrip("-").isdigit():
                    continue
                try:
                    float(_stripped)
                    continue
                except ValueError:
                    pass
                if _fvs.startswith("[") or _fvs.startswith("{"):
                    continue
                # §P0 数字索引键盲区修复：键为列号索引（int 或纯数字 str）=
                # LLM 退化把列序号当键（fields 键约定为列名，纯数字绝非列名）。
                # 此类键无法定位真实列 → _col_type_for 必返空 → 下方 scalar-num/
                # bool 判定恒假 → 中文碎片放行灌库（reward 行 ={2:'...',42:'...'}
                # 即此盲区）。通用拦截：数字索引键含中文叙述值 = 灌值污染，整键
                # 删除（无可映射列，保留只会致 Step3 match 失败翻转整条 intent）。
                _is_num_key = (isinstance(_fk, int)
                               or (isinstance(_fk, str) and _fk.strip().isdigit()))
                if _is_num_key and _cn_char_re.search(_fvs):
                    del fields[_fk]
                    try:
                        self._think(
                            f"灌值守卫：删除 {stem}/{sheet} 数字索引键[{_fk}]"
                            f"（列号索引键含中文=LLM退化灌值,无可映射列,整键剔除）")
                    except Exception:
                        pass
                    continue
                _ct = _da._col_type_for(stem, sheet, str(_fk)) if _da else ""
                _ctl = (_ct or "").lower()
                _is_scalar_num = ("int" in _ctl or "long" in _ctl
                                  or "float" in _ctl or "double" in _ctl
                                  or "number" in _ctl or "decimal" in _ctl)
                _is_scalar_bool = "bool" in _ctl
                if (_is_scalar_num or _is_scalar_bool) and _cn_char_re.search(_fvs):
                    # §枚举转码前置：int/bool 列填中文标签（如"节日"）先查 enum_resolver
                    # 转数字码，命中则改写 fields 消除灌值。
                    # 转码失败 → 保留原始中文值（不清空），让 Step2 字段层报
                    # TYPE_MISMATCH（"str「节日」无法转 int"）+ ask 用户填数字码。
                    # 清空成 '' 会丢失原始值，Step2 只能报"str 空"，用户不知道填了什么。
                    _mapped = None
                    try:
                        from .core.enum_resolver import get_enum_resolver as _ger
                        _er = _ger()
                        _mapped = _er.resolve_label(stem, sheet, str(_fk), _fvs)
                    except Exception:
                        _mapped = None
                    if _mapped is not None:
                        fields[_fk] = _mapped
                        try:
                            self._think(f"灌值守卫：枚举转码 {stem}/{sheet} 列[{_fk}]"
                                        f"「{_fvs}」→{_mapped}")
                        except Exception:
                            pass
                    else:
                        # 保留原始值，交 Step2 报 TYPE_MISMATCH + ask
                        try:
                            self._think(f"灌值守卫：{stem}/{sheet} 列[{_fk}]"
                                        f"中文值「{_fvs}」落在{_ctl}列，无枚举映射，"
                                        f"保留待 Step2 校验转码")
                        except Exception:
                            pass

    def _assemble(self, split_intents: list, text: str,
                  locator_result: Optional[LocatorResult],
                  *, do_backfill: bool = False) -> list[NLIntent]:
        """SplitIntent[] → NLIntent[] + produces 推断（公共尾部）。

        §速度1：do_backfill=True（多段路径收尾）时做一次全局缺表对账+重拆，
        替代原段级 backfill 串行重拆。单段路径（_parse_whole）默认 False——
        其 split_intents 来自 decompose() 主路径已 backfill（decompose_agent.py:322）。
        """
        # §速度1 全局 backfill：各段产出汇合后用全局 locator_result.candidates +
        # fk_edges 做一次 expected/produced 对账，缺表一次性单表重拆补漏（vs 段级
        # 每段对各自窄候选重复 backfill 的串行重拆，多段时墙钟爆）。零绑业务词。
        if do_backfill and locator_result and split_intents:
            try:
                import os as _os
                _per_to = int(_os.getenv("CODEMAKER_DECOMPOSE_TIMEOUT", "40"))
                _da = self._decompose_agent
                if _da is not None:
                    _fk_block = _da._build_fk_block(locator_result.fk_edges)
                    split_intents = _da._backfill_missing(
                        text, split_intents, locator_result.candidates,
                        locator_result.fk_edges, _fk_block, _per_to,
                        column_signal=getattr(locator_result, "column_signal", None))
            except Exception:  # noqa: BLE001
                logger.warning("ParseAgent _assemble 全局 backfill 失败", exc_info=True)
        # §3.1 step 6a: SplitIntent → NLIntent 适配（SubTask 超集）
        nl_intents = self._dedupe_nl_intents(
            [self._split_to_nl(si, text) for si in split_intents])
        # 同一 sheet 的稀疏/空壳影子 intent 去重（月华邮件类 4 条→2 条）：
        # 大候选池单 prompt 退化时，同一 sheet 会同时产"仅占位符的空壳 add"与
        # "真实字段 add"两版。按 (action,stem,sheet) 分组，组内丢弃「字段更少且
        # 字段集是他人子集」的影子（保留字段最全的 canonical），再叠 _drop_empty_
        # add_shadows 的空壳守卫。仅同 sheet 组内互比，不同 sheet/不同定位值绝不误杀。
        nl_intents = self._dedupe_same_sheet_shadows(nl_intents)
        # 孤立全空壳 add 过滤：LLM 面对候选池噪声表（如 item）会产 fields 全空的
        # add（只挂 produces=new_xxx_id 占位）。这类空壳既无字段可写，又没被本批
        # 其他 intent 消费（不是任何 FK 链前置），写盘必失败。框架级判据：
        # ① fields 无任何非空值；② 本批无 intent 消费其 produces。同时满足 → 删。
        nl_intents = self._drop_orphan_empty_adds(nl_intents)
        # §P1 缺陷A 修复：灌值守卫 choke point 化。_assemble 是 DecomposeAgent LLM 路径
        # 的汇合点，所有 SplitIntent→NLIntent 在此统一过 _scrub_narrative_scalar 一道闸，
        # 兜住 LLM 退化把长叙述灌进数字/布尔列的 intent（清空灌值字段，不丢整条）。
        # 与 parse_baseline 的同方法配合，覆盖 Step1 全部子路径。
        self._scrub_narrative_scalar(nl_intents)
        self._resolve_same_batch_name_refs(nl_intents, locator_result)
        # 既有表 FK 中文名→id 解析：LLM 把「金灵根」「太虚剑意」等中文名直接填进
        # int 型 FK 列（spirit_id/school_ability_id），但 spirit/school_ability 是
        # **已存在**的表（非本批 producer）。_resolve_same_batch_name_refs 只覆盖
        # 本批 producer 的 name refs，这里补既有表：按 FK 边定位目标表 → 精确名匹配
        # 唯一行 → 改写为该行 PK 值。命中唯一才改，多命中/无命中保留原文交 Step2 ask。
        self._resolve_existing_name_fk(nl_intents, locator_result)
        nl_intents = self._compile_step1_references(nl_intents, text, locator_result)
        self._think(f"ParseAgent 产出 {len(nl_intents)} 条 NLIntent(source=llm_decompose)")
        return nl_intents

    def _compile_step1_references(self, intents: list[NLIntent], text: str,
                                  locator_result: Optional[LocatorResult]) -> list[NLIntent]:
        """Normalize Step1 executable references after LLM extraction."""
        if not intents:
            return intents
        fk_edges = getattr(locator_result, "fk_edges", None) or []
        self._prune_fields_not_in_schema(intents)
        self._align_placeholder_families(intents)
        self._ensure_produced_primary_fields(intents)
        self._replace_reused_explicit_pks_with_placeholders(intents, fk_edges)
        self._backfill_missing_fk_fields(intents, fk_edges)
        self._resolve_ordinal_placeholders(intents, fk_edges)
        self._expand_repeated_child_configs(intents, text, fk_edges)
        self._resolve_placeholders_to_explicit_pks(intents)
        self._resolve_placeholders_by_embedded_explicit_pks(intents, fk_edges)
        self._fill_empty_fks_from_explicit_pks(intents, fk_edges)
        self._drop_foreign_placeholder_primary_fields(intents, fk_edges)
        self._backfill_same_workbook_placeholder_fields(intents)
        self._expand_enumerated_same_sheet_adds(intents, text)
        self._supplement_option_rows_from_text(intents, text)
        self._compact_indexed_field_gaps(intents)
        self._prune_fields_not_in_schema(intents)
        intents = self._dedupe_same_sheet_same_explicit_pk(intents)
        intents = self._dedupe_same_sheet_shadows(intents)
        intents = self._dedupe_same_sheet_natural_duplicates(intents)
        self._uniquify_reused_placeholder_primary_fields(intents)
        self._demote_consumed_duplicate_producers(intents)
        self._clean_self_consumes(intents)
        self._clean_dangling_consumes(intents)
        # 子表主键即外键（school_spirit.神通id → school_ability）时，把自引用主键
        # 绑到上游新产出，避免主键被自增成无关 ID
        self._bind_self_primary_to_upstream(intents, fk_edges)
        try:
            infer_produces_consumes(intents)
        except Exception:
            logger.warning("ParseAgent produces_inference 失败,保留原 intent", exc_info=True)
        for it in intents:
            prod = it.extras.get("produces") if it.extras else None
            if prod and not it.produces_label:
                it.produces_label = str(prod)
        self._resolve_ordinal_placeholders(intents, fk_edges)
        self._resolve_placeholders_to_explicit_pks(intents)
        self._resolve_placeholders_by_embedded_explicit_pks(intents, fk_edges)
        self._fill_empty_fks_from_explicit_pks(intents, fk_edges)
        self._replace_reused_explicit_pks_with_placeholders(intents, fk_edges)
        self._drop_foreign_placeholder_primary_fields(intents, fk_edges)
        self._uniquify_reused_placeholder_primary_fields(intents)
        self._demote_consumed_duplicate_producers(intents)
        self._clean_self_consumes(intents)
        self._clean_dangling_consumes(intents)
        return intents

    def _dedupe_same_sheet_same_explicit_pk(self, intents: list[NLIntent]) -> list[NLIntent]:
        if not intents:
            return intents

        def _real(value) -> str | None:
            if value is None:
                return None
            s = str(value).strip()
            if not s or (s.startswith("<") and s.endswith(">")):
                return None
            return s

        def _label(it: NLIntent) -> str:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            return str(label or "").strip().strip("<>").strip()

        groups: dict[tuple[str, str, str], list[int]] = {}
        norm_fields_by_idx: dict[int, dict[str, tuple[str, object]]] = {}
        for idx, it in enumerate(intents):
            if getattr(it, "action", "") != "add":
                continue
            stem = (getattr(it, "table_hint", "") or "").strip().lower()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            if not stem or not sheet:
                continue
            pk_names = {self._field_name_norm(x)
                        for x in self._primary_field_names(stem, sheet)}
            if not pk_names:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            canon = self._field_canon_map(stem, sheet)
            norm: dict[str, tuple[str, object]] = {}
            pk_values: list[str] = []
            for col, value in fields.items():
                col_norm = self._field_name_norm(col)
                key = canon.get(col_norm, col_norm)
                norm[key] = (col, value)
                if key in pk_names:
                    rv = _real(value)
                    if rv is not None:
                        pk_values.append(f"{key}={rv}")
            if not pk_values:
                continue
            norm_fields_by_idx[idx] = norm
            groups.setdefault((stem, sheet.lower(), "|".join(sorted(pk_values))), []).append(idx)

        if not groups:
            return intents

        label_remap: dict[str, str] = {}
        drop: set[int] = set()

        def _score(idx: int) -> tuple[int, int, int, int]:
            norm = norm_fields_by_idx.get(idx, {})
            real_count = sum(1 for _, value in norm.values() if _real(value) is not None)
            provided_count = sum(1 for _, value in norm.values()
                                 if value is not None and str(value).strip() != "")
            text_len = sum(len(str(value)) for _, value in norm.values()
                           if _real(value) is not None)
            return (real_count, provided_count, text_len, -idx)

        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            winner = sorted(idxs, key=_score, reverse=True)[0]
            winner_fields = (getattr(intents[winner], "extras", None) or {}).get("fields") or {}
            if not isinstance(winner_fields, dict):
                continue
            winner_norm = norm_fields_by_idx.get(winner, {})
            winner_by_norm = {k: col for k, (col, _) in winner_norm.items()}
            winner_label = _label(intents[winner])
            for loser in idxs:
                if loser == winner:
                    continue
                loser_fields = (getattr(intents[loser], "extras", None) or {}).get("fields") or {}
                if isinstance(loser_fields, dict):
                    for norm_key, (src_col, src_value) in norm_fields_by_idx.get(loser, {}).items():
                        dst_col = winner_by_norm.get(norm_key)
                        if dst_col is None:
                            winner_fields[src_col] = src_value
                            winner_by_norm[norm_key] = src_col
                            continue
                        if _real(winner_fields.get(dst_col)) is None and _real(src_value) is not None:
                            winner_fields[dst_col] = src_value
                loser_label = _label(intents[loser])
                if loser_label and winner_label:
                    label_remap[loser_label] = winner_label
                elif loser_label and not winner_label:
                    intents[winner].produces_label = loser_label
                    if getattr(intents[winner], "extras", None) is not None:
                        intents[winner].extras["produces"] = loser_label
                    winner_label = loser_label
                drop.add(loser)

        if not drop:
            return intents
        kept = [it for idx, it in enumerate(intents) if idx not in drop]
        if label_remap:
            for it in kept:
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if isinstance(fields, dict):
                    for col, value in list(fields.items()):
                        if isinstance(value, str):
                            raw = value.strip().strip("<>").strip()
                            if raw in label_remap:
                                fields[col] = f"<{label_remap[raw]}>"
                it.consumes_labels = [
                    label_remap.get(str(x).strip().strip("<>").strip(), x)
                    for x in (getattr(it, "consumes_labels", None) or [])
                ]
                consumes = (getattr(it, "extras", None) or {}).get("consumes")
                if isinstance(consumes, dict):
                    for col, label in list(consumes.items()):
                        raw = str(label).strip().strip("<>").strip()
                        if raw in label_remap:
                            consumes[col] = label_remap[raw]
        return kept

    def _ensure_produced_primary_fields(self, intents: list[NLIntent]) -> int:
        n = 0
        for it in intents or []:
            if getattr(it, "action", "") != "add":
                continue
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = getattr(it, "sheet_hint", "") or ""
            pk_names = {
                self._field_name_norm(x)
                for x in self._primary_field_names(stem, sheet)
            }
            if not pk_names:
                continue
            found_non_empty = False
            for key, value in fields.items():
                if self._field_name_norm(key) not in pk_names:
                    continue
                if value is not None and str(value).strip() != "":
                    found_non_empty = True
                    break
            if found_non_empty:
                continue
            target_key = ""
            headers = self._headers_for(stem, sheet)
            type_row = self._type_row_for(stem, sheet)
            for h, t in zip(headers, type_row):
                raw_type = str(t or "").split(":", 1)[0].strip()
                names = {self._field_name_norm(h), self._field_name_norm(raw_type)}
                if names & pk_names:
                    target_key = raw_type or str(h or "").strip()
                    break
            if not target_key:
                target_key = next(iter(self._primary_field_names(stem, sheet)), "")
            if not target_key:
                continue
            fields[target_key] = f"<{str(label).strip().strip('<>').strip()}>"
            n += 1
        return n

    def _resolve_placeholders_by_embedded_explicit_pks(self, intents: list[NLIntent], fk_edges: list) -> int:
        if not intents or not fk_edges:
            return 0
        explicit_by_target: dict[tuple[str, str], object] = {}
        for it in intents or []:
            if getattr(it, "action", "") != "add":
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            pk_names = {
                self._field_name_norm(x)
                for x in self._primary_field_names(stem, sheet)
            }
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not pk_names or not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                if self._field_name_norm(key) not in pk_names:
                    continue
                if value is None:
                    continue
                s = str(value).strip()
                if not s or (s.startswith("<") and s.endswith(">")):
                    continue
                explicit_by_target[(stem, sheet)] = value
                break
        if not explicit_by_target:
            return 0

        edge_target_by_child_col: dict[tuple[str, str, str], tuple[str, str]] = {}
        for edge in fk_edges or []:
            target = (
                (getattr(edge, "to_stem", "") or "").lower(),
                (getattr(edge, "to_sheet", "") or "").lower(),
            )
            if target not in explicit_by_target:
                continue
            edge_target_by_child_col[(
                (getattr(edge, "from_stem", "") or "").lower(),
                (getattr(edge, "from_sheet", "") or "").lower(),
                self._field_name_norm(getattr(edge, "from_column", "") or ""),
            )] = target

        n = 0
        resolved_labels: set[str] = set()
        for it in intents or []:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            for key, value in list(fields.items()):
                if not isinstance(value, str):
                    continue
                raw = value.strip()
                if not (raw.startswith("<") and raw.endswith(">")):
                    continue
                label = raw.strip("<>").strip()
                target = edge_target_by_child_col.get((stem, sheet, self._field_name_norm(key)))
                if target is None:
                    continue
                explicit = explicit_by_target[target]
                explicit_s = str(explicit).strip()
                if explicit_s and explicit_s in label:
                    fields[key] = explicit
                    resolved_labels.add(label)
                    n += 1
            if resolved_labels:
                old = list(getattr(it, "consumes_labels", None) or [])
                new = [x for x in old if str(x).strip().strip("<>").strip() not in resolved_labels]
                if len(new) != len(old):
                    it.consumes_labels = new
                    n += len(old) - len(new)
        return n

    @staticmethod
    def _backfill_missing_fk_fields(intents: list[NLIntent], fk_edges: list) -> int:
        producers: dict[tuple[str, str], list[str]] = {}
        for it in intents:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            producers.setdefault((
                (getattr(it, "table_hint", "") or "").lower(),
                (getattr(it, "sheet_hint", "") or "").lower(),
            ), []).append(str(label))
        n = 0
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            for edge in fk_edges:
                from_stem = (getattr(edge, "from_stem", "") or "").lower()
                from_sheet = (getattr(edge, "from_sheet", "") or "").lower()
                if stem != from_stem or sheet != from_sheet:
                    continue
                target_key = (
                    (getattr(edge, "to_stem", "") or "").lower(),
                    (getattr(edge, "to_sheet", "") or "").lower(),
                )
                labels = producers.get(target_key) or []
                if len(labels) != 1:
                    continue
                col = str(getattr(edge, "from_column", "") or "").split(":")[0].strip()
                if not col or str(fields.get(col, "")).strip():
                    continue
                label = labels[0]
                fields[col] = f"<{label}>"
                if label not in (getattr(it, "consumes_labels", None) or []):
                    it.consumes_labels.append(label)
                n += 1
        return n

    def _backfill_same_workbook_placeholder_fields(self, intents: list[NLIntent]) -> int:
        producers: list[tuple[str, str, str, list[str]]] = []
        for it in intents:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            cols = [str(k) for k, v in fields.items()
                    if str(v).strip() == f"<{label}>"]
            if cols:
                producers.append((
                    (getattr(it, "table_hint", "") or "").lower(),
                    (getattr(it, "sheet_hint", "") or "").lower(),
                    str(label),
                    cols,
                ))
        if not producers:
            return 0
        n = 0
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            existing_norm = {self._field_name_norm(k) for k in fields}
            headers = self._headers_for(stem, getattr(it, "sheet_hint", "") or "")
            type_row = self._type_row_for(stem, getattr(it, "sheet_hint", "") or "")
            for p_stem, p_sheet, label, p_cols in producers:
                if p_stem != stem or p_sheet == sheet:
                    continue
                wanted = {self._field_name_norm(c) for c in p_cols}
                target_col = ""
                for h, t in zip(headers, type_row):
                    names = {self._field_name_norm(h)}
                    if t:
                        names.add(self._field_name_norm(str(t).split(":")[0]))
                    if names & wanted:
                        target_col = str(h or str(t).split(":")[0])
                        break
                if not target_col and p_cols:
                    target_col = p_cols[0]
                if not target_col or self._field_name_norm(target_col) in existing_norm:
                    continue
                fields[target_col] = f"<{label}>"
                existing_norm.add(self._field_name_norm(target_col))
                if label not in (getattr(it, "consumes_labels", None) or []):
                    it.consumes_labels.append(label)
                n += 1
        return n

    @staticmethod
    def _field_name_norm(value) -> str:
        return re.sub(r"[\s_:\-./\\()\[\]（）【】]+", "", str(value or "").lower())

    def _headers_for(self, stem: str, sheet: str) -> list:
        return self._schema_row_for(stem, sheet, type_row=False)

    def _type_row_for(self, stem: str, sheet: str) -> list:
        return self._schema_row_for(stem, sheet, type_row=True)

    def _schema_row_for(self, stem: str, sheet: str, *, type_row: bool) -> list:
        if self._cli is None or not stem or not sheet:
            return []
        try:
            tables = {p.stem.lower(): p for p in self._cli.list_tables()}
            path = tables.get(stem.lower())
            if path is None:
                return []
            real_sheet = sheet
            try:
                sheets = self._cli.get_sheets(path) or []
                real_sheet = next(
                    (s for s in sheets if str(s).lower() == str(sheet).lower()),
                    sheet)
            except Exception:
                real_sheet = sheet
            if type_row:
                reader = getattr(self._cli, "read_type_row", None)
                return reader(path, real_sheet) if callable(reader) else []
            return self._cli.read_header(path, real_sheet) or []
        except Exception:
            return []

    def _read_sheet_rows(self, stem: str, sheet: str) -> list:
        """读 (stem, sheet) 既有数据行（按 stem/sheet 小写缓存，一次解析只算一遍）。"""
        if not stem or not sheet:
            return []
        cache = getattr(self, "_rows_cache", None)
        if cache is None:
            cache = {}
            self._rows_cache = cache
        key = (str(stem).strip().lower(), str(sheet).strip().lower())
        if key in cache:
            return cache[key]
        rows: list = []
        try:
            if self._cli is not None:
                tables = {p.stem.lower(): p for p in self._cli.list_tables()}
                path = tables.get(str(stem).strip().lower())
                if path is not None:
                    real_sheet = sheet
                    try:
                        sheets = self._cli.get_sheets(path) or []
                        real_sheet = next(
                            (s for s in sheets
                             if str(s).lower() == str(sheet).lower()), sheet)
                    except Exception:
                        real_sheet = sheet
                    rows = self._cli.read_sheet(path, real_sheet) or []
        except Exception:
            rows = []
        cache[key] = rows
        return rows

    def _shared_value_columns(self, stem: str, sheet: str) -> set:
        """既有数据里「取值有重复」的列名归一集合 = 多行共用的引用列。

        身份列（主键）取值必然互不相同；某列一旦出现重复值，说明它是可被多行
        共用的引用列，例如 school_talent/SchoolTalentLevel 的 buff_id=600003 被
        同一天赋的 1 级/2 级两行共用。在这类列上「同一个显式值被多条 intent 复用」
        是**合法**的，不该被当成"重复主键"改写成占位符（否则用户明确给的
        「被动 600003」会被替换成 <new_school_talent_level_1_id>）。
        """
        headers = self._headers_for(stem, sheet) or []
        types = self._type_row_for(stem, sheet) or []
        rows = self._read_sheet_rows(stem, sheet)
        if not headers or not rows:
            return set()
        # 中文显示名与 row2 英文规范名都要登记：下游比对用的是 _field_canon_map
        # 归一后的英文规范键（buff_id → buffid），只登记中文键会漏判。
        canon_of: dict[int, set] = {}
        for idx, h in enumerate(headers):
            if not h:
                continue
            names = {self._field_name_norm(h)}
            if idx < len(types) and types[idx]:
                names.add(self._field_name_norm(
                    str(types[idx]).split(":", 1)[0].strip()))
            names.discard("")
            canon_of[idx] = names
        out: set = set()
        for idx, names in canon_of.items():
            seen: set = set()
            for row in rows:
                if not isinstance(row, (list, tuple)) or idx >= len(row):
                    continue
                v = row[idx]
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                if s in seen:
                    out |= names
                    break
                seen.add(s)
        return out


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
        nl_intents = self._dedupe_nl_intents([
            self._split_to_nl(si, text, source="splitter_baseline",
                              ai_check_skipped=True)
            for si in splitter_intents
        ])
        # §P1 缺陷A 修复：splitter_baseline 兜底产出的 NLIntent 也过 _scrub_narrative_scalar，
        # 兜住零 LLM 旁路（_splitter_baseline/CrossTableIntentSplitter 不经 _to_split_intents）
        # 的灌值 intent。与 _assemble 同方法，覆盖 Step1 全部子路径。
        self._scrub_narrative_scalar(nl_intents)
        self._prune_fields_not_in_schema(nl_intents)
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

    def _field_canon_map(self, stem: str, sheet: str) -> dict[str, str]:
        """字段键 → 规范键映射（中文显示名/英文规范名统一到同一规范键）。

        用真实表头（row1 显示名 + row2 规范名）做桥：同一列的中文键「模板ID」和
        英文键「template_id」都归一映射到「templateid」。cli 不可用/读不到表头时
        返回空（调用方退回纯文本 _field_name_norm 归一，不做中英桥）。
        """
        m: dict[str, str] = {}
        if self._cli is None:
            return m
        try:
            headers = self._headers_for(stem, sheet) or []
            types = self._type_row_for(stem, sheet) or []
        except Exception:
            return m
        for h, t in zip(headers, types):
            h = str(h or "").strip()
            t = str(t or "").strip()
            # row2 规范名剥类型后缀（template_id:int → template_id），否则 canon
            # 键会带 "int"/"string" 后缀，与 LLM 产出的裸列名（template_id）对不上。
            t_base = t.split(":", 1)[0].strip() if t else ""
            if not h and not t_base:
                continue
            # 规范键取 row2 基础规范名（剥类型）；row2 缺失退回 row1 显示名。
            canon = ParseAgent._field_name_norm(t_base or h)
            if not canon:
                continue
            for src in (h, t_base, t):
                if src:
                    m[ParseAgent._field_name_norm(src)] = canon
        return m

    @staticmethod
    def _drop_orphan_empty_adds(intents: list[NLIntent]) -> list[NLIntent]:
        """删孤立全空壳 add intent（框架级，防噪声表空壳落盘）。

        判据：add 且 fields 无任何非空值（None/空串/占位符都算空），且本批
        无其他 intent 消费其 produces（非 FK 链前置）。两者同时满足才删——
        被消费的空壳（如纯 producer 占位行）保留，交下游拓扑回填。
        """
        if not intents:
            return intents
        # 仅当本批 >1 条（跨表上下文）才做孤立空壳过滤：单条空 add 可能是
        # 用户单表新增（如「新增 quest」），空 fields 交 Step2 补，不能删。
        if len(intents) <= 1:
            return intents

        def _has_real_value(fields) -> bool:
            if not isinstance(fields, dict):
                return False
            for v in fields.values():
                if v is None:
                    continue
                s = str(v).strip()
                if s == "":
                    continue
                if s.startswith("<") and s.endswith(">"):
                    continue  # 占位符 = 待补，不算实值
                return True
            return False

        # 被消费的 produces 标签集合（跨 intent，供"孤立"判定）
        consumed: set[str] = set()
        for it in intents:
            cl = getattr(it, "consumes_labels", None) or []
            for c in cl:
                consumed.add(str(c).strip())
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            import re as _re
            for v in fields.values():
                if isinstance(v, str):
                    for m in _re.finditer(r"<\s*([^>]+?)\s*>", v):
                        consumed.add(m.group(1).strip())

        kept: list = []
        for it in intents:
            if getattr(it, "action", "") != "add":
                kept.append(it)
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields")
            if _has_real_value(fields):
                kept.append(it)
                continue
            label = getattr(it, "produces_label", None) or \
                ((getattr(it, "extras", None) or {}).get("produces"))
            label_s = str(label).strip() if label else ""
            if label_s and label_s in consumed:
                kept.append(it)  # 被消费的空壳 producer，保留供拓扑
                continue
            # 孤立空壳 → 删
        return kept

    def _resolve_same_batch_name_refs(self, intents: list[NLIntent],
                                      locator_result: Optional[LocatorResult]) -> int:
        """Rewrite same-batch display-name FK values to produced placeholders.

        This is intentionally schema/FK driven. If the LLM creates an entity in one
        intent and later writes that entity's display name into an id/FK column, the
        downstream validator sees a type mismatch. When FK edges prove the consumer
        column points to the producer table and the producer name is unique in this
        batch, convert the value to <producer_label>.
        """
        if not intents:
            return 0
        edge_targets: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
        for edge in (getattr(locator_result, "fk_edges", None) or []):
            from_stem = (getattr(edge, "from_stem", "") or "").lower()
            from_sheet = (getattr(edge, "from_sheet", "") or "").lower()
            from_col = str(getattr(edge, "from_column", "") or "").split(":")[0]
            to_stem = (getattr(edge, "to_stem", "") or "").lower()
            to_sheet = (getattr(edge, "to_sheet", "") or "").lower()
            if from_stem and from_sheet and from_col and to_stem and to_sheet:
                edge_targets.setdefault(
                    (from_stem, from_sheet, self._field_name_norm(from_col)),
                    set(),
                ).add((to_stem, to_sheet))

        producers: dict[tuple[str, str], list[tuple[str, set[str]]]] = {}
        name_key_markers = {"name", "title"}
        for it in intents:
            if (getattr(it, "action", "") or "").lower() not in {"add", "create"}:
                continue
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            names: set[str] = set()
            for key, value in fields.items():
                key_s = str(key or "")
                key_n = self._field_name_norm(key_s)
                looks_name = (
                    key_n in name_key_markers
                    or "name" in key_n
                    or "title" in key_n
                    or "名称" in key_s
                    or "名字" in key_s
                )
                if looks_name and isinstance(value, str):
                    value_s = value.strip()
                    if value_s and not (value_s.startswith("<") and value_s.endswith(">")):
                        names.add(value_s)
            if names:
                producers.setdefault((
                    (getattr(it, "table_hint", "") or "").lower(),
                    (getattr(it, "sheet_hint", "") or "").lower(),
                ), []).append((str(label), names))

        if not producers:
            return 0
        n = 0
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            for col, value in list(fields.items()):
                if not isinstance(value, str):
                    continue
                value_s = value.strip()
                if not value_s or (value_s.startswith("<") and value_s.endswith(">")):
                    continue
                try:
                    float(value_s.replace(",", ""))
                    continue
                except ValueError:
                    pass
                col_n = self._field_name_norm(col)
                if "id" not in col_n and "编号" not in str(col):
                    continue
                targets = edge_targets.get((stem, sheet, col_n)) or set()
                matches: list[str] = []
                for target_key, rows in producers.items():
                    if targets and target_key not in targets:
                        continue
                    for label, names in rows:
                        if value_s in names:
                            matches.append(label)
                matches = sorted(set(matches))
                if len(matches) != 1:
                    continue
                label = matches[0]
                fields[col] = f"<{label}>"
                if label not in (getattr(it, "consumes_labels", None) or []):
                    it.consumes_labels.append(label)
                n += 1
        if n:
            self._think(f"Step1 same-batch name refs resolved: {n}")
        return n

    def _resolve_existing_name_fk(self, intents: list[NLIntent],
                                  locator_result: Optional[LocatorResult]) -> int:
        """既有表 FK 中文名→id 解析（框架级，覆盖 spirit/ability 等已存在表）。

        场景：LLM 把「金灵根」「太虚剑意」等中文名直接填进 int 型 FK 列
        （school_spirit.spirit_id / school_ability_id）。这些目标表（spirit/
        school_ability）是**已存在**的表，不是本批 producer，_resolve_same_batch_
        name_refs 不覆盖。这里按 FK 边定位目标表 → 精确名匹配唯一行 → 改写为
        该行 PK 值。命中唯一才改；多命中/无命中保留原文交 Step2 ask（不误填）。
        """
        if not intents or self._cli is None:
            return 0
        # FK 边索引：consumer (stem,sheet,col_norm) → target (to_stem, to_sheet)
        edge_targets: dict[tuple[str, str, str], tuple[str, str]] = {}
        for edge in (getattr(locator_result, "fk_edges", None) or []):
            from_stem = (getattr(edge, "from_stem", "") or "").lower()
            from_sheet = (getattr(edge, "from_sheet", "") or "").lower()
            from_col = str(getattr(edge, "from_column", "") or "").split(":")[0]
            to_stem = (getattr(edge, "to_stem", "") or "").lower()
            to_sheet = (getattr(edge, "to_sheet", "") or "").lower()
            if from_stem and from_sheet and from_col and to_stem:
                edge_targets[(from_stem, from_sheet,
                              self._field_name_norm(from_col))] = (to_stem, to_sheet)

        # 目标表数据缓存：stem → 解析后的 (rows, resolved_sheet)，懒加载
        _data_cache: dict[tuple[str, str], tuple[list, str]] = {}

        def _table_rows(stem: str, sheet: str) -> tuple[list, str]:
            key = (stem, sheet)
            if key in _data_cache:
                return _data_cache[key]
            rows: list = []
            resolved = sheet
            try:
                tables = {p.stem.lower(): p for p in self._cli.list_tables()}
                path = tables.get(stem)
                if path is not None:
                    sheets = self._cli.get_sheets(path) or []
                    biz = [s for s in sheets
                           if s and "说明" not in s and "CONFIG" not in s.upper()]
                    if not sheet:
                        resolved = biz[0] if biz else ""
                    else:
                        # sheet 存在性 + 大小写不敏感回退（FK 边运行时推导的
                        # to_sheet 可能是小写 'spirit'，真实 sheet 是 'Spirit'）
                        hit = next((s for s in sheets if s == sheet), None)
                        if hit is None:
                            hit = next((s for s in sheets
                                        if s.lower() == sheet.lower()), None)
                        if hit is None and biz:
                            hit = next((s for s in biz
                                        if s.lower() == sheet.lower()), biz[0])
                        resolved = hit or sheet
                    rows = self._cli.read_sheet(path, resolved) or []
            except Exception:
                rows = []
            _data_cache[key] = (rows, resolved)
            return rows, resolved

        # 名称列候选：name/名称/名字/title/描述（取第一个命中的非空值）
        def _name_cols(headers) -> list[int]:
            idxs = []
            for i, h in enumerate(headers or []):
                hn = self._field_name_norm(h)
                if hn and ("name" in hn or "title" in hn
                           or "名称" in str(h) or "名字" in str(h)):
                    idxs.append(i)
            return idxs

        n = 0
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            for col, value in list(fields.items()):
                if not isinstance(value, str):
                    continue
                value_s = value.strip()
                if not value_s:
                    continue
                if value_s.startswith("<") and value_s.endswith(">"):
                    # LLM 偶从 few-shot 抄来 <resolved_from_中文名> 占位符，
                    # 剥出内部中文名继续按既有表解析。
                    inner = value_s[1:-1].strip()
                    _m = re.match(r"resolved_from_(.+)$", inner)
                    if _m:
                        value_s = _m.group(1).strip()
                    else:
                        continue  # 其他占位符（<new_xxx>）交拓扑，不处理
                if not any('\u4e00' <= ch <= '\u9fff' for ch in value_s):
                    continue  # 非中文名，跳过（数字/英文 id 不动）
                col_n = self._field_name_norm(col)
                if "id" not in col_n and "编号" not in str(col):
                    continue  # 仅处理 id/编号类列
                target = edge_targets.get((stem, sheet, col_n))
                if not target:
                    continue
                to_stem, to_sheet = target
                rows, resolved_sheet = _table_rows(to_stem, to_sheet)
                if not rows:
                    continue
                headers = self._headers_for(to_stem, resolved_sheet) or []
                name_idx = _name_cols(headers)
                if not name_idx:
                    continue
                # 精确名匹配唯一行 → PK 是首列（或含 id 的首个列）
                # 匹配策略：① 精确相等 ② 双向子串（表名「金」是用户值「金灵根」
                # 的子串，或用户值「金」是表名「金灵根」的子串）。取全部命中，
                # 唯一才解析；多命中/无命中保留原文交 Step2 ask。
                matched: list[int] = []
                for ri, r in enumerate(rows):
                    for ni in name_idx:
                        cell = str(r[ni]).strip() if ni < len(r) and r[ni] is not None else ""
                        if not cell:
                            continue
                        if cell == value_s or cell in value_s or value_s in cell:
                            matched.append(ri)
                            break
                if len(matched) != 1:
                    continue  # 多命中/无命中 → 保留原文交 Step2 ask
                row = rows[matched[0]]
                pk = None
                if row:
                    pk = row[0]
                if pk is None or str(pk).strip() == "":
                    continue
                fields[col] = pk
                n += 1
        if n:
            self._think(f"Step1 existing-name FK resolved: {n}")
        return n

    def _schema_field_key_map(self, stem: str, sheet: str) -> dict[str, str]:
        """Map accepted field-name variants to the sheet's canonical row2 key."""
        out: dict[str, str] = {}
        headers = self._headers_for(stem, sheet) or []
        types = self._type_row_for(stem, sheet) or []
        for idx in range(max(len(headers), len(types))):
            h = str(headers[idx] if idx < len(headers) else "" or "").strip()
            t = str(types[idx] if idx < len(types) else "" or "").strip()
            t_base = t.split(":", 1)[0].strip() if t else ""
            canon = t_base or h
            if not canon:
                continue
            for src in (h, t_base, t):
                if src:
                    out[self._field_name_norm(src)] = canon
        return out

    def _remap_field_to_schema(self, stem: str, sheet: str, col: str) -> str:
        key_map = self._schema_field_key_map(stem, sheet)
        col_n = self._field_name_norm(col)
        if not col_n or col_n in key_map:
            return key_map.get(col_n, "")
        candidates: list[str] = []
        for valid_norm, valid_key in key_map.items():
            if not valid_norm:
                continue
            if valid_norm == "id" and col_n != valid_norm:
                continue
            id_like = valid_norm.endswith("id") or col_n.endswith("id")
            if id_like and (col_n.endswith(valid_norm) or valid_norm.endswith(col_n)):
                candidates.append(valid_key)
        candidates = sorted(set(candidates))
        return candidates[0] if len(candidates) == 1 else ""

    def _prune_fields_not_in_schema(self, intents: list[NLIntent]) -> int:
        """Drop fields that cannot exist on the selected sheet; remap unique aliases."""
        n = 0
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict) or not fields:
                continue
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            key_map = self._schema_field_key_map(stem, sheet)
            if not key_map:
                continue
            recognized = 0
            for col in fields:
                col_s = str(col)
                col_n = self._field_name_norm(col_s)
                if col_n in key_map or self._remap_field_to_schema(stem, sheet, col_s):
                    recognized += 1
            if recognized == 0:
                continue
            for col, value in list(fields.items()):
                col_s = str(col)
                col_n = self._field_name_norm(col_s)
                if col_n in key_map:
                    canon = key_map[col_n]
                    if canon and canon != col_s:
                        if canon not in fields:
                            fields[canon] = value
                        del fields[col]
                        n += 1
                    continue
                remap = self._remap_field_to_schema(stem, sheet, col_s)
                if remap:
                    if remap not in fields:
                        fields[remap] = value
                    del fields[col]
                    n += 1
                    continue
                del fields[col]
                n += 1
        if n:
            self._think(f"Step1 schema field cleanup changed {n} fields")
        return n

    def _resolve_ordinal_placeholders(self, intents: list[NLIntent], fk_edges: list) -> int:
        """Resolve unbound <new_xxx_id_1> style labels by FK target and ordinal."""
        produced_by_target: dict[tuple[str, str], list[str]] = {}
        produced_labels: set[str] = set()
        for it in intents:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            label = str(label)
            produced_labels.add(label)
            produced_by_target.setdefault((
                (getattr(it, "table_hint", "") or "").lower(),
                (getattr(it, "sheet_hint", "") or "").lower(),
            ), []).append(label)

        edge_targets: dict[tuple[str, str, str], tuple[str, str]] = {}
        for edge in fk_edges or []:
            from_stem = (getattr(edge, "from_stem", "") or "").lower()
            from_sheet = (getattr(edge, "from_sheet", "") or "").lower()
            from_col = str(getattr(edge, "from_column", "") or "").split(":")[0]
            to_stem = (getattr(edge, "to_stem", "") or "").lower()
            to_sheet = (getattr(edge, "to_sheet", "") or "").lower()
            if from_stem and from_sheet and from_col and to_stem and to_sheet:
                edge_targets[
                    (from_stem, from_sheet, self._field_name_norm(from_col))
                ] = (to_stem, to_sheet)

        def _placeholder_label(value) -> str:
            if not isinstance(value, str):
                return ""
            s = value.strip()
            return s[1:-1].strip() if s.startswith("<") and s.endswith(">") else ""

        def _ordinal(label: str) -> int:
            m = (re.search(r"(?:_id)?_(\d+)$", label or "")
                 or re.search(r"(\d+)(?=_id$)", label or ""))
            return int(m.group(1)) if m else 0

        n = 0
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            consumes = (getattr(it, "extras", None) or {}).get("consumes")
            for col, value in list(fields.items()):
                old_label = _placeholder_label(value)
                if not old_label or old_label in produced_labels:
                    continue
                col_norm = self._field_name_norm(col)
                target = edge_targets.get((stem, sheet, col_norm))
                if not target and col_norm != "id":
                    for maybe_target in produced_by_target:
                        if maybe_target == (stem, sheet):
                            continue
                        pk_names = {
                            self._field_name_norm(x)
                            for x in self._primary_field_names(maybe_target[0], maybe_target[1])
                        }
                        if col_norm in pk_names:
                            target = maybe_target
                            break
                if not target:
                    continue
                labels = produced_by_target.get(target) or []
                if not labels:
                    continue
                ord_n = _ordinal(old_label)
                if ord_n:
                    if ord_n > len(labels):
                        continue
                    new_label = labels[ord_n - 1]
                elif len(labels) == 1:
                    new_label = labels[0]
                else:
                    continue
                fields[col] = f"<{new_label}>"
                if old_label in getattr(it, "consumes_labels", []):
                    it.consumes_labels = [
                        new_label if x == old_label else x
                        for x in it.consumes_labels
                    ]
                elif new_label not in getattr(it, "consumes_labels", []):
                    it.consumes_labels.append(new_label)
                if isinstance(consumes, dict):
                    for c_key, c_val in list(consumes.items()):
                        if c_key == col or c_val == old_label:
                            consumes[c_key] = new_label
                n += 1
        if n:
            self._think(f"Step1 ordinal placeholders resolved: {n}")
        return n

    def _dedupe_same_sheet_shadows(self, intents: list[NLIntent]) -> list[NLIntent]:
        """同一 (action,stem,sheet) 组内去稀疏影子 intent（Step1 确定性修复）。

        场景：LLM 单 prompt 面对大候选池/同 workbook 多 sheet 时，会把同一目标
        sheet 拆成两版 add——① 稀疏影子（MailTemplate 只写主键，标题/内容空；
        或只写占位符 <new_xxx_id>），② 真实字段的 canonical 版（含 title/content）。
        两版同 sheet 同 action，现有 _dedupe_nl_intents / _drop_empty_add_shadows
        都不去（字段签名不同），导致 Step3 对该 sheet 写两行——影子先写盘留
        空行/占行，canonical 再写撞主键冲突。

        判据（保守，防误杀）：
          - 组内字段键先经 _field_canon_map 做中英桥（"模板ID"/"template_id"→同键），
            cli 不可用时退回纯文本归一。
          - 影子判定：A 的「非空实值」集合是 B 的「非空实值」集合的子集，
            且每个非空实值在 B 中同键同值（或 B 该键为空/占位符，视为待补）。
            空串、None、<占位符> 一律忽略——它们是"待补"哨兵，不参与影子判定。
            这样「{模板ID:30019, 标题:'', 内容:''}」是「{模板ID:<ph>, 标题:'月华…',
            内容:'…'}」的影子（影子只有主键实值，canonical 有全部实值）。
          - 仅 add 意图参与；set/delete 同 sheet 多条是合法多行定位，绝不互删。
          - 迭代剔除直到无影子；仅同 sheet 组内互比，跨 sheet 绝不互删。
        """

        def _real(v) -> str | None:
            """返回非空实值（str），空串/None/占位符返回 None（视为待补）。"""
            if v is None:
                return None
            s = str(v).strip()
            if not s:
                return None
            if s.startswith("<") and s.endswith(">"):
                return None
            return s

        if not intents:
            return intents
        kept = list(intents)
        for _ in range(len(kept) + 1):
            changed = False
            # 分组（组内比较）
            groups: dict[tuple, list[int]] = {}
            for i, it in enumerate(kept):
                key = (
                    getattr(it, "action", None),
                    (getattr(it, "table_hint", "") or "").strip().lower(),
                    (getattr(it, "sheet_hint", "") or "").strip().lower(),
                )
                groups.setdefault(key, []).append(i)
            drop: set[int] = set()
            for key, idxs in groups.items():
                if len(idxs) < 2:
                    continue
                # 仅 add 参与同 sheet 影子去重：set/delete 同 sheet 多条是
                # 不同行定位的合法多意图（BuildingInteract idle/collect、
                # 多行 modify），绝不互删。
                if key and key[0] not in ("add", None):
                    continue
                entries = []
                for i in idxs:
                    fields = (getattr(kept[i], "extras", None) or {}).get("fields") or {}
                    if not isinstance(fields, dict):
                        fields = {}
                    # 先删本表不存在的幻觉键（如 MailTemplate 里的「全服邮件ID」），
                    # 否则影子判定的键集合比较会被幻觉键污染（与 _prune_fields_not_
                    # in_schema 同口径，这里独立兜底保证本方法可单独使用）。
                    canon = self._field_canon_map(
                        getattr(kept[i], "table_hint", "") or "",
                        getattr(kept[i], "sheet_hint", "") or "")
                    valid_keys = set(canon.values()) if canon else None
                    norm: dict[str, object] = {}
                    for k, v in fields.items():
                        nk = ParseAgent._field_name_norm(k)
                        canon_key = canon.get(nk, nk)
                        if valid_keys is not None and canon_key not in valid_keys:
                            continue
                        norm[canon_key] = v
                    entries.append((i, norm))
                for ai, (ia, fa) in enumerate(entries):
                    for bi, (ib, fb) in enumerate(entries):
                        if ai == bi or ia in drop or ib in drop:
                            continue
                        # 提供的列集合：非空串/非 None 的键（占位符 <ph> 也算
                        # "提供了该列"，只是值为占位符待解析）。全空键不算。
                        def _provided(d: dict) -> set:
                            return {k for k, v in d.items()
                                    if v is not None and str(v).strip() != ""}
                        def _defaultish(v) -> bool:
                            if v is None:
                                return True
                            s = str(v).strip()
                            if not s:
                                return True
                            if s.startswith("<") and s.endswith(">"):
                                return True
                            return s in {"0", "0.0", "false", "False"}
                        a_keys = _provided(fa)
                        b_keys = _provided(fb)
                        a_real = {k: _real(v) for k, v in fa.items()
                                  if _real(v) is not None}
                        b_real = {k: _real(v) for k, v in fb.items()
                                  if _real(v) is not None}
                        a_info = {k: v for k, v in fa.items() if not _defaultish(v)}
                        b_info = {k: v for k, v in fb.items() if not _defaultish(v)}
                        if not a_keys and b_keys:
                            drop.add(ia)
                            changed = True
                            continue
                        if not a_keys:
                            continue
                        # a 的列集合是 b 的子集，或两者 produces 相同且 b 比 a
                        # 含更多真实值，均可判影子；后者覆盖“同键但空值版 +
                        # canonical 实值版”的 LLM 重试残留。
                        same_produces = (
                            (getattr(kept[ia], "produces_label", None)
                             or (getattr(kept[ia], "extras", None) or {}).get("produces"))
                            and
                            (getattr(kept[ia], "produces_label", None)
                             or (getattr(kept[ia], "extras", None) or {}).get("produces"))
                            ==
                            (getattr(kept[ib], "produces_label", None)
                             or (getattr(kept[ib], "extras", None) or {}).get("produces"))
                        )
                        same_produces_richer = (
                            same_produces
                            and (a_keys & b_keys)
                            and (len(a_real), len(a_keys)) < (len(b_real), len(b_keys)))
                        shared_anchor = any(
                            k in b_keys and (
                                _defaultish(fa.get(k))
                                or _defaultish(fb.get(k))
                                or str(fa.get(k)).strip() == str(fb.get(k)).strip()
                            )
                            for k in (a_keys & b_keys)
                        )
                        default_shell_shadow = (
                            shared_anchor
                            and len(a_info) < len(b_info)
                            and set(a_info).issubset(set(b_info))
                        )
                        if not (a_keys < b_keys or same_produces_richer or default_shell_shadow):
                            continue
                        # a 的每个非占位实值都要在 b 中同键同值（b 该键空/占位 =
                        # 待补，不算冲突）。
                        shadow = True
                        for k, av in fa.items():
                            if av is None or str(av).strip() == "":
                                continue
                            av_s = str(av).strip()
                            if av_s.startswith("<") and av_s.endswith(">"):
                                continue
                            bv = fb.get(k)
                            if bv is None or str(bv).strip() == "":
                                continue
                            bv_s = str(bv).strip()
                            if bv_s.startswith("<") and bv_s.endswith(">"):
                                continue
                            if (bv_s != av_s and not same_produces_richer
                                    and not default_shell_shadow):
                                shadow = False
                                break
                        if shadow:
                            if same_produces_richer or default_shell_shadow:
                                src = (getattr(kept[ia], "extras", None) or {}).get("fields") or {}
                                dst = (getattr(kept[ib], "extras", None) or {}).get("fields") or {}
                                if isinstance(src, dict) and isinstance(dst, dict):
                                    dst_norm = {
                                        canon.get(ParseAgent._field_name_norm(k),
                                                  ParseAgent._field_name_norm(k)): k
                                        for k in dst
                                    }
                                    for mk, mv in src.items():
                                        mn = canon.get(ParseAgent._field_name_norm(mk),
                                                       ParseAgent._field_name_norm(mk))
                                        old_key = dst_norm.get(mn)
                                        if old_key is None or str(dst.get(old_key, "")).strip() == "":
                                            dst[mk] = mv
                            drop.add(ia)
                            changed = True
            if drop:
                kept = [it for i, it in enumerate(kept) if i not in drop]
            if not changed:
                break
        return kept

    def _replace_reused_explicit_pks_with_placeholders(self, intents: list[NLIntent],
                                                       fk_edges: list) -> int:
        if not intents:
            return 0

        def _real(value) -> str | None:
            if value is None:
                return None
            s = str(value).strip()
            if not s or (s.startswith("<") and s.endswith(">")):
                return None
            return s

        def _label(it: NLIntent) -> str:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            return str(label or "").strip().strip("<>").strip()

        foreign_fk_cols: set[tuple[str, str, str]] = set()
        for e in fk_edges or []:
            fs = (getattr(e, "from_stem", "") or "").strip().lower()
            fsh = (getattr(e, "from_sheet", "") or "").strip().lower()
            ts = (getattr(e, "to_stem", "") or "").strip().lower()
            tsh = (getattr(e, "to_sheet", "") or "").strip().lower()
            fc = self._field_name_norm(getattr(e, "from_column", "") or "")
            if fs and fc and (fs, fsh) != (ts, tsh):
                foreign_fk_cols.add((fs, fsh, fc))

        groups: dict[tuple[str, str, str, str], list[tuple[int, str, str, dict[str, tuple[str, object]]]]] = {}
        for idx, it in enumerate(intents):
            if getattr(it, "action", "") != "add":
                continue
            label = _label(it)
            if not label:
                continue
            stem = (getattr(it, "table_hint", "") or "").strip().lower()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            pk_names = {self._field_name_norm(x)
                        for x in self._primary_field_names(stem, sheet)}
            if not stem or not sheet or not pk_names:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            canon = self._field_canon_map(stem, sheet)
            norm: dict[str, tuple[str, object]] = {}
            for col, value in fields.items():
                cn = canon.get(self._field_name_norm(col), self._field_name_norm(col))
                norm[cn] = (col, value)
            # §可共用引用列豁免：既有数据里取值有重复的列（buff_id / talent_id /
            # model_id 这类多行共用的引用列）不是身份列，同一显式值被多条 intent
            # 复用是合法的，转成占位符反而把用户给的真实引用值弄丢。
            shared_cols = self._shared_value_columns(stem, sheet)
            id_like_names = {
                k for k in norm
                if k not in shared_cols
                and (
                    k in pk_names
                    or (
                        "id" in k
                        and (stem, sheet.lower(), k) not in foreign_fk_cols
                    )
                )
            }
            for pk in id_like_names:
                item = norm.get(pk)
                if not item:
                    continue
                rv = _real(item[1])
                if rv is None:
                    continue
                groups.setdefault((stem, sheet.lower(), pk, rv), []).append((idx, label, item[0], norm))

        replace: dict[str, list[tuple[int, str, str]]] = {}
        for (_stem, _sheet, _pk, pk_value), rows in groups.items():
            labels = {label for _idx, label, _col, _norm in rows}
            if len(rows) < 2 or len(labels) < 2:
                continue
            signatures = []
            for _idx, _label_s, _col, norm in rows:
                sig = {
                    k: _real(v)
                    for k, (_orig, v) in norm.items()
                    if k != _pk and _real(v) is not None
                }
                signatures.append(sig)
            conflict = False
            for pos, a in enumerate(signatures):
                for b in signatures[pos + 1:]:
                    shared = set(a) & set(b)
                    if any(a[k] != b[k] for k in shared) or (a and b and a != b):
                        conflict = True
                        break
                if conflict:
                    break
            if not conflict:
                continue
            for idx, label, pk_col, _norm in rows:
                fields = (getattr(intents[idx], "extras", None) or {}).get("fields") or {}
                if isinstance(fields, dict):
                    fields[pk_col] = f"<{label}>"
                    replace.setdefault(label, []).append((idx, pk_value, _stem))

        if not replace:
            return 0

        by_label = {
            label: (old_value, stem)
            for label, rows in replace.items()
            for _idx, old_value, stem in rows[:1]
        }
        fk_cols: dict[tuple[str, str], set[str]] = {}
        for e in fk_edges or []:
            to_stem = (getattr(e, "to_stem", "") or "").strip().lower()
            to_sheet = (getattr(e, "to_sheet", "") or "").strip().lower()
            from_stem = (getattr(e, "from_stem", "") or "").strip().lower()
            from_sheet = (getattr(e, "from_sheet", "") or "").strip().lower()
            from_col = self._field_name_norm(getattr(e, "from_column", "") or "")
            if to_stem and from_stem and from_col:
                fk_cols.setdefault((from_stem, from_sheet), set()).add(from_col)

        changed = sum(len(v) for v in replace.values())
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            consumes = {
                str(x).strip().strip("<>").strip()
                for x in (getattr(it, "consumes_labels", None) or [])
            }
            if not consumes:
                continue
            stem = (getattr(it, "table_hint", "") or "").strip().lower()
            sheet = (getattr(it, "sheet_hint", "") or "").strip().lower()
            canon = self._field_canon_map(stem, sheet)
            allowed_fk_cols = fk_cols.get((stem, sheet), set())
            relevant_labels = sorted(
                consumes & set(by_label),
                key=lambda x: (self._placeholder_ordinal(x) or 10**6, x))
            assigned_cols: set[str] = set()
            values_to_labels: dict[str, list[str]] = {}
            for label in relevant_labels:
                old_value, _parent_stem = by_label[label]
                values_to_labels.setdefault(old_value, []).append(label)
            for old_value, labels in values_to_labels.items():
                if len(labels) <= 1:
                    continue
                matching_cols = []
                for col, value in fields.items():
                    if _real(value) != old_value:
                        continue
                    col_norm = canon.get(self._field_name_norm(col), self._field_name_norm(col))
                    if allowed_fk_cols and col_norm not in allowed_fk_cols:
                        continue
                    nums = re.findall(r"\d+", str(col))
                    order = int(nums[-1]) if nums else 10**6
                    matching_cols.append((order, col))
                if len(matching_cols) < len(labels):
                    continue
                matching_cols.sort(key=lambda x: (x[0], x[1]))
                for (_order, col), label in zip(matching_cols, labels):
                    fields[col] = f"<{label}>"
                    assigned_cols.add(col)
                    changed += 1
            for label in relevant_labels:
                old_value, _parent_stem = by_label[label]
                for col, value in list(fields.items()):
                    if col in assigned_cols:
                        continue
                    if _real(value) != old_value:
                        continue
                    col_norm = canon.get(self._field_name_norm(col), self._field_name_norm(col))
                    if allowed_fk_cols and col_norm not in allowed_fk_cols:
                        continue
                    fields[col] = f"<{label}>"
                    changed += 1
        if changed:
            self._think(f"Step1 reused explicit PKs converted to placeholders: {changed}")
        return changed

    def _uniquify_reused_placeholder_primary_fields(self, intents: list[NLIntent]) -> int:
        if not intents:
            return 0

        def _placeholder(value) -> str:
            if not isinstance(value, str):
                return ""
            s = value.strip()
            if s.startswith("<") and s.endswith(">"):
                return s.strip("<>").strip()
            return ""

        def _label(it: NLIntent) -> str:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            return str(label or "").strip().strip("<>").strip()

        groups: dict[tuple[str, str, str, str], list[tuple[int, str, str, dict[str, object]]]] = {}
        for idx, it in enumerate(intents):
            if getattr(it, "action", "") != "add":
                continue
            stem = (getattr(it, "table_hint", "") or "").strip().lower()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            pk_names = {self._field_name_norm(x)
                        for x in self._primary_field_names(stem, sheet)}
            if not stem or not sheet or not pk_names:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            canon = self._field_canon_map(stem, sheet)
            norm: dict[str, tuple[str, object]] = {}
            for col, value in fields.items():
                cn = canon.get(self._field_name_norm(col), self._field_name_norm(col))
                norm[cn] = (col, value)
            for pk in pk_names:
                item = norm.get(pk)
                if not item:
                    continue
                ph = _placeholder(item[1])
                if ph:
                    groups.setdefault((stem, sheet.lower(), pk, ph), []).append(
                        (idx, item[0], ph, fields))

        changed = 0
        for (_stem, _sheet, _pk, shared_label), rows in groups.items():
            if len(rows) < 2:
                continue
            signatures = []
            for idx, _pk_col, _ph, fields in rows:
                signatures.append({
                    self._field_name_norm(k): str(v).strip()
                    for k, v in fields.items()
                    if self._field_name_norm(k) != _pk
                    and v is not None
                    and str(v).strip()
                    and not _placeholder(v)
                })
            if len({tuple(sorted(sig.items())) for sig in signatures}) <= 1:
                continue
            used: set[str] = set()
            for pos, (idx, pk_col, _ph, fields) in enumerate(rows, start=1):
                it = intents[idx]
                current = _label(it)
                if current and current != shared_label and current not in used:
                    new_label = current
                elif current == shared_label and shared_label not in used:
                    new_label = shared_label
                else:
                    base = current or shared_label or f"new_{_stem}_id"
                    new_label = f"{base}_{pos}"
                    while new_label in used:
                        pos += 1
                        new_label = f"{base}_{pos}"
                used.add(new_label)
                if fields.get(pk_col) != f"<{new_label}>":
                    fields[pk_col] = f"<{new_label}>"
                    changed += 1
                if not _label(it) or _label(it) == shared_label and new_label != shared_label:
                    it.produces_label = new_label
                    if getattr(it, "extras", None) is not None:
                        it.extras["produces"] = new_label
                if new_label != shared_label:
                    it.consumes_labels = [
                        x for x in (getattr(it, "consumes_labels", None) or [])
                        if str(x).strip().strip("<>").strip() != shared_label
                    ]
        if changed:
            self._think(f"Step1 reused placeholder PKs uniquified: {changed}")
        return changed

    def _dedupe_same_sheet_natural_duplicates(self, intents: list[NLIntent]) -> list[NLIntent]:
        if not intents:
            return intents
        groups: dict[tuple, list[int]] = {}
        for idx, it in enumerate(intents):
            key = (
                getattr(it, "action", None),
                (getattr(it, "table_hint", "") or "").strip().lower(),
                (getattr(it, "sheet_hint", "") or "").strip().lower(),
            )
            groups.setdefault(key, []).append(idx)

        def _real(value) -> str | None:
            if value is None:
                return None
            s = str(value).strip()
            if not s or (s.startswith("<") and s.endswith(">")):
                return None
            return s

        def _label(it) -> str:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            return str(label or "").strip().strip("<>").strip()

        label_ref_count: dict[str, int] = {}
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            for label in self._field_placeholder_labels(fields):
                label_ref_count[label] = label_ref_count.get(label, 0) + 1
            for label in getattr(it, "consumes_labels", None) or []:
                label_s = str(label).strip().strip("<>").strip()
                if label_s:
                    label_ref_count[label_s] = label_ref_count.get(label_s, 0) + 1

        drop: set[int] = set()
        remap: dict[str, str] = {}
        for key, idxs in groups.items():
            if len(idxs) < 2 or (key and key[0] not in ("add", None)):
                continue
            pk_names = {
                self._field_name_norm(x)
                for x in self._primary_field_names(key[1], key[2])
            }
            if not pk_names:
                continue
            entries = []
            for idx in idxs:
                fields = (getattr(intents[idx], "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    fields = {}
                canon = self._field_canon_map(
                    getattr(intents[idx], "table_hint", "") or "",
                    getattr(intents[idx], "sheet_hint", "") or "")
                norm: dict[str, object] = {}
                for col, value in fields.items():
                    col_norm = self._field_name_norm(col)
                    norm[canon.get(col_norm, col_norm)] = value
                non_pk_real = {
                    col: _real(value)
                    for col, value in norm.items()
                    if col not in pk_names and _real(value) is not None
                }
                pk_real = {
                    col: _real(value)
                    for col, value in norm.items()
                    if col in pk_names and _real(value) is not None
                }
                entries.append((idx, non_pk_real, pk_real, norm))
            for pos, a in enumerate(entries):
                ia, a_natural, a_pk, a_norm = a
                if ia in drop or len(a_natural) < 2:
                    continue
                for ib, b_natural, b_pk, b_norm in entries[pos + 1:]:
                    if ib in drop or a_natural != b_natural:
                        continue
                    a_score = (len(a_pk), len(a_natural), -ia)
                    b_score = (len(b_pk), len(b_natural), -ib)
                    winner, loser = (ia, ib) if a_score >= b_score else (ib, ia)
                    loser_label = _label(intents[loser])
                    winner_label = _label(intents[winner])
                    if loser_label and label_ref_count.get(loser_label, 0) and not winner_label:
                        continue
                    if loser_label and winner_label:
                        remap[loser_label] = winner_label
                    drop.add(loser)
        if remap:
            for it in intents:
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if isinstance(fields, dict):
                    for col, value in list(fields.items()):
                        if not isinstance(value, str):
                            continue
                        raw = value.strip()
                        if raw.startswith("<") and raw.endswith(">"):
                            label = raw.strip("<>").strip()
                            if label in remap:
                                fields[col] = f"<{remap[label]}>"
                it.consumes_labels = [
                    remap.get(str(x).strip().strip("<>").strip(), x)
                    for x in (getattr(it, "consumes_labels", None) or [])
                ]
        if not drop:
            return intents
        return [it for idx, it in enumerate(intents) if idx not in drop]

    def _demote_consumed_duplicate_producers(self, intents: list[NLIntent]) -> int:
        if not intents:
            return 0

        def _label(it) -> str:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            return str(label or "").strip().strip("<>").strip()

        by_label: dict[str, list[NLIntent]] = {}
        for it in intents:
            label = _label(it)
            if label:
                by_label.setdefault(label, []).append(it)
        duplicates = {label for label, rows in by_label.items() if len(rows) > 1}
        if not duplicates:
            return 0

        n = 0
        for label in duplicates:
            rows = by_label[label]
            keep = rows[0]
            demote: list[NLIntent] = []
            for it in rows:
                stem = (getattr(it, "table_hint", "") or "").lower()
                sheet = (getattr(it, "sheet_hint", "") or "").lower()
                pk_names = {
                    self._field_name_norm(x)
                    for x in self._primary_field_names(stem, sheet)
                }
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    fields = {}
                non_pk_refs_label = False
                pk_refs_label = False
                for col, value in fields.items():
                    if not isinstance(value, str):
                        continue
                    raw = value.strip()
                    if not (raw.startswith("<") and raw.endswith(">")):
                        continue
                    if raw.strip("<>").strip() != label:
                        continue
                    col_norm = self._field_name_norm(col)
                    if pk_names and col_norm in pk_names:
                        pk_refs_label = True
                    elif col_norm != "id":
                        non_pk_refs_label = True
                if non_pk_refs_label:
                    demote.append(it)
                elif pk_refs_label:
                    keep = it
            for it in demote:
                if it is keep:
                    continue
                if getattr(it, "produces_label", None):
                    it.produces_label = None
                    n += 1
                extras = getattr(it, "extras", None)
                if isinstance(extras, dict) and extras.get("produces"):
                    extras.pop("produces", None)
                    n += 1
        by_label = {}
        for it in intents:
            label = _label(it)
            if label:
                by_label.setdefault(label, []).append(it)
        for label, rows in by_label.items():
            if len(rows) <= 1:
                continue

            def _owns_label(it: NLIntent) -> tuple[int, int]:
                stem = (getattr(it, "table_hint", "") or "").lower()
                sheet = (getattr(it, "sheet_hint", "") or "").lower()
                pk_names = {
                    self._field_name_norm(x)
                    for x in self._primary_field_names(stem, sheet)
                } or {"id"}
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    fields = {}
                pk_ref = 0
                any_ref = 0
                for col, value in fields.items():
                    if not isinstance(value, str):
                        continue
                    raw = value.strip()
                    if not (raw.startswith("<") and raw.endswith(">")):
                        continue
                    if raw.strip("<>").strip() != label:
                        continue
                    any_ref = 1
                    if self._field_name_norm(col) in pk_names:
                        pk_ref = 1
                return pk_ref, any_ref

            keep = max(rows, key=_owns_label)
            for it in rows:
                if it is keep:
                    continue
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if isinstance(fields, dict):
                    for col, value in list(fields.items()):
                        if not isinstance(value, str):
                            continue
                        raw = value.strip()
                        if raw.startswith("<") and raw.endswith(">") and raw.strip("<>").strip() == label:
                            fields.pop(col, None)
                if getattr(it, "produces_label", None):
                    it.produces_label = None
                    n += 1
                extras = getattr(it, "extras", None)
                if isinstance(extras, dict) and extras.get("produces"):
                    extras.pop("produces", None)
                    n += 1
        return n

    @staticmethod
    def _fk_edge_targets(fk_edges) -> dict:
        """(from_stem, from_sheet, from_col_norm) -> {(to_stem, to_sheet)}（仅跨表边）。"""
        out: dict = {}
        for e in fk_edges or []:
            fs = (getattr(e, "from_stem", "") or "").strip().lower()
            fsh = (getattr(e, "from_sheet", "") or "").strip().lower()
            ts = (getattr(e, "to_stem", "") or "").strip().lower()
            tsh = (getattr(e, "to_sheet", "") or "").strip().lower()
            fc = re.sub(r"[\s_:\-./\\()\[\]（）【】]+", "",
                        str(getattr(e, "from_column", "") or "").lower())
            if fs and fsh and fc and ts and tsh and (fs, fsh) != (ts, tsh):
                out.setdefault((fs, fsh, fc), set()).add((ts, tsh))
        return out

    def _bind_self_primary_to_upstream(self, intents: list[NLIntent],
                                       fk_edges: list) -> int:
        """自引用主键 → 绑定上游 producer（子表主键本身就是外键的场景）。

        场景：school_spirit 的「神通id」既是本表主键，又是指向 school_ability
        的外键。LLM 只会把主键列填成自己的 produces 占位符 <new_school_spirit_id>，
        Step2 于是按自增分配 110~113，"把神通和灵根绑定"的语义彻底丢失（神通id
        应为新神通 8012~8015）。

        判据（全部满足才改，否则一律不动，宁可退化为自动分配）：
          1. add intent，主键列当前是占位符且恰为本条自己的 produces label
             （= 用户没显式给主键）
          2. 该主键列存在跨表 FK 边指向 (to_stem, to_sheet)
          3. 本批该目标恰好有 N 个 producer，且本表本 sheet 的自引用 intent
             也恰好 N 条 → 按出现顺序一一绑定
        """
        if not intents or not fk_edges:
            return 0

        def _label(it) -> str:
            lab = getattr(it, "produces_label", None)
            if not lab and getattr(it, "extras", None):
                lab = it.extras.get("produces")
            return str(lab or "").strip().strip("<>").strip()

        def _placeholder(value) -> str:
            if not isinstance(value, str):
                return ""
            s = value.strip()
            return s[1:-1].strip() if s.startswith("<") and s.endswith(">") else ""

        edge_targets = self._fk_edge_targets(fk_edges)
        produced_by_target: dict = {}
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            lab = _label(it)
            if not lab:
                continue
            produced_by_target.setdefault(
                ((getattr(it, "table_hint", "") or "").strip().lower(),
                 (getattr(it, "sheet_hint", "") or "").strip().lower()),
                []).append(lab)

        groups: dict = {}
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            own = _label(it)
            if not own:
                continue
            stem = (getattr(it, "table_hint", "") or "").strip().lower()
            sheet_l = (getattr(it, "sheet_hint", "") or "").strip().lower()
            sheet = getattr(it, "sheet_hint", "") or ""
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            pk_names = {self._field_name_norm(x)
                        for x in self._primary_field_names(stem, sheet)}
            for col, value in fields.items():
                cn = self._field_name_norm(col)
                if cn not in pk_names or _placeholder(value) != own:
                    continue
                targets = edge_targets.get((stem, sheet_l, cn)) or set()
                for tgt in targets:
                    groups.setdefault((stem, sheet_l, cn, tgt), []).append((it, col))

        n = 0
        for (_stem, _sheet, _cn, target), rows in groups.items():
            producers = produced_by_target.get(target) or []
            if not producers or len(producers) != len(rows):
                continue
            for (it, col), lab in zip(rows, producers):
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                fields[col] = f"<{lab}>"
                it.consumes_labels = list(
                    getattr(it, "consumes_labels", None) or [])
                if lab not in it.consumes_labels:
                    it.consumes_labels.append(lab)
                extras = getattr(it, "extras", None)
                if isinstance(extras, dict):
                    consumes = extras.get("consumes")
                    if isinstance(consumes, dict):
                        consumes[col] = lab
                n += 1
        if n:
            self._think(f"Step1 self-primary bound to upstream producers: {n}")
        return n

    def _drop_foreign_placeholder_primary_fields(self, intents: list[NLIntent],
                                                 fk_edges: list = None) -> int:
        if not intents:
            return 0
        produced = set()
        produced_at: dict = {}
        for it in intents:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if label:
                lab = str(label).strip().strip("<>").strip()
                produced.add(lab)
                produced_at.setdefault(lab, set()).add(
                    ((getattr(it, "table_hint", "") or "").strip().lower(),
                     (getattr(it, "sheet_hint", "") or "").strip().lower()))
        edge_targets = self._fk_edge_targets(fk_edges) if fk_edges else {}

        def _placeholder(value) -> str:
            if not isinstance(value, str):
                return ""
            raw = value.strip()
            if raw.startswith("<") and raw.endswith(">"):
                return raw.strip("<>").strip()
            return ""

        n = 0
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            own = getattr(it, "produces_label", None)
            if not own and getattr(it, "extras", None):
                own = it.extras.get("produces")
            own = str(own or "").strip().strip("<>").strip()
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            pk_names = {
                self._field_name_norm(x)
                for x in self._primary_field_names(stem, sheet)
            }
            if not pk_names:
                pk_names = {"id"}
            refs_by_col = {
                col: _placeholder(value)
                for col, value in fields.items()
                if _placeholder(value)
            }
            non_pk_refs = {
                label for col, label in refs_by_col.items()
                if self._field_name_norm(col) not in pk_names
            }
            for col, label in list(refs_by_col.items()):
                col_norm = self._field_name_norm(col)
                if col_norm not in pk_names:
                    continue
                if not label or label == own or label not in produced:
                    continue
                if label in non_pk_refs:
                    del fields[col]
                    n += 1
                    continue
                # §自引用主键已绑上游（_bind_self_primary_to_upstream）：本列 FK 目标
                # 的 producer 就是 label → 保留该绑定，不要回填成自己的 produces
                # label（否则 school_spirit 的 神通id 又被打回 <new_school_spirit_id>）。
                if edge_targets:
                    _tgts = edge_targets.get((stem, sheet, col_norm)) or set()
                    if _tgts and (produced_at.get(label) or set()) & _tgts:
                        continue
                if own:
                    fields[col] = f"<{own}>"
                    it.consumes_labels = [
                        x for x in (getattr(it, "consumes_labels", None) or [])
                        if str(x).strip().strip("<>").strip() != label
                    ]
                    n += 1
                    continue
                if not own:
                    base = f"new_{stem}_id" if stem else "new_row_id"
                    new_label = base
                    suffix = 1
                    while new_label in produced:
                        suffix += 1
                        new_label = f"{base}_{suffix}"
                    fields[col] = f"<{new_label}>"
                    it.produces_label = new_label
                    if getattr(it, "extras", None) is not None:
                        it.extras["produces"] = new_label
                    produced.add(new_label)
                    it.consumes_labels = [
                        x for x in (getattr(it, "consumes_labels", None) or [])
                        if str(x).strip().strip("<>").strip() != label
                    ]
                    n += 1
                    continue
        return n

    @staticmethod
    def _clean_self_consumes(intents: list[NLIntent]) -> int:
        n = 0
        for it in intents or []:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            label_s = str(label).strip()
            old = list(getattr(it, "consumes_labels", None) or [])
            new = [x for x in old if str(x).strip() != label_s]
            if len(new) != len(old):
                it.consumes_labels = new
                n += len(old) - len(new)
            consumes = (getattr(it, "extras", None) or {}).get("consumes")
            if isinstance(consumes, dict):
                for key, value in list(consumes.items()):
                    if str(value).strip().strip("<>") == label_s:
                        del consumes[key]
                        n += 1
        return n

    @staticmethod
    def _field_placeholder_labels(fields: dict) -> set[str]:
        labels: set[str] = set()
        if not isinstance(fields, dict):
            return labels
        for value in fields.values():
            if isinstance(value, str):
                raw = value.strip()
                if raw.startswith("<") and raw.endswith(">"):
                    label = raw.strip("<>").strip()
                    if label:
                        labels.add(label)
        return labels

    @staticmethod
    def _clean_dangling_consumes(intents: list[NLIntent]) -> int:
        n = 0
        for it in intents or []:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            referenced = ParseAgent._field_placeholder_labels(fields)
            consumes = (getattr(it, "extras", None) or {}).get("consumes")
            if isinstance(consumes, dict):
                for value in consumes.values():
                    if isinstance(value, str):
                        label = value.strip().strip("<>").strip()
                        if label:
                            referenced.add(label)
            old = list(getattr(it, "consumes_labels", None) or [])
            new = [x for x in old if str(x).strip().strip("<>").strip() in referenced]
            if len(new) != len(old):
                it.consumes_labels = new
                n += len(old) - len(new)
        return n

    def _expand_repeated_child_configs(self, intents: list[NLIntent],
                                       text: str, fk_edges: list) -> int:
        if not intents:
            return 0
        parent_labels: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for idx, it in enumerate(intents):
            if getattr(it, "action", "") != "add":
                continue
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            parent_labels.setdefault((
                (getattr(it, "table_hint", "") or "").lower(),
                (getattr(it, "sheet_hint", "") or "").lower(),
            ), []).append((idx, str(label)))
        if not parent_labels:
            return 0

        edge_by_child_col: dict[tuple[str, str, str], tuple[str, str]] = {}
        for edge in fk_edges or []:
            child_key = (
                (getattr(edge, "from_stem", "") or "").lower(),
                (getattr(edge, "from_sheet", "") or "").lower(),
                self._field_name_norm(getattr(edge, "from_column", "") or ""),
            )
            target = (
                (getattr(edge, "to_stem", "") or "").lower(),
                (getattr(edge, "to_sheet", "") or "").lower(),
            )
            if child_key[0] and child_key[1] and child_key[2] and target[0]:
                edge_by_child_col[child_key] = target

        sequences = self._ordered_numeric_sequences(text or "")
        added: list[NLIntent] = []
        import copy
        for idx, child in enumerate(list(intents)):
            if getattr(child, "action", "") != "add":
                continue
            fields = (getattr(child, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            child_stem = (getattr(child, "table_hint", "") or "").lower()
            child_sheet = (getattr(child, "sheet_hint", "") or "").lower()
            for col, value in list(fields.items()):
                if not isinstance(value, str):
                    continue
                old_label = value.strip().strip("<>").strip()
                if not old_label:
                    continue
                target = edge_by_child_col.get((
                    child_stem, child_sheet, self._field_name_norm(col)))
                if target is None:
                    col_norm = self._field_name_norm(col)
                    for maybe_target in parent_labels:
                        if maybe_target[0] != child_stem or maybe_target[1] == child_sheet:
                            continue
                        pk_names = {
                            self._field_name_norm(x)
                            for x in self._primary_field_names(maybe_target[0], maybe_target[1])
                        }
                        if col_norm in pk_names:
                            target = maybe_target
                            break
                labels = parent_labels.get(target or ())
                if not labels or len(labels) <= 1:
                    continue
                parent_label_set = {label for _, label in labels}
                if old_label not in parent_label_set:
                    ord_pos = self._placeholder_ordinal(old_label)
                    if ord_pos and 1 <= ord_pos <= len(labels):
                        old_label = labels[ord_pos - 1][1]
                        fields[col] = f"<{old_label}>"
                        child.consumes_labels = [
                            old_label if x == value.strip().strip("<>").strip() else x
                            for x in (getattr(child, "consumes_labels", None) or [])
                        ]
                    if old_label in parent_label_set:
                        continue
                    referenced = [
                        str(x).strip().strip("<>").strip()
                        for x in (getattr(child, "consumes_labels", None) or [])
                        if str(x).strip().strip("<>").strip() in parent_label_set
                    ]
                    if len(referenced) != 1:
                        family_matches = [
                            label for _, label in labels
                            if self._placeholder_family(label)
                            == self._placeholder_family(old_label)
                        ]
                        old_label = labels[0][1]
                        fields[col] = f"<{old_label}>"
                    else:
                        old_label = referenced[0]
                        fields[col] = f"<{old_label}>"
                existing = set()
                for other in intents:
                    if ((getattr(other, "table_hint", "") or "").lower() != child_stem
                            or (getattr(other, "sheet_hint", "") or "").lower() != child_sheet):
                        continue
                    ofields = (getattr(other, "extras", None) or {}).get("fields") or {}
                    if not isinstance(ofields, dict):
                        continue
                    oval = ofields.get(col)
                    if isinstance(oval, str):
                        existing.add(oval.strip().strip("<>").strip())
                for parent_pos, (_parent_idx, label) in enumerate(labels):
                    if label in existing:
                        continue
                    clone = copy.deepcopy(child)
                    clone_fields = (clone.extras or {}).get("fields") or {}
                    for k, v in list(clone_fields.items()):
                        if isinstance(v, str):
                            clone_fields[k] = v.replace(f"<{old_label}>", f"<{label}>")
                    clone_fields[col] = f"<{label}>"
                    self._apply_ordered_sequence_values(
                        clone_fields, fields, sequences, parent_pos, len(labels))
                    clone.consumes_labels = [
                        label if x == old_label else x
                        for x in (getattr(clone, "consumes_labels", None) or [])
                    ]
                    if label not in clone.consumes_labels:
                        clone.consumes_labels.append(label)
                    prod = getattr(clone, "produces_label", None)
                    if prod:
                        new_prod = f"{prod}_{parent_pos + 1}"
                        clone.produces_label = new_prod
                        clone.extras["produces"] = new_prod
                        for k, v in list(clone_fields.items()):
                            if isinstance(v, str) and v.strip() == f"<{prod}>":
                                clone_fields[k] = f"<{new_prod}>"
                    added.append(clone)
                    existing.add(label)
                break
        if added:
            intents.extend(added)
        return len(added)

    def _resolve_placeholders_to_explicit_pks(self, intents: list[NLIntent]) -> int:
        explicit: dict[str, object] = {}
        for it in intents or []:
            if getattr(it, "action", "") != "add":
                continue
            label = getattr(it, "produces_label", None) or \
                ((getattr(it, "extras", None) or {}).get("produces"))
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = getattr(it, "sheet_hint", "") or ""
            pk_names = self._primary_field_names(stem, sheet)
            if not pk_names:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                if self._field_name_norm(key) not in pk_names:
                    continue
                if value is None:
                    continue
                s = str(value).strip()
                if not s or (s.startswith("<") and s.endswith(">")):
                    continue
                explicit[f"new_{stem}_id"] = value
                if label:
                    explicit[str(label).strip().strip("<>").strip()] = value
                break
        if not explicit:
            return 0
        n = 0
        for it in intents or []:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if isinstance(fields, dict):
                for key, value in list(fields.items()):
                    if not isinstance(value, str):
                        continue
                    raw = value.strip()
                    if not (raw.startswith("<") and raw.endswith(">")):
                        continue
                    label = raw.strip("<>").strip()
                    if label in explicit:
                        fields[key] = explicit[label]
                        n += 1
            old = list(getattr(it, "consumes_labels", None) or [])
            new = [x for x in old if x not in explicit]
            if len(new) != len(old):
                it.consumes_labels = new
                n += len(old) - len(new)
        return n

    def _fill_empty_fks_from_explicit_pks(self, intents: list[NLIntent], fk_edges: list) -> int:
        if not intents or not fk_edges:
            return 0
        explicit_by_target: dict[tuple[str, str], object] = {}
        ambiguous: set[tuple[str, str]] = set()
        for it in intents or []:
            if getattr(it, "action", "") != "add":
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            pk_names = {
                self._field_name_norm(x)
                for x in self._primary_field_names(stem, sheet)
            }
            if not pk_names:
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            for key, value in fields.items():
                if self._field_name_norm(key) not in pk_names:
                    continue
                if value is None:
                    continue
                s = str(value).strip()
                if not s or (s.startswith("<") and s.endswith(">")):
                    continue
                target = (stem, sheet)
                if target in explicit_by_target and explicit_by_target[target] != value:
                    ambiguous.add(target)
                else:
                    explicit_by_target[target] = value
                break
        for target in ambiguous:
            explicit_by_target.pop(target, None)
        if not explicit_by_target:
            return 0

        edge_target_by_child_col: dict[tuple[str, str, str], tuple[str, str]] = {}
        for edge in fk_edges or []:
            child_key = (
                (getattr(edge, "from_stem", "") or "").lower(),
                (getattr(edge, "from_sheet", "") or "").lower(),
                self._field_name_norm(getattr(edge, "from_column", "") or ""),
            )
            target = (
                (getattr(edge, "to_stem", "") or "").lower(),
                (getattr(edge, "to_sheet", "") or "").lower(),
            )
            if child_key[0] and child_key[1] and child_key[2] and target in explicit_by_target:
                edge_target_by_child_col[child_key] = target

        n = 0
        for it in intents or []:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            stem = (getattr(it, "table_hint", "") or "").lower()
            sheet = (getattr(it, "sheet_hint", "") or "").lower()
            for key, value in list(fields.items()):
                if value is not None and str(value).strip() != "":
                    continue
                target = edge_target_by_child_col.get((stem, sheet, self._field_name_norm(key)))
                if target is None:
                    continue
                fields[key] = explicit_by_target[target]
                n += 1
        return n

    def _expand_enumerated_same_sheet_adds(self, intents: list[NLIntent], text: str) -> int:
        if not intents or not text:
            return 0

        def _chunks() -> list[str]:
            patterns = [
                r"(第[一二三四五六七八九十\d]+条[:：]?.*?)(?=第[一二三四五六七八九十\d]+条[:：]?|$)",
                r"(选项编号\s*\d+.*?)(?=选项编号\s*\d+|$)",
            ]
            out: list[str] = []
            for pat in patterns:
                for m in re.finditer(pat, text, re.S):
                    chunk = m.group(1).strip(" ，,；;。")
                    if chunk and chunk not in out:
                        out.append(chunk)
            return out

        def _first_quote(s: str) -> str:
            m = re.search(r"['\"]([^'\"]+)['\"]", s)
            return m.group(1).strip() if m else ""

        def _field_by_norm(keys: list[str], *needles: str) -> str:
            for k in keys:
                kn = self._field_name_norm(k)
                if any(n in kn for n in needles):
                    return k
            return ""

        def _extract(keys: list[str], chunk: str) -> dict:
            out: dict = {}
            key_col = _field_by_norm(keys, "key", "程序引用关键字")
            if key_col:
                m = re.search(r"\bkey\b\s*(?:用|=|为)?\s*([A-Za-z][A-Za-z0-9_]*)",
                              chunk, re.I)
                if m:
                    out[key_col] = m.group(1)
            type_col = _field_by_norm(keys, "type", "类型")
            if type_col:
                m = re.search(r"类型\s*(?:用|=|为)?\s*([A-Za-z0-9_\u4e00-\u9fff]+)",
                              chunk)
                if m:
                    out[type_col] = m.group(1).strip(" ，,；;。")
            value_col = _field_by_norm(keys, "value", "原文")
            if value_col:
                m = re.search(r"原文\s*['\"]([^'\"]+)['\"]", chunk)
                out[value_col] = (m.group(1).strip() if m else _first_quote(chunk))

            option_id_col = _field_by_norm(keys, "optionid", "选项编号")
            if option_id_col:
                m = re.search(r"选项编号\s*(\d+)", chunk)
                if m:
                    out[option_id_col] = int(m.group(1))
            option_text_col = _field_by_norm(keys, "optiontext", "选项内容")
            if option_text_col:
                m = re.search(r"(?:文字|选项内容)\s*['\"]([^'\"]+)['\"]", chunk)
                out[option_text_col] = (m.group(1).strip() if m else _first_quote(chunk))
            func_col = _field_by_norm(keys, "functiontype", "功能类型")
            if func_col:
                m = re.search(r"功能类型\s*(\d+)", chunk)
                if m:
                    out[func_col] = int(m.group(1))
            conv_col = ""
            for k in keys:
                kn = self._field_name_norm(k)
                if "convid" in kn and "optionid" not in kn:
                    conv_col = k
                    break
            if conv_col:
                m = re.search(r"(?:跳到|跳转到|对话)\s*(\d+)", chunk)
                if m:
                    out[conv_col] = int(m.group(1))
            return {k: v for k, v in out.items() if k and v is not None and str(v).strip() != ""}

        chunks = _chunks()
        if len(chunks) < 2:
            return 0
        import copy
        n = 0
        groups: dict[tuple[str, str], list[NLIntent]] = {}
        for it in intents:
            if getattr(it, "action", "") != "add":
                continue
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict) or not fields:
                continue
            groups.setdefault((
                (getattr(it, "table_hint", "") or "").lower(),
                (getattr(it, "sheet_hint", "") or "").lower(),
            ), []).append(it)

        for rows in groups.values():
            exemplar = rows[0]
            keys = [str(k) for row in rows
                    for k in ((getattr(row, "extras", None) or {}).get("fields") or {}).keys()]
            keys = list(dict.fromkeys(keys))
            if not keys:
                continue
            has_batch_shape = (
                bool(_field_by_norm(keys, "key", "程序引用关键字") and _field_by_norm(keys, "value", "原文"))
                or bool(_field_by_norm(keys, "optionid", "选项编号") and _field_by_norm(keys, "optiontext", "选项内容"))
            )
            if not has_batch_shape:
                continue
            parsed = [_extract(keys, chunk) for chunk in chunks]
            parsed = [p for p in parsed if p]
            if len(parsed) <= len(rows):
                continue
            existing = {
                tuple(sorted((str(k), repr(v)) for k, v in (((getattr(row, "extras", None) or {}).get("fields") or {}).items())))
                for row in rows
            }
            for fields in parsed:
                sig = tuple(sorted((str(k), repr(v)) for k, v in fields.items()))
                if sig in existing:
                    continue
                clone = copy.deepcopy(exemplar)
                clone.extras["fields"] = fields
                clone.produces_label = None
                clone.consumes_labels = []
                clone.extras.pop("produces", None)
                clone.extras.pop("consumes", None)
                intents.append(clone)
                existing.add(sig)
                n += 1
        return n

    def _supplement_option_rows_from_text(self, intents: list[NLIntent], text: str) -> int:
        if not intents or not text or not self._cli:
            return 0
        chunks = [
            m.group(1).strip(" ，,；;。")
            for m in re.finditer(r"(选项编号\s*\d+.*?)(?=选项编号\s*\d+|$)", text, re.S)
        ]
        if not chunks:
            return 0

        def _quote(s: str) -> str:
            m = re.search(r"['\"]([^'\"]+)['\"]", s)
            return m.group(1).strip() if m else ""

        def _col_by_norm(cols: list[str], *needles: str) -> str:
            for col in cols:
                norm = self._field_name_norm(col)
                if any(n in norm for n in needles):
                    return col
            return ""

        stems = {
            (getattr(it, "table_hint", "") or "").lower()
            for it in intents
            if getattr(it, "action", "") == "add" and getattr(it, "table_hint", "")
        }
        existing = {
            ((getattr(it, "table_hint", "") or "").lower(),
             (getattr(it, "sheet_hint", "") or "").lower(),
             tuple(sorted((str(k), repr(v)) for k, v in (((getattr(it, "extras", None) or {}).get("fields") or {}).items()))))
            for it in intents
        }
        n = 0
        created_ids_by_stem: dict[str, list[int]] = {}
        try:
            tables = {p.stem.lower(): p for p in self._cli.list_tables()}
        except Exception:
            tables = {}
        for stem in stems:
            path = tables.get(stem)
            if path is None:
                continue
            try:
                sheets = [s for s in self._cli.get_sheets(path)
                          if s and "说明" not in s and "CONFIG" not in s.upper()]
            except Exception:
                sheets = []
            for sheet in sheets:
                headers = self._headers_for(stem, sheet)
                types = self._type_row_for(stem, sheet)
                cols = []
                for h, t in zip(headers, types):
                    raw_type = str(t or "").split(":", 1)[0].strip()
                    cols.append(raw_type or str(h or "").strip())
                option_id_col = _col_by_norm(cols, "optionid", "选项编号")
                option_text_col = _col_by_norm(cols, "optiontext", "选项内容")
                if not option_id_col or not option_text_col:
                    continue
                func_col = _col_by_norm(cols, "functiontype", "功能类型")
                conv_col = ""
                for col in cols:
                    norm = self._field_name_norm(col)
                    if "convid" in norm and "optionid" not in norm:
                        conv_col = col
                        break
                for chunk in chunks:
                    m_id = re.search(r"选项编号\s*(\d+)", chunk)
                    if not m_id:
                        continue
                    fields = {
                        option_id_col: int(m_id.group(1)),
                        option_text_col: _quote(chunk),
                    }
                    if func_col:
                        m_func = re.search(r"功能类型\s*(\d+)", chunk)
                        if m_func:
                            fields[func_col] = int(m_func.group(1))
                    if conv_col:
                        m_conv = re.search(r"(?:跳到|跳转到|对话)\s*(\d+)", chunk)
                        if m_conv:
                            fields[conv_col] = int(m_conv.group(1))
                    sig = (stem, sheet.lower(),
                           tuple(sorted((str(k), repr(v)) for k, v in fields.items())))
                    if sig in existing:
                        continue
                    intents.append(NLIntent(
                        action="add",
                        table_hint=stem,
                        sheet_hint=sheet,
                        raw=chunk,
                        extras={"fields": fields, "source": "enumerated_option_fallback"},
                        source="enumerated_option_fallback",
                    ))
                    existing.add(sig)
                    created_ids_by_stem.setdefault(stem, []).append(int(m_id.group(1)))
                    n += 1

        for it in intents:
            stem = (getattr(it, "table_hint", "") or "").lower()
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not stem or not isinstance(fields, dict):
                continue
            option_id = None
            for key, value in fields.items():
                if "optionid" not in self._field_name_norm(key):
                    continue
                try:
                    option_id = int(str(value).strip())
                except ValueError:
                    option_id = None
                break
            if option_id is not None and option_id not in created_ids_by_stem.setdefault(stem, []):
                created_ids_by_stem[stem].append(option_id)

        if created_ids_by_stem:
            for it in intents:
                stem = (getattr(it, "table_hint", "") or "").lower()
                ids = sorted(created_ids_by_stem.get(stem) or [])
                if not ids:
                    continue
                fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for idx, oid in enumerate(ids):
                    for key in (f"options[{idx}]", f"选项{idx + 1}"):
                        if key in fields and str(fields.get(key) or "").strip():
                            break
                    else:
                        headers = self._headers_for(stem, getattr(it, "sheet_hint", "") or "")
                        types = self._type_row_for(stem, getattr(it, "sheet_hint", "") or "")
                        for h, t in zip(headers, types):
                            raw_type = str(t or "").split(":", 1)[0].strip()
                            col = raw_type or str(h or "").strip()
                            if self._field_name_norm(col) == self._field_name_norm(f"options[{idx}]"):
                                fields[col] = oid
                                n += 1
                                break
        return n

    @staticmethod
    def _compact_indexed_field_gaps(intents: list[NLIntent]) -> int:
        n = 0
        idx_re = re.compile(r"^(?P<base>.+)\[(?P<idx>\d+)\]$")
        for it in intents or []:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            by_base: dict[str, list[tuple[str, int]]] = {}
            for key in fields.keys():
                m = idx_re.match(str(key))
                if not m:
                    continue
                by_base.setdefault(m.group("base"), []).append((str(key), int(m.group("idx"))))
            for base, items in by_base.items():
                present = {idx for _, idx in items}
                if 0 in present or len(items) != 1:
                    continue
                old_key, _old_idx = items[0]
                new_key = f"{base}[0]"
                if new_key in fields:
                    continue
                fields[new_key] = fields.pop(old_key)
                n += 1
        return n

    @staticmethod
    def _placeholder_family(label: str) -> str:
        s = str(label or "").strip().strip("<>").strip().lower()
        s = re.sub(r"_(\d+)(?=_id$)", "", s)
        s = re.sub(r"_(\d+)$", "", s)
        return s

    @staticmethod
    def _placeholder_ordinal(label: str) -> int | None:
        s = str(label or "").strip().strip("<>").strip().lower()
        m = (re.search(r"_(\d+)(?=_id$)", s)
             or re.search(r"_(\d+)$", s)
             or re.search(r"(\d+)(?=_id$)", s))
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def _align_placeholder_families(self, intents: list[NLIntent]) -> int:
        n = 0
        for it in intents or []:
            label = getattr(it, "produces_label", None)
            if not label and getattr(it, "extras", None):
                label = it.extras.get("produces")
            if not label:
                continue
            label_s = str(label).strip()
            label_family = self._placeholder_family(label_s)
            pk_names = self._primary_field_names(
                getattr(it, "table_hint", "") or "",
                getattr(it, "sheet_hint", "") or "")
            touched_own_pk = False
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if isinstance(fields, dict):
                for key, value in list(fields.items()):
                    if pk_names and self._field_name_norm(key) not in pk_names:
                        continue
                    if not isinstance(value, str):
                        continue
                    raw = value.strip()
                    if not (raw.startswith("<") and raw.endswith(">")):
                        continue
                    old = raw.strip("<>").strip()
                    if old != label_s and self._placeholder_family(old) == label_family:
                        fields[key] = f"<{label_s}>"
                        touched_own_pk = True
                        n += 1
                    elif old == label_s:
                        touched_own_pk = True
            if not touched_own_pk:
                continue
            old_cons = list(getattr(it, "consumes_labels", None) or [])
            new_cons = [
                x for x in old_cons
                if self._placeholder_family(x) != label_family
            ]
            if len(new_cons) != len(old_cons):
                it.consumes_labels = new_cons
                n += len(old_cons) - len(new_cons)
        return n

    def _primary_field_names(self, stem: str, sheet: str) -> set[str]:
        headers = self._headers_for(stem, sheet) or []
        types = self._type_row_for(stem, sheet) or []
        names: set[str] = set()
        if headers:
            names.add(self._field_name_norm(headers[0]))
        if types:
            first = str(types[0] or "").split(":", 1)[0].strip()
            if first:
                names.add(self._field_name_norm(first))
        names.discard("")
        return names

    @staticmethod
    def _ordered_numeric_sequences(text: str) -> list[list[int]]:
        if not text:
            return []
        seqs: list[list[int]] = []
        for m in re.finditer(r"(?:按顺序|分别|依次)[^。；;，,]{0,40}", text):
            nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
            if len(nums) >= 2:
                seqs.append(nums)
        return seqs

    @staticmethod
    def _apply_ordered_sequence_values(target_fields: dict, exemplar_fields: dict,
                                       sequences: list[list[int]],
                                       pos: int, total: int) -> None:
        if pos <= 0 or not sequences:
            return
        for seq in sequences:
            if len(seq) < total:
                continue
            first = str(seq[0])
            for key, old_value in exemplar_fields.items():
                if str(old_value).strip() != first:
                    continue
                target_fields[key] = seq[pos]

    @staticmethod
    def _dedupe_nl_intents(intents: list[NLIntent]) -> list[NLIntent]:
        """Remove exact or near-duplicate Step1 intents from retry/backfill merges."""
        out: list[NLIntent] = []
        seen: set[tuple] = set()
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if isinstance(fields, dict):
                field_sig = tuple(sorted((str(k), repr(v)) for k, v in fields.items()))
            else:
                field_sig = repr(fields)
            key = (
                getattr(it, "action", None),
                getattr(it, "table_hint", None),
                getattr(it, "sheet_hint", None),
                getattr(it, "locator_field", None),
                repr(getattr(it, "locator_value", None)),
                tuple(getattr(it, "locator_fields", None) or []),
                tuple(repr(v) for v in (getattr(it, "locator_values", None) or [])),
                field_sig,
                getattr(it, "produces_label", None),
            )
            if key in seen:
                continue
            seen.add(key)
            dup_idx = next(
                (idx for idx, old in enumerate(out)
                 if ParseAgent._looks_semantic_duplicate(old, it)),
                None,
            )
            if dup_idx is not None:
                old = out[dup_idx]
                if ParseAgent._semantic_dup_score(it) > ParseAgent._semantic_dup_score(old):
                    out[dup_idx] = it
                continue
            out.append(it)
        out = ParseAgent._drop_empty_add_shadows(out)
        return out

    @staticmethod
    def _drop_empty_add_shadows(intents: list[NLIntent]) -> list[NLIntent]:
        non_empty_groups: set[tuple] = set()
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            if any(str(v).strip() for v in fields.values()):
                non_empty_groups.add((
                    getattr(it, "action", None),
                    (getattr(it, "table_hint", None) or "").lower(),
                    (getattr(it, "sheet_hint", None) or "").lower(),
                ))
        out: list[NLIntent] = []
        for it in intents:
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            group = (
                getattr(it, "action", None),
                (getattr(it, "table_hint", None) or "").lower(),
                (getattr(it, "sheet_hint", None) or "").lower(),
            )
            if (getattr(it, "action", None) == "add"
                    and group in non_empty_groups
                    and isinstance(fields, dict)
                    and fields
                    and not any(str(v).strip() for v in fields.values())):
                continue
            if (getattr(it, "action", None) == "add"
                    and group in non_empty_groups
                    and isinstance(fields, dict)
                    and ParseAgent._is_sparse_shadow(it, intents)):
                continue
            out.append(it)
        return out

    @staticmethod
    def _is_sparse_shadow(it: NLIntent, intents: list[NLIntent]) -> bool:
        fields = (getattr(it, "extras", None) or {}).get("fields") or {}
        if not isinstance(fields, dict):
            return False
        vals = {repr(v) for v in fields.values()
                if str(v).strip() and not str(v).strip().startswith("<")}
        if not vals:
            return False
        group = (
            getattr(it, "action", None),
            (getattr(it, "table_hint", None) or "").lower(),
            (getattr(it, "sheet_hint", None) or "").lower(),
        )
        for other in intents:
            if other is it:
                continue
            other_group = (
                getattr(other, "action", None),
                (getattr(other, "table_hint", None) or "").lower(),
                (getattr(other, "sheet_hint", None) or "").lower(),
            )
            if other_group != group:
                continue
            ofields = (getattr(other, "extras", None) or {}).get("fields") or {}
            if not isinstance(ofields, dict):
                continue
            ovals = {repr(v) for v in ofields.values()
                     if str(v).strip() and not str(v).strip().startswith("<")}
            if vals < ovals and len(ovals) >= len(vals) + 2:
                return True
        return False

    @staticmethod
    def _semantic_dup_score(it: NLIntent) -> tuple[int, int, int]:
        fields = (getattr(it, "extras", None) or {}).get("fields") or {}
        field_count = len(fields) if isinstance(fields, dict) else 0
        ph_count = 0
        if isinstance(fields, dict):
            ph_count = sum(1 for v in fields.values()
                           if isinstance(v, str) and v.strip().startswith("<"))
        has_produces = 1 if getattr(it, "produces_label", None) else 0
        has_consumes = 1 if getattr(it, "consumes_labels", None) else 0
        # Prefer richer field maps; prefer produces only when placeholders/FK use exists.
        prod_score = has_produces if ph_count or has_consumes else -has_produces
        return (field_count, ph_count + has_consumes, prod_score)

    @staticmethod
    def _looks_semantic_duplicate(a: NLIntent, b: NLIntent) -> bool:
        base_a = (
            getattr(a, "action", None),
            (getattr(a, "table_hint", None) or "").lower(),
            (getattr(a, "sheet_hint", None) or "").lower(),
            getattr(a, "locator_field", None),
            repr(getattr(a, "locator_value", None)),
            tuple(getattr(a, "locator_fields", None) or []),
            tuple(repr(v) for v in (getattr(a, "locator_values", None) or [])),
        )
        base_b = (
            getattr(b, "action", None),
            (getattr(b, "table_hint", None) or "").lower(),
            (getattr(b, "sheet_hint", None) or "").lower(),
            getattr(b, "locator_field", None),
            repr(getattr(b, "locator_value", None)),
            tuple(getattr(b, "locator_fields", None) or []),
            tuple(repr(v) for v in (getattr(b, "locator_values", None) or [])),
        )
        if base_a != base_b:
            return False
        fa = (getattr(a, "extras", None) or {}).get("fields") or {}
        fb = (getattr(b, "extras", None) or {}).get("fields") or {}
        if not isinstance(fa, dict) or not isinstance(fb, dict):
            return False
        vals_a = {repr(v) for v in fa.values()
                  if str(v).strip() and not str(v).strip().startswith("<")}
        vals_b = {repr(v) for v in fb.values()
                  if str(v).strip() and not str(v).strip().startswith("<")}
        if len(vals_a) < 2 or len(vals_b) < 2:
            return False
        overlap = len(vals_a & vals_b)
        return overlap >= 2 and overlap * 2 >= min(len(vals_a), len(vals_b))

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
        raw_fields = getattr(si, "fields", None) or {}
        fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
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
                for h in (_ce.hits if _ce else [])
            ]
        consumes_labels: list[str] = []
        seen_consumes: set[str] = set()

        def _collect_consumes(value) -> None:
            if isinstance(value, str):
                for m in re.finditer(r"<\s*([^>]+?)\s*>", value):
                    label = m.group(1).strip()
                    if label and label.lower() != "auto" and label not in seen_consumes:
                        seen_consumes.add(label)
                        consumes_labels.append(label)
            elif isinstance(value, dict):
                for nested in value.values():
                    _collect_consumes(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    _collect_consumes(nested)

        _collect_consumes(fields)

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
            consumes_labels=consumes_labels,
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
