"""定位 SubAgent:全输入走,产候选表 + sheet + 跨 sheet FK 链。

替代原 detect_cross_table_action 关键词闸门 + table_locator 单表定位 +
_llm_chain_decompose 内联候选收集三处散落逻辑。

职责边界(与 DecomposeAgent/ValidatorAgent 严格分层):
  - LocatorAgent: 输入 text → 输出候选表(stem/sheet/conf) + FK 边
  - DecomposeAgent: 消费 Locator 输出 + schema → 产 SplitIntent[]
  - ValidatorAgent: 消费 SplitIntent[] → 校验引用闭环 + 修正

全输入走设计(原则11):
  关键词闸门有覆盖盲区(新链型漏检),全部输入交 LocatorAgent 探候选表,
  ≥2 表或含 FK 链时触发 DecomposeAgent 跨表链分解;<2 走单表路径。
  无关键词依赖 → 真正泛化。

内部主路径用 TableLocator 5 级递进定位(规则),歧义时 LLM 裁决。
FK 边用 RelationGraph 声明式数据(table_relations.json)。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .base import SubAgent
from .llm_agent import LLMSubAgent

logger = logging.getLogger(__name__)


@dataclass
class CandidateTable:
    """单候选表定位结果。

    Attributes:
        stem: 表 stem(如 pet)
        sheet: 命中 sheet(如 Pet),空表示仅文件级命中
        confidence: 置信度 [0,1]
        level: 命中级别标签(透传 TableLocator 的 level)
        matched_term: 实际匹配到的字符串
    """
    stem: str
    sheet: str = ""
    confidence: float = 0.0
    level: str = ""
    matched_term: str = ""


@dataclass
class FKEdge:
    """单条外键边(供 DecomposeAgent 决定 produces/consumes)。

    Attributes:
        from_stem: 源表 stem(consumer)
        from_sheet: 源 sheet
        from_column: 源表 FK 列名
        to_stem: 目标表 stem(producer)
        to_sheet: 目标 sheet
        to_column: 目标被引用列名
    """
    from_stem: str
    from_sheet: str
    from_column: str
    to_stem: str
    to_sheet: str
    to_column: str


@dataclass
class LocatorResult:
    """LocatorAgent 整体输出。

    Attributes:
        candidates: 候选表列表(按置信度降序)
        fk_edges: 候选表之间的 FK 边(仅含两端均在 candidates 内的边)
        ambiguous: 是否歧义(同档多匹配,需上层确认)
        column_signal: 列名提取信号（Step1 前置 ColumnExtractor 产出），供
            DecomposeAgent 注入 LLM prompt 参考。None 表示未做列名提取。
            信号含：extracted_terms（列名 token）、candidate_stems（列名反查
            命中的表）、hits（每条命中含 column/stem/sheet/score）。
    """
    candidates: list[CandidateTable] = field(default_factory=list)
    fk_edges: list[FKEdge] = field(default_factory=list)
    ambiguous: bool = False
    column_signal: Any = None

    @property
    def is_cross_table(self) -> bool:
        """是否触发跨表链路径:≥2 规则候选表 或 规则候选间含 FK 边。

        column_extract 补的候选（confidence=0.70，仅作 LLM 参考池扩容，非规则
        决策）不计入——否则单表查询（如「查询灵兽饕餮」）被列名反查命中的
        pet_evolve 等参考表撑到 ≥2 候选或抽到 FK 边，误判跨表走 DecomposeAgent
        而非单表路径。
        """
        _rule_cands = [c for c in self.candidates
                       if getattr(c, "level", "") not in ("column_extract", "substring")]
        _rule_stems = {c.stem for c in _rule_cands}
        _rule_fk = [e for e in (self.fk_edges or [])
                    if getattr(e, "from_stem", "") in _rule_stems
                    and getattr(e, "to_stem", "") in _rule_stems]
        return len(_rule_cands) >= 2 or bool(_rule_fk)


def _stem_of_path(p: str) -> str:
    """relation path → stem:'pet/pet.xlsx' → 'pet'。"""
    s = str(p).replace("\\", "/").rstrip("/")
    if s.endswith(".xlsx"):
        s = s[:-5]
    return s.rsplit("/", 1)[-1]


_IDX_SUFFIX_RE = re.compile(r"\[\d+\]$")


def _base_col(col: str) -> str:
    """列名规范化：'school_ability_id[0]:int' → 'school_ability_id'；
    '神通id' → '神通id'。剥类型冒号、数组下标、括号注释、换行。"""
    s = str(col or "").split(":", 1)[0].strip()
    s = _IDX_SUFFIX_RE.sub("", s)
    s = s.split("（", 1)[0].split("(", 1)[0].strip()
    return s


def _ref_base(base: str) -> str:
    """列名 → 被引表名：'school_id'→'school'，'spirit_id'→'spirit'，
    '物品编号'→'物品'，'talent_id'→'talent'。非引用列返空。"""
    b = str(base or "").rsplit(".", 1)[-1]
    if not b:
        return ""
    if b.endswith("_id") and len(b) > 3:
        return b[:-3]
    if b.endswith("编号") and len(b) > 2:
        return b[:-2]
    if b.endswith("id") and len(b) > 2:
        return b[:-2]
    if b.endswith("ID") and len(b) > 2:
        return b[:-2]
    return ""


def _is_id_col(base: str) -> bool:
    """列名是否 id 类（主键候选）：含 id/ID/编号。"""
    b = base or ""
    return ("id" in b.lower()) or ("编号" in b)


class LocatorAgent(LLMSubAgent):
    """定位 Agent:全输入探候选表 + FK 链。

    流程:
      1. TableLocator 5 级递进(规则主路径)产候选
      2. 歧义时 LLM 裁决(复用 _call_llm)
      3. RelationGraph 抽候选表间 FK 边
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None,
                 locator=None, relation_graph=None):
        super().__init__("LocatorAgent", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="定位候选表 + FK 链")
        self._cli = cli
        self._locator = locator
        self._relation_graph = relation_graph
        # §Step1 列名信号:内置 ColumnExtractor,locate 时先跑列名反查,
        # 把命中的表补进 candidates(给 LLM 候选池扩容,非规则决策)。
        # DecomposeAgent 仍由 LLM 在 candidates 内选表。修案例2 fabao 未进候选
        # 致 _filter_intents 误杀;案例3 activity 未进候选致 LLM 被迫选错。
        self._column_extractor = None
        try:
            from .column_extractor import ColumnExtractor
            self._column_extractor = ColumnExtractor()
        except Exception:
            self._column_extractor = None

    def locate(self, text: str) -> LocatorResult:
        """主入口:text → LocatorResult。

        失败返回空 LocatorResult(上层降级走单表路径)。

        §Step1 列名信号补候选:规则主路径产候选后,跑 ColumnExtractor 反查列名命中表,
        以 confidence=0.70(level=column_extract)补进 candidates。这是候选池扩容,
        不是规则决策——DecomposeAgent 仍由 LLM 在 candidates 内选表。修案例2 fabao
        未进候选致 _filter_intents 硬过滤删掉 LLM 正确产出。
        """
        if not text or not text.strip():
            return LocatorResult()
        self.add_thinking("定位", f"LocatorAgent 开始探测候选表")
        complex_input = self._is_complex_input(text)
        # 0. §列名信号前置(0 LLM):提取列名 token → 反向索引反查候选表,
        #    供步骤1b 补进 candidates,并挂到 result.column_signal 供 DecomposeAgent prompt 参考
        column_signal = None
        if self._column_extractor is not None:
            try:
                column_signal = self._column_extractor.extract(text)
                if column_signal and column_signal.has_signal:
                    self.add_thinking("定位",
                        f"ColumnExtractor 提取列名 {len(column_signal.extracted_terms)} 个"
                        f"，反查候选表 {len(column_signal.candidate_stems)} 个"
                        f"（{','.join(column_signal.candidate_stems[:5])}），补进候选池供 LLM 参考")
            except Exception:
                logger.warning("LocatorAgent ColumnExtractor 失败", exc_info=True)
                column_signal = None
        # 1. 规则主路径:TableLocator 5 级
        outcome = self._rule_locate(text)
        candidates: list[CandidateTable] = []
        ambiguous = False
        if outcome and outcome.best:
            for r in self._merge_candidates(outcome):
                candidates.append(CandidateTable(
                    stem=r.stem, sheet=r.sheet or "",
                    confidence=r.confidence, level=r.level,
                    matched_term=r.matched_term,
                ))
            ambiguous = outcome.is_ambiguous
        # 1a. §列名信号补候选:ColumnExtractor 反查命中的表补进 candidates。
        #     过滤门槛(防候选池膨胀致并发 LLM 爆炸——案例2补了9个噪声表触发10次LLM):
        #     - 多列命中表:优先补(如 fabao 命中"法宝描述"+"名称"2列)
        #     - 单列命中且命中列是专有列(非通用列):补(如 activity 命中"活动类型")
        #     - 单列命中且命中列是通用列(名称/描述/类型/id):不补(噪声——44表含"名称")
        #     - 数量上限 topK=4,按命中列数降序+得分降序取前K
        #     confidence=0.70,让 DecomposeAgent LLM 能在候选内选到(修 fabao 被幻觉过滤)
        _GENERIC_COLS = {"名称", "描述", "类型", "id", "ID", "编号", "备注", "说明"}
        if column_signal and column_signal.has_signal:
            existing = {c.stem for c in candidates}
            # 按 stem 聚合命中,统计专有列命中数
            stem_agg: dict[str, list] = {}
            for h in column_signal.hits:
                stem_agg.setdefault(h.stem, []).append(h)
            # §数据驱动歧义列抑制：某列在命中集里映射到 >=_AMBIG_K 张不同表
            # （如 model_id 命中 assistant/city/guild），说明它是跨表共享列（非判别性），
            # 不能凭它单独把这些表补进候选——否则噪声表(guild/assistant)在 _cand_cap 下
            # 挤掉真正的动作主语表(combat/pve_combat_npc/spawn)。通用判据（列的跨表频次），
            # 不绑业务词/表，是 _GENERIC_COLS 硬编码集合的数据驱动泛化。
            _col_stem_freq: dict[str, set] = {}
            for h in column_signal.hits:
                _col_stem_freq.setdefault(h.column, set()).add(h.stem)
            _AMBIG_K = 3
            _ambig_cols = {c for c, ss in _col_stem_freq.items() if len(ss) >= _AMBIG_K}
            # 新增：substring 确定性命中表（判别列直接子串命中）优先补，高置信必留。
            # 这是"文本原样出现列名"的强证据，不参与 topK=4 截断，防 pve_combat_npc
            # 等靠判别列命中但被其他 column_extract 表挤出的漏表。
            # 专有列判据：substring 补候选要求该 stem 至少 1 个命中列是专有列
            # （该列名在 hits 里只映射 1 个 stem）。跨表共享列（"技能列表"命中
            # pve_combat_npc+spell_group、"属性修改"命中 item+spell）不作单独证据，
            # 防 spell/spell_group 等同名列表被误补进候选。
            _sub_hits = [h for h in column_signal.hits
                         if getattr(h, "source", "") == "substring"]
            if _sub_hits:
                _sub_agg: dict[str, list] = {}
                _sub_col_freq: dict[str, set] = {}
                for h in _sub_hits:
                    _sub_agg.setdefault(h.stem, []).append(h)
                    _sub_col_freq.setdefault(h.column, set()).add(h.stem)
                for _s_stem, _s_hs in _sub_agg.items():
                    if _s_stem in existing:
                        continue
                    _disc = [h for h in _s_hs
                             if h.column not in _GENERIC_COLS
                             and len(_sub_col_freq.get(h.column, set())) == 1]
                    if not _disc:
                        continue
                    _best_h = max(_disc, key=lambda h: h.score)
                    candidates.append(CandidateTable(
                        stem=_s_stem, sheet=_best_h.sheet,
                        confidence=0.85, level="substring",
                        matched_term=_best_h.column,
                    ))
                    existing.add(_s_stem)
            # 打分:专有列命中数为主,得分次之
            scored: list[tuple[int, float, str]] = []  # (专有列命中数, 最高分, stem)
            for stem, hs in stem_agg.items():
                if stem in existing:
                    continue
                # 专有列 = 命中列里非通用列 且 非跨表歧义列的（判别性列）
                specific = [h for h in hs
                            if h.column not in _GENERIC_COLS
                            and h.column not in _ambig_cols]
                if not specific:
                    continue  # 全是通用列/歧义共享列命中,不补(噪声)
                best = max(h.score for h in specific)
                scored.append((len(specific), best, stem))
            scored.sort(key=lambda x: (-x[0], -x[1]))
            for n_spec, best_score, stem in scored[:4]:
                stem_hits = stem_agg[stem]
                best_h = max(stem_hits, key=lambda h: h.score)
                candidates.append(CandidateTable(
                    stem=stem, sheet=best_h.sheet,
                    confidence=0.70, level="column_extract",
                    matched_term=best_h.column,
                ))
                existing.add(stem)
        # 1a-2. §泛化收紧:复杂输入下,若某 column_extract 候选命中该表的列里
        # FK 引用列占多数(如 model_id/space编号/坐标 出现在 guild/space/combat,
        # 但这些只是该段提到的"参数",不是该段的动作主语),降权到 0.50 让 _cand_cap
        # 把它挤掉,把名额留给动作主路由表 + 其 FK 邻接(如对话树叙述里提 model_id/
        # space_id/坐标,但动作主语是"建对话"→ interaction)。
        # 通用规则(FK 列占多数 = 附加上下文),不绑业务关键词。
        if complex_input:
            for c in candidates:
                if c.level != "column_extract":
                    continue
                stem_hits = stem_agg.get(c.stem, [])
                if not stem_hits:
                    continue
                spec_cols = [h.column for h in stem_hits
                             if h.column not in _GENERIC_COLS]
                if not spec_cols:
                    continue
                fk_cnt = sum(1 for col in spec_cols
                             if self._is_fk_reference_column(c.stem, c.sheet, col))
                if fk_cnt * 2 >= len(spec_cols):  # FK 占多数 → 附加上下文
                    c.confidence = 0.50
        # 1b-2. §P0 spawn/entity 语义探测（须在 _expand_by_fk 之前：FK 扩表会先把
        # spawn_quest_entity/entity_prefab 以 0.5/0.4 低置信度加进候选，导致本探测器
        # 的"已存在"检查跳过、0.8 高置信度写不进去，cap 裁剪时被当弱命中挤出）。
        # 任务链里"刷XX""放在space_id 坐标""新建一位点击后展开对话"等 spawn/实体语义，
        # alias 层没有对应词（"实体/NPC"别名不覆盖"长老/叛徒"，spawn 表无别名），
        # 且 FK 扩表只从"已在候选内"的表出发——entity_prefab 不在候选 → spawn 表
        # 永远补不进来（school_quest_chain case0 因此漏 3 张链核心表）。通用语义词
        # 补候选，confidence=0.8 稳过强命中档（≥0.80 必留）。纯召回，LLM 仍据
        # schema 决定是否产 intent。
        def _add_if_missing(stem: str, conf: float, level: str, matched: str) -> None:
            if not any(c.stem == stem for c in candidates):
                candidates.append(CandidateTable(
                    stem=stem, confidence=conf, level=level, matched_term=matched))
        if re.search(r'刷|刷新', text) and re.search(r'坐标|放在|space_id', text):
            _add_if_missing("spawn_world_entity", 0.8, "spawn_semantic", "刷新+坐标")
        if '任务' in text and re.search(r'刷|刷新|prefab', text):
            _add_if_missing("spawn_quest_entity", 0.8, "spawn_semantic", "任务刷新")
        if re.search(r'新建一位|点击后展开|点击后弹出', text):
            _add_if_missing("entity_prefab", 0.8, "entity_semantic", "新建实体")
        # 1b. FK 关系驱动补表(relation graph 任一端 stem 在候选内则补对端)。
        #     把 alias 未直接命中但语义相关的表(如 interaction/spawn_world_entity)
        #     纳入候选,而不用 locate_all 全量列名匹配(会引入 40+ 噪声表膨胀 DecomposeAgent 上下文)。
        #     触发条件：complex_input，或规则级候选 ≥2（与 is_cross_table 同口径，
        #     排除 column_extract/substring 噪声——否则「查询灵兽饕餮」单表查询会被
        #     pet_refine/pet_evolve 等列名噪声表撑到 ≥2 误触扩表变跨表）。
        _rule_cands = [c for c in candidates
                       if getattr(c, "level", "") not in ("column_extract", "substring")]
        if complex_input or len(_rule_cands) >= 2:
            for c in self._expand_by_fk(candidates, complex_input):
                if not any(x.stem == c.stem for x in candidates):
                    candidates.append(c)
        # §P0-3 候选池总量上限：复杂多指令 + 规则ambiguous全收 + 列名补 + FK扩表
        # 可叠到 50+ 候选 → 并发主路径每表一次 LLM → 50 次 LLM 爆炸。
        # cap 默认 8（env 可调），按置信度降序保留，优先规则命中(高conf) + 列名专有列命中。
        # 保留策略：① conf>=0.80(规则强命中)全留 ② 其余按conf降序取到cap ③ 同conf保列名命中
        _cand_cap = max(4, int(os.environ.get("CODEMAKER_LOCATOR_MAX_CANDIDATES", "8")))
        # §框架级 A（保召回）：复杂跨表输入合法地涉及更多表（本例 reward+combat+
        # pve_combat_npc+entity_prefab+spawn+interaction+activity+item ≥8），固定 cap=8
        # 会让判别性 column_extract 候选（如 pve_combat_npc 靠"技能列表/等级公式"命中）
        # 被 8 张 alias 强命中表挤出 → 真正的动作主语表缺失、子任务丢失。单 prompt 路径
        # 下候选多只是 prompt 变长（非多次 LLM），复杂输入放宽 cap 以保召回。通用判据
        # （输入复杂度），不绑业务词/表。
        if complex_input:
            _cand_cap = max(_cand_cap, 12)
        if len(candidates) > _cand_cap:
            # 分档：强命中(>=0.80)必留，弱命中按conf降序补到cap
            strong = [c for c in candidates if c.confidence >= 0.80]
            weak = sorted([c for c in candidates if c.confidence < 0.80],
                          key=lambda c: (c.confidence,
                                         c.level == "column_extract"),
                          reverse=True)
            candidates = strong + weak[:max(0, _cand_cap - len(strong))]
            self.add_thinking("定位",
                f"候选池超上限({len(candidates)+len(weak[:0])}→{_cand_cap})，"
                f"按置信度裁剪保留 {len(candidates)} 个")
        # 2. LLM 裁决:歧义或无命中时(复杂输入保留多候选交 DecomposeAgent,不走收敛)
        # §Step1 定位歧义修复：原逻辑复杂输入一律跳过 LLM 收敛——但 _is_complex_input
        # 的判定只靠对话/选项/支线/采集/多id 等"内容形态"关键词，覆盖不了"表名相似/
        # 别名冲突"这类纯定位歧义（如 input 提到「怪物 spawn 表」时 spawn_world_entity
        # 与 spawn_* 多表 alias 强命中，同档多候选 ambiguous=True，但无对话/选项关键词
        # → 误判为非复杂输入 → 走 LLM 收敛分支，但 _llm_resolve 只返回单 stem 把候选
        # 收敛到单表 → 跨表链 is_cross_table=False → 漏触发 DecomposeAgent → 跨表拆分
        # 变单表退化）。现补判据：ambiguous 歧义若伴有 FK 边（候选间有跨表引用关系，
        # 说明是跨表链而非纯噪声歧义），不收敛，保留多候选走 DecomposeAgent 让 LLM 在
        # 拆分阶段（schema+列名信号更全）而非定位阶段做表选择，更不易选错。
        _ambiguous_with_fk = False
        if ambiguous:
            pre_fk_edges = self._collect_fk_edges(candidates)
            _ambiguous_with_fk = len(pre_fk_edges) > 0
            if _ambiguous_with_fk:
                self.add_thinking("定位",
                    f"歧义候选含 {len(pre_fk_edges)} 条 FK 边，判定为跨表链非纯噪声"
                    f"歧义，保留 {len(candidates)} 候选交 DecomposeAgent 拆分阶段做表"
                    f"选择，不在定位层 LLM 收敛单表（防跨表链被短路成单表）")
        if (not complex_input and not _ambiguous_with_fk
                and (ambiguous or not candidates) and self.parser):
            llm_stem = self._llm_resolve(text,
                [c.stem for c in candidates] if candidates else None)
            if llm_stem:
                # LLM 裁定的 stem 若不在候选内,补一个低置信度候选
                if not any(c.stem == llm_stem for c in candidates):
                    candidates.append(CandidateTable(
                        stem=llm_stem, confidence=0.5,
                        level="llm_inferred", matched_term=llm_stem,
                    ))
                # §列名信号保护:LLM 收敛时不要丢掉 column_extract 候选
                # (它们是列名反查的高置信命中,应保留供 DecomposeAgent 参考)。
                # 原:仅保留 LLM 裁定 + conf>=0.9。改:额外保留 column_extract 级别。
                candidates = [c for c in candidates
                              if c.stem == llm_stem or c.confidence >= 0.90
                              or c.level == "column_extract"]
                ambiguous = False
        elif _ambiguous_with_fk:
            # 歧义 + FK 边：保留多候选交 DecomposeAgent，但降低 ambiguous 标记
            # （保留只是不再触发 LLM 收敛，is_cross_table 判定仍正确）
            self.add_thinking("定位",
                f"歧义候选含 {len(pre_fk_edges)} 条 FK 边，判定为跨表链非纯噪声歧义，"
                f"保留 {len(candidates)} 候选交 DecomposeAgent 拆分阶段做表选择，"
                f"不在定位层 LLM 收敛单表（防跨表链被短路成单表）")
        # 3. FK 边:候选表之间的 relation graph 边
        fk_edges = self._collect_fk_edges(candidates)
        self.add_thinking("定位",
                          f"LocatorAgent 产出 {len(candidates)} 候选表, "
                          f"{len(fk_edges)} FK 边, cross={bool(fk_edges)}"
                          + (" (复杂输入汇集)" if complex_input else ""))
        return LocatorResult(candidates=candidates, fk_edges=fk_edges,
                             ambiguous=ambiguous, column_signal=column_signal)

    # ── 内部 ───────────────────────────────────────────────────

    def _rule_locate(self, text: str):
        """TableLocator 5 级递进定位。"""
        locator = self._get_locator()
        if locator is None:
            return None
        try:
            return locator.locate(text)
        except Exception:
            logger.debug("TableLocator.locate 失败", exc_info=True)
            return None

    @staticmethod
    def _is_fk_reference_column(stem: str, sheet: str, column: str) -> bool:
        """该列是否是 stem/sheet 表里指向他表的 FK 引用列(非本地核心列)。

        双信号判定(通用,不绑业务关键词):
        A) 命名约定:列名匹配引用列模式(以 _id/id/编号 结尾,或带数字索引的 data.N.xxx
           路径,如 model_id/space编号/option_function.data.1.conv_id)。这类列按游戏配表
           约定就是跨表引用键,不是本地语义字段。命中即视为附加上下文。
        B) RelationGraph 声明:from_path 含该 stem 且 from_column 命中 → FK 引用列。
        任一命中即降权。失败/无图 → 仅靠命名信号,不误伤核心列(名称/描述/类型 等)。
        """
        if not column:
            return False
        col_norm = column.strip().lower()
        # A) 命名约定:引用列模式
        if col_norm.endswith("_id") or col_norm == "id" or col_norm.endswith("编号"):
            return True
        # data.N.<ref> 形式的嵌套引用键(对话树 option_function.data.1.conv_id 等)
        if re.search(r"\bdata\.\d+\.", col_norm):
            return True
        # 带中文"编号"后缀的引用列(space编号/combat编号/quest编号)
        if column.endswith("编号"):
            return True
        # B) RelationGraph 声明式 FK
        try:
            from ..core.table_relations import RelationGraph  # 延迟导入避循环
            g = RelationGraph.load()
        except Exception:
            return False
        if not g or not getattr(g, "relations", None):
            return False
        for r in g.relations:
            if Path(r.from_path).stem != stem:
                continue
            if sheet and r.from_sheet and r.from_sheet != sheet:
                continue
            if (r.from_column or "").strip().lower() == col_norm:
                return True
        return False

    def _merge_candidates(self, outcome) -> list:
        """合并 best + ambiguous 为候选列表(去重同 path+sheet)。

        接受 TableLocator.LocateOutcome(有 best/ambiguous 属性)。
        """
        out = []
        seen = set()
        if outcome.best:
            out.append(outcome.best)
            seen.add((outcome.best.stem, outcome.best.sheet or ""))
        for r in getattr(outcome, "ambiguous", []) or []:
            key = (r.stem, r.sheet or "")
            if key not in seen:
                out.append(r)
                seen.add(key)
        # 按置信度降序
        out.sort(key=lambda r: getattr(r, "confidence", 0.0), reverse=True)
        return out

    def _is_complex_input(self, text: str) -> bool:
        """复杂多表输入信号:对话/选项/支线/采集/完成奖励 或多 id/引号名。

        复杂输入交 DecomposeAgent(LLM)做意图分解,LocatorAgent 不在定位层 LLM 收敛,
        避免单一高置信别名(如旧版"奖励"误路由)把跨表链短路成单候选,
        使 is_cross_table=False 漏触发 DecomposeAgent。
        """
        if any(k in text for k in (
            '选项', '对话', '支线', '主线', '采集', '完成奖励', '任务奖励',
            '接下', '接取', '领取', '提交任务',
        )):
            return True
        # 多 id 引用(reward_id/group_id/spawn_id 等交叉表信号)
        ids = re.findall(r'\b[A-Za-z_]+_id\b\s*[:：]?\s*\d+', text)
        if len(ids) >= 2:
            return True
        # 多引号名称(NPC名 + 任务名 + 对话文本 等)
        # F5: 仅"2+ 引号名"太宽松("灵田"+"初级灵田"纯实体名也触发 5 LLM decompose)，
        # 须含跨表业务关键词才视为复杂输入，避免简单一物连引号都过度分解。
        q = re.findall(r"['\"][^'\"]{2,30}['\"]", text)
        if len(q) >= 2 and any(k in text for k in (
            '对话', '选项', '任务', '战斗', '奖励', '邮件', '进化',
            '接取', '领取', '提交', '采集', '完成', '支线', '主线',
        )):
            return True
        return False

    def _expand_by_fk(self, candidates: list[CandidateTable],
                      complex_input: bool = False) -> list[CandidateTable]:
        """FK 关系驱动扩表:relation graph 任一端 stem 在候选内则补对端 stem。

        非复杂输入：只补「候选表引用列明确指向」的表（fk_inferred，运行时推导）。
        复杂输入：额外走 BFS 传递闭包多跳扩表（O20d：entity_prefab/quest/reward
        链的 2 跳外关联表）。

        置信度分档：
          - fk_inferred（缺口5 运行时推导：候选表引用列明确指向的表）
            → 0.60，低于 column_extract 0.70（旁证不挤目标表）
          - fk_expanded（复杂输入 BFS 泛化）→ 0.50/0.40 按跳衰减

        缺口4：隐式 FK 发现。case2 school_spirit.spirit_id→pet 未在 table_relations
        声明，关系图无此边。补：扫候选表 header 找 _id/xxxid 后缀列（如 spirit_id），
        用反向列名索引反查含该 id 列的表（pet 含 灵根id），补进 adj 邻接表走 BFS。
        """
        rg = self._get_relation_graph()
        if not candidates:
            return []
        # FK 邻接表:双向(任一端命中候选即扩对端)
        adj: dict[str, set[str]] = {}
        if rg is not None:
            for r in rg.relations:
                fs = _stem_of_path(r.from_path)
                ts = _stem_of_path(r.to_path)
                if fs == ts or not fs or not ts:
                    continue
                adj.setdefault(fs, set()).add(ts)
                adj.setdefault(ts, set()).add(fs)
        # 缺口4：隐式 FK 扩展。扫候选表 header 的 _id/xxxid 后缀列，
        # 反查含该列（中英文经 alias 对齐）的表加入 adj。
        self._expand_by_implicit_fk(candidates, adj)
        # 缺口5：运行时 FK 边推导驱动的扩表（候选表引用列明确指向池外表）。
        # 这些是强证据表，直接以高置信度返回，不在 BFS 衰减里混。
        inferred_targets: set[str] = self._expand_by_inferred_fk(candidates, adj)
        # 非复杂输入：只补引用列直接指向的表（fk_inferred），不走 BFS 多跳——
        # 多跳会把 guild(16 sheet)/item(11 sheet) 的二级关联（quest/reward/spell）
        # 全拉进来，噪声压过真正的目标表。复杂输入才需要 BFS 传递闭包。
        out: list[CandidateTable] = []
        for stem in sorted(inferred_targets):
            out.append(CandidateTable(
                stem=stem, confidence=0.60, level="fk_inferred",
                matched_term="fk_inferred",
            ))
        if not complex_input:
            return out
        # BFS 多跳,跳数衰减置信度,2 跳上限
        import os as _os
        max_hops = max(1, int(_os.environ.get("CODEMAKER_LOCATOR_FK_HOPS", "2")))
        seen = {c.stem for c in candidates} | inferred_targets
        new_entries: list[tuple[str, int]] = []  # (stem, hop)
        frontier = list(seen)
        for hop in range(1, max_hops + 1):
            next_frontier: list[str] = []
            for s in frontier:
                for nb in adj.get(s, ()):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    next_frontier.append(nb)
                    new_entries.append((nb, hop))
            frontier = next_frontier
            if not frontier:
                break
        # 置信度衰减:hop1=0.50, hop2=0.40(每跳 -0.10)
        conf_by_hop = {1: 0.50, 2: 0.40}
        for stem, hop in new_entries:
            out.append(CandidateTable(
                stem=stem, confidence=conf_by_hop.get(hop, 0.30),
                level="fk_expanded", matched_term=f"fk_h{hop}",
            ))
        return out

    def _expand_by_implicit_fk(self, candidates: list[CandidateTable],
                                adj: dict[str, set[str]]) -> None:
        """缺口4：隐式 FK 发现，扩 adj 邻接表。

        扫候选表（cli 可读时）每个 sheet 的 header，找 _id / xxxid 后缀列
        （如 school_spirit 的 spirit_id），用反向列名索引反查含该 id 列的表
        （pet 含 灵根id），补进 adj。中英文经 column_aliases 对齐。
        仅扩邻接表，不直接加 candidates（由 _expand_by_fk 的 BFS 统一处理）。
        """
        _cli = getattr(self, "_cli", None)
        if not _cli or not candidates:
            return
        try:
            from ..locator.table_index import get_column_reverse_index
            rev_idx = get_column_reverse_index()
        except Exception:
            rev_idx = {}
        if not rev_idx:
            return
        # 候选表 stem → path 映射（读 header 需 path）
        all_tables = {}
        try:
            all_tables = {p.stem: p for p in _cli.list_tables()}
        except Exception:
            return
        for cand in candidates:
            p = all_tables.get(cand.stem)
            if p is None:
                continue
            try:
                sheets = _cli.get_sheets(p)
            except Exception:
                continue
            for sh in sheets:
                if not sh or "说明" in sh or "CONFIG" in sh:
                    continue
                try:
                    hdrs = _cli.read_header(p, sh) or []
                except Exception:
                    continue
                for h in hdrs:
                    if not h:
                        continue
                    # row2 规范名（括号内 a.b.C 取末段 C），或 row1 中文
                    base = str(h).split("（")[0].split("(")[0].strip()
                    # 找 _id / id 结尾列（spirit_id / 灵根id）
                    low = base.lower()
                    if not (low.endswith("_id") or low.endswith("id")
                            or base.endswith("id")):
                        continue
                    # 反查含此列的表：rev_idx 键是列名（可能是中文 灵根id 或英文 spirit_id）
                    # 双路径查：base 原名 + 末段
                    for query_col in {base, low, base.rsplit(".", 1)[-1] if "." in base else base}:
                        hits = rev_idx.get(query_col) or rev_idx.get(query_col.lower())
                        if not hits:
                            continue
                        for hit_stem, _hit_sheet in hits:
                            if hit_stem == cand.stem or not hit_stem:
                                continue
                            adj.setdefault(cand.stem, set()).add(hit_stem)
                            adj.setdefault(hit_stem, set()).add(cand.stem)
                        break  # 命中一次即够，避免重复扫

    def _expand_by_inferred_fk(self, candidates: list[CandidateTable],
                               adj: dict[str, set[str]]) -> set[str]:
        """缺口5：运行时 FK 边推导驱动的扩表（零手工、实时跟随表结构）。

        扫候选表真实表头里的引用列（xxx_id/xxxid/xxx编号 → ref），在全表池
        反查 ref 命中的真实表，补进 adj 并返回强证据目标 stem 集合。
        与 _expand_by_implicit_fk 不同：本方法按「列名 → 被引表名」精确推导
        （school_ability_id → school_ability），而 implicit 依赖反向列名索引
        （跨表共享列名时不准）。
        典型场景：school.School.school_ability_id[0] → school_ability 未进候选
        （「神通」别名把候选引到泛化 ability.xlsx），此方法把它拉回来。
        """
        _cli = getattr(self, "_cli", None)
        if not _cli or not candidates:
            return set()
        try:
            all_tables = {p.stem: p for p in _cli.list_tables()}
        except Exception:  # noqa: BLE001
            return set()
        all_stems = set(all_tables.keys())
        if not all_tables:
            return set()
        inferred: set[str] = set()
        for cand in candidates:
            p = all_tables.get(cand.stem)
            if p is None:
                continue
            try:
                sheets = [s for s in _cli.get_sheets(p)
                          if s and "说明" not in s and "CONFIG" not in s.upper()]
            except Exception:  # noqa: BLE001
                continue
            for sh in sheets:
                try:
                    hdrs = _cli.read_header(p, sh) or []
                    trow = _cli.read_type_row(p, sh) or []
                except Exception:  # noqa: BLE001
                    continue
                for h, t in zip(hdrs, trow):
                    col = _base_col(str(t or "").strip() or str(h or "").strip())
                    if not col:
                        continue
                    ref = _ref_base(col)
                    if not ref or ref == cand.stem:
                        continue
                    targets = [s for s in all_stems if s == ref]
                    if not targets and ref.endswith("_id"):
                        body = ref[:-3]
                        targets = [s for s in all_stems if s == body]
                    for to_stem in targets:
                        inferred.add(to_stem)
                        adj.setdefault(cand.stem, set()).add(to_stem)
                        adj.setdefault(to_stem, set()).add(cand.stem)
        return inferred

    def _collect_all_level_hits(self, text: str) -> list:
        """复杂输入:返回 TableLocator.locate_all 全部级别命中(含 0.60 列名命中)。

        保留供调试/兜底;主路径用 _expand_by_fk 做 FK 精准扩表以避免噪声。
        """
        locator = self._get_locator()
        if locator is None:
            return []
        try:
            return locator.locate_all(text)
        except Exception:
            logger.debug("locate_all 复杂输入收集失败", exc_info=True)
            return []

    def _llm_resolve(self, text: str, candidates: list[str] = None) -> Optional[str]:
        """歧义/未命中时 LLM 选表 stem。复用 StepAIEnhancer.ai_resolve_table prompt 风格。

        §Step1 定位歧义修复：纯噪声歧义（无 FK 边，候选间无关联）才走单 stem 收敛。
        有 FK 边的歧义已在上层 locate() 直接判定保留多候选，不会进到本方法。
        本方法仅服务于"真歧义但非跨表链"的场景（如 spawn_world_entity 与
        spawn_region 同档命中但无 FK 关系），LLM 选最贴近动作主语的单表收敛。
        """
        all_tables = []
        try:
            if self._cli and hasattr(self._cli, "list_tables"):
                all_tables = [p.stem for p in self._cli.list_tables()]
        except Exception:
            pass
        cand_desc = "、".join(candidates) if candidates else "无"
        pool = "、".join(all_tables[:40]) if all_tables else "（未提供全表）"
        scenario = "多候选歧义" if candidates else "规则未命中"
        prompt = f"""你是配表路由专家。根据用户指令选最合适的目标表 stem。

## 用户指令
{text}

## 场景
{scenario}:候选 = {cand_desc}

## 可用表池
{pool}

## 路由提示
- NPC/怪物/宠物 → entity_prefab
- 道具/装备 → item
- 邮件 → mail
- 交互/对话 → interaction
- 任务 → quest
- 生成点 → spawn_world_entity

只输出表 stem(如 entity_prefab),无其他文字。"""
        raw = self._call_llm(prompt, timeout=30)
        if not raw:
            return None
        if isinstance(raw, dict):
            for key in ("stem", "table", "table_hint", "result", "answer"):
                val = raw.get(key)
                if isinstance(val, str) and val.strip():
                    raw = val
                    break
            else:
                vals = [v for v in raw.values() if isinstance(v, str) and v.strip()]
                raw = vals[0] if len(vals) == 1 else ""
        elif isinstance(raw, list):
            vals = [v for v in raw if isinstance(v, str) and v.strip()]
            raw = vals[0] if len(vals) == 1 else ""
        if not isinstance(raw, str) or not raw.strip():
            return None
        stem = raw.strip().splitlines()[0].strip()
        # 校验 LLM 输出在表池内(防幻觉)
        pool_check = (candidates or []) + all_tables
        if pool_check and stem not in pool_check:
            for p in pool_check:
                if stem in p or p in stem:
                    return p
            return None
        return stem

    def _collect_fk_edges(self, candidates: list[CandidateTable]) -> list[FKEdge]:
        """抽候选表之间的 FK 边：运行时自动推导为主，静态 json 为覆盖层。

        运行时推导（体现 Agent 自主能力，零手工维护）：
          扫候选表每个业务 sheet 的真实表头（row1 显示名 + row2 规范名），
          按列名模式识别引用列（xxx_id/xxxid/xxx编号 → xxx），再在候选池内
          反查被引表的主键列（首列 / 与 stem 同名列 / 任意 id 列），自动建边。
          这样学校链 school→school_ability→school_spirit 等无需在
          table_relations.json 里人工登记，新增表/新增列自动生效。

        静态 json（table_relations.json）作为人工补充的覆盖层：优先采纳
        静态边（含人工指定 from_column/to_column 的精确语义），运行时推导
        只补静态边没有覆盖到的表对。
        """
        if not candidates:
            return []
        cand_stems = {c.stem for c in candidates}
        edges: list[FKEdge] = []
        seen: set[tuple] = set()

        def _add(e: FKEdge) -> None:
            key = (e.from_stem, e.from_sheet, _base_col(e.from_column),
                   e.to_stem, e.to_sheet, _base_col(e.to_column))
            if key in seen:
                return
            seen.add(key)
            edges.append(e)

        # 1) 静态 json（人工覆盖层，优先）
        # §修复：原 `fs != ts` 把同 stem（同一 workbook）跨 sheet 的人工登记
        # 关系也滤掉了——如 interaction.InteractionConv.选项N → interaction.
        # InteractionConvOption（对话树选项引用），from/to 同属 interaction stem
        # 但是不同 sheet，属于合法且需要的 FK 边，不应被当"同表自环"丢弃。
        # 改为只排除真正的自环（同 stem 同 sheet 同列，无跨行/跨 sheet 语义）。
        rg = self._get_relation_graph()
        if rg is not None:
            for r in rg.relations:
                fs = _stem_of_path(r.from_path)
                ts = _stem_of_path(r.to_path)
                is_self_loop = (
                    fs == ts and (r.from_sheet or "") == (r.to_sheet or "")
                    and (r.from_column or "").strip().lower()
                    == (r.to_column or "").strip().lower())
                if fs in cand_stems and ts in cand_stems and not is_self_loop:
                    _add(FKEdge(
                        from_stem=fs, from_sheet=r.from_sheet,
                        from_column=r.from_column,
                        to_stem=ts, to_sheet=r.to_sheet, to_column=r.to_column,
                    ))
        # 2) 运行时自动推导（主路径，体现 Agent 能力）
        for e in self._infer_fk_edges(candidates):
            _add(e)
        return edges

    def _infer_fk_edges(self, candidates: list[CandidateTable]) -> list[FKEdge]:
        """运行时 FK 边自动推导：候选表真实表头 → 列名模式 → 边。

        仅依赖 cli（真实读 xlsx 表头）+ 候选池内反查，不依赖任何手工关系表。
        每表每个业务 sheet 提取「本表主键列集合」与「引用列集合」：
          - 主键列：首列 / 列名以本表 stem 开头或同名的 id 列
            （如 school.School 首列 'school:int'；spirit.Spirit 'spirit_id:int'）
          - 引用列：xxx_id / xxxid / xxx编号 模式的列，ref=xxx
        对每条引用列，在候选池内找 ref 命中的表，向该表每个主键列建一条 FKEdge。
        """
        cli = getattr(self, "_cli", None)
        if not cli or not candidates:
            return []
        try:
            all_tables = {p.stem: p for p in cli.list_tables()}
        except Exception:  # noqa: BLE001
            all_tables = {}
        if not all_tables:
            return []

        cand_stems = {c.stem for c in candidates}
        # 候选表 → 主键列 {stem: [(sheet, col)]}
        pk_cols: dict[str, list[tuple[str, str]]] = {}
        # 候选表 → 引用列 {stem: [(sheet, col, ref)]}
        ref_cols: dict[str, list[tuple[str, str, str]]] = {}

        for stem in cand_stems:
            p = all_tables.get(stem)
            if p is None:
                continue
            try:
                sheets = [s for s in cli.get_sheets(p)
                          if s and "说明" not in s and "CONFIG" not in s.upper()]
            except Exception:  # noqa: BLE001
                continue
            for sh in sheets:
                try:
                    hdrs = cli.read_header(p, sh) or []
                    trow = cli.read_type_row(p, sh) or []
                except Exception:  # noqa: BLE001
                    continue
                pk_for_sheet: list[str] = []
                ref_for_sheet: list[tuple[str, str]] = []
                for i, (h, t) in enumerate(zip(hdrs, trow)):
                    # 规范名优先（row2，如 school_ability_id[0]:int）；空则显示名
                    col_raw = str(t or "").strip() or str(h or "").strip()
                    col = _base_col(col_raw)
                    if not col:
                        continue
                    # 主键判定（严格，防多 sheet 表每 sheet 首列都当 PK 的噪声）：
                    #   ① 首列 且 列名是 id 类 → 主键
                    #   ② 列名 == 本表 stem（如 school / spirit_id==spirit）
                    if (i == 0 and _is_id_col(col)) or col.lower() == stem.lower():
                        if col not in pk_for_sheet:
                            pk_for_sheet.append(col)
                    # 引用列判定：xxx_id / xxxid / xxx编号
                    ref = _ref_base(col)
                    if ref:
                        ref_for_sheet.append((col, ref))
                if pk_for_sheet:
                    pk_cols.setdefault(stem, []).extend(
                        (sh, c) for c in pk_for_sheet)
                if ref_for_sheet:
                    ref_cols.setdefault(stem, []).extend(
                        (sh, c, r) for c, r in ref_for_sheet)

        # 同表多 sheet 引用：ref 精确命中候选 stem → 直接建边
        edges: list[FKEdge] = []
        # ref → 命中的候选 stem（精确 stem 相等优先；否则首段匹配）
        for from_stem, refs in ref_cols.items():
            for from_sheet, col, ref in refs:
                targets = [t for t in cand_stems if t == ref]
                # 复合引用列（school_ability_id）的 ref=school_ability 未命中时，
                # 去掉末尾 _id 段后的主体（school_ability）才是真正的被引表。
                # 不做"截到 stem"这类宽匹配——防 school_ability_id 错指 school。
                if not targets and ref.endswith("_id"):
                    body = ref[:-3]
                    targets = [t for t in cand_stems if t == body]
                if not targets and from_stem in cand_stems:
                    same_workbook_pks = pk_cols.get(from_stem) or []
                    if any(
                        to_sheet != from_sheet
                        and (
                            _ref_base(to_col) == ref
                            or _base_col(to_col).lower() == ref
                            or _base_col(to_col).lower().startswith(ref + "_")
                        )
                        for to_sheet, to_col in same_workbook_pks
                    ):
                        targets = [from_stem]
                if not targets:
                    continue
                for to_stem in targets:
                    to_pk = pk_cols.get(to_stem)
                    if not to_pk:
                        continue
                    # 每条引用列对每张被引表只取 1 条最佳主键边（防 item 11 sheet
                    # 每个 sheet 首列都建边的噪声）。优先级：列名==to_stem（主键名
                    # 与表名同）→ 首列 → 其余。
                    best = None
                    for to_sheet, to_col in to_pk:
                        if to_stem == from_stem and to_sheet == from_sheet:
                            continue
                        to_col_base = _base_col(to_col).lower()
                        to_col_ref = _ref_base(to_col) or ""
                        score = (2 if to_col.lower() == to_stem.lower() else 0) \
                            + (1 if to_col == to_pk[0][1] else 0)
                        if to_stem == from_stem:
                            score += 3
                        if to_col_ref == ref or to_col_base == ref or to_col_base.startswith(ref + "_"):
                            score += 4
                        if best is None or score > best[0]:
                            best = (score, to_sheet, to_col)
                    if best:
                        edges.append(FKEdge(
                            from_stem=from_stem, from_sheet=from_sheet,
                            from_column=col,
                            to_stem=to_stem, to_sheet=best[1],
                            to_column=best[2],
                        ))
        return edges

    def _get_locator(self):
        if self._locator is not None:
            return self._locator
        try:
            from ..locator.table_locator import TableLocator
            self._locator = TableLocator()
        except Exception as e:
            logger.debug("TableLocator 初始化失败: %s", e, exc_info=True)
            return None
        return self._locator

    def _get_relation_graph(self):
        if self._relation_graph is not None:
            return self._relation_graph
        try:
            from ..table_relations import RelationGraph
            self._relation_graph = RelationGraph.load()
        except Exception as e:
            logger.debug("RelationGraph.load 失败: %s", e, exc_info=True)
            return None
        return self._relation_graph

    def _run_impl(self, prompt: str, skill_docs: list, context: dict):
        """SubAgent 接口适配:从 context 取 text,产 LocatorResult dict。"""
        text = context.get("text") or prompt
        result = self.locate(text)
        return {
            "sql_or_ops": [],
            "locator_result": result,
            "produces": None,
            "references": [],
            "target_table": "",
            "target_sheet": "",
        }


__all__ = ["LocatorAgent", "LocatorResult", "CandidateTable", "FKEdge"]
