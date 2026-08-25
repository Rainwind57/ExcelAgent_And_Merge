"""分解 SubAgent:消费 LocatorResult + schema,产 SplitIntent[] 含 produces/consumes。

替代 cross_table_splitter.py 11 个 _build_*_intents 硬编码模板。
新链型零代码——LLM 读候选表真实表头列名 + FK 边产每表一 op。

职责边界:
  - 输入: LocatorResult(candidates + fk_edges) + schema(每表 row1/row2 表头)
  - 输出: list[SplitIntent](每表一 op,含 produces/consumes)
  - 安全网: 产 <2 intent 或畸形 → 调用方回退 cross_table_splitter 规则模板

规则安全网保留原则:
  LLM 为主,规则为兜底。DecomposeAgent 产 ≥2 intent 时取代 splitter;
  产 <2/畸形时 splitter 11 模式作 fallback,保 ok 率不回归。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .base import SubAgent
from .llm_agent import LLMSubAgent
from .locator_agent import LocatorResult, CandidateTable, FKEdge

logger = logging.getLogger(__name__)


# 复用 cross_table_splitter.SplitIntent 结构(不重新定义,保兼容)
def _SplitIntent():
    """延迟导入避免循环 import。"""
    from ..cross_table_splitter import SplitIntent
    return SplitIntent


class DecomposeAgent(LLMSubAgent):
    """分解 Agent:LLM 产每表一 op + produces/consumes。

    流程:
      1. 从 LocatorResult 取候选表 + FK 边
      2. 读每表所有业务 sheet 的 row1+row2 表头,构 schema 块
      3. LLM 产 JSON 数组,每元素 {table,sheet,action,fields,produces,consumes}
      4. 解析为 SplitIntent 列表
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("DecomposeAgent", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="跨表链分解产 SplitIntent[]")
        self._cli = cli
        # §schema 内存缓存：(stem, sheet) -> (headers, type_row)。
        # 同 session 内主路径+兜底+段级重跑重复读同表头，缓存省 60-70% I/O。
        # 失效靠进程重启（配表 xlsx 结构静态，单进程内不变）。
        self._schema_cache: dict[tuple[str, str], tuple[list, list]] = {}

    def decompose(self, text: str, locator_result: LocatorResult) -> list:
        """主入口:text + LocatorResult → SplitIntent[]。

        O1 重构:反转主次——候选表数 ≤ 阈值时走单 prompt 主路径(N→1,token 砍 50-70%),
        失败/产 <2 降级并发每表;候选表数 > 阈值时保持并发主路径(防单 prompt schema
        过大致超时空返),并发产 <2 触发单 prompt 兜底。阈值由 env 控制,默认 3。

        §Step1 列名信号注入：locator_result.column_signal 透传给 _build_prompt +
        _build_schema_block，让 LLM 看着列名命中信号选表选 sheet 选列（LLM 主导，
        规则信号为辅）。修复案例三 spirit 误路由——LLM 看到"活动类型→activity"
        信号会选 activity 而非 spirit。

        Args:
            text: 用户原始指令
            locator_result: LocatorAgent 产出(候选表 + FK 边 + column_signal)

        Returns:
            SplitIntent 列表(每表一 op)。失败返回空列表,
            调用方降级走 cross_table_splitter 规则模板。
        """
        if not text or not locator_result or not locator_result.candidates:
            return []
        if not self.parser:
            logger.warning("DecomposeAgent 无 parser,跳过")
            return []

        import os as _os
        # §P1-2.2 超时下调 90→40：P0-0 分段后候选 ≤3/段，单段小 schema 不需 90s
        # 思考；长超时让 LLM 卡住拖垮整链。40s 够单段拆分。
        per_to = int(_os.environ.get("CODEMAKER_DECOMPOSE_TIMEOUT", "40"))
        candidates = locator_result.candidates
        fk_block = self._build_fk_block(locator_result.fk_edges)
        column_signal = getattr(locator_result, "column_signal", None)

        single_threshold = int(_os.environ.get(
            "CODEMAKER_DECOMPOSE_SINGLE_PROMPT_THRESHOLD", "3"))
        use_single = len(candidates) <= single_threshold
        # 缓存 column_signal 供 _splitter_baseline 零 LLM 兜底用
        self._last_column_signal = column_signal

        self.add_thinking("细分",
            f"DecomposeAgent {'单 prompt' if use_single else '并发'}主路径"
            f"({len(candidates)} 表,阈值 {single_threshold},timeout={per_to}s)")

        all_intents: list = []
        all_dropped: list[str] = []
        def _merge(res):
            nonlocal all_intents, all_dropped
            if isinstance(res, tuple) and len(res) == 2:
                its, drp = res
                all_intents = its or []
                if drp:
                    for _s in drp:
                        if _s not in all_dropped:
                            all_dropped.append(_s)
            else:
                all_intents = res or []

        # §P1-2.3 砍降级链：候选已被 P0-0 分段收到 ≤阈值，单 prompt 足够。原"单→并发→单"
        # 对同批表跑 3 遍 LLM，拖垮链路。现单 prompt 产空/产 <2 不再降级并发每表，
        # 直接走下方 _splitter_baseline 零 LLM 兜底（段级对账会重跑 decompose_segment）。
        if use_single:
            _merge(self._decompose_single_prompt(
                text, candidates, fk_block, per_to, column_signal=column_signal))
        else:
            _merge(self._decompose_parallel(
                text, candidates, fk_block, per_to, column_signal=column_signal))

        # §叙述灌值丢弃后单表 schema 重拆（方案 C）：对被丢弃的 stem 逐个用单表候选
        # 重新跑 _decompose_single_prompt（小 schema → LLM 产出质量好），重拆产出的
        # intent 若仍叙述灌值则不再重试（防死循环，限 1 轮）。让合法表（如 activity/
        # reward/combat）在 LLM 首次大候选池退化产垃圾后，有机会在小 schema 下重产出
        # 合法 fields，而非子任务丢失。通用机制，不绑业务词。
        if all_dropped:
            retry_stems = [s for s in all_dropped if s]
            self.add_thinking("细分",
                f"DecomposeAgent 叙述灌值丢弃 {len(retry_stems)} stem，"
                f"触发单表 schema 重拆：{retry_stems[:8]}")
            # 构单表候选：从原 candidates 找该 stem 的 CandidateTable，单表小 schema
            cand_by_stem = {c.stem: c for c in candidates}
            for _rs in retry_stems:
                _rc = cand_by_stem.get(_rs)
                if _rc is None:
                    # 候选外 stem，构造单表候选（sheet 留空让 schema 读全部业务 sheet）
                    _rc = CandidateTable(stem=_rs, sheet="", confidence=0.5,
                                          level="retry_single", matched_term="")
                try:
                    _rit, _rdrp = self._decompose_single_prompt(
                        text, [_rc], fk_block, per_to, column_signal=column_signal)
                except Exception:  # noqa: BLE001
                    _rit, _rdrp = [], []
                if _rit:
                    self.add_thinking("细分",
                        f"单表重拆 {_rs} 产 {len(_rit)} 条意图（叙述灌值丢弃后补救）")
                    all_intents.extend(_rit)
                else:
                    self.add_thinking("细分",
                        f"单表重拆 {_rs} 仍产空/叙述灌值，该 stem 子任务未能补救")

        self.add_thinking("细分",
            f"DecomposeAgent 产出 {len(all_intents)} 条意图"
            f"({'单 prompt' if use_single else '并发'}"
            f"{'+' if all_dropped else ''}{'重拆' if all_dropped else ''})")
        # §零 LLM 兜底：LLM 路径产空（serve 慢/超时/非 JSON）时不返空，
        # 改用确定性 splitter_baseline（cross_table_splitter 11 模板，0 LLM）。
        # 保链路完整走通——LLM 不稳时仍能产可执行 intent，不依赖 serve 健康。
        if not all_intents:
            fb = self._splitter_baseline(text, candidates, locator_result.fk_edges)
            if fb:
                self.add_thinking("细分",
                    f"DecomposeAgent LLM 路径产空,零 LLM 兜底产 {len(fb)} 条"
                    f"(splitter_baseline 11 模板)")
                all_intents = fb
        # §分段单 prompt 兜底（与 splitter_baseline 互补）：仍空时按 split_multi_intent
        # 分段，每段调 decompose_segment（段内候选裁剪 + 小 prompt），LLM 路径产准。
        # 比 splitter_baseline 11 模板覆盖广（模板只命中已知链型），但依赖 LLM 可用。
        if not all_intents:
            try:
                from ..parser.multi_intent_splitter import split_multi_intent
                segs = split_multi_intent(text)
                if segs and len(segs) > 1:
                    seg_intents: list = []
                    for seg_text in segs:
                        # 段级独立 locate（用主 locator_result 的候选裁剪）
                        pruned = self._prune_segment_candidates(
                            seg_text, candidates, column_signal,
                            locator_result.fk_edges)
                        seg_fk = self._build_fk_block(locator_result.fk_edges)
                        seg_out = self._decompose_single_prompt(
                            seg_text, pruned, seg_fk, per_to,
                            column_signal=column_signal)
                        # _decompose_single_prompt 返回 (intents, dropped_stems) tuple
                        seg_its = seg_out[0] if isinstance(seg_out, tuple) else seg_out
                        if seg_its:
                            seg_intents.extend(seg_its)
                    if seg_intents:
                        self.add_thinking("细分",
                            f"DecomposeAgent 全表 prompt 产空,分段单 prompt 兜底产 {len(seg_intents)} 条"
                            f"({len(segs)} 段)")
                        all_intents = seg_intents
            except Exception:  # noqa: BLE001
                logger.debug("分段单 prompt 兜底失败", exc_info=True)
        return all_intents

    def decompose_segment(self, seg: str, locator_result: LocatorResult) -> list:
        """按段分解：单段文本 + 该段候选表 → SplitIntent[]。

        预处理分段后的入口（§优化：分而治之）。段内文本短、候选表少（每段独立
        locate 后候选精准），走单 prompt 主路径。段内单 op 正常（一条指令可能只产
        一个 op），不强行 <2 降级，避免段级双跑拖累。

        §段内候选裁剪：段级 locate 可能仍含多表候选（case1 单段跨6表），按段文本
        + column_signal hits 裁剪，只保留命中表 + FK 链相关表。控 prompt token
        <8k（vs 全候选 22k），防 serve 空响应。

        Args:
            seg: 单段指令文本（split_multi_intent 产出的段）
            locator_result: 该段的 LocatorResult（独立 locate 产出）

        Returns:
            SplitIntent 列表。失败返回空列表。
        """
        if not seg or not locator_result or not locator_result.candidates:
            return []
        if not self.parser:
            logger.warning("DecomposeAgent 无 parser,跳过段分解")
            return []
        import os as _os
        # 段级 schema 小(单段+剪枝后候选表少)，不需整句级长 timeout。默认与
        # decompose 整句入口(L85)对齐为 40，消除两处默认不一致；runner/env
        # 可经 CODEMAKER_DECOMPOSE_TIMEOUT 进一步下压（串行/并发累加墙钟由此收敛）。
        per_to = int(_os.environ.get("CODEMAKER_DECOMPOSE_TIMEOUT", "40"))
        candidates = self._prune_segment_candidates(
            seg, locator_result.candidates,
            getattr(locator_result, "column_signal", None),
            locator_result.fk_edges)
        fk_block = self._build_fk_block(locator_result.fk_edges)
        column_signal = getattr(locator_result, "column_signal", None)
        # 缓存 column_signal 供 _splitter_baseline 零 LLM 兜底用
        self._last_column_signal = column_signal
        self.add_thinking("细分",
            f"DecomposeAgent 段分解({len(candidates)}/{len(locator_result.candidates)} 候选,timeout={per_to}s)")
        seg_out = self._decompose_single_prompt(
            seg, candidates, fk_block, per_to, column_signal=column_signal)
        # _decompose_single_prompt 返回 (intents, dropped_stems) tuple
        intents = seg_out[0] if isinstance(seg_out, tuple) else seg_out
        dropped = seg_out[1] if isinstance(seg_out, tuple) and len(seg_out) > 1 else []
        # §段级叙述灌值丢弃后单表重拆（与主入口 decompose 同逻辑）：段内 LLM 退化产
        # 垃圾 fields 的 stem，用单表小 schema 重拆一次。防子任务丢失。
        if dropped:
            self.add_thinking("细分",
                f"DecomposeAgent 段级丢弃 {len(dropped)} stem，触发段级单表重拆：{dropped[:6]}")
            cand_by_stem = {c.stem: c for c in candidates}
            for _rs in dropped:
                if not _rs:
                    continue
                _rc = cand_by_stem.get(_rs) or CandidateTable(
                    stem=_rs, sheet="", confidence=0.5,
                    level="retry_single", matched_term="")
                try:
                    _rit, _ = self._decompose_single_prompt(
                        seg, [_rc], fk_block, per_to, column_signal=column_signal)
                except Exception:  # noqa: BLE001
                    _rit = []
                if _rit:
                    self.add_thinking("细分",
                        f"段级单表重拆 {_rs} 产 {len(_rit)} 条意图")
                    intents.extend(_rit)
                else:
                    self.add_thinking("细分",
                        f"段级单表重拆 {_rs} 仍产空/叙述灌值，未能补救")
        # §零 LLM 兜底：段分解产空时走 _splitter_baseline（与主流程一致），
        # 保段级覆盖（多段指令某段 LLM 产空不漏）。
        if not intents:
            fb = self._splitter_baseline(seg, candidates, locator_result.fk_edges)
            if fb:
                self.add_thinking("细分",
                    f"DecomposeAgent 段分解产空,零 LLM 兜底产 {len(fb)} 条")
                intents = fb
        return intents

    def _prune_segment_candidates(self, seg: str, candidates: list,
                                   column_signal, fk_edges: list) -> list:
        """段内候选表裁剪：只保留段文本命中表 + FK 链相关表。

        命中信号（按优先级）：
          1. column_signal hits 的 stem（列名反查命中）
          2. 段文本子串匹配 stem/别名
          3. FK 边端点表（保链路完整性，如 spawn 引用 entity_prefab）

        无信号时（seg 极短或 locator 已精准）不裁剪，原样返回。
        裁剪后保留数 < 2 时也原样返回（避免误裁到单表丢链路）。

        §P0 候选超量强制裁剪：候选 >5 表时单 prompt schema 过大 → LLM 超时产空
        → 兜底产空 → Step3 path2 别名扫描产碎片（根因：harness 超时，非 LLM 模型）。
        候选 >5 时按 column_signal 命中列数 + 段文本子串命中 强度排序取 top 3，
        FK 依赖表无条件保留（保链路）。控 prompt token <8k 防 serve 超时空返。
        """
        if not seg or not candidates or len(candidates) <= 3:
            return list(candidates)
        seg_lower = seg.lower()
        # 1. column_signal hits stem（按命中列数加权）
        sig_stems: dict[str, int] = {}  # stem -> 命中列数
        if column_signal is not None:
            for h in getattr(column_signal, "hits", []) or []:
                stem = getattr(h, "stem", "") or ""
                if stem:
                    sig_stems[stem.lower()] = sig_stems.get(stem.lower(), 0) + 1
        # 2. 段文本子串匹配 stem
        text_stems: set[str] = set()
        for c in candidates:
            stem = (getattr(c, "stem", "") or "").lower()
            if stem and stem in seg_lower:
                text_stems.add(stem)
        # 3. FK 边端点表
        fk_stems: set[str] = set()
        for e in fk_edges or []:
            for attr in ("from_stem", "to_stem", "from_table", "to_table"):
                v = getattr(e, attr, "") or ""
                if v:
                    fk_stems.add(v.lower())
        # 合并命中表
        hit_stems = set(sig_stems) | text_stems | fk_stems
        if not hit_stems:
            return list(candidates)
        # §P0 候选超量裁剪：>5 表按命中强度取 top 3 + FK 依赖表全保
        if len(candidates) > 5:
            # FK 依赖表无条件保留（保链路完整性）
            fk_cands = [c for c in candidates
                        if (getattr(c, "stem", "") or "").lower() in fk_stems]
            # 非FK表按命中强度（列信号命中数 + 段文本子串命中）排序取 top 3
            non_fk_cands = [c for c in candidates
                            if (getattr(c, "stem", "") or "").lower() not in fk_stems]
            def _strength(c):
                _s = (getattr(c, "stem", "") or "").lower()
                _sig = sig_stems.get(_s, 0)
                _txt = 1 if _s in text_stems else 0
                # 弱信号（column_extract 级）降权，防 model_id 跨表共享列拉无关表
                _lvl = (getattr(c, "level", "") or "").lower()
                _lvl_w = 0 if _lvl in ("column_extract", "column_reverse") else 1
                return (_sig * 2 + _txt + _lvl_w, getattr(c, "confidence", 0))
            non_fk_cands.sort(key=_strength, reverse=True)
            pruned = fk_cands + non_fk_cands[:max(0, 3 - len(fk_cands))]
            # 至少保 2 表防误裁
            if len(pruned) < 2:
                pruned = non_fk_cands[:3] + fk_cands
            return pruned[:5] if len(pruned) > 5 else pruned
        pruned = [c for c in candidates
                  if (getattr(c, "stem", "") or "").lower() in hit_stems]
        # 裁剪后 <2 表保链路不破：回退原候选
        if len(pruned) < 2:
            return list(candidates)
        return pruned

    def _splitter_baseline(self, text: str, candidates: list,
                           fk_edges: list) -> list:
        """零 LLM 兜底：LLM 路径产空时产确定性 intent。

        两路径合并（保覆盖）：
          a. cross_table_splitter 11 模板（detect_cross_table_action 命中则用，
             覆盖 npc/item/mail/quest/pet/school/combat/residence 等已知链型）
          b. ColumnExtractor 候选表 → 每表产 1 条 add intent（fields 用指令
             文本提到的列值，无值留空）+ FK 边 → produces/consumes 占位符连线

        不调 LLM，不依赖 serve 健康。产空仍返 []（极少见，splitter 模板
        不命中 + 候选表无 FK 边），由 Step1 外层再回退。
        """
        all_fb: list = []
        # a. splitter 11 模板
        try:
            from ..core.cross_table_splitter import (
                CrossTableIntentSplitter, detect_cross_table_action)
            if detect_cross_table_action(text):
                sp = CrossTableIntentSplitter()
                sp_intents = sp.split(text)
                if sp_intents:
                    all_fb.extend(sp_intents)
        except Exception:  # noqa: BLE001
            logger.warning("DecomposeAgent splitter_baseline 模板失败",
                           exc_info=True)
        # b. ColumnExtractor 信号兜底：每候选表产 1 条 add intent
        # 候选表已含列名信号，从 text 提取列值填 fields
        if len(all_fb) < len(candidates):
            # column_signal 在 LocatorResult 上（非 CandidateTable），从外层传入
            cs = getattr(self, "_last_column_signal", None)
            sig_hits = []
            if cs is not None:
                sig_hits = getattr(cs, "hits", []) or []
            # 按 stem 聚合命中列
            sig_by_stem: dict[str, list] = {}
            for h in sig_hits:
                stem = getattr(h, "stem", "") or ""
                if stem:
                    sig_by_stem.setdefault(stem, []).append(h)
            existing_stems = {(getattr(i, "table_hint", "") or "").lower()
                              for i in all_fb}
            # §P1 防空壳 noise：ColumnExtractor 候选含"文件级弱命中"表（text 只泛提
            # 一下、无任何列值信号）。对这类 fields 全空的候选产 add intent → Step3
            # 必"无法解析新增内容"→计 execute_failed_no_llm failure（噪音，非真失败）。
            # 保留被 FK 依赖的前置表（其他 intent consumes 其 produces，即使无列值也
            # 该产以供下游拓扑回填）。通用判据（列值信号 + FK 关系图），不绑业务词/测例。
            _producer_stems = {(getattr(e, "to_stem", "") or "").lower()
                               for e in (fk_edges or [])}
            # §框架级 B（fail-soft，不臆造错表）：仅靠列名反查命中的弱信号候选
            # （level=column_extract/column_reverse）不代表输入语义上指向该表——如
            # "model_id" 是跨表共享列，命中 guild/assistant 只是列名巧合，非动作主语。
            # 对这类弱信号候选不臆造 add intent（宁可软失败跳过，也不写猜测的错表）；
            # 只对语义命中表（alias/文件名/sheet/llm 推断）或被 FK 引用的前置表产 intent。
            # 通用判据（命中级别 taxonomy + FK 图），不绑业务词/表/测例。
            _WEAK_LEVELS = {"column_extract", "column_reverse"}
            SI = _SplitIntent()
            import re as _re
            for cand in candidates:
                stem = getattr(cand, "stem", "") or ""
                if not stem or stem.lower() in existing_stems:
                    continue
                _lvl = (getattr(cand, "level", "") or "").lower()
                if _lvl in _WEAK_LEVELS and stem.lower() not in _producer_stems:
                    continue
                fields: dict = {}
                for h in sig_by_stem.get(stem, []):
                    col = getattr(h, "column", "") or ""
                    if not col:
                        continue
                    # 从 text 扫 "col 值" 或 "col=值" 模式。
                    # §P1 防 A 类碎片污染：原正则 [\d\u4e00-\u9fff]+ 裸匹配中文连续段
                    # （如"叫焚天赤龙"被当 reward_id 值灌入 str 列写盘成功但碎片污染行）。
                    # 现只提纯数字 / 数字+字母 token（编号/ID/概率/数量等标量特征），
                    # 整段中文叙述留给模板或 LLM 产，baseline 不裸提中文值。
                    # §框架级（字段对应错）：值必须**紧邻**列名（≤6 个非数字字符内），
                    # 否则会跨整段抓到远处无关数字（如"坐标"抓到别处的 BOSS 坐标）。
                    # 通用判据（值形态 + 邻接），不绑业务词/表/测例。
                    _pat = _re.compile(
                        rf"{_re.escape(col)}[^\d]{{0,6}}?(\d+(?:\.\d+)?%?)",
                        _re.IGNORECASE)
                    _m = _pat.search(text)
                    if _m:
                        fields[col] = _m.group(1)
                # fields 全空 且 非 FK 被依赖前置 → noise 候选，不产空壳 intent
                if not fields and stem.lower() not in _producer_stems:
                    continue
                all_fb.append(SI(
                    text=text, table_hint=stem,
                    sheet_hint=getattr(cand, "sheet", "") or "",
                    action="add",
                    fields=fields,
                    produces=f"new_{stem}_id" if stem else None,
                ))
        return all_fb

    def _decompose_single_prompt(self, text: str, candidates: list[CandidateTable],
                                  fk_block: str, per_to: int,
                                  column_signal=None) -> list:
        """单 prompt 全候选 schema 合置,产跨表业务链拆分。

        O1 主路径(候选表数 ≤ 阈值)/并发兜底(并发产 <2 时)。自建 cancel event 镜像
        run 级,主路径也响应取消。§P1-2.2 timeout=per_to（40s，P0-0 分段后小 schema 不需 2x）。

        §Step1 列名信号：column_signal 透传给 _build_schema_block（命中 sheet 排序）
        + _build_prompt（注入列名信号块），LLM 看着信号选表选列。
        """
        schema_all = self._build_schema_block(candidates, text=text, column_signal=column_signal)
        if not schema_all:
            return []
        _stems = [c.stem for c in candidates if getattr(c, "stem", None)]
        prompt = self._build_prompt(text, schema_all, fk_block,
                                    column_signal=column_signal, fill_stems=_stems)
        client = getattr(self.parser, "client", None)
        if client is None:
            return []
        import threading as _t
        _ce = _t.Event()
        _run_ce = getattr(self.parser, "_cancel_event", None)
        if _run_ce is not None:
            def _mirror():
                if _run_ce.wait():
                    _ce.set()
            _t.Thread(target=_mirror, daemon=True).start()
        from .base import _isolated_empty_dir
        raw = ""
        try:
            sr = client.create_session(
                directory=_isolated_empty_dir(),
                model=getattr(self.parser, "model", ""))
            if getattr(sr, "ok", False):
                from .llm_gate import llm_throttle
                with llm_throttle():
                    resp = client.prompt(sr.session_id, prompt, timeout=per_to,
                                          model=getattr(self.parser, "model", ""),
                                          cancel_event=_ce)
                self._bump_llm("decompose")
                raw = getattr(resp, "response_text", "") or ""
        except Exception as e:  # noqa: BLE001
            self.add_thinking("细分",
                f"DecomposeAgent 单 prompt 调用失败({type(e).__name__}: {e})")
            return []
        if not raw:
            self.add_thinking("细分", "DecomposeAgent 单 prompt 空响应")
            return []
        arr = self._parse_json_array(raw)
        if not arr:
            self.add_thinking("细分", "DecomposeAgent 单 prompt 非 JSON 数组")
            return [], []
        intents, dropped = self._to_split_intents(arr, text)
        valid_stems = {c.stem.lower() for c in candidates}
        filtered = self._filter_intents(intents, candidates, valid_stems,
                                         path="单 prompt")
        self.add_thinking("细分",
            f"DecomposeAgent 单 prompt 产出 {len(filtered)} 条意图"
            f"（丢弃 {len(dropped)} 叙述灌值 stem）")
        return filtered, dropped

    def _decompose_parallel(self, text: str, candidates: list[CandidateTable],
                             fk_block: str, per_to: int,
                             column_signal=None) -> list:
        """并发每表单 prompt(原主路径)。候选表数 > 阈值时主路径,否则降级兜底。

        保留原 fail-fast:前 2 候选均空响应 → 取消剩余并发,降级规则模板/单 prompt。

        §Step1 列名信号：透传给每表 _build_schema_block + _build_prompt。
        """
        import os as _os
        import threading as _threading
        from concurrent.futures import ThreadPoolExecutor

        jobs = []
        for cand in candidates:
            schema_one = self._build_schema_block(
                [cand], text=text, column_signal=column_signal)
            if not schema_one:
                continue
            jobs.append((cand, self._build_prompt(
                text, schema_one, fk_block, column_signal=column_signal,
                fill_stems=[cand.stem] if getattr(cand, "stem", None) else [])))
        if not jobs:
            return []

        max_workers = int(_os.environ.get("CODEMAKER_DECOMPOSE_WORKERS", "4")) or 1
        _retry_env = int(_os.environ.get("CODEMAKER_DECOMPOSE_RETRY", "1"))
        # §P0 失败容错：原 `retries = 0 if len(candidates) >= 4 else max(0, _retry_env)`
        # 把"候选多"当"简单"反了——复杂跨表输入候选常 ≥4，4 个无关表全失败立即放弃
        # 不重试，拖垮活动表这条成功但 LLM 在压力下产最小 JSON。改：至少重试 1 次，
        # 首输出残缺时可补救。候选多 ≠ 简单，恰恰是复杂输入更需重试机会。
        retries = max(1, _retry_env)

        _local_ce = _threading.Event()
        _run_ce = getattr(self.parser, "_cancel_event", None)
        if _run_ce is not None:
            def _mirror_cancel():
                if _run_ce.wait():
                    _local_ce.set()
            _threading.Thread(target=_mirror_cancel, daemon=True).start()

        def _run_one(job):
            from .base import _isolated_empty_dir
            cand, prompt = job
            client = getattr(self.parser, "client", None)
            if client is None:
                return [], []
            raw = ""
            last_err = ""
            import time as _time
            backoff_base = float(_os.environ.get("CODEMAKER_DECOMPOSE_RETRY_BACKOFF", "1"))
            for _attempt in range(retries + 1):
                try:
                    sr = client.create_session(
                        directory=_isolated_empty_dir(),
                        model=getattr(self.parser, "model", ""))
                    if not getattr(sr, "ok", False):
                        last_err = "建会话失败"
                    else:
                        from .llm_gate import llm_throttle
                        with llm_throttle():
                            resp = client.prompt(sr.session_id, prompt, timeout=per_to,
                                                  model=getattr(self.parser, "model", ""),
                                                  cancel_event=_local_ce)
                        self._bump_llm("decompose")
                        raw = getattr(resp, "response_text", "") or ""
                        if raw:
                            break
                        if getattr(resp, "error_type", "") == "cancelled":
                            break
                        last_err = "空响应/超时"
                except Exception as e:  # noqa: BLE001
                    last_err = f"{type(e).__name__}: {e}"
                if _attempt < retries and backoff_base > 0:
                    _time.sleep(backoff_base * (2 ** _attempt))
            if not raw:
                self.add_thinking("细分",
                    f"DecomposeAgent {cand.stem} 调用失败({last_err}, retry={retries})")
                return [], []
            arr = self._parse_json_array(raw)
            if not arr:
                self.add_thinking("细分", f"DecomposeAgent {cand.stem} 非 JSON 数组")
                return [], []
            intents, dropped = self._to_split_intents(arr, text)
            valid_stems = {c.stem.lower() for c in candidates}
            filtered = self._filter_intents(intents, candidates, valid_stems,
                                             path=f"并发({cand.stem})")
            return filtered, dropped

        all_intents: list = []
        all_dropped: list[str] = []
        _drop_lock = _threading.Lock()
        def _collect(res):
            if isinstance(res, tuple) and len(res) == 2:
                its, drp = res
                all_intents.extend(its or [])
                if drp:
                    with _drop_lock:
                        for _s in drp:
                            if _s not in all_dropped:
                                all_dropped.append(_s)
        if len(jobs) == 1:
            _collect(_run_one(jobs[0]))
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as ex:
                futures = [ex.submit(_run_one, j) for j in jobs]
                first_two = [f.result() or ([], []) for f in futures[:2]]
                _collect(first_two[0])
                _collect(first_two[1])
                if not first_two[0][0] and not first_two[1][0]:
                    _local_ce.set()
                    self.add_thinking("细分",
                        "DecomposeAgent 前 2 候选均空响应，取消剩余并发候选，降级兜底")
                else:
                    for f in futures[2:]:
                        _collect(f.result() or ([], []))
        return all_intents, all_dropped

    # ── schema 构建 ─────────────────────────────────────────────

    def _read_schema_cached(self, p, stem: str, sheet: str) -> tuple[list, list]:
        """读表头+类型行，命中内存缓存跳过文件 I/O。

        配表 xlsx 结构静态（单进程内不变），同 session 多次 decompose（主路径+
        兜底+段级重跑）重复读同表头，缓存省 I/O。读异常返 ([],[]) 不抛。
        """
        key = (stem, sheet)
        cached = self._schema_cache.get(key)
        if cached is not None:
            return cached
        hdrs, trow = [], []
        try:
            hdrs = self._cli.read_header(p, sheet) or []
            trow = self._cli.read_type_row(p, sheet) or []
        except Exception:  # noqa: BLE001
            hdrs, trow = [], []
        result = (list(hdrs), list(trow))
        self._schema_cache[key] = result
        return result

    def _col_type_for(self, stem: str, sheet: str, col: str) -> str:
        """§P1-3.1 灌值按列类型判：查 stem/sheet 的 col 列类型（row2 规范名）。

        供 _to_split_intents 判"长叙述落在 int/float/bool 列→灌值；落在 str/描述列→合法保留"。
        复用 _read_schema_cached（内存缓存）。无 cli/读失败/列不匹配 → 返空串（保守，判为
        未知类型→不判灌值，避免误杀合法值）。
        """
        if not self._cli or not stem or not col:
            return ""
        # stem → path 缓存（list_tables 一次，后续查内存）
        if not hasattr(self, "_table_index_cache"):
            try:
                self._table_index_cache = {p.stem: p
                                          for p in self._cli.list_tables()}
            except Exception:  # noqa: BLE001
                self._table_index_cache = {}
        p = self._table_index_cache.get(stem)
        if p is None:
            return ""
        if not sheet:
            # 无 sheet 时取首个业务 sheet
            try:
                sheets = self._cli.get_sheets(p)
                biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s]
                sheet = biz[0] if biz else ""
            except Exception:  # noqa: BLE001
                sheet = ""
        if not sheet:
            return ""
        hdrs, trow = self._read_schema_cached(p, stem, sheet)
        if not hdrs or not trow:
            return ""
        # §P1 缺陷B 修复：字段键同时比对 row1 显示名 + row2 冒号前规范名。
        # row1=中文显示名（"对话内容"），row2=规范名:类型（"prompt_text:string" 或
        # "options[0]:int"）。LLM 产出的字段键可能是 row1 或 row2 规范名，两路都查。
        # 类型取 row2 冒号后半段（"int"/"string"）；无冒号退回整串。
        col_clean = (col or "").split(":")[0].strip().lower()
        for h, t in zip(hdrs, trow):
            if not h:
                continue
            h_clean = (h or "").split(":")[0].strip().lower()
            t_clean = (t or "").split(":")[0].strip().lower()
            if h_clean == col_clean or t_clean == col_clean:
                # 类型取 row2 冒号后半段；无冒号退回整串
                _tv = str(t or "")
                if ":" in _tv:
                    return _tv.split(":", 1)[1].strip()
                return _tv
        # §P1 缺陷A 兜底：指定 sheet 查不到该列时，遍历该 stem 全部业务 sheet。
        # 修"splitter_baseline 产 SI 未设 sheet_hint / sheet_hint 盲取首个 sheet 致
        # _col_type_for 查不到列类型 → _scrub 不拦 → 叙述灌值漏到 Step3 写盘"。
        # 任一 sheet 命中该列且为数字/bool 类型即返（按列类型兜底，不绑业务词/测例）。
        try:
            _all_sheets = self._cli.get_sheets(p) if self._cli else []
            _biz_sheets = [s for s in (_all_sheets or [])
                           if s and "说明" not in s and "CONFIG" not in s]
        except Exception:  # noqa: BLE001
            _biz_sheets = []
        for _alt_sheet in _biz_sheets:
            if _alt_sheet == sheet:
                continue
            _ah, _at = self._read_schema_cached(p, stem, _alt_sheet)
            for h, t in zip(_ah, _at):
                if not h:
                    continue
                h_clean = (h or "").split(":")[0].strip().lower()
                t_clean = (t or "").split(":")[0].strip().lower()
                if h_clean == col_clean or t_clean == col_clean:
                    _tv = str(t or "")
                    if ":" in _tv:
                        return _tv.split(":", 1)[1].strip()
                    return _tv
        return ""

    def _build_schema_block(self, candidates: list[CandidateTable],
                             text: str = "", column_signal=None) -> str:
        """读每表所有业务 sheet 的 row1+row2 表头,构 schema 块。

        Token 预算裁剪:每表 sheet 数 / 列数可配置(防 prompt 膨胀致超时)。

        §增强：sheet 优先级排序。当 text 非空时，命中 text 列名/显示名的 sheet
        排前（避免多 sheet 表如 item(12 sheet) 因 max_sheets 截断把目标 sheet
        Fabao 切掉，致 LLM 无"阴阳属性权重"列名产空）。命中数相同的保持原顺序。

        §Step1 列名信号增强：column_signal（ColumnExtractor 产出）的 hits 含
        "列名→sheet"反查命中，据此对目标 sheet 加权排序，确保 LLM 必见命中 sheet
        的 schema（修复案例二/三目标 sheet 被截断）。
        """
        if self._cli is None:
            return ""
        import os as _os
        # §P1-2.5 schema 瘦身：按候选数动态调 sheets/cols。候选少（≤3）给 4 sheet
        # 保覆盖，候选多（>5）压 2 sheet 防 prompt 膨胀致超时。cols 同理。
        _n_cands = len(candidates)
        _default_sheets = "4" if _n_cands <= 3 else ("2" if _n_cands > 5 else "3")
        _default_cols = "16" if _n_cands <= 3 else ("8" if _n_cands > 5 else "12")
        max_sheets = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_SHEETS", _default_sheets)))
        max_cols = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_COLS", _default_cols)))
        # 列名信号：构建 (stem, sheet) -> 命中列名集合，供 sheet 排序加权
        sig_sheet_hits: dict[tuple[str, str], set[str]] = {}
        if column_signal is not None:
            for h in getattr(column_signal, "hits", []) or []:
                sig_sheet_hits.setdefault(
                    (h.stem, h.sheet), set()).add(h.column)
        all_tables = {}
        try:
            all_tables = {p.stem: p for p in self._cli.list_tables()}
        except Exception:
            return ""
        lines: list[str] = []
        for cand in candidates:
            p = all_tables.get(cand.stem)
            if p is None:
                continue
            try:
                sheets = self._cli.get_sheets(p)
            except Exception:
                continue
            biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s]
            # sheet 命中排序：读每 sheet 表头，列名/显示名在 text 中出现 → 命中数高优先
            # §列名信号：column_signal 反查命中的 sheet 额外加 hits 数权重
            if text or sig_sheet_hits:
                sheet_hits: list[tuple[int, int, str, list, list]] = []  # (hits, orig_idx, sheet, hdrs, trow)
                for idx, sh in enumerate(biz):
                    hdrs, trow = self._read_schema_cached(p, cand.stem, sh)
                    hits = 0
                    for h in hdrs:
                        if h and str(h) in text:
                            hits += 1
                    # §列名信号加权：该 sheet 在 column_signal 命中的列名数
                    sig_h = len(sig_sheet_hits.get((cand.stem, sh), set()))
                    hits += sig_h * 2  # 信号命中权重 ×2，压过纯文本子串
                    sheet_hits.append((hits, idx, sh, hdrs, trow))
                # 命中数降序，原顺序稳定
                sheet_hits.sort(key=lambda x: (-x[0], x[1]))
                ordered = sheet_hits
            else:
                ordered = []
                for idx, sh in enumerate(biz):
                    hdrs, trow = self._read_schema_cached(p, cand.stem, sh)
                    ordered.append((0, idx, sh, hdrs, trow))
            for _hits, _idx, sh, hdrs, trow in ordered[:max_sheets]:
                cols = []
                # 列名信号命中的列强制保留（优先级最高，防命中列被 max_cols 截断）
                sig_cols = sig_sheet_hits.get((cand.stem, sh), set())
                # (display_name, is_signal) 元组，命中列先排
                col_tuples = []
                for h, t in zip(hdrs, trow):
                    if not h:
                        continue
                    name = str(h) + (f"（{t}）" if t and str(t) != str(h) else "")
                    col_tuples.append((name, str(h) in sig_cols))
                # 命中列在前，其余按原顺序
                col_tuples.sort(key=lambda x: (not x[1],))
                # 命中列必留（即使超 max_cols），其余按 max_cols 上限补齐
                kept = [name for name, is_sig in col_tuples if is_sig]
                rest = [name for name, is_sig in col_tuples if not is_sig]
                # 命中列占额，剩余补到 max_cols
                rest_budget = max(0, max_cols - len(kept))
                cols = kept + rest[:rest_budget]
                if cols:
                    lines.append(f"- {cand.stem}/{sh}: " + " | ".join(cols))
        return "\n".join(lines) if lines else ""

    def _build_fk_block(self, fk_edges: list[FKEdge]) -> str:
        """构 FK 块:每条边 from.column → to.column。"""
        if not fk_edges:
            return "（无显式 FK）"
        lines = []
        for e in fk_edges:
            lines.append(f"  {e.from_stem}.{e.from_sheet}.{e.from_column} → "
                         f"{e.to_stem}.{e.to_sheet}.{e.to_column}")
        return "\n".join(lines)

    # ── LLM prompt ─────────────────────────────────────────────

    def _build_prompt(self, text: str, schema_block: str, fk_block: str,
                      column_signal=None, fill_stems: Optional[list[str]] = None) -> str:
        """构 LLM prompt:候选表 schema + 列名信号 + FK 链 + 指令 + 输出格式。

        §增强：①「每段必产≥1意图」硬约束 ② few-shot 示例 ③ 不确定时产保守意图而非空。
        §Step1 列名信号注入：column_signal 块告诉 LLM "用户输入提到这些列名，它们
        在这些表/sheet 出现"，LLM 据此选表选 sheet 选列（LLM 主导，信号为辅）。
        修复案例三 spirit 误路由——信号明确 "活动类型→activity"，LLM 应选 activity。
        """
        signal_block = self._build_column_signal_block(column_signal)
        signal_section = ""
        if signal_block:
            signal_section = (
                f"## 列名命中信号（规则反查，供你参考，最终决策权在你）\n{signal_block}\n\n"
                "提示：上面信号是规则从用户输入提取列名 token 后反查索引所得，"
                "表示用户提到的列名在这些表/sheet 出现。优先从信号命中的表选目标表，"
                "但要结合 schema 与指令语义判断——若信号表与指令意图不符，按指令为准。\n\n"
            )
        few_shot = ""
        try:
            _fs_fn = getattr(self.parser, "_build_few_shot_block", None) \
                if getattr(self, "parser", None) else None
            if callable(_fs_fn):
                few_shot = _fs_fn(text) or ""
        except Exception:
            few_shot = ""
        few_shot_section = f"{few_shot}\n\n" if few_shot else ""
        # 填表规则注入：rules/fill/*.md 用户手打知识（强约束，拼进 prompt）
        fill_rules = ""
        if fill_stems:
            try:
                from ..core.rules_loader import load_fill_rules
                fill_rules = load_fill_rules(fill_stems)
            except Exception:
                logger.debug("填表规则加载失败", exc_info=True)
        fill_rules_section = f"{fill_rules}\n\n" if fill_rules else ""
        return (
            few_shot_section +
            fill_rules_section +
            "你是配表跨表链分解器。一条指令可能涉及多张表(经外键关联)。"
            "请分解为每张表一个原子操作,用真实表头列名。\n\n"
            f"## 候选表 schema(row1 显示名,row2 规范名)\n{schema_block}\n\n"
            + signal_section +
            f"## 外键关联(决定 produces/consumes)\n{fk_block}\n\n"
            f"## 指令\n{text}\n\n"
            "## 输出 fenced JSON 数组,每元素一个原子操作:\n"
            "```json\n[{\"table\":\"<stem>\",\"sheet\":\"<sheet名>\",\"action\":\"add|set|delete|get\","
            "\"fields\":{<真实表头列名>:<值>},\"produces\":\"new_<stem>_id 或空\","
            "\"consumes\":{<列名>:\"<produces_label>\"},"
            "\"locator_field\":\"<set/delete 行定位列名 或空>\","
            "\"locator_value\":\"<set/delete 行定位值 或空>\","
            "\"locator_fields\":[<复合主键列名>],\"locator_values\":[<复合主键值>]}]\n```\n"
            "规则:\n"
            "- action 取值：add=新增行, set=修改行, delete=删除行, get=查询/查看。"
            "「查看/查询/显示/列出」→ get；「新增/添加/配一个」→ add；「修改/改为/设置」→ set；「删除/去掉」→ delete。"
            "get/delete 时 fields 可空，但 get/set/delete 查特定行时**必须**产 locator_field+locator_value"
            "（如「查看灵兽饕餮的属性」→ locator_field=\"名称\", locator_value=\"饕餮\"）。\n"
            "- 复合主键表（如 ResidenceEntry 用 residence_id+obstacle_id 双键定位行）："
            "产 locator_fields=[\"residence_id\",\"obstacle_id\"] + locator_values=[30005,10110]，"
            "此时 locator_field/locator_value 留空。单主键表仍用 locator_field/locator_value 单值。\n"
            "- ⚠ 不要调用任何工具，不要读取/搜索/打开任何文件(含上述 stem 对应的 xlsx)，"
            "严格仅基于本 prompt 文本中已给出的 schema 文本作答\n"
            "- ⚠【硬约束】指令中每一个明确动作(新增/修改/删除/查询)都必须产出至少1条意图。"
            "宁可产保守意图(仅填指令明确给的列,其余列留空待 Step2 校验补)也不要漏。"
            "若指令含「同时/然后/并且/再配」等连接,每个子句都要产对应意图,不可合并丢弃。\n"
            "- fields 键必须用上面 schema 的真实表头列名(row1 显示名)。"
            "**若 schema 列含点分规范键（括号内为 a.b.C 形式，如「体力资质（aptitude_base.StrPotCon）」），"
            "fields 键用点分规范键（aptitude_base.StrPotCon）而非中文显示名**，"
            "确保嵌套字段（aptitude_base.StrPotCon / attributes.HPMaxCon）精确写入。\n"
            "- 新增行若主键自动(未在指令给)→ produces=\"new_<stem>_id\"。"
            "⚠【主键列占位符硬约束】当 produces 标了 new_<stem>_id 时，该表的主键列"
            "（schema 第1列，通常叫 XXid/XX编号/item_id 等）的 fields 值**必须**填"
            " \"<produces_label>\" 占位符（如 \"<new_item_id>\"），**绝不能留空字符串 \"\"**。"
            "系统会按拓扑序先产出真实主键值后自动回填该占位符。留空会导致主键列写空值失败。\n"
            "- 引用他表新产出的 ID → 该字段值用 \"<produces_label>\" 占位符,并在 consumes 标注\n"
            "- ⚠【同表多行互相引用】当同一 sheet 产出多行且彼此引用(如对话树的多个句子/"
            "选项、多级进化链、多段关卡)时,每行 produces 必须用**唯一**标签"
            "(如 conv_root_id / conv_prove_id / opt_try_id,而不是都叫 new_<stem>_id),"
            "引用方在 consumes 里精确写目标那一行的唯一标签。标签重名会被当成同一行→"
            "引用串到错行或形成假环。允许前向引用(先声明的行引用后声明的行),"
            "系统会按依赖自动排序、被引用行先建。\n"
            "- set/delete 操作：用 locator_field+locator_value 标注定位行（如「删除活动名称为春节活动的行」→ "
            "locator_field=\"活动名称\", locator_value=\"春节活动\"），fields 仅放需修改的列（delete 可空）\n"
            "- 级联删除+反向引用清理：指令含「连带清掉/一并删掉/清理引用」时，"
            "每个相关表产独立 delete/set intent。如「删 quest 250003 + 清 quest 250002 的 next_quest_ids 引用 + "
            "删 spawn_quest_entity entity_prefab_id=10045 + 删 reward reward_id=10045」→ 产 4 条独立 intent："
            "① delete quest（locator_field=任务id, locator_value=250003）"
            "② set quest 清 next_quest_ids（locator_field=任务id, locator_value=250002, fields={next_quest_ids:空}）"
            "③ delete spawn_quest_entity（locator_field=entity_prefab_id, locator_value=10045）"
            "④ delete reward（locator_field=reward_id, locator_value=10045）。"
            "各表的 locator_field/locator_value 各自独立，不可合并到同一表。\n"
            "- ⚠ 同一表同一 sheet 相同 op+相同定位值+相同 fields 只产一条"
            "(不要因 consumes 占位符不同重复产同配置);"
            "但若指令对同一表同一 sheet 有多个不同业务子任务"
            "(如 BuildingInteract 的 idle+collect 多状态行,或不同行不同 locator_value),"
            "必须按真实业务子任务产多条,每条 fields 各自不同,不可合并丢弃\n"
            "- 不确定某列是否该填时:能从指令推断则填,不能则留空(\"\")由下游补,不要瞎编值\n"
            "- 仅输出 JSON 数组,不要解释,不要前后缀任何文字\n\n"
            "## 示例(跨表链,produces/consumes 占位符规范)\n"
            "指令:「新增主线任务叫封魔录,任务号60001,奖励包配一下」\n"
            "```json\n[{\"table\":\"quest\",\"sheet\":\"Quest\",\"action\":\"add\","
            "\"fields\":{\"任务名\":\"封魔录\",\"任务号\":\"60001\"},"
            "\"produces\":\"new_quest_id\",\"consumes\":{}},"
            "{\"table\":\"reward\",\"sheet\":\"Reward\",\"action\":\"add\","
            "\"fields\":{},\"produces\":\"new_reward_id\","
            "\"consumes\":{\"quest_id\":\"new_quest_id\"}}]\n```"
            + (
                "\n## 示例2(同 sheet 多行互引用:对话树→每行唯一 produces 标签,允许前向引用)\n"
                "指令:「点击弹出对话'你可愿一试?',选项'我愿一试'跳到第二句'先去证明实力',"
                "第二句给选项'我出发了'结束;另一选项'再想想'直接结束」\n"
                "```json\n[{\"table\":\"interaction\",\"sheet\":\"InteractionConv\",\"action\":\"add\","
                "\"fields\":{\"prompt_text\":\"你可愿一试?\",\"options[0]\":\"<opt_try_id>\","
                "\"options[1]\":\"<opt_think_id>\"},\"produces\":\"conv_root_id\","
                "\"consumes\":{\"options[0]\":\"opt_try_id\",\"options[1]\":\"opt_think_id\"}},"
                "{\"table\":\"interaction\",\"sheet\":\"InteractionConvOption\",\"action\":\"add\","
                "\"fields\":{\"option_text\":\"我愿一试\",\"option_function.data.1.conv_id\":\"<conv_prove_id>\"},"
                "\"produces\":\"opt_try_id\","
                "\"consumes\":{\"option_function.data.1.conv_id\":\"conv_prove_id\"}},"
                "{\"table\":\"interaction\",\"sheet\":\"InteractionConv\",\"action\":\"add\","
                "\"fields\":{\"prompt_text\":\"先去证明实力\",\"options[0]\":\"<opt_go_id>\"},"
                "\"produces\":\"conv_prove_id\",\"consumes\":{\"options[0]\":\"opt_go_id\"}},"
                "{\"table\":\"interaction\",\"sheet\":\"InteractionConvOption\",\"action\":\"add\","
                "\"fields\":{\"option_text\":\"我出发了\"},\"produces\":\"opt_go_id\",\"consumes\":{}},"
                "{\"table\":\"interaction\",\"sheet\":\"InteractionConvOption\",\"action\":\"add\","
                "\"fields\":{\"option_text\":\"再想想\"},\"produces\":\"opt_think_id\",\"consumes\":{}}]\n```"
                if ("InteractionConv" in schema_block or "interaction" in schema_block.lower()) else ""
            )
        )

    def _build_column_signal_block(self, column_signal) -> str:
        """构列名信号块：把 ColumnExtractor 产出格式化为 LLM 可读文本。

        格式：
          - 提取的列名 token: 名称, 活动类型, ...
          - 命中信号（按表聚合）:
            activity=0.85), 活动名称(score=0.85), 活动id(score=0.85)
            fabao/Fabao: 法宝描述(score=0.85), 名称(score=0.85)
            ...
        命中表按置信度降序，每表最多列 5 个，避免 prompt 膨胀。
        """
        if column_signal is None:
            return ""
        terms = getattr(column_signal, "extracted_terms", []) or []
        hits = getattr(column_signal, "hits", []) or []
        if not terms and not hits:
            return ""
        # 按表聚合命中
        by_stem: dict[str, list] = {}
        for h in hits:
            key = f"{h.stem}/{h.sheet}" if h.sheet else h.stem
            by_stem.setdefault(key, []).append(h)
        # 按最高 score 降序
        sorted_keys = sorted(by_stem.keys(),
                             key=lambda k: max(h.score for h in by_stem[k]),
                             reverse=True)
        lines = [f"提取的列名 token: {', '.join(terms[:15])}"]
        if by_stem:
            lines.append("命中（列名→表/sheet）:")
            for k in sorted_keys[:8]:  # 最多 8 个表，控制 token
                hs = by_stem[k][:5]  # 每表最多 5 列
                cols = ", ".join(f"{h.column}(score={h.score:.2f})" for h in hs)
                lines.append(f"  {k}: {cols}")
        return "\n".join(lines)

    # ── 解析 ───────────────────────────────────────────────────

    def _parse_json_array(self, raw: str) -> list:
        """从 LLM 返回解析 JSON 数组。容忍 fenced code block、裸 JSON、多数组、单 dict。"""
        # 1) fenced ```json [ ... ]```（显式组1）
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if not m:
            # 2) 裸 JSON 数组（也带组1，避免 m.group(1) 在无组正则上报 IndexError）
            m = re.search(r"(\[\s*\{.*\}\s*\])", raw, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(1))
                return arr if isinstance(arr, list) else []
            except ValueError:
                pass
        # 3) 单 dict（LLM 未包数组，仅产一个 op）→ 包装成 [dict] 接收，避免单 op 输出被丢弃
        md = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL) or \
            re.search(r"(\{.*\})", raw, re.DOTALL)
        if md:
            try:
                d = json.loads(md.group(1))
                if isinstance(d, dict):
                    return [d]
            except ValueError:
                pass
        return []

    def _to_split_intents(self, arr: list, text: str) -> tuple:
        """LLM JSON 数组 → (SplitIntent 列表, 丢弃 stem 列表)。

        consumes 字段值替换为 <label> 占位符。

        §叙述灌值检测：LLM 在大候选池/复杂输入下偶发退化，把整段叙述按位置切片塞进
        fields（键是数字索引/列序号，值是含中文标点的长叙述片段，非结构化数据）。这类
        垃圾 intent 进 Step3 必然 type 校验失败→execute_failed_no_llm，污染失败清单 +
        浪费 ask 轮次。检测到即加入 dropped_stems 返回，由上层触发单表 schema 重拆
        （小 schema → LLM 产出质量好），不直接进 Step3。通用判据（值长度+内容特征），
        不绑业务词。
        """
        SI = _SplitIntent()
        intents: list = []
        dropped_stems: list[str] = []
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
            # O20g：set/delete 操作的行定位信号（locator_field/locator_value）
            # LLM 应对"删除活动名称为X的行"产 locator_field=活动名称, locator_value=X。
            # 缺失时下游 _run_set/_run_delete 会从 fields 兜底提取（保不崩）。
            loc_field = str(item.get("locator_field") or "").strip() or None
            loc_value = str(item.get("locator_value") or "").strip() or None
            # 复合主键：locator_fields/locator_values 数组（case5 ResidenceEntry 双键）
            loc_fields = item.get("locator_fields") or []
            loc_values = item.get("locator_values") or []
            if isinstance(loc_fields, list) and isinstance(loc_values, list):
                loc_fields = [str(x).strip() for x in loc_fields if str(x).strip()]
                loc_values = [str(x).strip() for x in loc_values if str(x).strip()]
            else:
                loc_fields, loc_values = [], []
            # consumes: 字段值替换为 <label> 占位符
            if isinstance(consumes, dict):
                for k, label in consumes.items():
                    if k in fields and label:
                        fields[k] = f"<{str(label).strip()}>"
            # §P1-3.1/3.2/3.3 灌值按列类型判 + 丢字段不丢整条：
            # LLM 偶发退化把长叙述塞进字段值。按列类型精准判：
            #   - 长叙述落在 int/float/bool 列 → 灌值（数字列不该含叙述）→ 清空该字段（置空
            #     待 Step2 补），不丢整条 intent（其余合法字段保留）
            #   - 长叙述落在 str/描述/text 列 → 合法保留（活动描述/对话文本本就长）
            #   - fields 键含纯数字索引 → LLM 退化产出（合法键是列名），整条丢（无法救字段）
            # 灌值字段占比高（≥半数非占位字段）→ 加入 dropped_stems 触发单表 schema 重拆
            # （小 schema → LLM 产出质量好），否则仅清空灌值字段继续。
            # 通用判据（列类型 + 值特征），不绑业务词/表/测例。
            _has_num_key = any(
                (isinstance(_k, int) or (isinstance(_k, str) and _k.strip().isdigit()))
                for _k in fields.keys())
            if _has_num_key:
                # 数字索引键 = LLM 退化整体产出，无法按字段救，整条丢触发重拆
                self.add_thinking("细分",
                    f"丢弃数字索引键 intent：{stem}/{sheet}（LLM 退化产出，"
                    f"将触发单表 schema 重拆）")
                if stem and stem not in dropped_stems:
                    dropped_stems.append(stem)
                continue
            _narr_in_scalar = 0
            _narr_cleared: list[str] = []
            for _fk in list(fields.keys()):
                _fv = fields[_fk]
                _fvs = str(_fv).strip() if _fv is not None else ""
                if len(_fvs) <= 30:
                    continue
                if not any(_p in _fvs for _p in "，。；：、！？"):
                    continue
                # 排除合法多值列表（"9101,9102,9103"）与 JSON 串
                _stripped = _fvs.replace(",", "").replace("，", "")
                if _stripped.lstrip("-").isdigit() or _fvs.startswith("[") or _fvs.startswith("{"):
                    continue
                # 按列类型判：仅 int/float/bool 标量列灌值才清空；str/描述列保留
                _ct = self._col_type_for(stem, sheet, str(_fk))
                _ctl = (_ct or "").lower()
                _is_scalar_num = ("int" in _ctl or "long" in _ctl
                                  or "float" in _ctl or "double" in _ctl
                                  or "number" in _ctl or "decimal" in _ctl)
                _is_scalar_bool = "bool" in _ctl
                if _is_scalar_num or _is_scalar_bool:
                    # 数字/布尔列灌了叙述 → 清空该字段（置空待 Step2 补）
                    fields[_fk] = ""
                    _narr_in_scalar += 1
                    _narr_cleared.append(str(_fk))
            if _narr_cleared:
                self.add_thinking("细分",
                    f"清空灌值字段 {stem}/{sheet}：{_narr_cleared[:6]}"
                    f"（长叙述落在数字/布尔列，已置空待 Step2 补，其余字段保留）")
            # 灌值字段占比高（≥半数非占位字段）→ 触发单表重拆（清空后 intent 信息太少）
            _non_ph_fields = [_fk for _fk, _fv in fields.items()
                              if str(_fv).strip() and not str(_fv).strip().startswith("<")]
            if _narr_in_scalar >= 2 and _non_ph_fields and \
                    _narr_in_scalar * 2 >= len(_non_ph_fields):
                self.add_thinking("细分",
                    f"灌值占比高 {stem}/{sheet}（{_narr_in_scalar}/{len(_non_ph_fields)} 非占位字段被清空），"
                    f"触发单表 schema 重拆补救")
                if stem and stem not in dropped_stems:
                    dropped_stems.append(stem)
                continue
            intents.append(SI(
                text=text, table_hint=stem, sheet_hint=sheet,
                action=act, fields=fields, produces=produces,
                locator_field=loc_field, locator_value=loc_value,
                locator_fields=loc_fields, locator_values=loc_values,
            ))
        return intents, dropped_stems

    def _filter_intents(self, intents: list, candidates: list[CandidateTable],
                        valid_stems: set, *, path: str) -> list:
        """过滤意图：候选内全保留；候选外但 alias/模糊命中真实表 stem 的保留为低置信。

        §优化⑤：避免定位漏的表直接丢弃。env CODEMAKER_DECOMPOSE_KEEP_ALIAS=1 灰度
        （默认关，保现状行为）。开时候选外 table_hint 若经 AliasMapping 解析到候选
        内某 stem 的别名，则原样保留（不重写 table_hint，下游 Step2 字段层校验接管）。
        """
        import os as _os
        keep_alias = _os.environ.get("CODEMAKER_DECOMPOSE_KEEP_ALIAS", "0") == "1"
        filtered: list = []
        dropped: list = []
        alias_map = None
        if keep_alias:
            try:
                from ..locator.alias_mapping import AliasMapping
                alias_map = AliasMapping.load()
            except Exception:
                alias_map = None
        for it in intents:
            th = (getattr(it, "table_hint", "") or "").lower()
            if th in valid_stems:
                filtered.append(it)
                continue
            kept = False
            if keep_alias and alias_map is not None and th:
                # th 是候选外 table_hint，查它是否是候选内某 stem 的别名
                # AliasMapping.mapping: alias -> file_path; Path(fp).stem 得真实 stem
                try:
                    from pathlib import Path as _P
                    # §增强：大小写不敏感查 alias（LLM 常返回首字母大写 table_hint）
                    th_raw = getattr(it, "table_hint", "") or ""
                    fp = alias_map.mapping.get(th_raw) \
                        or alias_map.mapping.get(th_raw.lower()) \
                        or alias_map.mapping.get(th_raw.capitalize())
                    if fp:
                        resolved_stem = _P(fp).stem.lower()
                        if resolved_stem in valid_stems:
                            filtered.append(it)
                            kept = True
                except Exception:
                    pass
            if not kept:
                dropped.append(getattr(it, "table_hint", ""))
        if dropped:
            self.add_thinking("细分",
                f"DecomposeAgent 过滤幻觉表 intent({path}): {dropped}")
        return filtered

    def _run_impl(self, prompt: str, skill_docs: list, context: dict):
        """SubAgent 接口适配:从 context 取 text+locator_result。"""
        text = context.get("text") or prompt
        locator_result = context.get("locator_result")
        if not locator_result:
            return None
        intents = self.decompose(text, locator_result)
        if not intents:
            return None
        # 转 dict fragment 供 base.run 包装
        return {
            "sql_or_ops": [{"action": i.action, "table_hint": i.table_hint,
                            "sheet_hint": i.sheet_hint, "fields": i.fields,
                            "produces": i.produces} for i in intents],
            "produces": None,
            "references": [],
            "split_intents": intents,
            "target_table": "",
            "target_sheet": "",
        }


__all__ = ["DecomposeAgent"]
