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
        """是否触发跨表链路径:≥2 候选表 或 含 FK 边。"""
        return len(self.candidates) >= 2 or bool(self.fk_edges)


def _stem_of_path(p: str) -> str:
    """relation path → stem:'pet/pet.xlsx' → 'pet'。"""
    s = str(p).replace("\\", "/").rstrip("/")
    if s.endswith(".xlsx"):
        s = s[:-5]
    return s.rsplit("/", 1)[-1]


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
            # 打分:专有列命中数为主,得分次之
            scored: list[tuple[int, float, str]] = []  # (专有列命中数, 最高分, stem)
            for stem, hs in stem_agg.items():
                if stem in existing:
                    continue
                # 专有列 = 命中列里非通用列的
                specific = [h for h in hs if h.column not in _GENERIC_COLS]
                if not specific:
                    continue  # 全是通用列命中,不补(噪声)
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
        # 1b. 复杂输入:FK 关系驱动补表(relation graph 任一端 stem 在候选内则补对端)。
        #     把 alias 未直接命中但语义相关的表(如 interaction/spawn_world_entity)
        #     纳入候选,而不用 locate_all 全量列名匹配(会引入 40+ 噪声表膨胀 DecomposeAgent 上下文)。
        if complex_input:
            for c in self._expand_by_fk(candidates):
                if not any(x.stem == c.stem for x in candidates):
                    candidates.append(c)
        # §P0-3 候选池总量上限：复杂多指令 + 规则ambiguous全收 + 列名补 + FK扩表
        # 可叠到 50+ 候选 → 并发主路径每表一次 LLM → 50 次 LLM 爆炸。
        # cap 默认 8（env 可调），按置信度降序保留，优先规则命中(高conf) + 列名专有列命中。
        # 保留策略：① conf>=0.80(规则强命中)全留 ② 其余按conf降序取到cap ③ 同conf保列名命中
        _cand_cap = max(4, int(os.environ.get("CODEMAKER_LOCATOR_MAX_CANDIDATES", "8")))
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
        if (not complex_input) and (ambiguous or not candidates) and self.parser:
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

    def _expand_by_fk(self, candidates: list[CandidateTable]) -> list[CandidateTable]:
        """FK 关系驱动扩表:relation graph 中任一端 stem 在候选内则补对端 stem。

        多跳传递闭包(O20d):复杂输入经 alias 命中 entity_prefab/quest/reward 后,
        不仅补直接 FK 对端(interaction/spawn_world_entity),还沿 FK 链继续扩
        到 2 跳外的关联表(如 reward↔item 经 reward_item→item),使 DecomposeAgent
        看到完整跨表链。新补表置信度按跳数衰减:0.50/0.40(level=fk_expanded),
        2 跳上限避免无限膨胀(防 DecomposeAgent 上下文噪声)。

        缺口4：隐式 FK 发现。case2 school_spirit.spirit_id→pet 未在 table_relations
        声明，关系图无此边。补：扫候选表 header 找 _id/xxxid 后缀列（如 spirit_id），
        用反向列名索引反查含该 id 列的表（pet 含 灵根id），补进 adj 邻接表走 BFS。
        """
        rg = self._get_relation_graph()
        if rg is None or not candidates:
            return []
        # FK 邻接表:双向(任一端命中候选即扩对端)
        adj: dict[str, set[str]] = {}
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
        if not adj:
            return []
        # BFS 多跳,跳数衰减置信度,2 跳上限
        import os as _os
        max_hops = max(1, int(_os.environ.get("CODEMAKER_LOCATOR_FK_HOPS", "2")))
        seen = {c.stem for c in candidates}
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
        out: list[CandidateTable] = []
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
        """歧义/未命中时 LLM 选表 stem。复用 StepAIEnhancer.ai_resolve_table prompt 风格。"""
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
        """从 RelationGraph 抽候选表之间的 FK 边。

        仅保留两端 stem 均在 candidates 内的边(聚焦跨表链)。
        """
        rg = self._get_relation_graph()
        if rg is None or not candidates:
            return []
        cand_stems = {c.stem for c in candidates}
        edges: list[FKEdge] = []
        for r in rg.relations:
            fs = _stem_of_path(r.from_path)
            ts = _stem_of_path(r.to_path)
            if fs in cand_stems and ts in cand_stems and fs != ts:
                edges.append(FKEdge(
                    from_stem=fs, from_sheet=r.from_sheet, from_column=r.from_column,
                    to_stem=ts, to_sheet=r.to_sheet, to_column=r.to_column,
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
