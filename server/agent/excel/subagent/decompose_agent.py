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
import os
import re
import threading
import time
from typing import Optional

from .base import SubAgent
from .llm_agent import LLMSubAgent
from .locator_agent import LocatorResult, CandidateTable, FKEdge

logger = logging.getLogger(__name__)


# §观测C：LLM IO 可观测 harness（env CODEMAKER_DECOMPOSE_TRACE=1 开）。
# 不绑业务词/表/测例，纯结构化遥测，定位"为何只产N条/字段缺失"的真实归因。
# 关闭时零开销（早判 env）。dump 截断防曝，仅 logger.info + 结构化 thinking 事件。
_TRACE_ON = os.environ.get("CODEMAKER_DECOMPOSE_TRACE", "0") == "1"

_DUMP_DIR = os.environ.get("CODEMAKER_DECOMPOSE_DUMP_DIR", "")
_TRACE_SEQ = 0


def _next_trace_seq() -> int:
    global _TRACE_SEQ
    _TRACE_SEQ += 1
    return _TRACE_SEQ


def _clip_s(s, n: int = 1800) -> str:
    """截断长字符串供 log（防 prompt/schema 全文刷屏）。"""
    s = str(s) if s is not None else ""
    return s if len(s) <= n else s[:n] + f"…<+{len(s) - n}B>"


def _dump_llm_io(site: str, prompt: str, raw: str, *, stems=None,
                 extra: dict = None) -> None:
    """LLM 调用 IO 落 log（env 开）。prompt/response 截断。

    若 CODEMAKER_DECOMPOSE_DUMP_DIR 指向可写目录，另存完整原文 + .meta.json，
    供离线复现（不经任何业务判据，纯 I/O 快照）。
    """
    if not _TRACE_ON:
        return
    seq = _next_trace_seq()
    _stems = list(stems) if stems else []
    logger.info(
        "[trace:%s #%d] stems=%s prompt≈%dB raw≈%dB\n"
        "--- PROMPT(snip) ---\n%s\n--- RAW(snip) ---\n%s",
        site, seq, _stems, len(prompt or ""), len(raw or ""),
        _clip_s(prompt, 1600), _clip_s(raw, 1600))
    if _DUMP_DIR and os.path.isdir(_DUMP_DIR):
        try:
            import json as _j
            tag = f"{site}_{seq:04d}"
            with open(os.path.join(_DUMP_DIR, f"{tag}.prompt.txt"),
                      "w", encoding="utf-8") as f:
                f.write(prompt or "")
            with open(os.path.join(_DUMP_DIR, f"{tag}.raw.txt"),
                      "w", encoding="utf-8") as f:
                f.write(raw or "")
            with open(os.path.join(_DUMP_DIR, f"{tag}.meta.json"),
                      "w", encoding="utf-8") as f:
                _j.dump({"site": site, "stems": _stems,
                         "prompt_bytes": len(prompt or ""),
                         "raw_bytes": len(raw or ""),
                         **(extra or {})}, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("dump_llm_io 落盘失败", exc_info=True)


def _push_telemetry(sink_cb, phase: str, payload: dict) -> None:
    """推结构化 thinking 事件（phase=__json:<kind>, detail=JSON 串）。

    sink_cb = DecomposeAgent.add_thinking（已含 thinking_sink 透传到前端 SSE）。
    前端/日志可据此渲染 decompose 产/留/丢清单，定位遗漏根因。
    """
    if not _TRACE_ON or sink_cb is None:
        return
    try:
        import json as _j
        sink_cb(f"__json:{phase}", _j.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


# 复用 cross_table_splitter.SplitIntent 结构(不重新定义,保兼容)
def _SplitIntent():
    """延迟导入避免循环 import。"""
    from ..core.cross_table_splitter import SplitIntent
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
                         prompt_template="跨表链分解产 SplitIntent[]",
                         default_phase="解析")
        self._cli = cli
        # §schema 内存缓存：(stem, sheet) -> (headers, type_row)。
        # 同 session 内主路径+兜底+段级重跑重复读同表头，缓存省 60-70% I/O。
        # 失效靠进程重启（配表 xlsx 结构静态，单进程内不变）。
        self._schema_cache: dict[tuple[str, str], tuple[list, list]] = {}
        # §schema block 本 run 缓存：同一 (candidates 签名 + column_signal 命中列集 +
        # budget 配置) 本 run 内只构一次，不重复读表/拼装/裁剪/打 thinking 日志。
        # 治"schema 贪心预算裁剪"日志反复刷屏（主路径+并发+段级+coverage+backfill
        # 多次调用同一 _build_schema_block，结果不变但每次都打印 → 用户看到 59 条同
        # 质裁剪日志，墙钟也浪费在重复拼装上）。schema 静态，跨 run 复用也安全。
        self._schema_block_cache: dict[tuple, str] = {}
        # §session 复用：主线程（串行路径）复用 self._sid；并行工作线程
        # 用 threading.local 独立 session，避免同 session 并发请求冲突。
        # 单条指令多次 decompose（主路径+重试+单表重拆）不再每次 create_session，
        # 省 1 次 HTTP 建会话 RTT/调用。
        self._tls = threading.local()
        # §P0 任务链分组跨组熔断：连续 N 组单 prompt 超时后短路剩余组 + 兜底链
        # 各级 LLM 调用，直接走 _splitter_baseline 零 LLM 兜底（治"一堆 timed out"）。
        # §跑通优先：N=env（默认 0 关闭熔断）。原默认 1 在 serve 慢时连环超时
        # 触发熔断后剩余组全跳 LLM 走零 LLM baseline，12 表任务链必产残缺。
        # 关熔断让每组都尝试，慢就慢，保产出完整性。
        # §跑通优先：N=env（默认 0 关闭熔断）。原默认 1 在 serve 慢时连环超时
        # 触发熔断后剩余组全跳 LLM 走零 LLM baseline，12 表任务链必产残缺。
        # 关熔断让每组都尝试，慢就慢，保产出完整性。
        try:
            self._chain_timeout_circuit = max(
                0, int(os.environ.get("CODEMAKER_DECOMPOSE_CHAIN_CIRCUIT", "0")))
        except (TypeError, ValueError):
            self._chain_timeout_circuit = 0
        self._chain_timeout_streak = 0

    def _chain_circuit_open(self) -> bool:
        """任务链超时熔断是否已打开（达到连续超时阈值）。"""
        return self._chain_timeout_streak >= self._chain_timeout_circuit > 0

    def _record_chain_timeout(self, resp_err: str) -> bool:
        """记录一次"可疑超时"（单 prompt 超时/空响应）。返回是否触发熔断。"""
        if self._chain_timeout_circuit <= 0:
            return False
        _is_timeout = any(_x in (resp_err or "").lower()
                          for _x in ("timed out", "timeout", "超时"))
        if _is_timeout or not (resp_err or ""):
            self._chain_timeout_streak += 1
        else:
            self._chain_timeout_streak = 0
        if self._chain_circuit_open():
            self.add_thinking(
                "细分",
                f"任务链连续 {self._chain_timeout_streak} 组超时，"
                f"熔断剩余 LLM 兜底，直接走零 LLM baseline（治连环 timed out）")
            return True
        return False

    def _ensure_session(self) -> str:
        """复用 codemaker session，避免每次 LLM 调用新建会话。

        主线程复用 self._sid；工作线程用线程局部 session。
        用空临时目录隔离（_isolated_empty_dir），避免 serve 端读资源目录致超时。
        """
        from .base import _isolated_empty_dir
        client = getattr(self.parser, "client", None)
        if client is None:
            return ""
        session_dir = _isolated_empty_dir()
        if threading.current_thread() is threading.main_thread():
            if getattr(self, "_sid", ""):
                return self._sid
            sr = client.create_session(directory=session_dir,
                                       model=getattr(self.parser, "model", ""))
            if getattr(sr, "ok", False):
                self._sid = sr.session_id
            return getattr(self, "_sid", "")
        sid = getattr(self._tls, "session_id", "")
        if sid:
            return sid
        sr = client.create_session(directory=session_dir,
                                   model=getattr(self.parser, "model", ""))
        if getattr(sr, "ok", False):
            sid = sr.session_id
            self._tls.session_id = sid
        return sid

    # ── 建议2：Step1 LLM 调用硬预算（默认 3，可回退 env=大数/0 关闭） ──
    def _init_step1_budget(self) -> None:
        """每次 Step1 decompose 入口重置 LLM 预算。CODEMAKER_STEP1_LLM_BUDGET 控制，
        默认 5（主拆分不经 gate；辅助 LLM：候选筛选/outline/表覆盖/dropped 重拆，单段
        最坏 outline1+表覆盖1+重拆后再 outline1 = 3-4 次，5 留余量防抖动）。超预算后
        _call_llm* 短路返 None，调用方走 baseline/partial，不再串行补洞。

        §多意图自适应：原默认 3 太紧，单段 dropped 重拆 + 兜底链 outline/表覆盖易触顶
        → 走 partial 致漏字段（效果差根因之一）。提到 5，覆盖单段最坏路径。env 仍可
        显式覆盖。主拆分 _decompose_single_prompt 直接调 client.prompt 不经 gate，
        预算只拦辅助/补洞 LLM。"""
        import os as _os
        from ..core.pipeline.llm_budget import LLMBudget
        try:
            _lim = int(_os.environ.get("CODEMAKER_STEP1_LLM_BUDGET", "5"))
        except (TypeError, ValueError):
            _lim = 5
        self._step1_budget = LLMBudget(_lim)
        self._budget_partial = False

    def _budget_gate(self) -> bool:
        """消费一次预算。返回 True=可调 LLM；False=预算耗尽（记 partial + trace）。"""
        b = getattr(self, "_step1_budget", None)
        if b is None:
            return True  # 未初始化预算（非 Step1 入口）→ 不限制
        if b.try_consume():
            return True
        self._budget_partial = True
        try:
            self.add_thinking(
                getattr(self, "_default_phase", "细分"),
                f"Step1 LLM 预算耗尽（上限 {b.limit} 次），跳过本次 LLM，"
                f"返回结构化 partial，不再串行补洞（治超时/墙钟爆炸）")
        except Exception:
            pass
        return False

    def _call_llm(self, prompt: str, timeout: int = 90):
        if not self._budget_gate():
            return None
        return super()._call_llm(prompt, timeout=timeout)

    def _call_llm_raw(self, prompt: str, timeout: int = 90):
        if not self._budget_gate():
            return None
        return super()._call_llm_raw(prompt, timeout=timeout)

    def decompose(self, text: str, locator_result: LocatorResult,
                  force_single: bool = False) -> list:
        """主入口:text + LocatorResult → SplitIntent[]。

        O1 重构:反转主次——候选表数 ≤ 阈值时走单 prompt 主路径(N→1,token 砍 50-70%),
        失败/产 <2 降级并发每表;候选表数 > 阈值时保持并发主路径(防单 prompt schema
        过大致超时空返),并发产 <2 触发单 prompt 兜底。阈值由 env 控制,默认 3。

        §P0 force_single：单任务链输入（split_multi_intent 判定的跨表任务链整段）
        候选表常 >阈值（12+ 表），走并发每表路径会让 LLM 只看到单表 schema，
        跨表 produces/consumes 链接全断（并发路径每表独立 prompt，无全链上下文）。
        单任务链必须整段单 prompt 全候选 schema 一起看才能拆出正确跨表链。
        force_single=True 时无视阈值，强制走单 prompt 主路径。

        §Step1 列名信号注入：locator_result.column_signal 透传给 _build_prompt +
        _build_schema_block，让 LLM 看着列名命中信号选表选 sheet 选列（LLM 主导，
        规则信号为辅）。修复案例三 spirit 误路由——LLM 看到"活动类型→activity"
        信号会选 activity 而非 spirit。

        Args:
            text: 用户原始指令
            locator_result: LocatorAgent 产出(候选表 + FK 边 + column_signal)
            force_single: 强制走单 prompt 全候选路径（任务链用）

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

        # 建议2：重置本次 Step1 LLM 预算（≤3 次，超则跳过补洞走 partial）
        self._init_step1_budget()
        # §P0 任务链超时熔断：新请求重置连击（防单实例跨请求污染）
        self._chain_timeout_streak = 0

        # §T10 重置本次链路已声明 produces_label 累积表（供分组 prompt 注入复用）
        self._declared_produces_vars = []
        # §T11 加载本次会话已归纳反模式（pending_review，同 session 内才注入，
        # 不跨 session 固化为规则库）作为"本次会话已知易错点"注入后续 Step1 prompt。
        # 数据源是 skill_updater 已有的结构化 dict（induce_anti_patterns 产出）。
        self._session_anti_patterns = []
        try:
            from ..core.skill_updater import get_skill_updater
            _su = get_skill_updater()
            for _ap in _su.load_pending_anti_patterns():
                _tp = getattr(_ap, "trigger_pattern", "") or ""
                _ra = getattr(_ap, "rationale", "") or ""
                if _tp:
                    self._session_anti_patterns.append(
                        {"trigger": _tp, "rationale": _ra})
        except Exception:
            logger.debug("T11 加载会话反模式失败(降级)", exc_info=True)

        # §去硬模板：领域链型确定性展开默认关闭（同 parse_agent.py 入口，理由一致）。
        # 可用 CODEMAKER_DECOMPOSE_DISABLE_DOMAIN=0 显式重新开启。
        if _os.environ.get("CODEMAKER_DECOMPOSE_DISABLE_DOMAIN", "1") != "1":
            try:
                _dom = self._try_domain_expander(text)
            except Exception:  # noqa: BLE001
                logger.warning("DecomposeAgent 领域展开器失败（回退 LLM 路径）",
                               exc_info=True)
                _dom = []
            if _dom:
                self.add_thinking("细分",
                    f"DecomposeAgent 领域链型确定性展开命中，产 {len(_dom)} 条意图"
                    f"（跳过 LLM decompose，根治超时/漏意图）")
                return _dom

        # §放宽：40→75，服务端真实响应普遍偏慢，40s 太紧一直 timeout；
        # 需要更松可再调大 env，或用 CODEMAKER_LLM_TIMEOUT_SCALE 全局按比例放宽。
        # §跑通优先：75→120。12 表全 schema 单 prompt serve 生成长 JSON 普遍
        # >75s，75s 必超时产空触发熔断。120s 覆盖 serve 实测响应分布。
        per_to = int(_os.environ.get("CODEMAKER_DECOMPOSE_TIMEOUT", "120"))
        candidates = locator_result.candidates
        # 候选分层裁剪（文档 #2/#3 "context 默认不注入"）：把 context 级噪声表
        # 从 schema 注入剔除，缩短 prompt。required/dependency 完整保留（不伤多表写入）。
        self._context_drop_stems = self._context_drop_set(locator_result)
        fk_block = self._build_fk_block(locator_result.fk_edges)
        column_signal = getattr(locator_result, "column_signal", None)
        # §知识增强：缓存 fk_edges 供 _build_schema_block 逐列标注 [PK]/[FK→...]，
        # 让 LLM 直接看到列间关系而非只看表头字符串，不引入硬编码业务规则
        # （数据源是 LocatorAgent 已算好的 fk_edges：声明式 json + 运行时列名模式
        # 推导，PK 来自 rules/validate 用户声明，均是数据驱动，非本函数硬判）。
        self._last_fk_edges = locator_result.fk_edges

        single_threshold = int(_os.environ.get(
            "CODEMAKER_DECOMPOSE_SINGLE_PROMPT_THRESHOLD", "3"))
        # §P0 force_single：任务链强制单 prompt（无视候选数阈值），
        # 全候选 schema 一起看才能拆出正确跨表 produces/consumes 链。
        use_single = force_single or len(candidates) <= single_threshold
        # §P0 任务链超时放宽：force_single 单 prompt 要拆 12+ 表全链，40s 不够
        # （case0 实测 40s 超时空返 → baseline 兜底产残缺 7 子任务）。90s 是
        # 原 decompose 整句超时值，任务链单 prompt 场景复用。
        if force_single and len(candidates) > single_threshold:
            per_to = max(per_to, int(_os.environ.get(
                "CODEMAKER_DECOMPOSE_CHAIN_TIMEOUT", "120")))
        # §P0 链式分组拆分：任务链候选 12+ 表时，单 prompt 全 schema 超时/空返
        # （case0 实测 12 表 120s 仍空响应）。但单表/小 schema 单 prompt 能正常
        # 产出（entity_prefab 单表重拆产 5541 字符完整链）。折中：按 FK 链分组
        # 后每组 ≤6 表跑单 prompt（全文本 + 该组 schema），组间 produces/consumes
        # 标签由全文本上下文保证一致，跨组引用不受影响（LLM 两组都看到全文）。
        # 阈值 CODEMAKER_DECOMPOSE_CHAIN_GROUP 默认 4（实测 4-5 表单 prompt 稳定产出，
        # 6 表超时）。
        _chain_group = int(_os.environ.get("CODEMAKER_DECOMPOSE_CHAIN_GROUP", "3"))
        force_grouped = (force_single and len(candidates) > _chain_group)
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
        if force_grouped:
            # §P0 链式分组：任务链 12+ 表单 prompt 超时，按组（≤6 表）跑单 prompt，
            # 每组全文本 + 该组 schema，产出合并。组间表不重叠（每表只出现在一组），
            # 无需去重；组内同 sheet 多行（对话树多句 conv/option）是合法多 intent，
            # 不能按 (table,sheet) 去重（会把对话树多句坍缩成一句）。
            # 注意 _decompose_single_prompt 返回类型不一致：失败返回 []（空列表），
            # 成功返回 (filtered, dropped) 二元组——需按类型分派（与 _merge 同口径）。
            # §跑通优先：2→4。原 2 组截断太早，12 表只跑 6 表就停产残缺。
            # 4 组覆盖 12 表全链，配合 timeout 120 每组有充足响应时间。
            _max_groups = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_CHAIN_GROUP_MAX", "4")))
            _groups_run = 0
            for _gi in range(0, len(candidates), _chain_group):
                if _groups_run >= _max_groups:
                    self.add_thinking("细分",
                        f"DecomposeAgent 链式分组达到上限 {_max_groups}，剩余候选交缺表对账/规则兜底")
                    break
                _groups_run += 1
                # 熔断打开：跳过剩余组，直接走零 LLM 兜底（治连环超时）
                if self._chain_circuit_open():
                    self.add_thinking("细分",
                        f"任务链超时熔断已打开，跳过剩余 "
                        f"{max(0, len(candidates) - _gi)} 候选表分组，交零 LLM 兜底")
                    break
                _chunk = candidates[_gi:_gi + _chain_group]
                _out = self._decompose_single_prompt(
                    text, _chunk, fk_block, per_to, column_signal=column_signal)
                if isinstance(_out, tuple) and len(_out) == 2:
                    _res, _drp = _out
                else:
                    _res, _drp = _out or [], []
                if _res:
                    # 本组成功产出 → 重置超时连击（继续后续组）
                    self._chain_timeout_streak = 0
                    all_intents.extend(_res)
                    # §T10 累积本组产出的 produces_label，供下一组 prompt 注入复用
                    for _it in _res:
                        _pl = (getattr(_it, "produces_label", None)
                               or (getattr(_it, "extras", None) or {}).get("produces"))
                        if _pl and str(_pl).strip():
                            _pls = str(_pl).strip()
                            if _pls not in self._declared_produces_vars:
                                self._declared_produces_vars.append(_pls)
                if _drp:
                    for _s in _drp:
                        if _s not in all_dropped:
                            all_dropped.append(_s)
            self.add_thinking("细分",
                f"DecomposeAgent 链式分组 {len(candidates)} 表→"
                f"{((len(candidates) + _chain_group - 1) // _chain_group)} 组"
                f"单 prompt，产出 {len(all_intents)} 条意图")
        elif use_single:
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

            def _retry_dropped_one(_rs: str):
                _rc = cand_by_stem.get(_rs)
                if _rc is None:
                    # 候选外 stem，构造单表候选。sheet 留空让 schema 读全部业务 sheet。
                    # 注意：这会把该 stem 全 sheet（含对话/选项等无关 sheet）拉进 prompt，
                    # LLM 看到无关 sheet schema 易幻觉产指令未提的意图（如任务链案例
                    # 误产对话树）。仅在原 candidates 未覆盖该 stem 时兜底，正常路径
                    # 走 cand_by_stem 命中带具体 sheet 的候选，schema 只读该 sheet。
                    _rc = CandidateTable(stem=_rs, sheet="", confidence=0.5,
                                          level="retry_single", matched_term="")
                try:
                    _rit, _rdrp = self._decompose_single_prompt(
                        text, [_rc], fk_block, per_to, column_signal=column_signal)
                except Exception:  # noqa: BLE001
                    _rit, _rdrp = [], []
                return _rs, _rit

            # §落地：串行 for 改并发（仿 _backfill_missing 同款 ThreadPool 模式）。
            # 原为逐 stem 串行等 per_to（40s）超时，N 个丢弃表就是 N×40s 叠加墙钟；
            # 改并发后墙钟收窄到"最慢的一个"，LLM 调用次数/成本不变。
            if len(retry_stems) <= 1:
                _dropped_results = [_retry_dropped_one(s) for s in retry_stems]
            else:
                from concurrent.futures import ThreadPoolExecutor as _TPE2
                import os as _os_dr
                _dr_workers = min(len(retry_stems),
                    int(_os_dr.environ.get("CODEMAKER_DECOMPOSE_WORKERS", "4")) or 1)
                with _TPE2(max_workers=_dr_workers) as _ex2:
                    _dropped_results = [f.result() for f in
                        [_ex2.submit(_retry_dropped_one, s) for s in retry_stems]]
            for _rs, _rit in _dropped_results:
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
        # 改用确定性 splitter_baseline（默认只剩通用 path b——ColumnExtractor
        # 候选表兜底，11 硬编码模板已默认关闭，见 _splitter_baseline 内部）。
        # 保链路完整走通——LLM 不稳时仍能产可执行 intent，不依赖 serve 健康。
        # 可用 CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK=0 额外重开 11 模板。
        if not all_intents:
            fb = self._splitter_baseline(text, candidates, locator_result.fk_edges)
            if fb:
                self.add_thinking("细分",
                    f"DecomposeAgent LLM 路径产空,零 LLM 兜底产 {len(fb)} 条"
                    f"(ColumnExtractor 通用兜底)")
                all_intents = fb
        # §分段单 prompt 兜底（与 splitter_baseline 互补）：仍空时先试 §A Outline
        # Planner（LLM 拆 operation，带 op_id + raw_span），outline 产 ≥2 段时按段
        # 逐个 decompose_segment 并把 op_id 标回 SplitIntent，供 Step2/Step4 按 op
        # 归因。outline 产 <2/失败时原样回退 split_multi_intent 正则分段，零回归。
        if not all_intents and not self._chain_circuit_open():
            try:
                outline_ops = self._llm_outline_operations(text)
            except Exception:  # noqa: BLE001
                logger.debug("Outline Planner 异常", exc_info=True)
                outline_ops = []
            if len(outline_ops) >= 2:
                seg_intents = []
                _op_metrics = []  # §三.3 可观测：每 op 的 schema_chars/candidate_count/llm_ms
                for op in outline_ops:
                    if self._chain_circuit_open():
                        break
                    seg_text = op["raw_span"]
                    pruned = self._prune_segment_candidates(
                        seg_text, candidates, column_signal,
                        locator_result.fk_edges)
                    seg_fk = self._build_fk_block(locator_result.fk_edges)
                    # 仅 trace 开时才多算一次 schema block 长度用于观测（纯字符串
                    # 拼装，无 LLM 调用；关闭时零额外开销）。
                    _schema_chars = (
                        len(self._build_schema_block(
                            pruned, text=seg_text, column_signal=column_signal) or "")
                        if _TRACE_ON else 0)
                    _t0 = time.time()
                    seg_out = self._decompose_single_prompt(
                        seg_text, pruned, seg_fk, per_to,
                        column_signal=column_signal)
                    _llm_ms = int((time.time() - _t0) * 1000)
                    seg_its = seg_out[0] if isinstance(seg_out, tuple) else seg_out
                    for _it in (seg_its or []):
                        try:
                            _it.op_id = op["op_id"]
                        except Exception:
                            pass
                    if seg_its:
                        seg_intents.extend(seg_its)
                    _op_metrics.append({
                        "op_id": op["op_id"],
                        "candidate_count": len(pruned),
                        "schema_chars": _schema_chars,
                        "llm_ms": _llm_ms,
                        "produced": len(seg_its or []),
                    })
                if seg_intents:
                    self.add_thinking("细分",
                        f"DecomposeAgent 全表 prompt 产空，Outline Planner 拆 "
                        f"{len(outline_ops)} 个 operation 兜底产 {len(seg_intents)} 条")
                    all_intents = seg_intents
                _push_telemetry(self.add_thinking, "outline_op_metrics",
                                {"ops": _op_metrics})
        if not all_intents and not self._chain_circuit_open():
            try:
                from ..parser.multi_intent_splitter import split_multi_intent
                segs = split_multi_intent(text)
                if segs and len(segs) > 1:
                    seg_intents: list = []
                    for seg_text in segs:
                        if self._chain_circuit_open():
                            break
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
        # 单对象输入：多条同表意图通常是把字段误拆成行，先合并再进入补漏/自检。
        if all_intents and not self._should_expand_missing_tables(text):
            before_merge = len(all_intents)
            all_intents = self._coalesce_single_object_intents(all_intents)
            if len(all_intents) != before_merge:
                self.add_thinking("细分",
                    f"单对象字段合并：{before_merge}→{len(all_intents)} 条意图")
        # §缺表覆盖对账 + 定向单表重拆补漏（Step1 兜底，纯增量，missing 空则零开销）
        if all_intents and not self._chain_circuit_open() and self._should_expand_missing_tables(text):
            all_intents = self._backfill_missing(
                text, all_intents, candidates, locator_result.fk_edges,
                fk_block, per_to, column_signal)
        # §ReAct 表级回读自检：规则集合差（_backfill_missing）兜不住的语义性遗漏，
        # 交给 LLM 自己看着候选池+FK图复核一遍（受 Step1 LLM 预算门控，预算耗尽
        # 自动跳过，不额外叠调用）。默认开，CODEMAKER_DECOMPOSE_TABLE_SELFCHECK=0
        # 关闭。
        if all_intents and not self._chain_circuit_open() and self._should_expand_missing_tables(text):
            all_intents = self._llm_verify_table_coverage(
                text, all_intents, candidates, locator_result.fk_edges, per_to)
        # §Step1 解析层健康自检 + 保守回填（dict 残留/consumes 标签悬空）：
        # 把 LLM 产出质量问题在 Step1 就抓出/修正，不流到 Step3 才爆。零 LLM，
        # 边界独立。返回修正/告警条数供观测。
        if all_intents:
            _lint_n = self._lint_split_intents(all_intents, locator_result.fk_edges)
            if _lint_n:
                self.add_thinking("细分",
                    f"Step1 lint 修正/告警 {_lint_n} 项（dict 残留/标签悬空）")
        # §观测C：decompose 全程汇总遥测——候选 vs 最终产出，定位遗漏在哪个环节。
        # 不绑业务词/表/测例，纯集合差运算。force_single/分组模式/兜底路径均覆盖。
        if _TRACE_ON:
            _cand_stems = sorted({str(getattr(c, "stem", "") or "").lower()
                                   for c in candidates})
            _prod_stems = sorted({str(getattr(it, "table_hint", "") or "").lower()
                                   for it in all_intents})
            _missing = sorted(set(_cand_stems) - set(_prod_stems))
            _extra = sorted(set(_prod_stems) - set(_cand_stems))
            _push_telemetry(self.add_thinking, "decompose_summary", {
                "path": "grouped" if force_grouped
                        else ("single" if use_single else "parallel"),
                "candidates": _cand_stems,
                "produced": _prod_stems,
                "still_missing": _missing,
                "off_candidate": _extra,
                "total": len(all_intents)})
        return all_intents

    def _should_expand_missing_tables(self, text: str) -> bool:
        """是否允许 Step1 缺表补漏/覆盖自检扩意图。"""
        t = str(text or "")
        if not t.strip():
            return False
        multi_markers = (
            "同时", "然后", "接着", "之后", "并且", "另外", "以及", "还有",
            "再配", "再建", "再加", "分别", "每个", "各自", "多个", "若干",
            "两条", "三条", "四条", "两种", "三种", "两个", "三个", "四个",
            "第一", "第二", "第三", "选项", "奖励", "战斗", "对话", "任务链",
        )
        if any(m in t for m in multi_markers):
            return True
        if re.search(r"(?:\d+|[一二三四五六七八九十])[\.、)]\s", t):
            return True
        return False

    def _coalesce_single_object_intents(self, intents: list) -> list:
        """单对象场景：把同表同 sheet 同 action 的字段碎片合成一条。"""
        if not intents:
            return []
        out: list = []
        by_key: dict[tuple, object] = {}
        for it in intents:
            key = (
                str(getattr(it, "table_hint", "") or "").lower(),
                str(getattr(it, "sheet_hint", "") or "").lower(),
                str(getattr(it, "action", "") or "").lower(),
            )
            if key not in by_key:
                by_key[key] = it
                out.append(it)
                continue
            base = by_key[key]
            bf = getattr(base, "fields", None)
            nf = getattr(it, "fields", None)
            if isinstance(bf, dict) and isinstance(nf, dict):
                for k, v in nf.items():
                    if k not in bf or str(bf.get(k, "")).strip() == "":
                        bf[k] = v
            for attr in ("locator_field", "locator_value"):
                if not getattr(base, attr, None) and getattr(it, attr, None):
                    setattr(base, attr, getattr(it, attr))
        return out

    def _try_domain_expander(self, text: str) -> list:
        """领域链型确定性展开器分发。命中返回 SplitIntent[]，否则 []。

        目前覆盖：新建门派全链（school full-chain）。后续可扩展 quest chain 等。
        产出 SplitIntent 与 LLM/cross_table_splitter 同形（produces + <label> 占位），
        下游 Step1→Step4 无需特判。
        """
        try:
            from ..core.school_chain_expander import build_school_new_chain_intents
            its = build_school_new_chain_intents(text)
            if its:
                return its
        except Exception:  # noqa: BLE001
            logger.warning("school_chain_expander 失败", exc_info=True)
        return []

    def _llm_outline_operations(self, text: str) -> list[dict]:
        """§A Outline Planner 第一阶段：LLM 轻量拆 operation（不看 schema/候选表，
        只归纳"有几个操作、每个操作对应原文哪一段"），供多指令/长输入先 outline
        再逐段 grounded 分解（对应文档 T13 outline-then-expand）。

        不引入表名/列名/关键词硬编码——纯 LLM 结构化输出，判断依据是原文本身。
        受 Step1 LLM 预算门控（复用 _call_llm_raw 内部 _budget_gate），预算耗尽
        或调用失败均返回 []，调用方原样回退 split_multi_intent 正则分段，零回归。
        """
        prompt = (
            "把下面这条游戏配置指令拆成若干个独立操作(operation)。\n"
            "只需要判断一共有几个操作、每个操作对应原文里的哪一段文字、"
            "大致是什么动作类型，不要涉及任何表名/列名/schema（那是下一阶段的事）。\n"
            "只输出一个 JSON 数组，不要输出任何其它文字或解释。每个元素形如：\n"
            '{"op_id":"op_1","raw_span":"该操作对应的原文片段(尽量原样摘录，'
            '不要遗漏也不要跨操作重叠)","action":"add|set|delete|get",'
            '"mentioned_entities":["提到的实体/名称"],'
            '"mentioned_values":["提到的关键数值/ID/坐标等"]}\n'
            f"指令原文：\n{text}"
        )
        try:
            # §放宽：20→30，服务端整体偏慢时 20s 仍会顶满；30 更稳，仍可经
            # CODEMAKER_LLM_TIMEOUT_SCALE 全局再放宽。
            raw = self._call_llm_raw(prompt, timeout=30)
        except Exception:  # noqa: BLE001
            logger.debug("Outline Planner LLM 调用失败", exc_info=True)
            return []
        if not raw:
            return []
        if _TRACE_ON:
            _dump_llm_io("outline", prompt, raw, stems=[])
        try:
            arr = self._parse_json_array(raw)
        except Exception:  # noqa: BLE001
            arr = []
        ops: list[dict] = []
        for i, item in enumerate(arr or []):
            if not isinstance(item, dict):
                continue
            span = str(item.get("raw_span") or "").strip()
            if not span:
                continue
            op_id = str(item.get("op_id") or "").strip() or f"op_{i + 1}"
            action = str(item.get("action") or "add").strip().lower()
            if action not in ("add", "set", "delete", "get"):
                action = "add"
            ops.append({
                "op_id": op_id,
                "raw_span": span,
                "action": action,
                "mentioned_entities": item.get("mentioned_entities") or [],
                "mentioned_values": item.get("mentioned_values") or [],
            })
        return ops

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
        # 建议2：重置本段 Step1 LLM 预算（≤3 次，超则跳过补洞走 partial）
        self._init_step1_budget()
        # 段级 schema 小(单段+剪枝后候选表少)，不需整句级长 timeout。默认与
        # decompose 整句入口对齐（§放宽：40→75，服务端偏慢，避免一直 timeout）；
        # runner/env 可经 CODEMAKER_DECOMPOSE_TIMEOUT 进一步调整。
        # §跑通优先：75→120。12 表全 schema 单 prompt serve 生成长 JSON 普遍
        # >75s，75s 必超时产空触发熔断。120s 覆盖 serve 实测响应分布。
        per_to = int(_os.environ.get("CODEMAKER_DECOMPOSE_TIMEOUT", "120"))
        # 候选分层裁剪（文档 #2/#3 "context 默认不注入"）：剔除 context 级噪声表 schema。
        self._context_drop_stems = self._context_drop_set(locator_result)
        candidates = self._prune_segment_candidates(
            seg, locator_result.candidates,
            getattr(locator_result, "column_signal", None),
            locator_result.fk_edges)
        fk_block = self._build_fk_block(locator_result.fk_edges)
        column_signal = getattr(locator_result, "column_signal", None)
        # §知识增强：同 decompose() 入口，缓存 fk_edges 供逐列 [PK]/[FK→...] 标注。
        self._last_fk_edges = locator_result.fk_edges
        # 缓存 column_signal 供 _splitter_baseline 零 LLM 兜底用
        self._last_column_signal = column_signal
        self.add_thinking("细分",
            f"DecomposeAgent 段分解({len(candidates)}/{len(locator_result.candidates)} 候选,timeout={per_to}s)")
        # §Step1 timeout 治本：段级候选 > 阈值时走并发每表（每表 1 小 prompt，
        # 1 表 schema，token 极小不易 timeout），不再单 prompt 拼 N 表 schema。
        # 段内跨表 produces/consumes 靠 prompt 全文 + 占位符规范 + _assemble 后置
        # infer_produces_consumes 接（与 _parse_segments 多段路径同构）。
        # 阈值 CODEMAKER_DECOMPOSE_SEGMENT_PARALLEL_THRESHOLD 默认 2（≤2 表单 prompt
        # 够小，>2 表并发每表避 timeout）。设 0 强制全走单 prompt（回旧行为）。
        _seg_par_thr = int(_os.environ.get(
            "CODEMAKER_DECOMPOSE_SEGMENT_PARALLEL_THRESHOLD", "2"))
        if _seg_par_thr > 0 and len(candidates) > _seg_par_thr:
            self.add_thinking("细分",
                f"DecomposeAgent 段级并发每表({len(candidates)}>{_seg_par_thr}，"
                f"避单 prompt 拼 N 表 schema timeout)")
            seg_out = self._decompose_parallel(
                seg, candidates, fk_block, per_to, column_signal=column_signal)
            # _decompose_parallel 返回 list[intent]（非 tuple）
            intents = seg_out if isinstance(seg_out, list) else (seg_out[0] if isinstance(seg_out, tuple) else seg_out)
            dropped = seg_out[1] if isinstance(seg_out, tuple) and len(seg_out) > 1 else []
        else:
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
            _valid_dropped = [_rs for _rs in dropped if _rs]
            # §落地③：重拆是单表小 schema，不需要主 prompt 的整超时预算——命中
            # 噪声表时顶满 per_to(120s) 才判空，白等。收窄到独立的短超时，命中
            # 真表通常很快有响应，判空也快得多。
            _bf_to = min(per_to, int(_os.environ.get("CODEMAKER_DECOMPOSE_BACKFILL_TIMEOUT", "45")))

            def _retry_dropped(_rs: str):
                _rc = cand_by_stem.get(_rs) or CandidateTable(
                    stem=_rs, sheet="", confidence=0.5,
                    level="retry_single", matched_term="")
                try:
                    _rit, _ = self._decompose_single_prompt(
                        seg, [_rc], fk_block, _bf_to, column_signal=column_signal)
                except Exception:  # noqa: BLE001
                    _rit = []
                return _rs, _rit

            # §落地①同款并发：dropped 多 stem 时逐个串行重拆同样是叠加墙钟，
            # 改并发发出（各 stem 互不依赖，可安全并行）。
            if len(_valid_dropped) <= 1:
                _dropped_results = [_retry_dropped(_rs) for _rs in _valid_dropped]
            else:
                from concurrent.futures import ThreadPoolExecutor as _TPE2
                _workers2 = min(len(_valid_dropped),
                                int(_os.environ.get("CODEMAKER_DECOMPOSE_WORKERS", "4")) or 1)
                with _TPE2(max_workers=_workers2) as _ex2:
                    _dropped_results = [f.result() for f in
                        [_ex2.submit(_retry_dropped, _rs) for _rs in _valid_dropped]]
            for _rs, _rit in _dropped_results:
                if _rit:
                    self.add_thinking("细分",
                        f"段级单表重拆 {_rs} 产 {len(_rit)} 条意图")
                    intents.extend(_rit)
                else:
                    self.add_thinking("细分",
                        f"段级单表重拆 {_rs} 仍产空/叙述灌值，未能补救")
        # §零 LLM 兜底：段分解产空时走 _splitter_baseline（与主流程一致），
        # 保段级覆盖（多段指令某段 LLM 产空不漏）。
        # §去硬模板：原分支叙事例外（npc_dialogue/npc_composite 跳过模板兜底）
        # 是为规避 cross_table_splitter 固定形状模板套不上任意分支形状的问题；
        # 该硬编码模板路径已默认关闭（_splitter_baseline 现只保留通用 path b），
        # 无需再做这类叙事特判，统一走同一条通用兜底。
        # §用户要求关闭兜底（LLM 单次调用应能产出 JSON）：注释掉段级 _splitter_baseline
        # 兜底，LLM 段产空即保持空，由上层 Step1 段级对账记录该段漏覆盖（soft error），
        # 不再回退硬编码规则路径。若需恢复取消注释下方块。
        # if not intents:
        #     fb = self._splitter_baseline(seg, candidates, locator_result.fk_edges)
        #     if fb:
        #         self.add_thinking("细分",
        #             f"DecomposeAgent 段分解产空,零 LLM 兜底产 {len(fb)} 条")
        #         intents = fb
        # §框架级：段级大候选池单 prompt 超时产空（如 school 段 5 表 schema 过大），
        # splitter 模板又不覆盖新链型 → 该段整段漏产。这里对裁剪后候选逐表单表
        # 重拆（单表小 schema 不易超时），把每张真实动作主语表拆出来。仅产空时
        # 触发，不叠正常路径。限每表 1 次 + 候选 ≤6 防爆。
        if not intents and candidates:
            # §P0 兜底链修复：默认重拆 6 表（原 2），并按置信度降序先拆——
            # 候选池被噪声表挤占时动作主语表常排在第 3+ 位，限 2 表永远拆不到
            # 它（月华 fabao 无对应列产空即此）。候选表按 conf 排序，conf 高的
            # 是规则/列名强命中，最可能是动作主语，优先拆。
            _max_single = max(0, int(_os.environ.get("CODEMAKER_DECOMPOSE_SEG_SINGLE_MAX", "6")))
            _sorted_cands = sorted(candidates,
                                   key=lambda c: (getattr(c, "confidence", 0.0) or 0.0,
                                                  getattr(c, "level", "") == "column_extract"),
                                   reverse=True)
            _per_table = _sorted_cands[:6][:_max_single]
            self.add_thinking("细分",
                f"DecomposeAgent 段级超时产空,逐表单表重拆 {len(_per_table)} 候选")
            _bf_to2 = min(per_to, int(_os.environ.get("CODEMAKER_DECOMPOSE_BACKFILL_TIMEOUT", "45")))

            def _retry_cand(_cand):
                try:
                    _rit, _ = self._decompose_single_prompt(
                        seg, [_cand], fk_block, _bf_to2, column_signal=column_signal)
                except Exception:  # noqa: BLE001
                    _rit = []
                return _cand, _rit

            # §落地①同款并发：同一模式，候选间互不依赖，改并发发出。
            if len(_per_table) <= 1:
                _cand_results = [_retry_cand(_c) for _c in _per_table]
            else:
                from concurrent.futures import ThreadPoolExecutor as _TPE3
                _workers3 = min(len(_per_table),
                                int(_os.environ.get("CODEMAKER_DECOMPOSE_WORKERS", "4")) or 1)
                with _TPE3(max_workers=_workers3) as _ex3:
                    _cand_results = [f.result() for f in
                        [_ex3.submit(_retry_cand, _c) for _c in _per_table]]
            for _cand, _rit in _cand_results:
                if _rit:
                    intents.extend(_rit)
                    self.add_thinking("细分",
                        f"单表重拆 {getattr(_cand, 'stem', '')} 产 {len(_rit)} 条")
                if len(intents) >= 20:  # 安全上限，防无限叠
                    break
        # §速度1：删段级 backfill —— 原每段产空后都跑"缺表对账+单表重拆"串行 LLM，
        # 多段时累计 backfill × N × per_to 串行致墙钟爆（实测 N=10 段 backfill 占大半）。
        # 段级 backfill 职责上移到 ParseAgent._assemble 全局一次对账+一次重拆：
        # 各段产出汇合后用全局 candidates 做一次 expected/produced 对账，缺表一次性
        # 重拆补漏（vs 段级每段对各自窄候选重拆，重复+串行）。零回归：缺表补充由全局兜。
        return intents

    def _llm_rank_segment_candidates(self, seg: str, candidates: list) -> Optional[list]:
        """LLM 复核段内候选表，筛出"本段真正要产出数据"的表子集（供替代 _strength 规则排序）。

        §匹配层判定门修复：候选 >5 时原直接走 _strength 规则打分选 top N，
        规则打分对多分支叙事（对话树+多选项等）容易挑错组合——通用列/间接
        FK 共享把噪声表（reward/item/player_common 等）打到高分挤掉真正需要
        的完整链路表，组合过杂致 schema 过大超时。改为先问 LLM 哪些表本段
        真正需要产出数据，规则仅当 LLM 不可用/失败/结果不合法时兜底。

        Returns:
            LLM 判定相关的 CandidateTable 子列表（按 LLM 给出顺序，最多 8 个）；
            LLM 不可用/失败/结果为空/不合法 → None（调用方原样按 _strength 兜底）。
        """
        if not self.parser or not candidates:
            return None
        desc = "\n".join(
            f"- {getattr(c, 'stem', '')}（匹配到「{getattr(c, 'matched_term', '')}」，"
            f"来源:{getattr(c, 'level', '')}，置信度:{getattr(c, 'confidence', 0.0):.2f}）"
            for c in candidates)
        prompt = f"""你是配表指令的候选表筛选员。下面是这一段指令的候选表，里面混有
"本段真正要产出/写入数据的表"和"仅因同名列/间接外键关联被误召回的噪声表"。

## 本段指令
{seg}

## 候选表列表
{desc}

## 任务
判断哪些表是本段真正需要产出数据的表。若本段涉及多分支结构（如对话树+多个
选项、任务链多阶段等），链路上的每一张表都要列出，不要漏；确实无关的噪声表
（仅同名列/间接FK带出）不要列。

## 输出格式（只输出 JSON，不要 markdown 代码块，不要解释）
{{"relevant_stems": ["stem1", "stem2", ...]}}
按重要性排序，最多列出 8 个。"""
        data = self._call_llm(prompt, timeout=25)
        if not isinstance(data, dict):
            return None
        stems = data.get("relevant_stems")
        if not isinstance(stems, list) or not stems:
            return None
        by_stem = {getattr(c, "stem", ""): c for c in candidates}
        picked = [by_stem[s] for s in stems
                  if isinstance(s, str) and s in by_stem]
        return picked[:8] if picked else None

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
        seg_route_text = re.sub(r"['\"][^'\"]*['\"]", " ", seg)
        seg_route_lower = seg_route_text.lower()

        def _term_is_route_signal(term: str) -> bool:
            term = str(term or "").strip()
            if not term:
                return True
            return term.lower() in seg_route_lower

        # 1. column_signal hits stem（按命中列数加权）。
        #    §只统计强信号源（substring/exact/alias）：column_reverse 是列名反向
        #    索引命中（如「模型」反查 model_prefab 表 9 个含"模型"的列），是弱
        #    信号，不计入 sig_stems——否则噪声表凭大量弱命中挤掉真正的动作主语
        #    （school_ability 是 FK 目标，无列信号命中，会被 model_prefab 挤出）。
        sig_stems: dict[str, int] = {}  # stem -> 强信号命中列数
        _WEAK_SRC = {"column_reverse", "column_extract"}
        if column_signal is not None:
            for h in getattr(column_signal, "hits", []) or []:
                stem = getattr(h, "stem", "") or ""
                _src = (getattr(h, "source", "") or "").lower()
                if not stem:
                    continue
                if _src in _WEAK_SRC:
                    continue
                sig_stems[stem.lower()] = sig_stems.get(stem.lower(), 0) + 1
        # 2. 段文本子串匹配 stem
        text_stems: set[str] = set()
        for c in candidates:
            stem = (getattr(c, "stem", "") or "").lower()
            if stem and stem in seg_lower:
                text_stems.add(stem)
        # 2b. 高置信度语义命中（alias/substring 级）：locate 已用别名把「门派」
        # 路由到 school、「神通」路由到 ability，这类 conf≥0.8 的候选是动作主语
        # 直接命中（或强语义命中），比 stem 英文子串匹配（中文文本必然 miss）可靠。
        # 计入 direct_hits 供 FK 裁剪与保留，替代纯文本子串的漏判。
        semantic_stems: set[str] = set()
        for c in candidates:
            _lvl = (getattr(c, "level", "") or "").lower()
            _conf = getattr(c, "confidence", 0.0) or 0.0
            if _conf >= 0.8 and _lvl not in ("column_extract", "column_reverse",
                                              "fk_inferred", "fk_expanded"):
                if not _term_is_route_signal(getattr(c, "matched_term", "") or ""):
                    continue
                semantic_stems.add((getattr(c, "stem", "") or "").lower())
        # 3. FK 边端点表——只保「与段内命中表相关」的端点，不收全局噪声链
        # （如「战斗模型」命中 combat 后，combat→space 的 FK 会把 space 也拉进来，
        # 而 space 与动作主语无关）。先算文本/列名/语义命中表，再只收这些命中表
        # 参与的 FK 边两端。
        direct_hits = set(sig_stems) | text_stems | semantic_stems
        fk_stems: set[str] = set()
        for e in fk_edges or []:
            from_stem = (getattr(e, "from_stem", "") or "").lower()
            to_stem = (getattr(e, "to_stem", "") or "").lower()
            if not direct_hits:
                break
            if from_stem in direct_hits:
                fk_stems.add(to_stem)
            if to_stem in direct_hits:
                fk_stems.add(from_stem)
        # 无直接命中时，回退保留全部 FK 端点（保链路）
        if not direct_hits:
            for e in fk_edges or []:
                for attr in ("from_stem", "to_stem"):
                    v = getattr(e, attr, "") or ""
                    if v:
                        fk_stems.add(v.lower())
        # 合并命中表（semantic 高置信命中优先保留）
        hit_stems = set(sig_stems) | text_stems | fk_stems | semantic_stems
        if not hit_stems:
            return list(candidates)
        # §P1-1 候选超量裁剪：>4 表触发 LLM 复核 + 强度排序（原 >5）。文档实测
        # 4-5 表单 prompt 稳定、6 表超时；4-5 表区间此前既不裁剪也无 LLM 复核，
        # 直接单 prompt 拼 5 表 schema 是超时高风险盲区。阈值降到 >4 把 5 表场景
        # 纳入 LLM 复核保护（LLM 通常挑出更精简组合缩小 schema）。
        if len(candidates) > 4:
            # §匹配层判定门修复：先问 LLM 哪些表本段真正需要产出数据，只在
            # LLM 不可用/失败/结果空时才退回下方 _strength 规则排序（不回归
            # 现有行为）。根因：纯规则打分对多分支叙事（对话树+多选项等）
            # 容易挑错组合——噪声表（reward/item 等）挤掉真正需要的完整链路
            # 表（如 interaction 覆盖对话+选项多个 sheet），组合过杂致 schema
            # 超时，超时后摔进不懂语义的零 LLM 兜底模板拼错分支结构（实测：
            # 把整段奖励叙述塞进选项文字字段）。LLM 直接读段文本判断，比规则
            # 打分更贴合语义，且通常挑出更精简组合（缩小 schema，降低超时率）。
            _llm_picked = self._llm_rank_segment_candidates(seg, candidates)
            if _llm_picked:
                self.add_thinking("细分",
                    f"LLM 候选筛选(段级)：{len(candidates)}→{len(_llm_picked)}"
                    f"，替代规则 _strength 排序挑选真正需要的表")
                return _llm_picked
            # 排序策略（框架级，不绑业务词）：
            #   - 语义命中（alias 级 conf≥0.8）> 列名信号命中 > FK 一跳目标 > 其他
            #   - 语义命中里按候选原始置信度排序（action 主语最相关）
            #   - FK 一跳目标（被语义命中表直接引用的表）次之——这是真正的
            #     链路必需表（如 school 引用 school_ability），优先于远端传递表
            #     （school_spirit 引用 spirit 是二跳，spirit 是已有表不需 LLM 拆）
            def _strength(c):
                _s = (getattr(c, "stem", "") or "").lower()
                _sig = sig_stems.get(_s, 0)
                _txt = 1 if _s in text_stems else 0
                _sem = 4 if _s in semantic_stems else 0
                _explicit = 4 if _s and _s in seg_route_lower else 0
                # FK 一跳目标（被语义命中表直接引用）是链路必需节点，权重高于
                # 语义噪声表（如「模型」alias 命中 model_prefab 但非动作主语）。
                # 仅对非语义命中的 FK 目标加权（语义命中的 FK 目标已由 _sem 覆盖）。
                _fk1 = 3 if (_s in fk_stems and _s not in semantic_stems) else 0
                _lvl = (getattr(c, "level", "") or "").lower()
                _lvl_w = 0 if _lvl in ("column_extract", "column_reverse",
                                        "fk_inferred", "fk_expanded") else 1
                return (_explicit + _sem + _fk1 + _sig + _txt + _lvl_w,
                        getattr(c, "confidence", 0.0) or 0.0)
            ranked = sorted(candidates, key=_strength, reverse=True)
            # §落地②实测：topN=3 时真实需要的表(如 entity_prefab)常被挤出主 prompt，
            # 靠事后逐表 backfill 补（每次顶满超时代价高）；topN=10 反而让主 prompt
            # 本身 schema 过大直接超时空响应（更差）。topN=5 实测两段主 prompt 均
            # 一次产出成功、无需再靠 backfill 补真实表，backfill 只剩确认噪声表
            # （见下方 CODEMAKER_DECOMPOSE_BACKFILL_TIMEOUT 收窄超时）。
            _top_n = max(2, int(os.environ.get("CODEMAKER_DECOMPOSE_SEGMENT_TOPN", "5")))
            pruned = ranked[:_top_n]
            # 至少保 2 表防误裁
            if len(pruned) < 2:
                pruned = candidates[:_top_n]
            return pruned
        pruned = [c for c in candidates
                  if (getattr(c, "stem", "") or "").lower() in hit_stems]
        # 裁剪后 <2 表保链路不破：回退原候选
        if len(pruned) < 2:
            return list(candidates)
        return pruned

    def _splitter_baseline(self, text: str, candidates: list,
                           fk_edges: list) -> list:
        """零 LLM 兜底：LLM 路径产空时产确定性 intent。

        §去硬模板：原 path a（cross_table_splitter 11 模板，正则抽字段/produces/
        consumes，跳过 LLM 决策）已整体移除。_key_value_type_baseline（"第N条"
        句式正则硬编码）默认关闭，只保留通用 path b —— ColumnExtractor 候选表 →
        每表产 1 条 add intent，字段值靠真实表头锚定通用抽取（value_extractor），
        不含任何表名/业务词硬编码分支。表/sheet/列匹配交给 LLM 主链路（decompose
        的真实 LLM 调用）完成，本方法只在 LLM 彻底产空时兜底，不越权替 LLM 做
        业务判断。可用 CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK=0 显式重新
        开启 _key_value_type_baseline 兜底（供对照/紧急回退）。

        不调 LLM，不依赖 serve 健康。产空仍返 []（由 Step1 外层再回退）。
        """
        import os as _os_sb
        _template_fallback_disabled = _os_sb.environ.get(
            "CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK", "1") == "1"
        all_fb: list = []
        if not _template_fallback_disabled:
            kv_fb = self._key_value_type_baseline(text, candidates)
            if kv_fb:
                return kv_fb
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
            try:
                from ..parser.multi_intent_splitter import _detect_action as _detect_fb_action
                _fb_action = _detect_fb_action(text)
            except Exception:
                _fb_action = "add"
            if _fb_action not in {"add", "set", "delete", "get"}:
                _fb_action = "add"

            def _stem_mentioned(_stem: str) -> bool:
                hay = (text or "").lower()
                s = (_stem or "").lower()
                if not s:
                    return False
                return s in hay or s.replace("_", "") in hay.replace("_", "")

            candidate_stems = {
                (getattr(c, "stem", "") or "").lower() for c in candidates
            }
            for _stem in sorted(self._all_table_stems()):
                if _stem not in candidate_stems and _stem_mentioned(_stem):
                    candidates = list(candidates) + [CandidateTable(
                        stem=_stem,
                        sheet=self._default_sheet_for(_stem),
                        confidence=0.95,
                        level="explicit_table_name",
                        matched_term=_stem,
                    )]
                    candidate_stems.add(_stem)

            _loc_field = None
            _loc_value = None
            _loc_m = _re.search(
                r"(?P<field>[A-Za-z_]*id|[A-Za-z_]+_id)\s*(?:为|是|=)?\s*(?P<value>\d+)",
                text, _re.IGNORECASE)
            if _loc_m:
                _loc_field = _loc_m.group("field")
                _loc_value = _loc_m.group("value")
            _set_value = None
            if _fb_action == "set":
                _set_m = _re.search(
                    r"(?:改成|改为|设置成|设置为|设为|更新为)\s*['\"](?P<v>[^'\"]+)['\"]",
                    text)
                if _set_m:
                    _set_value = _set_m.group("v")

            for cand in candidates:
                stem = getattr(cand, "stem", "") or ""
                if not stem or stem.lower() in existing_stems:
                    continue
                if _fb_action in {"set", "delete", "get"} and not _stem_mentioned(stem):
                    # §get 意图中文表名兜底：中文输入用"灵兽"而非英文 stem "pet"，
                    # _stem_mentioned 按 stem 英文名匹配会误跳。候选若是 alias 级
                    # 强命中（matched_term 是中文别名如"灵兽"），视为已提及，不跳过。
                    _mt = str(getattr(cand, "matched_term", "") or "")
                    _lvl = (getattr(cand, "level", "") or "").lower()
                    if _mt and (_mt in text or _lvl in ("alias", "explicit_table_name")):
                        pass  # 强命中候选，不跳过
                    else:
                        continue
                _lvl = (getattr(cand, "level", "") or "").lower()
                if _lvl in _WEAK_LEVELS and stem.lower() not in _producer_stems:
                    continue
                fields: dict = {}
                if _fb_action == "set" and _set_value:
                    if "名字" in text:
                        fields["名字"] = _set_value
                    elif "名称" in text:
                        fields["名称"] = _set_value
                if _fb_action == "add":
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
                    # §现象3 确定性字段抽取补强（header 锚定 "列名<分隔>值"）：
                    # 弱 baseline 只抓"列名紧邻数字"，漏掉 str 列的"列名：值"、引号名、
                    # 图标路径等。value_extractor 高精度（需真实表头名 + 分隔符命中原文，
                    # 几乎无歧义）补齐这些确定性字段，残余模糊字段仍留给 LLM/模板。
                    # 默认开，CODEMAKER_BASELINE_VALUE_EXTRACTOR=0 可回退。不绑表名/字段。
                    import os as _os_ve
                    if _os_ve.environ.get(
                            "CODEMAKER_BASELINE_VALUE_EXTRACTOR", "1") != "0":
                        try:
                            from ..core.pipeline.value_extractor import (
                                extract_fields_from_text)
                            _idx_cache = getattr(self, "_table_index_cache", None) or {}
                            _vp = _idx_cache.get(stem) or _idx_cache.get(stem.lower())
                            _vsheet = (getattr(cand, "sheet", "")
                                       or self._default_sheet_for(stem))
                            if _vp is not None and _vsheet:
                                _vhdrs, _ = self._read_schema_cached(_vp, stem, _vsheet)
                                if _vhdrs:
                                    _ve = extract_fields_from_text(
                                        text, _vhdrs, existing=fields)
                                    for _vc, _vv in (_ve.get("fields") or {}).items():
                                        if _vc not in fields:
                                            fields[_vc] = _vv
                        except Exception:  # noqa: BLE001
                            logger.debug("baseline value_extractor 补强失败",
                                         exc_info=True)
                cand_loc_field = _loc_field
                if cand_loc_field:
                    for e in fk_edges or []:
                        if ((getattr(e, "from_stem", "") or "").lower() == stem.lower()
                                and (getattr(e, "to_column", "") or "").lower() == cand_loc_field.lower()):
                            cand_loc_field = getattr(e, "from_column", "") or cand_loc_field
                            break
                # fields 全空 且已有模板产出时，不再追加候选空壳。
                # 模板已经覆盖了确定性链路；此时继续按 FK producer 补空 intent
                # 会把弱相关候选表（item/space/reward 等）臆造成写入任务。
                if not fields and existing_stems:
                    continue
                if _fb_action == "set" and not fields:
                    continue
                # fields 全空 且 非 FK 被依赖前置 → noise 候选，不产空壳 intent
                if _fb_action == "add" and not fields and stem.lower() not in _producer_stems:
                    continue
                # §get 意图 locator 补全：查询指令若无 id=数字，按"名称"列定位。
                # 从 text 提取实体名（引号内内容，或 matched_term 之后的中文词），
                # 填 locator_field=名称/name, locator_value=实体名。避免 LLM 自由
                # 编造 pet_grade 这类幻觉列作过滤条件。
                if _fb_action == "get" and not cand_loc_field:
                    _entity = None
                    # 优先：引号内的内容
                    _q = _re.search(r"['\"\u201c\u2018]([^'\"\u201d\u2019]+)['\"\u201d\u2019]", text)
                    if _q:
                        _entity = _q.group(1).strip()
                    # 次选：matched_term（如"灵兽"）之后紧邻的中文名词
                    if not _entity:
                        _mt = str(getattr(cand, "matched_term", "") or "")
                        if _mt and _mt in text:
                            _after = text[text.index(_mt) + len(_mt):]
                            _em = _re.search(r"^[\s,，的]*(?P<n>[\u4e00-\u9fff]{1,8})", _after)
                            if _em:
                                _entity = _em.group("n").strip()
                    if _entity:
                        cand_loc_field = "名称"
                        _loc_value = _entity
                all_fb.append(SI(
                    text=text, table_hint=stem,
                    sheet_hint=getattr(cand, "sheet", "") or "",
                    action=_fb_action,
                    fields=fields,
                    locator_field=cand_loc_field,
                    locator_value=_loc_value,
                    produces=(f"new_{stem}_id" if _fb_action == "add" and stem else None),
                ))
        return all_fb

    def _key_value_type_baseline(self, text: str, candidates: list) -> list:
        rows = self._extract_key_value_type_rows(text)
        if not rows or self._cli is None:
            return []

        def _norm(value) -> str:
            return re.sub(r"[\s_:\-./\\()\[\]{}]+", "", str(value or "").lower())

        def _role_for(header, type_name) -> str | None:
            names = {_norm(header), _norm(str(type_name or "").split(":")[0])}
            if names & {"key", "程序引用key", "程序引用关键字", "策划文档对照关键字"}:
                return "key"
            if names & {"value", "原文", "内容", "文本", "文案", "提示文案"}:
                return "value"
            if names & {"type", "类型", "分类"}:
                return "type"
            return None

        route_text = re.sub(r"['\"][^'\"]*['\"]|[“‘][^”’]*[”’]", " ", text or "")
        route_lower = route_text.lower()
        try:
            all_tables = {p.stem: p for p in self._cli.list_tables()}
        except Exception:
            return []

        options: list[tuple[tuple[float, float], CandidateTable, str, dict[str, str]]] = []
        for cand in candidates or []:
            stem = getattr(cand, "stem", "") or ""
            p = all_tables.get(stem)
            if p is None:
                continue
            try:
                sheets = [getattr(cand, "sheet", "")] if getattr(cand, "sheet", "") else self._cli.get_sheets(p)
            except Exception:
                sheets = []
            for sheet in sheets or []:
                if not sheet or "CONFIG" in str(sheet).upper() or "说明" in str(sheet):
                    continue
                hdrs, trow = self._read_schema_cached(p, stem, sheet)
                role_cols: dict[str, str] = {}
                for h, t in zip(hdrs, trow):
                    role = _role_for(h, t)
                    if role and role not in role_cols:
                        role_cols[role] = str(t or h).split(":")[0].strip() or str(h).strip()
                if not {"key", "value", "type"} <= set(role_cols):
                    continue
                term = str(getattr(cand, "matched_term", "") or "").strip().lower()
                level = str(getattr(cand, "level", "") or "").lower()
                stem_l = stem.lower()
                sheet_l = str(sheet).lower()
                route = 0.0
                if term and term in route_lower:
                    route += 4.0
                if stem_l and (stem_l in route_lower or stem_l.replace("_", "") in route_lower.replace("_", "")):
                    route += 3.0
                if sheet_l and sheet_l in route_lower:
                    route += 2.0
                if level not in {"column_extract", "column_reverse"}:
                    route += 1.0
                options.append(((route, float(getattr(cand, "confidence", 0.0) or 0.0)),
                                cand, str(sheet), role_cols))
        if not options:
            return []
        options.sort(key=lambda x: x[0], reverse=True)
        if len(options) > 1 and options[0][0][0] <= options[1][0][0] and options[0][0][0] < 2.0:
            return []
        if options[0][0][0] < 1.0 and len(options) > 1:
            return []

        _score, cand, sheet, role_cols = options[0]
        SI = _SplitIntent()
        stem = getattr(cand, "stem", "") or ""
        out = []
        for idx, row in enumerate(rows, start=1):
            out.append(SI(
                text=row.get("text") or text,
                table_hint=stem,
                sheet_hint=sheet,
                action="add",
                fields={
                    role_cols["value"]: row["value"],
                    role_cols["key"]: row["key"],
                    role_cols["type"]: row["type"],
                },
                produces=f"new_{stem}_{idx}_id" if stem else None,
            ))
        return out

    @staticmethod
    def _extract_key_value_type_rows(text: str) -> list[dict[str, str]]:
        if not text:
            return []
        marker = re.compile(r"第[一二三四五六七八九十百千万\d]+条")
        matches = list(marker.finditer(text))
        if not matches:
            return []
        global_type = None
        gm = re.search(r"type\s*都?\s*(?:填|用|为|是|=|:|：)?\s*['\"]?([A-Za-z0-9_\-\u4e00-\u9fff]+)",
                       text, re.IGNORECASE)
        if not gm:
            gm = re.search(r"类型\s*都?\s*(?:填|用|为|是|=|:|：)?\s*['\"]?([A-Za-z0-9_\-\u4e00-\u9fff]+)",
                           text, re.IGNORECASE)
        if gm:
            global_type = gm.group(1).strip().strip("'\"，,。；;")
        rows: list[dict[str, str]] = []
        for pos, match in enumerate(matches):
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
            chunk = text[match.start():end]
            vm = re.search(r"['\"]([^'\"]+)['\"]|[“‘]([^”’]+)[”’]", chunk)
            km = re.search(r"(?:程序引用\s*)?key\s*(?:用|为|是|=|:|：)\s*['\"]?([A-Za-z0-9_.\-]+)",
                           chunk, re.IGNORECASE)
            tm = re.search(r"(?:type|类型)\s*(?:填|用|为|是|=|:|：)?\s*['\"]?([A-Za-z0-9_\-\u4e00-\u9fff]+)",
                           chunk, re.IGNORECASE)
            value = (vm.group(1) or vm.group(2)).strip() if vm else ""
            key = km.group(1).strip().strip("'\"，,。；;") if km else ""
            typ = tm.group(1).strip().strip("'\"，,。；;") if tm else (global_type or "")
            if value and key and typ:
                rows.append({"text": chunk.strip(), "value": value, "key": key, "type": typ})
        return rows

    def _backfill_missing(self, text: str, intents: list, candidates: list,
                          fk_edges: list, fk_block: str, per_to: int,
                          column_signal=None) -> list:
        """缺表覆盖对账 + 定向单表重拆补漏（Step1 兜底，纯增量）。

        LLM 单 prompt 面对大 schema 会退化漏拆（BOSS 战斗段只拆 combat_data，
        漏 pve_combat_npc/entity_prefab/spawn；对话段只拆 Interaction，漏 conv/option）。
        本方法在 LLM 产出后对账 expected vs produced，缺的表用单表小 schema 重拆一次。

        expected 判据（通用，不绑业务词）：
          1. 非弱级候选表 (stem, sheet)——level 非 column_extract/column_reverse
             （alias/文件/sheet/llm/substring/fk_expanded 均算语义命中）
          2. FK 边两端 (stem, sheet)——覆盖同 stem 多 sheet（interaction 的
             InteractionConv/InteractionConvOption 经 Interaction 对话边进入）

        正常链路 produced 覆盖 expected → missing 空 → 零额外 LLM 调用。
        只在 LLM 漏拆时补跑小 schema（每 stem 重拆 1 次，限流防爆）。
        """
        if not intents or not candidates:
            return intents
        _WEAK = {"column_extract", "column_reverse"}
        expected: set[tuple] = set()
        cand_by_stem: dict[str, CandidateTable] = {}
        for c in candidates:
            stem = (getattr(c, "stem", "") or "").lower()
            if not stem:
                continue
            cand_by_stem.setdefault(stem, c)
            lvl = (getattr(c, "level", "") or "").lower()
            if lvl in _WEAK:
                continue
            expected.add((stem, (getattr(c, "sheet", "") or "").strip()))
        for e in fk_edges or []:
            fs = (getattr(e, "from_stem", "") or "").lower()
            ts = (getattr(e, "to_stem", "") or "").lower()
            if fs:
                expected.add((fs, (getattr(e, "from_sheet", "") or "").strip()))
            if ts:
                expected.add((ts, (getattr(e, "to_sheet", "") or "").strip()))
        produced = {((getattr(it, "table_hint", "") or "").lower(),
                     (getattr(it, "sheet_hint", "") or "").strip())
                    for it in intents}
        missing = expected - produced
        if not missing:
            return intents
        import os as _os
        _max = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_BACKFILL_MAX", "3")))
        missing_stems = sorted({s for s, _ in missing})[:_max]
        self.add_thinking("细分",
            f"DecomposeAgent 缺表对账：expected {len(expected)} sheet，"
            f"produced {len(produced)} sheet，缺 {missing_stems}")

        # §落地①：补漏重试改并发。原为 for 循环逐 stem 串行重拆，每次最多等
        # per_to（120s）超时，N 个缺表就是 N×120s 叠加墙钟（案例实测 3 个缺表
        # 撑到 600s+）。改线程池并发发出，同样的 LLM 调用次数/成本，墙钟时间
        # 收窄到"并发里最慢的一个"而非"逐个叠加"。
        # §落地③：重拆超时单独收窄（单表小 schema 不需整句超时预算，命中噪声
        # 表判空也不该等满 120s）。
        _bf_to3 = min(per_to, int(_os.environ.get("CODEMAKER_DECOMPOSE_BACKFILL_TIMEOUT", "45")))

        def _retry_one(_ms: str):
            _base = cand_by_stem.get(_ms)
            _rc = CandidateTable(
                stem=_ms, sheet="",
                confidence=getattr(_base, "confidence", 0.5) if _base is not None else 0.5,
                level="retry_single",
                matched_term=getattr(_base, "matched_term", "") if _base is not None else "",
            )
            try:
                _res = self._decompose_single_prompt(
                    text, [_rc], fk_block, _bf_to3, column_signal=column_signal)
                return _ms, (_res[0] if isinstance(_res, tuple) else _res)
            except Exception:  # noqa: BLE001
                return _ms, []

        if len(missing_stems) == 1:
            _results = [_retry_one(missing_stems[0])]
        else:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            _workers = min(len(missing_stems),
                           int(_os.environ.get("CODEMAKER_DECOMPOSE_WORKERS", "4")) or 1)
            with _TPE(max_workers=_workers) as _ex:
                _results = [f.result() for f in
                            [_ex.submit(_retry_one, _ms) for _ms in missing_stems]]

        _existing = {((getattr(it, "table_hint", "") or "").lower(),
                      (getattr(it, "sheet_hint", "") or "").strip())
                     for it in intents}
        for _ms, _rit in _results:
            if _rit:
                _added = 0
                for _ni in _rit:
                    _nk = ((getattr(_ni, "table_hint", "") or "").lower(),
                           (getattr(_ni, "sheet_hint", "") or "").strip())
                    if _nk in _existing:
                        continue
                    intents.append(_ni)
                    _existing.add(_nk)
                    _added += 1
                self.add_thinking("细分",
                    f"缺表重拆 {_ms} 补 {_added} 条意图")
            else:
                self.add_thinking("细分",
                    f"缺表重拆 {_ms} 仍产空（该表可能非动作主语，如 FK 引用目标）")
        return intents

    def _llm_verify_table_coverage(self, text: str, intents: list, candidates: list,
                                    fk_edges: list, per_to: int) -> list:
        """§ReAct 表级回读自检：把当前产出表清单 + 候选池的 FK 关联图丢回 LLM，
        问一遍"这些候选表里有没有该产但没产的"。

        与 _backfill_missing 的关键区别：_backfill_missing 用规则算 expected（候选
        非弱级 + FK 边两端）与 produced 做集合差，缺了就重拆——是"算法猜该补哪个"。
        本方法把同一份结构事实（候选表 + FK 图）连同当前产出**原样摊给 LLM**，让
        LLM 自己判断"看着这些表和它们的关联，你的产出是否漏了什么"——是"LLM 自己
        决定该补哪个"，不是规则代劳。两者互补：_backfill_missing 兜底纯结构性漏拆
        （FK 两端必须同时出现的硬约束），本方法补规则覆盖不到的语义性遗漏（如
        "指令提到刷新实体但没建 spawn_world_entity"，FK 图上 entity_prefab 未必
        直接连 spawn_world_entity，规则集合差找不到，需要 LLM 读语义判断）。

        §grounding 硬约束（防幻觉）：LLM 只能从**给定候选池**里选缺失表，不允许
        提出候选池外的新表名——同 _llm_complete_fields 的"允许列表"设计原则。
        返回的缺失 stem 会各自触发一次单表小 schema 重拆（真实 LLM 决定字段），
        不是本方法直接编字段。

        Args:
            text: 原始指令
            intents: 当前已产出的 SplitIntent 列表
            candidates: 候选表列表（grounding 边界，缺失表只能从这里选）
            fk_edges: 候选表之间的 FK 关系
            per_to: 单表重拆超时（复用主 decompose 的超时设置）

        Returns:
            intents（原地补充后返回；LLM 关闭/失败/无候选/无缺失 → 原样返回）。
        """
        import os as _os_tc
        if _os_tc.getenv("CODEMAKER_DECOMPOSE_TABLE_SELFCHECK", "1") == "0":
            return intents
        if not intents or not candidates or self.parser is None:
            return intents
        produced_stems = {(getattr(it, "table_hint", "") or "").strip().lower()
                           for it in intents if getattr(it, "table_hint", "")}
        # 候选池里"还没产出"的表——只有这些才可能被 LLM 判定为缺失（grounding 边界）
        unproduced = [c for c in candidates
                      if (c.stem or "").strip().lower() not in produced_stems]
        # §精确率反向检查不要求 unproduced 非空——候选已全部产出时仍可能存在
        # "弱信号凑巧混入的多余表"需要复核，不能因为没有缺表候选就整体跳过。
        # 只有"两边都没什么好查"（无未产出候选 且 产出表数 <=1，没有多余可言）才跳过。
        if not unproduced and len(produced_stems) <= 1:
            return intents

        produced_lines = []
        for it in intents:
            stem = getattr(it, "table_hint", "") or "?"
            sheet = getattr(it, "sheet_hint", "") or "?"
            prod = getattr(it, "produces", "") or ""
            produced_lines.append(f"- {stem}/{sheet}" + (f"（产出 {prod}）" if prod else ""))

        unproduced_lines = [f"- {c.stem}/{c.sheet or '?'}" for c in unproduced]

        fk_lines = []
        _prod_and_unprod = produced_stems | {(c.stem or "").lower() for c in unproduced}
        for e in fk_edges or []:
            fs = (getattr(e, "from_stem", "") or "").lower()
            ts = (getattr(e, "to_stem", "") or "").lower()
            if fs in _prod_and_unprod and ts in _prod_and_unprod:
                fk_lines.append(
                    f"- {getattr(e, 'from_stem', '')}.{getattr(e, 'from_column', '')} "
                    f"-> {getattr(e, 'to_stem', '')}.{getattr(e, 'to_column', '')}")

        prompt = (
            "你在做配表拆解的自我复核。下面是同一条指令，你刚才已经拆出的表清单，"
            "以及候选池里还没被你使用的表、和它们之间的外键关联图。请重新看一遍"
            "指令，做两件事：\n"
            "1）判断候选池里「还没用到的表」中，有没有其实应该产出但被你漏掉的"
            "（只能从「候选池未用表」里选，不允许提出这个列表之外的新表）。\n"
            "2）判断「你已产出的表」里，有没有其实指令根本没要求、只是弱信号"
            "凑巧混进来的多余表（比如只因为跟已产出表有外键关联、或候选池噪声，"
            "但原文根本没提到要新增/修改它的具体内容）。\n\n"
            f"## 指令\n{text}\n\n"
            f"## 你已产出的表\n" + "\n".join(produced_lines) + "\n\n"
            f"## 候选池里还没用到的表\n" + "\n".join(unproduced_lines) + "\n\n"
            + (f"## 这些表之间的外键关联\n" + "\n".join(fk_lines) + "\n\n" if fk_lines else "")
            + "判断标准：只有当指令原文明确要求新增/修改这张表的具体数据（能从原文"
              "找到对应的名称/id/描述等实际内容）才算需要；仅仅因为该表与其他表"
              "存在外键关联、或候选池里凑巧出现，并不代表指令真的要动它——这种情况"
              "既不算漏产也应该算多余。拿不准就都不填（宁可保留现状，不做改动）。\n"
            + '只输出 JSON：{"missing_stems": ["表名", ...], "extra_stems": ["表名", ...]}，'
              '都没有则都填 []。'
        )
        data = None
        try:
            data = self._call_llm(prompt, timeout=max(15, min(per_to, 25)))
        except Exception:  # noqa: BLE001
            logger.warning("ReAct 表级自检 LLM 调用异常，跳过", exc_info=True)
            return intents
        if not isinstance(data, dict):
            return intents

        # §精确率反向检查：产出表里被判定"多余"的，安全前提下移除（不是别的
        # intent 依赖的 FK producer 才移除——避免撕断真实跨表引用链）。
        extra = data.get("extra_stems")
        if isinstance(extra, list) and extra:
            _extra_norm = {s.strip().lower() for s in extra if isinstance(s, str)}
            _extra_norm &= produced_stems  # grounding：只能是已产出的表，不接受幻觉
            if _extra_norm:
                def _referenced_elsewhere(stem: str) -> bool:
                    _labels = {str(getattr(it, "produces", "") or "").strip()
                               for it in intents
                               if (getattr(it, "table_hint", "") or "").strip().lower() == stem
                               and getattr(it, "produces", "")}
                    _labels.discard("")
                    if not _labels:
                        return False
                    for other in intents:
                        if (getattr(other, "table_hint", "") or "").strip().lower() == stem:
                            continue
                        _f = getattr(other, "fields", None)
                        if not isinstance(_f, dict):
                            continue
                        for v in _f.values():
                            _vs = str(v)
                            if any(f"<{lb}>" in _vs for lb in _labels):
                                return True
                    return False

                _removed = []
                _kept_intents = []
                for it in intents:
                    _stem = (getattr(it, "table_hint", "") or "").strip().lower()
                    if _stem in _extra_norm and not _referenced_elsewhere(_stem):
                        _removed.append(_stem)
                        continue
                    _kept_intents.append(it)
                if _removed:
                    self.add_thinking("细分",
                        f"ReAct 表级自检：LLM 判定 {sorted(set(_removed))} 为多余表"
                        f"（弱信号凑巧混入,原文未要求,且非其它表FK依赖）,已剔除")
                    intents = _kept_intents

        missing = data.get("missing_stems")
        if not isinstance(missing, list) or not missing:
            return intents
        valid_stems = {(c.stem or "").strip().lower() for c in unproduced}
        confirmed = [s for s in missing
                     if isinstance(s, str) and s.strip().lower() in valid_stems]
        if not confirmed:
            return intents
        self.add_thinking("细分",
            f"ReAct 表级自检：LLM 判定候选池里漏产 {confirmed}，触发单表重拆补救")
        cand_by_stem = {c.stem: c for c in unproduced}

        def _verify_retry_one(_ms: str):
            _rc = cand_by_stem.get(_ms) or cand_by_stem.get(_ms.strip())
            if _rc is None:
                return _ms, []
            try:
                _res = self._decompose_single_prompt(
                    text, [_rc], self._build_fk_block(fk_edges), per_to)
                return _ms, (_res[0] if isinstance(_res, tuple) else _res)
            except Exception:  # noqa: BLE001
                return _ms, []

        if len(confirmed) <= 1:
            _vresults = [_verify_retry_one(s) for s in confirmed]
        else:
            from concurrent.futures import ThreadPoolExecutor as _TPE_TC
            _tc_workers = min(len(confirmed), 4)
            with _TPE_TC(max_workers=_tc_workers) as _ex_tc:
                _vresults = [f.result() for f in
                             [_ex_tc.submit(_verify_retry_one, s) for s in confirmed]]
        _existing_keys = {((getattr(it, "table_hint", "") or "").lower(),
                           (getattr(it, "sheet_hint", "") or "").strip())
                          for it in intents}
        for _ms, _rit in _vresults:
            if not _rit:
                self.add_thinking("细分",
                    f"ReAct 表级自检补拆 {_ms} 仍产空（可能确实不需要该表）")
                continue
            _added = 0
            for _ni in _rit:
                _nk = ((getattr(_ni, "table_hint", "") or "").lower(),
                       (getattr(_ni, "sheet_hint", "") or "").strip())
                if _nk in _existing_keys:
                    continue
                intents.append(_ni)
                _existing_keys.add(_nk)
                _added += 1
            if _added:
                self.add_thinking("细分",
                    f"ReAct 表级自检补拆 {_ms} 补 {_added} 条意图")
        return intents

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
        import time as _time_retry
        # §抖动容错 + 空数组重试：原来只在 LLM 返回空字符串（raw=""）时重试，
        # 对"LLM 返回合法 JSON 但内容是空数组 []"（确信地什么都不产）完全没有
        # 二次确认——这正是分段路径里"某段整体产空"复现（同输入多次跑，产空的
        # 段位置不同）的根因之一：单次采样的偶然空判定被直接当最终结果接受。
        # 改为：raw 为空 或 解析出的 arr 为空，都算"可疑空结果"，都在同一重试
        # 预算内重采样一次（自然由 LLM 自身非确定性产生不同样本，等价轻量
        # self-consistency：不做真正的多数投票，但至少给一次"换一次采样"的
        # 机会，比"死认第一次的空结果"更稳）。超时不重试（顶满仍会顶满，
        # 浪费墙钟），仅对"抖动/确信空判定"生效。
        # env：CODEMAKER_DECOMPOSE_SINGLE_RETRY 控制总重试次数（默认 1，同一
        # 预算覆盖两种触发条件，不叠加）。
        _RETRY_MAX = max(0, int(os.environ.get("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "1")))
        raw = ""
        _resp_err = ""
        arr: list = []
        for _attempt in range(_RETRY_MAX + 1):
            raw = ""
            _resp_err = ""
            try:
                session_id = self._ensure_session()
                if not session_id:
                    _resp_err = "create_session failed"
                else:
                    from .llm_gate import llm_throttle
                    with llm_throttle():
                        resp = client.prompt(session_id, prompt, timeout=per_to,
                                              model=getattr(self.parser, "model", ""),
                                              cancel_event=_ce)
                    self._bump_llm("decompose")
                    raw = getattr(resp, "response_text", "") or ""
                    _resp_err = str(getattr(resp, "error", "")
                                    or getattr(resp, "error_type", "") or "")
                if raw:
                    arr = self._parse_json_array(raw)
                    if arr:
                        # §T8 失败重试携带定向线索（opt-in）：
                        # CODEMAKER_DECOMPOSE_RETRY_HINT=1 时，首次产出非空但候选表的
                        # FK 列未被任何输出引用 → 构造定向线索拼入 prompt 重试一次。
                        # 线索内容来自 fk_edges/candidates 结构比对（代码算好的事实），
                        # 不是新写的业务判断规则。零 LLM 额外成本（仅可疑时触发）。
                        if (os.environ.get("CODEMAKER_DECOMPOSE_RETRY_HINT", "0") == "1"
                                and _attempt < _RETRY_MAX):
                            _hint = self._build_retry_hint(arr, candidates)
                            if _hint:
                                self.add_thinking("细分",
                                    f"§T8 定向线索重试：{_hint[:60]}...")
                                _hinted_prompt = prompt + "\n\n## 上一轮可能遗漏\n" + _hint
                                try:
                                    _sid2 = self._ensure_session()
                                    if _sid2:
                                        from .llm_gate import llm_throttle
                                        with llm_throttle():
                                            _resp2 = client.prompt(
                                                _sid2, _hinted_prompt, timeout=per_to,
                                                model=getattr(self.parser, "model", ""),
                                                cancel_event=_ce)
                                        self._bump_llm("decompose")
                                        _raw2 = getattr(_resp2, "response_text", "") or ""
                                        if _raw2:
                                            _arr2 = self._parse_json_array(_raw2)
                                            if _arr2 and len(_arr2) >= len(arr):
                                                arr = _arr2
                                                raw = _raw2
                                                break
                                except Exception:
                                    logger.debug("T8 定向重试失败(降级)", exc_info=True)
                        break  # 成功且非空 → 出循环
            except Exception as e:  # noqa: BLE001
                _resp_err = f"{type(e).__name__}: {e}"
                if _attempt >= _RETRY_MAX:
                    self.add_thinking("细分",
                        f"DecomposeAgent 单 prompt 调用失败({_resp_err})")
                    return [], []
            # 可疑空结果（空响应 或 合法但空数组）：超时不重试，其余重采样一次
            if _attempt < _RETRY_MAX:
                _is_timeout = any(_x in _resp_err.lower()
                                  for _x in ("timed out", "timeout", "超时"))
                if _is_timeout:
                    self._record_chain_timeout(_resp_err)
                    self.add_thinking("细分",
                        f"DecomposeAgent 单 prompt 超时空响应(stems={_stems}),"
                        f"不重试节省墙钟（重试仍顶满 timeout）")
                    break
                if not raw:
                    self.add_thinking("细分",
                        f"DecomposeAgent 单 prompt 空响应(stems={_stems}),"
                        f"抖动重试 attempt={_attempt+1}/{_RETRY_MAX}")
                else:
                    self.add_thinking("细分",
                        f"DecomposeAgent 单 prompt 返回合法空数组(stems={_stems}),"
                        f"重采样确认 attempt={_attempt+1}/{_RETRY_MAX}"
                        f"（防单次采样偶然判定为空）")
                _time_retry.sleep(1.5)  # 短退避 1.5s 待 serve 抖动恢复
                continue
        # §T7 双路并行采样 + 结构比对择优（对抗式自校验，opt-in）：
        # CODEMAKER_DECOMPOSE_DUAL_SAMPLE=1 时，首路成功后再并发采一路（同 prompt
        # 利用 LLM 自身非确定性），做结构一致性比对（表集合 + 字段集合）：
        #   - 一致 → 直接采用首路，零额外延迟感（并发耗时 ≈ 单路）
        #   - 不一致 → 取并集（两路都有的表/字段保留，仅一路有的也保留交后续 Step2
        #     校验判定，不在此处新写"谁对谁错"的业务规则）
        # 全程复用已有结构比对（表/字段集合），不新写业务判断逻辑。
        _DUAL = os.environ.get("CODEMAKER_DECOMPOSE_DUAL_SAMPLE", "0") == "1"
        if _DUAL and arr:
            try:
                import threading as _td
                from concurrent.futures import ThreadPoolExecutor as _TPE
                _second = {"arr": None, "raw": ""}
                def _sample_second():
                    try:
                        _sid = self._ensure_session()
                        if not _sid:
                            return
                        from .llm_gate import llm_throttle
                        with llm_throttle():
                            _resp = client.prompt(_sid, prompt, timeout=per_to,
                                                  model=getattr(self.parser, "model", ""),
                                                  cancel_event=_ce)
                        _second["raw"] = getattr(_resp, "response_text", "") or ""
                        if _second["raw"]:
                            _second["arr"] = self._parse_json_array(_second["raw"])
                    except Exception:
                        _second["arr"] = None
                # 首路成功后并发采第二路（同 prompt 独立调用）
                with _TPE(max_workers=1) as _ex:
                    _fut = _ex.submit(_sample_second)
                    try:
                        _fut.result(timeout=per_to + 5)
                    except Exception:
                        pass
                self._bump_llm("decompose")
                _arr2 = _second["arr"]
                if _arr2:
                    _set1 = {(str(x.get("table", "")).lower(),
                              str(x.get("sheet", "")).lower())
                             for x in arr if isinstance(x, dict)}
                    _set2 = {(str(x.get("table", "")).lower(),
                              str(x.get("sheet", "")).lower())
                             for x in _arr2 if isinstance(x, dict)}
                    _fields1 = {f"{str(x.get('table','')).lower()}/{str(x.get('sheet','')).lower()}:{sorted((x.get('fields') or {}).keys())}"
                                for x in arr if isinstance(x, dict) and isinstance(x.get("fields"), dict)}
                    _fields2 = {f"{str(x.get('table','')).lower()}/{str(x.get('sheet','')).lower()}:{sorted((x.get('fields') or {}).keys())}"
                                for x in _arr2 if isinstance(x, dict) and isinstance(x.get("fields"), dict)}
                    if _set1 == _set2 and _fields1 == _fields2:
                        self.add_thinking("细分",
                            f"§T7 双采样结构一致（{len(arr)} 条），直接采用首路")
                    else:
                        # 不一致 → 取并集（按 table/sheet 去重保留两路产出，交后续 Step2 校验）
                        _seen = set()
                        _merged = []
                        for _x in (arr + _arr2):
                            if not isinstance(_x, dict):
                                continue
                            _k = (str(_x.get("table", "")).lower(),
                                  str(_x.get("sheet", "")).lower(),
                                  str(_x.get("action", "")).lower())
                            if _k in _seen:
                                continue
                            _seen.add(_k)
                            _merged.append(_x)
                        self.add_thinking("细分",
                            f"§T7 双采样结构不一致（首路 {len(arr)} / 二路 {len(_arr2)}"
                            f"表集差异 {_set1 ^ _set2 or '无'}），取并集 {len(_merged)} 条"
                            f"交后续 Step2 校验")
                        arr = _merged
            except Exception:
                logger.debug("T7 双采样失败(降级单路)", exc_info=True)
        # §观测C：落 prompt/raw IO（env 开），定位 LLM 实际产出与退化。
        _dump_llm_io("single_prompt", prompt, raw, stems=_stems,
                      extra={"text_bytes": len(text or ""),
                             "schema_chars": len(schema_all or ""),
                             "retry_attempt": _attempt,
                             "resp_err": _resp_err[:40]})
        if not raw:
            self._record_chain_timeout(_resp_err)
            self.add_thinking("细分", "DecomposeAgent 单 prompt 空响应")
            _push_telemetry(self.add_thinking, "decompose_io", {
                "site": "single_prompt", "stems": _stems,
                "raw_bytes": 0, "arr_len": 0, "kept": 0,
                "dropped_narr": [], "filtered_out": [],
                "verdict": _resp_err or "empty_response"})
            return []
        arr = self._parse_json_array(raw)
        if not arr:
            self.add_thinking("细分", "DecomposeAgent 单 prompt 非 JSON 数组")
            _push_telemetry(self.add_thinking, "decompose_io", {
                "site": "single_prompt", "stems": _stems,
                "raw_bytes": len(raw), "arr_len": 0, "kept": 0,
                "dropped_narr": [], "filtered_out": [],
                "verdict": "non_json"})
            return [], []
        # §观测C：记录 LLM 原始返回的每条 table/sheet/action，定位是否跨表多产
        _llm_rows = [{"table": str(x.get("table","") if isinstance(x,dict) else ""),
                      "sheet": str(x.get("sheet","") if isinstance(x,dict) else ""),
                      "action": str(x.get("action","") if isinstance(x,dict) else "")}
                     for x in arr if isinstance(x, dict)]
        intents, dropped = self._to_split_intents(arr, text)
        # §A2 valid_stems = 全表池 ∪ 本段候选：切段后每段 LLM 可跨段产合法表
        #（combat 段产 entity_prefab/interaction），原"本段候选"窄白名单会丢跨段合法
        # 产出（实测 arr_len=5 kept=[]）。全表池保跨段真实表，∪候选保候选内 alias
        #（如 fake 测试 stem / locator 部分 alias）。cli None 时降为候选（原行为）。
        valid_stems = {c.stem.lower() for c in candidates if getattr(c, "stem", "")}
        # §观测C：记录被 _filter_intents 丢弃的表（哪些 stem 跨组/非候选被丢）
        _before = [str(getattr(it, "table_hint", "") or "") for it in intents]
        filtered = self._filter_intents(intents, candidates, valid_stems,
                                         path="单 prompt")
        _filtered = [str(getattr(it, "table_hint", "") or "") for it in filtered]
        self.add_thinking("细分",
            f"DecomposeAgent 单 prompt 产出 {len(filtered)} 条意图"
            f"（丢弃 {len(dropped)} 叙述灌值 stem）")
        _push_telemetry(self.add_thinking, "decompose_io", {
            "site": "single_prompt", "stems": _stems,
            "raw_bytes": len(raw), "arr_len": len(arr),
            "llm_rows": _llm_rows,
            "before_filter": _before,
            "kept": _filtered,
            "dropped_narr": list(dropped),
            "verdict": "ok"})
        return filtered, dropped

    def _build_retry_hint(self, arr: list, candidates: list[CandidateTable]) -> str:
        """§T8 构造定向重试线索：基于已有结构化信息（fk_edges/candidates/arr）比对。

        线索内容来自代码算好的事实，不是新写业务判断规则：
          - 候选表的 FK 列未被任何输出引用 → "上一轮候选表 X 的 FK 列 Y 未被任何
            输出引用，检查是否遗漏该表的写入"
          - 候选表完全未出现在输出 → "候选表 X 完全未出现在输出，若指令涉及该表请补充"
        无缺漏返回空串（不触发重试）。
        """
        if not arr or not candidates:
            return ""
        # 收集本路输出已引用的表/字段集合
        out_tables: set = set()
        out_cols: set = set()
        for x in arr:
            if not isinstance(x, dict):
                continue
            _t = str(x.get("table", "")).lower()
            _s = str(x.get("sheet", "")).lower()
            if _t:
                out_tables.add(_t)
            _fields = x.get("fields") or {}
            if isinstance(_fields, dict):
                for k in _fields:
                    out_cols.add((_t, _s, str(k).split(":")[0].strip().lower()))
        hints: list[str] = []
        # FK 列未被引用线索
        for _e in (getattr(self, "_last_fk_edges", None) or []):
            _fs = str(getattr(_e, "from_stem", "") or "").lower()
            _fsh = str(getattr(_e, "from_sheet", "") or "").lower()
            _fc = str(getattr(_e, "from_column", "") or "").split(":")[0].strip().lower()
            if not _fs or not _fc:
                continue
            if _fs not in out_tables:
                continue
            if (_fs, _fsh, _fc) not in out_cols:
                hints.append(
                    f"候选表 {_fs}/{_fsh} 的 FK 列 {_fc}（指向 "
                    f"{getattr(_e, 'to_stem', '')}）未被任何输出引用，"
                    f"检查是否遗漏该表的写入")
        # 候选表完全未出现线索（仅报候选中真实相关的，不报 context 噪声表）
        _drop = set(getattr(self, "_context_drop_stems", None) or set())
        for c in candidates:
            _cs = str(getattr(c, "stem", "") or "").lower()
            if not _cs or _cs in _drop:
                continue
            if _cs not in out_tables:
                hints.append(
                    f"候选表 {_cs} 完全未出现在输出，若指令涉及该表请补充对应写入")
        return "\n".join(hints[:3])  # 最多3条，避免 prompt 膨胀

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
                    session_id = self._ensure_session()
                    if not session_id:
                        last_err = "建会话失败"
                    else:
                        from .llm_gate import llm_throttle
                        with llm_throttle():
                            resp = client.prompt(session_id, prompt, timeout=per_to,
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
                _push_telemetry(self.add_thinking, "decompose_io", {
                    "site": "parallel", "stems": [cand.stem],
                    "raw_bytes": 0, "arr_len": 0, "kept": [],
                    "dropped_narr": [], "verdict": last_err or "empty"})
                return [], []
            # §观测C：并发每表 LLM IO 落盘
            _dump_llm_io("parallel", prompt, raw, stems=[cand.stem],
                          extra={"text_bytes": len(text or "")})
            arr = self._parse_json_array(raw)
            if not arr:
                self.add_thinking("细分", f"DecomposeAgent {cand.stem} 非 JSON 数组")
                _push_telemetry(self.add_thinking, "decompose_io", {
                    "site": "parallel", "stems": [cand.stem],
                    "raw_bytes": len(raw), "arr_len": 0, "kept": [],
                    "dropped_narr": [], "verdict": "non_json"})
                return [], []
            _llm_rows = [{"table": str(x.get("table","") if isinstance(x,dict) else ""),
                           "sheet": str(x.get("sheet","") if isinstance(x,dict) else ""),
                           "action": str(x.get("action","") if isinstance(x,dict) else "")}
                          for x in arr if isinstance(x, dict)]
            intents, dropped = self._to_split_intents(arr, text)
            # §A2 同 single_prompt：valid_stems = 全表池 ∪ 本段候选（防跨段合法产出被丢）
            valid_stems = {c.stem.lower() for c in candidates if getattr(c, "stem", "")}
            _before = [str(getattr(it, "table_hint", "") or "") for it in intents]
            filtered = self._filter_intents(intents, candidates, valid_stems,
                                             path=f"并发({cand.stem})")
            _kept = [str(getattr(it, "table_hint", "") or "") for it in filtered]
            _push_telemetry(self.add_thinking, "decompose_io", {
                "site": "parallel", "stems": [cand.stem],
                "raw_bytes": len(raw), "arr_len": len(arr),
                "llm_rows": _llm_rows, "before_filter": _before,
                "kept": _kept, "dropped_narr": list(dropped),
                "verdict": "ok"})
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
                # §不 fail-fast：前 2 候选均空时不再 _local_ce.set() 取消剩余。
                # serve 端 LLM 时延波动大（同输入时快时慢），前 2 表 timeout 不代表
                # 后 3 表也会 timeout——并发同时跑时，后表可能正好赶上 serve 恢复。
                # 立即 cancel 会丢掉这些本可能成功的表 → 5 表全空 → 熔断 → baseline
                # → 0 产出。改为等全部跑完，保住部分成功意图（"允许失败，成功照常
                # 操作表格"），全空再降级兜底。代价：墙钟 = max(per_to)≈最慢一个，
                # 但比"快速 cancel + 0 产出"更符合用户预期。
                _early_empty = 0
                for f in futures:
                    try:
                        _r = f.result() or ([], [])
                    except Exception:  # noqa: BLE001
                        _r = ([], [])
                    _collect(_r)
                    if not _r[0]:
                        _early_empty += 1
                    else:
                        _early_empty = 0  # 重置连续空计数（serve 恢复了）
                _total_empty = _early_empty == len(futures)
                if _total_empty:
                    self.add_thinking("细分",
                        f"DecomposeAgent 并发 {len(futures)} 表全空响应"
                        f"（serve 超时/不稳），降级兜底")
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

    def _all_table_stems(self) -> set:
        """§A2 全表 stems 池（复用 _table_index_cache，list_tables 一次后查内存）。

        供 _filter_intents 的 valid_stems 用：切段后每段 LLM 可能跨段产合法表
        （combat 段产 entity_prefab/interaction——"刷NPC/对话树"语义），原
        valid_stems 用本段窄候选会把这类合法跨段产出当"幻觉表"丢弃（实测
        arr_len=5 kept=[]）。改用全表 stems 池：Step1 filter 只防"产了不存在
        的表 stem"真幻觉，不防"产错但存在的表 stem"（后者交 Step2 COL_NOT_FOUND）。
        Step1 职责边界 = 解析产出（schema-grounded），不做列名/语义正确性判定。
        cli 不可用时返空集（调用方回退本段候选）。
        """
        if not self._cli:
            return set()
        if not hasattr(self, "_table_index_cache") or not self._table_index_cache:
            try:
                self._table_index_cache = {p.stem: p
                                           for p in self._cli.list_tables()}
            except Exception:  # noqa: BLE001
                self._table_index_cache = {}
        return {str(k).lower() for k in (self._table_index_cache or {}).keys()}

    def _default_sheet_for(self, stem: str) -> str:
        """Return a conservative business sheet for explicit table-name fallback."""
        if not self._cli or not stem:
            return ""
        if not hasattr(self, "_table_index_cache") or not self._table_index_cache:
            try:
                self._table_index_cache = {p.stem: p
                                           for p in self._cli.list_tables()}
            except Exception:  # noqa: BLE001
                self._table_index_cache = {}
        p = (self._table_index_cache or {}).get(stem)
        if p is None:
            p = (self._table_index_cache or {}).get(stem.lower())
        if p is None:
            return ""
        try:
            sheets = self._cli.get_sheets(p) or []
        except Exception:  # noqa: BLE001
            return ""
        biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s.upper()]
        if not biz:
            return ""
        if len(biz) == 1:
            return biz[0]
        norm_stem = stem.replace("_", "").lower()
        for s in biz:
            if s.replace("_", "").lower() == norm_stem:
                return s
        return biz[0]

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
        col_clean = str(col or "").split(":")[0].strip().lower()
        for h, t in zip(hdrs, trow):
            if not h:
                continue
            h_clean = str(h or "").split(":")[0].strip().lower()
            t_clean = str(t or "").split(":")[0].strip().lower()
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
                h_clean = str(h or "").split(":")[0].strip().lower()
                t_clean = str(t or "").split(":")[0].strip().lower()
                if h_clean == col_clean or t_clean == col_clean:
                    _tv = str(t or "")
                    if ":" in _tv:
                        return _tv.split(":", 1)[1].strip()
                    return _tv
        return ""

    def _flatten_dict_fields(self, fields: dict, stem: str, sheet) -> list:
        """§#2 dict 嵌套值治理（边界独立，不依赖写盘/_coerce 旧路径）。

        LLM 偶把子属性合并成对象塞进单列（如 ability 某列 = {cost:0,
        require_level:1}），dict 落到写盘 openpyxl 抛 "Cannot convert dict
        to Excel" → 整行追加失败。schema 驱动处理：
          - 子键命中真实表头（row1 显示名 / row2 规范名，去类型后缀小写比对）
            且该列名未被 fields 占用 → 展开到对应列；
          - 非表头子键 / 列表dict / 无 schema → 原列置空（待 Step2 补，不崩写盘）。
        返回被处理的列名清单供 thinking 上报。
        """
        notes: list[str] = []
        _cli = getattr(self, "_cli", None)
        if not isinstance(fields, dict) or not _cli or not stem:
            # 无 cli/stem 无法取 schema → 把显式 dict/list-dict 置空防写盘崩
            for _k in list(fields.keys()):
                _v = fields[_k]
                if isinstance(_v, dict) or (
                        isinstance(_v, list) and _v and isinstance(_v[0], dict)):
                    fields[_k] = ""
                    notes.append(f"{_k}→置空(无schema)")
            return notes
        if not hasattr(self, "_table_index_cache"):
            try:
                self._table_index_cache = {p.stem: p
                                           for p in _cli.list_tables()}
            except Exception:  # noqa: BLE001
                self._table_index_cache = {}
        _p = self._table_index_cache.get(stem)
        _sheet = sheet
        if _p is not None and not _sheet:
            try:
                _sheets = _cli.get_sheets(_p)
                _biz = [s for s in _sheets if s and "说明" not in s and "CONFIG" not in s]
                _sheet = _biz[0] if _biz else ""
            except Exception:  # noqa: BLE001
                _sheet = ""
        def _schema_name(x) -> str:
            return re.split(r'[:\uff1a\n\r]', str(x or ""))[0].strip().lower()

        def _schema_type(x) -> str:
            s = str(x or "")
            if ":" in s:
                return s.rsplit(":", 1)[-1].strip().lower()
            return ""

        _hdr_clean: set = set()
        _type_by_col: dict[str, str] = {}
        if _p is not None and _sheet:
            try:
                _hdrs, _trow = self._read_schema_cached(_p, stem, _sheet)
                for _h, _t in zip(_hdrs or [], _trow or []):
                    for _name in (_schema_name(_h), _schema_name(_t)):
                        if _name:
                            _hdr_clean.add(_name)
                            _type_by_col[_name] = _schema_type(_t)
            except Exception:  # noqa: BLE001
                pass
        for _k in list(fields.keys()):
            _v = fields[_k]
            if isinstance(_v, dict):
                _kn = _schema_name(_k)
                _kt = _type_by_col.get(_kn, "")
                if _kn in _hdr_clean and any(x in _kt for x in ("dict", "map", "json", "object")):
                    notes.append(f"{_k}→保留dict列")
                    continue
                _kept = 0
                for _sk, _sv in _v.items():
                    _skn = _schema_name(_sk)
                    if _skn and (_skn in _hdr_clean) and str(_sk) not in fields:
                        fields[str(_sk)] = _sv
                        _kept += 1
                fields[_k] = ""  # 原列置空：dict 绝不落写盘
                notes.append(f"{_k}→展开{_kept}子键")
            elif isinstance(_v, list) and _v and isinstance(_v[0], dict):
                fields[_k] = ""
                notes.append(f"{_k}→列表dict置空")
        return notes

    def _lint_split_intents(self, intents: list, fk_edges: list = None) -> int:
        """§Step1 解析层健康自检 + 保守自动修正（零 LLM，聚焦 step1 产出质量）。

        把 LLM 产出的质量问题在 Step1 就抓出/修正，避免流到 Step3 才爆：
          1. dict/list-dict 残留双保险（_flatten_dict_fields 漏网 → 再置空，绝不落盘）
          2. consumes 占位符标签闭环：每个 <label> 找匹配 produces；不匹配 →
             按 FK 边回填到被引表的唯一 produces（多 producer 歧义不回填只 warning），
             治 produces/consumes 命名不一致（#3 的 Step1 兜底，边界独立、不混叠
             operation_orchestrator._lookup 旧模糊策略）。
        不阻断，仅 thinking 上报 + 保守回填。返回修正/告警条数。
        """
        n = 0
        if not intents:
            return 0
        _produces = {(getattr(it, "produces", "") or "").strip()
                     for it in intents if getattr(it, "produces", "")}
        _produces.discard("")
        # FK 边索引：(from_stem_lower, from_sheet_lower, from_column_lower) → to_stem
        _fk: dict = {}
        for _e in (fk_edges or []):
            try:
                _fs = (
                    str(getattr(_e, "from_stem", "") or "").lower(),
                    str(getattr(_e, "from_sheet", "") or "").lower(),
                    str(getattr(_e, "from_column", "") or "").split(":")[0].strip().lower(),
                )
                _fk[_fs] = str(getattr(_e, "to_stem", "") or "")
            except Exception:  # noqa: BLE001
                pass
        for it in intents:
            _tbl = str(getattr(it, "table_hint", "") or "").strip()
            _sht = str(getattr(it, "sheet_hint", "") or "").strip()
            _fields = getattr(it, "fields", None)
            if not isinstance(_fields, dict):
                continue
            for _col in list(_fields.keys()):
                _v = _fields[_col]
                # 1) dict/list-dict 残留 → 置空（绝不落盘 serial 化失败）
                _is_nested = isinstance(_v, dict) or (
                        isinstance(_v, list) and _v and isinstance(_v[0], dict))
                if _is_nested:
                    try:
                        _ct = self._col_type_for(_tbl, _sht, str(_col)).lower()
                    except Exception:
                        _ct = ""
                    if any(x in _ct for x in ("dict", "map", "json", "object")):
                        continue
                    _fields[_col] = ""
                    self.add_thinking("细分",
                        f"lint: {_tbl}/{_sht} 列[{_col}] 残留 dict/list → 置空（防落盘崩）")
                    n += 1
                    continue
                if not isinstance(_v, str) or "<" not in _v:
                    continue
                # 2) 标签闭环检查 + FK 边保守回填
                for _m in __import__("re").finditer(r"<\s*([^>]+?)\s*>", _v):
                    _label = _m.group(1).strip()
                    if _label.lower() == "auto":
                        continue
                    if _label in _produces:
                        continue
                    _cc = str(_col).split(":")[0].strip().lower()
                    _to_stem = _fk.get((_tbl.lower(), _sht.lower(), _cc))
                    if _to_stem:
                        _prods = [x for x in intents
                                  if str(getattr(x, "table_hint", "") or "").lower()
                                  == _to_stem.lower()
                                  and (getattr(x, "produces", "") or "")]
                        if len(_prods) == 1:
                            _nl = _prods[0].produces
                            _fields[_col] = _v.replace(f"<{_label}>", f"<{_nl}>")
                            self.add_thinking("细分",
                                f"lint: {_tbl}/{_sht} 列[{_col}] 标签悬空 "
                                f"<{_label}>→<{_nl}>（FK 边回填到唯一 producer）")
                            n += 1
                        elif len(_prods) == 0:
                            self.add_thinking("细分",
                                f"lint: {_tbl}/{_sht} 列[{_col}] <{_label}> 无匹配 produces"
                                f"，被引表 {_to_stem} 本批无 producer")
                        else:
                            self.add_thinking("细分",
                                f"lint: {_tbl}/{_sht} 列[{_col}] <{_label}> 无匹配 produces，"
                                f"被引表 {_to_stem} 有 {len(_prods)} 个 producer，歧义不回填")
                    else:
                        self.add_thinking("细分",
                            f"lint: {_tbl}/{_sht} 列[{_col}] <{_label}> 无匹配 produces"
                            f"，无 FK 边可回填（交 Step2/backfill 兜底）")
        return n

    def _annotate_schema_role(self, lines: list[str], groups: dict) -> list[str]:
        """§B 证据卡：给 schema 行显式打 required/dependency/context 角色标签。

        不新增业务判断——角色数据完全来自 locator 已算好的 candidate_groups
        （真实 schema/FK 图/LLM 复核分层结果），本函数只做纯字符串标注，让 LLM
        无论走哪条 schema 裁剪路径（贪心预算/分层摘要/未触发预算）都能看到"这张表
        是主目标/依赖表/旁证表"，而不是只在 prompt 超预算时才隐式感知。
        """
        if not groups:
            return lines
        tier_of: dict[str, str] = {}
        for tier in ("required", "dependency", "context"):
            for stem in groups.get(tier, []) or []:
                tier_of.setdefault(stem, tier)
        if not tier_of:
            return lines
        out = []
        for ln in lines:
            m = re.match(r"^- ([^/]+)/([^:]+):", ln)
            if m:
                stem, sheet = m.group(1), m.group(2)
                tier = tier_of.get(stem)
                if tier:
                    ln = ln.replace(f"- {stem}/{sheet}:", f"- {stem}/{sheet}[{tier}]:", 1)
            out.append(ln)
        return out

    def _context_drop_set(self, locator_result) -> set:
        """从 schema 注入中剔除的 context 级噪声表 stem 集合。

        文档 #2/#3「候选池撑长 prompt」的安全修法：只剔除 context 级——即弱信号
        （column_extract/substring）、且经 locator 阶段 LLM 复核判定「非真正涉及」、
        又不与 required 表 FK 关联的旁证表。required（含 LLM 判定相关）/dependency
        （FK 关联，多表写入需其列）完整保留，绝不误删动作主语表或用户要写的表。

        依赖 LocatorResult.candidate_groups（locator 已用 LLM 复核 + FK 关联分好层）。
        默认启用；CODEMAKER_DECOMPOSE_DROP_CONTEXT=0 关闭回退（零行为改动）。
        """
        import os as _os
        if _os.environ.get("CODEMAKER_DECOMPOSE_DROP_CONTEXT", "1") == "0":
            return set()
        groups = getattr(locator_result, "candidate_groups", None) or {}
        _drop = set(groups.get("context", []) or [])
        # §P1-3 FK 端点保护：context 分组误判（分类把 FK 链上表分进 context）会
        # 静默丢链上表 → 跨表 produces 断裂。凡出现在本批候选 FK 边（两端均在
        # 候选内）上的表绝不剔除——链路必需节点，即使弱信号召回。
        if _drop:
            _fk_stems: set[str] = set()
            for _e in (getattr(locator_result, "fk_edges", None) or []):
                _fs = (getattr(_e, "from_stem", "") or "").strip().lower()
                _ts = (getattr(_e, "to_stem", "") or "").strip().lower()
                if _fs:
                    _fk_stems.add(_fs)
                if _ts:
                    _fk_stems.add(_ts)
            _drop = {s for s in _drop if s not in _fk_stems}
        return _drop

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
        # §schema block 本 run 缓存：同一 (candidates 签名 + column_signal 命中列集
        # + budget/sheets/cols 配置) 只构一次，命中直接返回，不重复读表/拼装/裁剪
        # /打 thinking 日志。治"schema 贪心预算裁剪"日志反复刷屏 + 主路径+并发+段级
        # +coverage+backfill 多处重复拼装同一样 block 的墙钟浪费。
        try:
            _sig_hits = frozenset(
                (h.stem, h.sheet, h.column)
                for h in (getattr(column_signal, "hits", []) or []))
            _cand_sig = tuple(sorted(
                (c.stem, c.sheet or "", c.level or "")
                for c in candidates if getattr(c, "stem", "")))
            _budget_cfg = (
                _os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET", "1500"),
                _os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_SHEETS", "32"),
                _os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_COLS", "64"),
            )
            _cache_key = (_cand_sig, _sig_hits, _budget_cfg,
                          frozenset(getattr(self, "_context_drop_stems", None) or set()))
            _cached = self._schema_block_cache.get(_cache_key)
            if _cached is not None:
                return _cached
        except Exception:  # noqa: BLE001
            _cache_key = None
        # §T6 Schema 裁剪从"候选数固定阈值"改成"内容重要性驱动的动态预算"：
        # 去掉候选数分桶（≤3/3-5/>5 → 固定 4/3/2 sheets、16/10/6 cols），改为贪心
        # 按字符预算填充（column_signal 命中列 > PK > FK > 其它，逐列累加到
        # CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET 即停）。关系列不再因候选数多被机械砍掉。
        # 可回退：显式设 CODEMAKER_DECOMPOSE_SCHEMA_SHEETS / _COLS 仍可强制上限
        # （默认大值=不裁，交贪心预算管；设小值则回退旧固定阈值行为）。
        _default_sheets = "32"   # 不限 sheet（贪心预算管边界）
        _default_cols = "64"     # 不限 cols（贪心预算管边界）
        max_sheets = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_SHEETS", _default_sheets)))
        max_cols = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_SCHEMA_COLS", _default_cols)))
        # 列名信号：构建 (stem, sheet) -> 命中列名集合，供 sheet 排序加权
        sig_sheet_hits: dict[tuple[str, str], set[str]] = {}
        if column_signal is not None:
            for h in getattr(column_signal, "hits", []) or []:
                sig_sheet_hits.setdefault(
                    (h.stem, h.sheet), set()).add(h.column)
        # §知识增强：逐列标注 [PK]/[FK→to_stem.to_sheet.to_col]，让 LLM 直接看到
        # 列间关系而非只看表头字符串（列关系是用户反馈的准确率痛点）。数据源
        # 全部数据驱动，非本函数硬判业务规则：
        #   - FK：LocatorAgent 已算好的 fk_edges（table_relations.json 声明式
        #     覆盖层 + 运行时列名模式推导，见 locator_agent._collect_fk_edges）
        #   - PK：rules/validate 用户声明的 primary_key（get_primary_key_overlay，
        #     同 column_aliases.yaml 一样是数据文件，非代码硬编码）
        def _norm(c) -> str:
            return str(c or "").split(":")[0].strip().lower()
        _fk_from_map: dict[tuple[str, str, str], str] = {}
        for _e in (getattr(self, "_last_fk_edges", None) or []):
            _k = (_e.from_stem, _e.from_sheet, _norm(_e.from_column))
            _fk_from_map[_k] = f"{_e.to_stem}.{_e.to_sheet}.{_norm(_e.to_column)}"
        try:
            from ..core.rules_loader import get_primary_key_overlay
            _pk_overlay = get_primary_key_overlay()
        except Exception:
            _pk_overlay = {}
        # §T9 列值域约束前置注入：加载 value_constraints.yaml（L1 派生 + rules/validate
        # 用户规则深合并），供列标注 [范围:min~max]。数据源已是现成 yaml，不新增硬编码判断。
        try:
            from ..core.agent import _load_value_constraints
            _vc = _load_value_constraints()
        except Exception:
            _vc = {}
        all_tables = {}
        try:
            all_tables = {p.stem: p for p in self._cli.list_tables()}
        except Exception:
            return ""
        # §B 证据卡：候选分层（required/dependency/context）供全路径角色标注。
        # 数据源是 locator 已算好的真实分层（LLM 复核 + FK 图），非本函数新增判断。
        try:
            from ..locator.candidate_grouping import classify_candidates
            _groups = classify_candidates(candidates)
        except Exception:
            _groups = {}
        lines: list[str] = []
        _records: list[dict] = []  # schema_budget 用（stem/sheet/cols/sig_cols）
        # context 级噪声表剔除（文档 #2/#3）：全部候选都在 drop 集时放弃裁剪（避免 schema 空）。
        _drop = set(getattr(self, "_context_drop_stems", None) or set())
        if _drop:
            _all_stems = {c.stem for c in candidates if getattr(c, "stem", None)}
            if not (_all_stems - _drop):
                _drop = set()
        _dropped_n = 0
        for cand in candidates:
            if _drop and cand.stem in _drop:
                _dropped_n += 1
                continue
            p = all_tables.get(cand.stem)
            if p is None:
                continue
            try:
                sheets = self._cli.get_sheets(p)
            except Exception:
                continue
            biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s]
            # §防诱导：若候选已带具体 sheet（locator/column_signal 已锚定目标 sheet），
            # 只读该 sheet，不拉全表业务 sheet。读全 sheet 会把无关 sheet（如任务链
            # 案例误把 interaction 的 InteractionConv/ConvOption 也注入）塞进 prompt，
            # LLM 看到无关 sheet schema 易幻觉产指令未提的意图（如指令没对话却产对话树）。
            # 仅当 cand.sheet 为空（粗筛候选/重拆兜底）时才读全 sheet 供 LLM 选。
            if getattr(cand, "sheet", ""):
                _want = cand.sheet
                biz = [s for s in biz if s == _want] or biz
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
                _pk_cols = set(_norm(c) for c in
                               (_pk_overlay.get(cand.stem.lower(), {}) or {}).get(sh, []) or [])
                # (display_name, is_signal, is_relation) 三元组，命中列/关系列先排
                col_tuples = []
                for h, t in zip(hdrs, trow):
                    if not h:
                        continue
                    name = str(h) + (f"（{t}）" if t and str(t) != str(h) else "")
                    _hn, _tn = _norm(h), _norm(t)
                    _is_pk = _hn in _pk_cols or _tn in _pk_cols
                    _fk_target = (_fk_from_map.get((cand.stem, sh, _hn))
                                  or _fk_from_map.get((cand.stem, sh, _tn)))
                    if _is_pk:
                        name = f"{name}[PK]"
                    elif _fk_target:
                        name = f"{name}[FK→{_fk_target}]"
                    # §T9 列值域约束前置注入：该列在 value_constraints.yaml 有 min/max
                    # 或 enum/allowed/values 等声明 → 追加 [范围]/[枚举]，与 [PK]/[FK]
                    # 同套机制。数据源 yaml，非硬编码。
                    try:
                        _vc_sheet = (_vc.get(cand.stem.lower(), {})
                                       .get(sh, {}) or {})
                        _vc_cols = _vc_sheet.get("columns") or {}
                        _vc_col = None
                        for _k, _v in _vc_cols.items():
                            if _norm(_k) == _hn or _norm(_k) == _tn:
                                _vc_col = _v
                                break
                        if _vc_col and isinstance(_vc_col, dict):
                            _mn = _vc_col.get("min")
                            _mx = _vc_col.get("max")
                            if _mn is not None or _mx is not None:
                                _ms = "" if _mn is None else str(_mn)
                                _xs = "" if _mx is None else str(_mx)
                                name = f"{name}[范围:{_ms}~{_xs}]"
                            _enum_vals = None
                            for _enum_key in ("enum", "values", "allowed",
                                              "choices", "options"):
                                _maybe = _vc_col.get(_enum_key)
                                if isinstance(_maybe, (list, tuple, set)):
                                    _enum_vals = list(_maybe)
                                    break
                            if _enum_vals:
                                _shown = [str(x) for x in _enum_vals[:12]]
                                _suffix = "/..." if len(_enum_vals) > 12 else ""
                                name = f"{name}[枚举:{'/'.join(_shown)}{_suffix}]"
                    except Exception:
                        pass
                    col_tuples.append((name, str(h) in sig_cols, _is_pk or bool(_fk_target)))
                # 命中列/关系列在前，其余按原顺序（关系列即使无信号命中也优先于
                # max_cols 截断被砍——列间关系是准确率关键，不能被噪声列挤掉）。
                col_tuples.sort(key=lambda x: (not x[1], not x[2]))
                # 命中列必留（即使超 max_cols），其余按 max_cols 上限补齐
                kept = [name for name, is_sig, _ in col_tuples if is_sig]
                rest = [name for name, is_sig, _ in col_tuples if not is_sig]
                # 命中列占额，剩余补到 max_cols
                rest_budget = max(0, max_cols - len(kept))
                cols = kept + rest[:rest_budget]
                if cols:
                    # §块1/2 prompt 改进：候选来源提示——弱信号候选（FK扩表/列名巧合/
                    # 语义补表等，非 alias/rule 直接命中）标注来源，帮 LLM 判断"这张表
                    # 是不是真的该用",而不是盲选。只标弱信号，强命中（alias/rule）不加
                    # 提示，防止 token 膨胀抵消 schema 预算裁剪的提速收益。
                    _lvl = (getattr(cand, "level", "") or "").strip()
                    _hint = ""
                    if _lvl in ("fk_expanded", "column_extract", "column_reverse",
                                "substring", "spawn_semantic", "entity_semantic",
                                "retry_single"):
                        _hint = f" [候选来源:{_lvl},供参考,请自行判断是否真正相关]"
                    lines.append(f"- {cand.stem}/{sh}: " + " | ".join(cols) + _hint)
                    _records.append({"stem": cand.stem, "sheet": sh,
                                     "cols": cols, "sig_cols": set(kept)})
        # schema_budget（MVP #4）：CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET>0 且完整
        # schema 超预算时，按候选分层降注入粒度（required 完整 / dependency 摘要 /
        # context 不注入），缩短 prompt 而非拉长 timeout。
        # §提速：默认值由 0 改为 6000（可回退：设 0 关闭）。只有 schema 块**超**该
        # 字符阈时才裁剪 dependency 表为摘要——小 prompt（单测/常见输入）字节不变，
        # 仅对"多候选表堆叠"的大 prompt 生效，直接压缩 serve 往返体量。
        # §提速（金标 shadow 数据驱动）：默认 1500。真实 locator 宽召回（4~12 候选）
        # 下，旧路径给全部候选注入完整 schema（实测 schOff 达 4006 字符）；预算 1500 +
        # 候选分层（required 完整 / dependency 转 PK+FK 摘要 / context 省略）实测把 schema
        # 平均压 ~62%（对齐文档"prompt 降 60%"目标）。可回退：设 0 关闭。
        _budget = 0
        try:
            _budget = int(_os.environ.get(
                "CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET", "1500"))
        except (TypeError, ValueError):
            _budget = 1500
        if _budget > 0 and _records:
            try:
                # §T6 优先走贪心字符预算（内容重要性驱动，非候选数分桶）。
                # 构建 pk_cols_by_table / fk_cols_by_table 供贪心判优先级。
                _pk_cols_by_table: dict[tuple, set] = {}
                for _k_stem, _sheets_map in (_pk_overlay or {}).items():
                    for _sh, _cols_list in (_sheets_map or {}).items():
                        _pk_cols_by_table[(_k_stem.lower(), str(_sh).lower())] = set(
                            _norm(_c) for _c in (_cols_list or []))
                _fk_cols_by_table: dict[tuple, set] = {}
                for _e in (getattr(self, "_last_fk_edges", None) or []):
                    _key = (_e.from_stem.lower(), _e.from_sheet.lower())
                    _fk_cols_by_table.setdefault(_key, set()).add(_norm(_e.from_column))
                from .schema_budget import apply_greedy_char_budget
                _budget_lines, _applied = apply_greedy_char_budget(
                    _records, _budget,
                    pk_cols_by_table=_pk_cols_by_table,
                    fk_cols_by_table=_fk_cols_by_table)
                if _applied:
                    self.add_thinking(
                        "解析",
                        f"schema 贪心预算裁剪：{len(lines)}→{len(_budget_lines)} 行"
                        f"（超 {_budget} 字符，命中列/PK/FK 优先，其它列按预算截断）")
                    _out = "\n".join(self._annotate_schema_role(_budget_lines, _groups))
                    if _cache_key is not None:
                        self._schema_block_cache[_cache_key] = _out
                    return _out
                # 贪心未触发（未超预算）→ 回退分层裁剪兜底
                from .schema_budget import apply_schema_budget
                _budget_lines, _applied = apply_schema_budget(_records, _groups, _budget)
                if _applied:
                    self.add_thinking(
                        "解析",
                        f"schema 预算裁剪：{len(lines)}→{len(_budget_lines)} 行"
                        f"（超 {_budget} 字符，dependency 转摘要/context 省略）")
                    _out = "\n".join(self._annotate_schema_role(_budget_lines, _groups))
                    if _cache_key is not None:
                        self._schema_block_cache[_cache_key] = _out
                    return _out
            except Exception:
                pass
        _out = "\n".join(self._annotate_schema_role(lines, _groups)) if lines else ""
        if _cache_key is not None and _out:
            self._schema_block_cache[_cache_key] = _out
        return _out

    def _build_fk_block(self, fk_edges: list[FKEdge]) -> str:
        """构 FK 块:每条边 from.column → to.column。"""
        if not fk_edges:
            return "（无显式 FK）"
        max_edges = max(1, int(os.environ.get("CODEMAKER_DECOMPOSE_FK_LIMIT", "20")))
        lines = []
        for e in fk_edges[:max_edges]:
            lines.append(f"  {e.from_stem}.{e.from_sheet}.{e.from_column} → "
                         f"{e.to_stem}.{e.to_sheet}.{e.to_column}")
        if len(fk_edges) > max_edges:
            lines.append(f"  ...({len(fk_edges) - max_edges} more omitted)")
        return "\n".join(lines)

    def _build_full_schema_block(self, stems: list[str]) -> tuple[str, dict]:
        """不截断地构建指定 stem 的全部业务 sheet schema（供 LLM 自检补漏用）。

        主 schema 块按 max_sheets/max_cols 裁剪，漏产的列名可能根本没出现在主
        prompt 里。自检补漏 pass 必须给全量列，否则 LLM 无从得知还有哪些列可补。
        返回 (schema 文本, 合法字段键表 {(stem, sheet): {列名...}})。
        """
        if self._cli is None:
            return "", {}
        try:
            all_tables = {p.stem: p for p in self._cli.list_tables()}
        except Exception:
            return "", {}
        lines: list[str] = []
        allowed: dict[tuple[str, str], set] = {}
        for stem in stems:
            p = all_tables.get(stem)
            if p is None:
                continue
            try:
                sheets = self._cli.get_sheets(p) or []
            except Exception:
                continue
            biz = [s for s in sheets if s and "说明" not in s and "CONFIG" not in s]
            for sh in biz:
                hdrs, trow = self._read_schema_cached(p, stem, sh)
                if not hdrs:
                    continue
                cols = []
                keys: set = set()
                for h, t in zip(hdrs, trow):
                    if not h:
                        continue
                    name = str(h) + (f"（{t}）" if t and str(t) != str(h) else "")
                    cols.append(name)
                    _disp = str(h).split(":")[0].strip()
                    keys.add(_disp)
                    keys.add(_disp.lower())
                    if t:
                        _base = str(t).split(":")[0].strip()
                        keys.add(_base)
                        keys.add(_base.lower())
                lines.append(f"- {stem}/{sh}: " + " | ".join(cols))
                allowed[(stem, sh)] = keys
                allowed[(stem, sh.lower())] = keys
        return "\n".join(lines), allowed

    def _llm_complete_fields(self, text: str, intents: list, per_to: int) -> list:
        """§能力级自检补漏：LLM 对照原文 + 全量 schema，补齐自己漏产的字面值字段。

        不写死任何业务列名：漏哪个列、值取原文哪处，全部由 LLM 判断。代码只做两件
        grounding 事——①给全量 schema（主块被裁剪，LLM 可能没看到那些列名）
        ②把补出的列限制在真实表头白名单内（防幻觉列）。失败/关闭时原样返回不阻断。

        兼容 SplitIntent（.fields 直挂）与 NLIntent（fields 在 .extras["fields"]）：
        两类都在 ParseAgent._assemble 汇合后调用一次，避免每段各补一遍的 N× LLM 开销。
        """
        import os as _os
        import json as _json
        if not intents or self.parser is None:
            return intents
        if _os.getenv("CODEMAKER_DECOMPOSE_COMPLETE", "1") == "0":
            return intents
        client = getattr(self.parser, "client", None)
        if client is None:
            return intents
        _add = [it for it in intents
                if (getattr(it, "action", "") or "").strip().lower() == "add"]
        if not _add:
            return intents

        def _fields_of(it):
            f = getattr(it, "fields", None)
            if isinstance(f, dict):
                return f
            ex = getattr(it, "extras", None) or {}
            f = ex.get("fields")
            return f if isinstance(f, dict) else None

        # §落地：低成本预判——原文字面值（引号名/裸数字）若已全部被现有字段值
        # 覆盖，大概率无字段可补，跳过这次昂贵 LLM 自检调用（默认 40s+）。只要
        # 有一个字面值未覆盖才真调 LLM 去核对。纯字符串包含判断，宁可误判"值得
        # 查"也不误判"无需查"（偏保守，不会漏检）。
        # 可用 CODEMAKER_DECOMPOSE_COMPLETE_SKIP_CHECK=0 关闭该预判，强制每次都调。
        if _os.getenv("CODEMAKER_DECOMPOSE_COMPLETE_SKIP_CHECK", "1") != "0":
            from ..core.pipeline.value_extractor import has_uncovered_literal_values
            _all_values = []
            for it in intents:
                for v in (_fields_of(it) or {}).values():
                    _all_values.append(v)
            if not has_uncovered_literal_values(text, _all_values):
                return intents

        stems = sorted({str(getattr(it, "table_hint", "") or "").strip()
                        for it in _add if getattr(it, "table_hint", "")})
        if not stems:
            return intents
        schema_full, allowed = self._build_full_schema_block(stems)
        if not schema_full:
            return intents

        _cur = []
        for it in intents:
            _cur.append({
                "table": getattr(it, "table_hint", "") or "",
                "sheet": getattr(it, "sheet_hint", "") or "",
                "action": getattr(it, "action", "") or "add",
                "fields": dict(_fields_of(it) or {}),
            })
        prompt = (
            "你是配表拆分自检员。下面是同一条指令的「真实表 schema」与你刚才拆出的"
            "「意图清单」。请逐条对照指令，把【指令明确给了值、但意图 fields 里缺失】"
            "的列补齐。\n\n"
            f"## 指令\n{text}\n\n"
            f"## 真实表 schema（全量列）\n{schema_full}\n\n"
            "## 你刚才拆出的意图\n"
            + _json.dumps(_cur, ensure_ascii=False, indent=2)
            + "\n\n"
            "## 规则\n"
            "1. 只补真实 schema 里存在的列，值必须严格取自指令原文，不得瞎编；\n"
            "2. int/float/bool 数字列：指令给中文标签就保留中文标签原词（下游会转码），"
            "不要臆测数字；\n"
            "3. 不得修改已有字段的值，不得增删/重排意图，不得改 table/sheet/action；\n"
            "4. 指令没给值的列不要补。\n\n"
            "## 输出\n只输出 JSON 数组（与输入意图同序同构，仅 fields 补缺），无其他文字。"
        )
        raw = self._call_llm_raw(prompt, timeout=max(20, per_to))
        if not raw:
            return intents
        arr = self._parse_json_array(raw)
        if not isinstance(arr, list):
            return intents
        merged = 0
        for idx, item in enumerate(arr):
            if idx >= len(intents) or not isinstance(item, dict):
                continue
            it = intents[idx]
            if str(item.get("table", "") or "").strip().lower() != \
                    str(getattr(it, "table_hint", "") or "").strip().lower():
                continue
            if str(item.get("sheet", "") or "").strip().lower() != \
                    str(getattr(it, "sheet_hint", "") or "").strip().lower():
                continue
            if str(item.get("action", "") or "add").strip().lower() != "add":
                continue
            fields = _fields_of(it)
            if fields is None:
                fields = {}
                ex = getattr(it, "extras", None)
                if isinstance(ex, dict):
                    ex["fields"] = fields
                else:
                    it.extras = {"fields": fields}
            keys = allowed.get(
                (str(getattr(it, "table_hint", "") or "").strip(),
                 str(getattr(it, "sheet_hint", "") or "").strip()),
                set())
            if not keys:
                continue
            nf = item.get("fields") or {}
            if not isinstance(nf, dict):
                continue
            for k, v in nf.items():
                if not k:
                    continue
                _kb = str(k).split(":")[0].strip()
                if any(str(ek).split(":")[0].strip() == _kb
                       for ek in fields.keys()):
                    continue  # 已有列不动
                if _kb not in keys and _kb.lower() not in keys:
                    continue  # 幻觉列不补
                sv = v if v is not None else ""
                if sv in ("", "<auto>"):
                    continue
                fields[k] = v
                merged += 1
        if merged:
            self.add_thinking("细分",
                f"DecomposeAgent 自检补漏 {merged} 个字段（LLM 对照原文补齐漏产）")
        return intents

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
        # §T10 produces/consumes 跨表变量声明表随链路累积注入：把当前链路已声明
        # 的 produces_label 列表（代码已有的结构化数据，非新写规则）作为"本次链
        # 已声明变量表"注入，供 LLM 复用已有命名而非重新发明，避免长链断裂。
        declared_section = ""
        _declared = list(getattr(self, "_declared_produces_vars", []) or [])
        if _declared:
            declared_section = (
                "## 本次链路已声明变量（复用同名 produces label，勿重新发明）\n"
                + "\n".join(f"- <{lbl}>" for lbl in _declared)
                + "\n\n引用本链已产出的 ID 时，consumes 用同名 label，字段值填 <同名label>。\n\n"
            )
        # §T11 反模式回灌：本次会话已归纳易错点（pending_review，同 session 内注入，
        # 不跨 session 固化）作为"已知易错点"提示 LLM 规避。
        anti_pattern_section = ""
        _aps = list(getattr(self, "_session_anti_patterns", []) or [])
        if _aps:
            _ap_lines = []
            for _ap in _aps[:5]:  # 最多5条，避免 prompt 膨胀
                _t = str(_ap.get("trigger", "") or "")[:80]
                _r = str(_ap.get("rationale", "") or "")[:80]
                _ap_lines.append(f"- 触发：{_t}" + (f"（{_r}）" if _r else ""))
            if _ap_lines:
                anti_pattern_section = (
                    "## 本次会话已知易错点（已归纳反模式，请规避）\n"
                    + "\n".join(_ap_lines) + "\n\n"
                )
        few_shot = self._build_few_shot_block(text, schema_block)
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
        semantic_output_section = ""
        if os.environ.get("CODEMAKER_DECOMPOSE_SEMANTIC_OUTPUT", "0").lower() in (
            "1", "true", "yes", "on"):
            semantic_output_section = (
                "## Optional semantic_plan output mode\n"
                "Return one fenced JSON object in this shape instead of the legacy array:\n"
                "```json\n"
                "{\"semantic_plan\":{\"version\":1,\"entities\":[{\"entity_id\":1,"
                "\"operation\":\"add|set|delete|get\","
                "\"target\":{\"table\":\"<stem>\",\"sheet\":\"<sheet>\"},"
                "\"locator\":{\"field\":\"\",\"value\":\"\",\"fields\":[],\"values\":[]},"
                "\"attributes\":[{\"name\":\"<schema column>\","
                "\"value\":\"<literal or <label>>\"}],\"produces\":\"\","
                "\"references\":[{\"field\":\"<column>\","
                "\"label\":\"<produces_label>\"}],\"raw\":\"<source clause>\"}]}}\n"
                "```\n"
                "Keep attributes schema-aware: attribute.name must be a real column "
                "from the selected sheet. Put cross-row references in attributes as "
                "\"<label>\" and mirror them in references. Do not invent "
                "resolved_from placeholders.\n\n"
            )
        if os.environ.get("CODEMAKER_DECOMPOSE_COMPACT_PROMPT", "1") != "0":
            compact_rules = (
                "你是 Excel 配表拆解器。只输出 JSON 数组，不要解释、不要 markdown。\n"
                "每个数组元素格式："
                "{\"table\":\"<stem>\",\"sheet\":\"<sheet>\",\"action\":\"add|set|delete|get\"," 
                "\"fields\":{真实列名:值},\"produces\":\"new_xxx_id或唯一语义标签或空\"," 
                "\"consumes\":{列名:\"produces_label\"},\"locator_field\":\"\",\"locator_value\":\"\"," 
                "\"locator_fields\":[],\"locator_values\":[]}。\n"
                "硬规则：1) fields 键必须来自 schema 的 row1 或 row2 冒号前列名；"
                "2) 一个新增/修改动作默认只产一条意图；名称、类型、描述、时间、数值等是同一行字段，必须合并进同一个 fields，绝不能拆成多条意图；"
                "只有原文明确说多个对象/多行（如多个活动、三条奖励、两个选项、第一条/第二条）时才逐行展开；"
                "3) 新增主键未给具体值时，主键列填 <produces_label>，并设置 produces；"
                "4) 引用本批新行时字段值填 <同名produces_label>，consumes 必须同名；"
                "5) 同一 sheet 多行互引用必须使用唯一标签（按实体语义命名，如 <new_<stem>_id>_<序号>）；"
                "6) set/delete 必须给 locator_field/locator_value（标定位行）；modify 等同 set。"
                "⚠set 的 locator_field 是「用哪列找这行」（如 name=测试法宝3），"
                "fields 是「改哪列改什么值」（如 法宝描述=测试描述修改）——"
                "定位列绝不塞进 fields，只放修改列。若只改1列且该列非主键，"
                "可用 target_field+value 直接标该列，不写 fields。\n"
                "7) 除 row2 类型为 dict/map/json 的真实列外，禁止把对象塞进字段值；"
                "Quest.target.data:dict 可填对象；list 类型可填数组。\n"
                "8) ⚠【幻觉硬约束】只产指令明确提到的动作和实体，禁止产出指令未提的表/sheet。"
                "如指令只讲任务链+战斗+奖励包，则禁止产对话树（InteractionConv/ConvOption）、"
                "禁止产 NPC 引导、禁止产交互 Interaction（除非指令明确说配交互/对话/点击）"
                "。schema 里出现的 sheet 只是候选，不代表都要产意图——只产指令语义直接对应的。"
                "不确定某表是否要写时，宁可不产（留给 Step2 询问）也不要臆造写入。\n"
            )
            if fill_rules:
                fill_rules_section = fill_rules[:1200] + "\n\n"
            else:
                fill_rules_section = ""
            return (
                fill_rules_section
                + semantic_output_section
                + compact_rules
                + f"## schema\n{schema_block}\n\n"
                + (signal_section if signal_section else "")
                + (declared_section if declared_section else "")
                + (anti_pattern_section if anti_pattern_section else "")
                + f"## FK\n{fk_block}\n\n"
                + f"## 指令\n{text}\n"
            )
        return (
            few_shot_section +
            fill_rules_section +
            semantic_output_section +
            "你是配表跨表链分解器。一条指令可能涉及多张表(经外键关联)。"
            "请分解为每张表一个原子操作,用真实表头列名。\n\n"
            f"## 候选表 schema(row1 显示名,row2 规范名)\n{schema_block}\n\n"
            + signal_section +
            (declared_section if declared_section else "") +
            (anti_pattern_section if anti_pattern_section else "") +
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
            "- ⚠【动作边界硬约束】一个新增/修改动作默认对应一个业务对象一条意图；名称、类型、描述、时间、数值等只是该对象字段，必须合并在同一 fields 中。"
            "例如「新增一个活动，名称春节活动，类型节日」只能产 1 条 activity add，不能把名称/类型拆成多条意图。"
            "只有原文明确出现多个对象/多行标记（多个活动、三条奖励、两个选项、第一条/第二条等）才逐行展开。"
            "若只是逗号分隔字段，绝不展开成多意图。\n"
            "- ⚠【列表展开硬约束】只有出现明确多行对象标记才展开：多个/若干/三条/两个/每个/各自/分别/第一条/第二条/选项1/选项2。"
            "字段值枚举不是多行标记；活动类型=节日、名称=春节活动、品质=紫色、等级=1 这类字段值只写入当前意图 fields，不能各产一条。"
            "同一表同一 sheet 的每个明确多行项目各产一条 add，跨表子配置也按项目数展开。"
            "输出条数必须覆盖所有明确多行项目，不允许只输出最后一项或把多行项目合并进一条 fields。\n"
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
            "- ⚠【来源/前置引用】当本批新增的实体在另一张表里作为「来源/前置/进化前/升级前/"
            "父级/所属」被引用时（如「新增X…X进化成Y」——进化表的『进化前/源』外键列指的就是 X），"
            "该外键列必须填 X 那条 add 的 \"<produces_label>\" 占位符并在 consumes 标注；"
            "若该表还有对应的『来源名称』文本列，也应填 X 的名称。不要因为原文未再次点名而漏填"
            "这条来源外键——它靠上下文语义（同一实体在两表出现）确定，而非字面重复。"
            "注意区分：同表里指向「结果/进化后」的另一条外键若原文已显式给了具体 ID，则保留该 ID，不要混填。\n"
            "- ⚠【produces/consumes 标签一致】消费方 consumes 里的 label **必须与被引用那条 add 的 "
            "produces 字面完全一致**，系统靠标签字面回填产出值，对不上则占位符悬空→下游整条失败。"
            "跨表引用整体走 `new_<stem>_id`（如引用 school_talent 新增行 → produces=\"new_school_talent_id\"，"
            "消费方 consumes={\"神通id\":\"new_school_talent_id\"}）；**只有「同 sheet 多行互引用」"
            "（对话树/多级进化链/多段关卡）才用唯一语义标签**（conv_root_id/opt_try_id…）。"
            "禁止消费方自创别名（如 produces 叫 new_school_talent_id 却 consumes 写 new_pojun_lv1_id），"
            "两者必须同名。\n"
            "- ⚠【同表多行互相引用】当同一 sheet 产出多行且彼此引用(如对话树的多个句子/"
            "选项、多级进化链、多段关卡)时,每行 produces 必须用**唯一**标签"
            "(如 conv_root_id / conv_prove_id / opt_try_id,而不是都叫 new_<stem>_id),"
            "引用方在 consumes 里精确写目标那一行的唯一标签。标签重名会被当成同一行→"
            "引用串到错行或形成假环。允许前向引用(先声明的行引用后声明的行),"
            "系统会按依赖自动排序、被引用行先建。\n"
            "- ⚠【批量父项 + 子配置展开】当指令先列出多个同类父项，随后用“每个/各自/"
            "按顺序/分别”给这些父项配置同一类子表字段时，必须为每个父项各产一条子表"
            "intent，并把序列值按父项顺序一一对应。不要只给第一个父项产子配置，也不要把"
            "多个父项的子配置合并成一条。若父项有 produces 标签，子配置的 FK 字段必须"
            "消费对应父项的标签。\n"
            "- set/delete 操作：用 locator_field+locator_value 标注定位行（如「删除活动名称为春节活动的行」→ "
            "locator_field=\"活动名称\", locator_value=\"春节活动\"），fields 仅放需修改的列（delete 可空）\n"
            "- ⚠【set/delete 单表硬约束】一个动作子句只定位**一张**目标表。若多张候选表"
            "都含定位词/列名（如「改门派X的战斗模型」中 school 与 combat 都含相关列），"
            "只产「动作主语直接命中的那张表」的 set 意图，**禁止**对每个候选表各产一条同定位"
            "同 fields 的 set——那会改到错误表。按此优先级选表：① 定位列名与动作主语列完全"
            "一致的表（「神通/神通描述」→ school_ability 而非泛化 ability）；② 被 FK 边"
            "引用的表；③ 专有列命中数最多的表。\n"
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
            "- ⚠【标量值硬约束】除 schema row2 类型明确为 dict/map/json 的真实列外，"
            "fields 的每个值必须是标量（数字/字符串/布尔）或占位符 \"<label>\"，"
            "禁止嵌套对象 `{...}` 或对象数组。若指令里某项含若干子属性（如 cost=0、require_level=1），"
            "应拆成各自对应表头列分别填，不要合并成一个对象塞进单列。"
            "例外：Quest.target.data:dict 这类真实 dict 列可直接填对象。\n"
            "- ⚠【枚举列中文标签硬约束】指令给某列的值是中文标签且该列在 schema 里是 int/数字枚举列"
            "（如「类型节日」「品质上品」「部位武器」）时，fields 值**必须填中文标签原词**"
            "（如 \"节日\"），**绝不能留空 \"\"**。系统会在下游把中文标签转成数字码，"
            "或报错提示用户确认填数字码。留空会丢失用户明确给的枚举信息。\n"
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
            + (
                "\n## ⚠ 对话树硬约束(InteractionConv/InteractionConvOption 相关指令必须遵守)\n"
                "- 只要某条 InteractionConv 的 options[N] 字段写入了 \"<opt_xxx_id>\"，就【必须】"
                "再产出一条同名的 InteractionConvOption add 意图（\"produces\":\"opt_xxx_id\"）"
                "作为该选项记录，二者标签一一对应，不允许只引用不产出。\n"
                "- 「下次再来/不了/离开/结束」这类【无后续动作的结束选项】同样必须产出对应的 "
                "InteractionConvOption 记录（其 option_function 可空或指向结束动作），不要因为"
                "\"没有后续\"就省略该选项记录——省略会导致 options[N] 的占位符悬空、整条指令校验失败。\n"
                "- 若对话里要发放奖励，记得在对应选项/对话上把奖励配置（如 option_function / "
                "effect 里的奖励包）也一并产出，不要只做跳转。\n"
                if ("InteractionConv" in schema_block or "interaction" in schema_block.lower()) else ""
            )
            + (
                "\n## ⚠ 多表候选时的选表决策（定位歧义场景）\n"
                "当候选表有多个且表名相近/含同名别名（如 spawn_world_entity 与 spawn_*、"
                "entity_prefab 与 pet/pet_evolve、item 与 equipment）时，按以下优先级选表：\n"
                "- 优先选动作主语直接命中的表（指令主语词出现在该表 schema 显示名或专有列名里），"
                "不要因 alias 命中相同关键词就选语义不符的表\n"
                "- 多表都能配但语义不同时，每个动作主语产独立 intent 落到对应表，"
                "不要合并到单一表丢弃其他\n"
                "- 跨表 FK 关联（schema 块里「外键关联」列出 produces/consumes 边）"
                "是强信号：动作链涉及多表时按 FK 链拆，每链节点一表一 intent\n"
                "- 单纯从输入文本无法判别该选哪张表时，优先选 schema 里列名匹配数最多、"
                "专有列（非 名称/描述/类型/id/编号/备注 等通用列）命中数最多的表\n"
            )
        )

    def _build_few_shot_block(self, text: str, schema_block: str) -> str:
        """Build compact, domain-pattern examples for the LLM.

        These examples are intentionally schematic: they teach decomposition shape,
        placeholder discipline, and batch handling without naming current test-case
        entities. Real column names must still come from the schema block.
        """
        text_l = (text or "").lower()
        schema_l = (schema_block or "").lower()
        examples: list[str] = [
            (
                "### few-shot: two-table forward reference\n"
                "Input: 新增邮件模板，标题'开服公告'，内容'欢迎'，并发全服邮件 global_id 7，奖励包 10001。\n"
                "Output:\n"
                "```json\n"
                "[{\"table\":\"mail\",\"sheet\":\"MailTemplate\",\"action\":\"add\","
                "\"fields\":{\"template_id\":\"<new_template_id>\",\"title\":\"开服公告\",\"content\":\"欢迎\"},"
                "\"produces\":\"new_template_id\",\"consumes\":{}},"
                "{\"table\":\"mail\",\"sheet\":\"GlobalMail\",\"action\":\"add\","
                "\"fields\":{\"global_id\":7,\"template_id\":\"<new_template_id>\",\"reward_id\":10001},"
                "\"produces\":\"\",\"consumes\":{\"template_id\":\"new_template_id\"}}]\n"
                "```"
            )
        ]
        if "tips" in text_l or "tips/" in schema_l:
            examples.append(
                "### few-shot: homogeneous batch rows\n"
                "Input: 配三条提示文案：第一条'背包已满' key 用 BAG_FULL 类型 tips；第二条'金币不足' key 用 GOLD_LACK 类型 tips；第三条'活动未开' key 用 ACTIVITY_CLOSED 类型 tips。\n"
                "Output:\n"
                "```json\n"
                "[{\"table\":\"tips\",\"sheet\":\"tips\",\"action\":\"add\","
                "\"fields\":{\"value\":\"背包已满\",\"key\":\"BAG_FULL\",\"type\":\"tips\"},"
                "\"produces\":\"\",\"consumes\":{}},"
                "{\"table\":\"tips\",\"sheet\":\"tips\",\"action\":\"add\","
                "\"fields\":{\"value\":\"金币不足\",\"key\":\"GOLD_LACK\",\"type\":\"tips\"},"
                "\"produces\":\"\",\"consumes\":{}},"
                "{\"table\":\"tips\",\"sheet\":\"tips\",\"action\":\"add\","
                "\"fields\":{\"value\":\"活动未开\",\"key\":\"ACTIVITY_CLOSED\",\"type\":\"tips\"},"
                "\"produces\":\"\",\"consumes\":{}}]\n"
                "```"
            )
        if "activity" in text_l or "activity/" in schema_l:
            examples.append(
                "### few-shot: single activity row\n"
                "Input: 开限时活动'试炼'，活动编号 3001，活动类型 1，描述'每日可参与'，开始时间 2026-01-01 00:00:00，结束时间 2026-01-07 23:59:59。\n"
                "Output:\n"
                "```json\n"
                "[{\"table\":\"activity\",\"sheet\":\"Activity\",\"action\":\"add\","
                "\"fields\":{\"id\":3001,\"activity_type\":1,\"name\":\"试炼\",\"desc\":\"每日可参与\","
                "\"start_time\":\"2026-01-01 00:00:00\",\"end_time\":\"2026-01-07 23:59:59\"},"
                "\"produces\":\"\",\"consumes\":{}}]\n"
                "```"
            )
        if "school" in text_l or "门派" in text or "神通" in text or "school/" in schema_l:
            examples.append(
                "### few-shot: school parent-child chain\n"
                "Input: 新建门派'示例门派'，门派编号 9，门派类型 1，模型 1001，战斗模型 1002；配两个神通，技能编号 901、902；每个神通再配等级行；灵根映射到天赋 600001、600002。\n"
                "Output:\n"
                "```json\n"
                "[{\"table\":\"school\",\"sheet\":\"School\",\"action\":\"add\","
                "\"fields\":{\"school\":\"<new_school_id>\",\"name\":\"示例门派\","
                "\"school_ability_id[0]\":\"<new_ability1_id>\",\"school_ability_id[1]\":\"<new_ability2_id>\"},"
                "\"produces\":\"new_school_id\",\"consumes\":{\"school_ability_id[0]\":\"new_ability1_id\","
                "\"school_ability_id[1]\":\"new_ability2_id\"}},"
                "{\"table\":\"school_ability\",\"sheet\":\"SchoolAbility\",\"action\":\"add\","
                "\"fields\":{\"school_ability_id\":\"<new_ability1_id>\",\"name\":\"神通一\"},"
                "\"produces\":\"new_ability1_id\",\"consumes\":{}},"
                "{\"table\":\"school_ability\",\"sheet\":\"SchoolAbilityLevel\",\"action\":\"add\","
                "\"fields\":{\"id\":\"<new_ability1_level_id>\",\"school_ability_id\":\"<new_ability1_id>\","
                "\"level\":0,\"common_spell_id\":901},"
                "\"produces\":\"new_ability1_level_id\",\"consumes\":{\"school_ability_id\":\"new_ability1_id\"}},"
                "{\"table\":\"school_spirit\",\"sheet\":\"SchoolSpirit\",\"action\":\"add\","
                "\"fields\":{\"school_id\":\"<new_school_id>\",\"school_ability_id\":\"<new_ability1_id>\","
                "\"spirit_id\":1,\"spirit_buffs[0]\":600001},"
                "\"produces\":\"\",\"consumes\":{\"school_id\":\"new_school_id\","
                "\"school_ability_id\":\"new_ability1_id\"}}]\n"
                "```\n"
                "Pattern notes: one repeated ability creates one SchoolAbility row plus one SchoolAbilityLevel row; "
                "each spirit/talent mapping is its own row; never collapse repeated rows into arrays unless the schema column itself is an array column. "
                "Only write fields that exist in the selected sheet: SchoolAbilityLevel carries level/cost/condition/spell ids, "
                "while ability names/descriptions belong to SchoolAbility. SchoolTalentLevel uses talent_id, not school_talent_id. "
                "Integer FK/id columns must receive numeric ids or produced placeholders like <new_ability1_id>; do not put display names "
                "such as ability names or spirit names into *_id fields."
            )
            examples.append(
                "### few-shot: modify/delete by natural-language row identity\n"
                "Input: 把门派'剑修'的战斗模型改成 1075；把神通'驭风'的描述改成'新描述'；下架神通'TEST'并清掉对应等级数据。\n"
                "Output:\n"
                "```json\n"
                "[{\"table\":\"school\",\"sheet\":\"School\",\"action\":\"set\","
                "\"locator_field\":\"name\",\"locator_value\":\"剑修\","
                "\"fields\":{\"combat_model_id\":1075},\"produces\":\"\",\"consumes\":{}},"
                "{\"table\":\"school_ability\",\"sheet\":\"SchoolAbility\",\"action\":\"set\","
                "\"locator_field\":\"name\",\"locator_value\":\"驭风\","
                "\"fields\":{\"desc\":\"新描述\"},\"produces\":\"\",\"consumes\":{}},"
                "{\"table\":\"school_ability\",\"sheet\":\"SchoolAbility\",\"action\":\"delete\","
                "\"locator_field\":\"name\",\"locator_value\":\"TEST\","
                "\"fields\":{},\"produces\":\"\",\"consumes\":{}},"
                "{\"table\":\"school_ability\",\"sheet\":\"SchoolAbilityLevel\",\"action\":\"delete\","
                "\"locator_field\":\"school_ability_id\",\"locator_value\":\"3333\","
                "\"fields\":{},\"produces\":\"\",\"consumes\":{}}]\n"
                "```\n"
                "Pattern notes: if an ID is known from existing data, use that ID; otherwise preserve the natural-language locator and let Step2/row resolver confirm it. "
                "Never write <resolved_from_...> placeholders into fields—that is not a real value."
            )
        return "\n\n".join(examples)

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

    def _normalise_json_payload(self, payload) -> list:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        plan = payload.get("semantic_plan")
        if isinstance(plan, dict):
            from ..core.pipeline.semantic_plan import (
                compile_semantic_plan_to_operation_items,
            )
            items, report = compile_semantic_plan_to_operation_items(
                plan, schema_getter=self._schema_for_semantic_entity)
            if not report.get("ok"):
                logger.warning("semantic_plan compile issues: %s",
                               report.get("issues"))
            return items
        if isinstance(payload.get("entities"), list):
            from ..core.pipeline.semantic_plan import (
                compile_semantic_plan_to_operation_items,
            )
            items, report = compile_semantic_plan_to_operation_items(
                payload, schema_getter=self._schema_for_semantic_entity)
            if not report.get("ok"):
                logger.warning("semantic entities compile issues: %s",
                               report.get("issues"))
            return items
        for key in ("intents", "operations", "items", "tasks"):
            wrapped = payload.get(key)
            if isinstance(wrapped, list):
                return wrapped
        return [payload]

    def _schema_for_semantic_entity(self, entity: dict) -> tuple[list, list]:
        target = entity.get("target") if isinstance(entity, dict) else {}
        target = target if isinstance(target, dict) else {}
        stem = str(target.get("table") or entity.get("table") or "").strip()
        sheet = str(target.get("sheet") or entity.get("sheet") or "").strip()
        if not stem or not sheet or self._cli is None:
            return [], []
        if not hasattr(self, "_table_index_cache") or not self._table_index_cache:
            try:
                self._table_index_cache = {p.stem: p
                                           for p in self._cli.list_tables()}
            except Exception:  # noqa: BLE001
                self._table_index_cache = {}
        path = (self._table_index_cache or {}).get(stem)
        if path is None:
            path = (self._table_index_cache or {}).get(stem.lower())
        if not path:
            return [], []
        return self._read_schema_cached(path, stem, sheet)

    def _extract_balanced_json(self, raw: str, open_ch: str, close_ch: str) -> Optional[str]:
        """从 raw 找第一个 open_ch 起、真正括号配对(跳过字符串内转义)闭合的子串。

        §得到json 稳健性修复：原贪婪正则 (\\[.*\\]) 遇 raw 夹杂解释文字里的
        无关括号（如"字段说明：见[备注]，数据如下 [{...}]"）会贪婪跨到错误的
        最后一个 ]，拼出"格式对但内容错"的假 JSON——比直接返回空更危险
        （空会走 LLM 修复兜底，错误内容会静默通过校验直接写错）。按字符正确
        计深度定位真正闭合的那一个，不存在则返回 None（交后续 LLM 修复兜底）。
        """
        start = raw.find(open_ch)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
        return None

    def _parse_json_array(self, raw: str) -> list:
        """从 LLM 返回解析 JSON 数组。容忍 fenced code block、裸 JSON、多数组、单 dict。"""
        if not raw:
            return self._llm_repair_json(raw)
        mf = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if mf:
            body = mf.group(1).strip()
            if body.startswith("{") or body.startswith("["):
                try:
                    return self._normalise_json_payload(json.loads(body))
                except ValueError:
                    pass
        # 1) 裸 JSON（整体已是合法数组/对象）
        body = str(raw or "").strip()
        if body.startswith("{") or body.startswith("["):
            try:
                return self._normalise_json_payload(json.loads(body))
            except ValueError:
                pass
        # 2) 括号配对扫描抓数组体（替代贪婪正则，见 _extract_balanced_json 注释）
        arr_body = self._extract_balanced_json(raw, "[", "]")
        if arr_body:
            try:
                return self._normalise_json_payload(json.loads(arr_body))
            except ValueError:
                pass
        # 3) 单 dict（LLM 未包数组，仅产一个 op）→ 包装成 [dict] 接收，避免单 op 输出被丢弃
        obj_body = self._extract_balanced_json(raw, "{", "}")
        if obj_body:
            try:
                return self._normalise_json_payload(json.loads(obj_body))
            except ValueError:
                pass
        # §得到json 判定门修复：规则清洗全部失败时原为静默 return []（有效数据
        # 整段丢弃，无兜底）。改为一次性 LLM 修复兜底——正则救不了的场景通常是
        # 单引号/尾逗号/散文夹带/截断等，LLM 看得懂上下文能修，规则只会更死板。
        # 只试一次（不重试链），失败/关闭仍 [] 兜底，不回归现有行为。
        return self._llm_repair_json(raw)

    def _llm_repair_json(self, raw: str) -> list:
        """规则清洗全部失败时的 LLM 兜底：修复非法/截断 JSON（仅一次）。"""
        if not raw or not raw.strip() or not self.parser:
            return []
        prompt = (
            "以下文本本应是合法 JSON 数组，但格式有问题（可能夹杂解释文字、"
            "用了单引号、多了尾逗号、代码块未闭合、被截断等）。请只输出修复后的"
            "合法 JSON 数组本身，不要解释、不要 markdown 代码块。若内容本身根本"
            "不含可提取的结构化数据，输出 []。\n\n## 原始文本\n" + raw[:4000]
        )
        try:
            fixed_raw = self._call_llm_raw(prompt, timeout=20)
        except Exception:  # noqa: BLE001
            return []
        if not fixed_raw:
            return []
        m = re.search(r"(\[.*\])", fixed_raw, re.DOTALL)
        if not m:
            return []
        try:
            result = self._normalise_json_payload(json.loads(m.group(1)))
        except ValueError:
            return []
        if result:
            self.add_thinking("细分", "DecomposeAgent JSON 解析失败,LLM 重新生成成功")
        return result

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
        _dropped_nondict = 0  # §主线2：非 dict 元素静默丢弃计数，转可追踪 trace
        for item in arr:
            if not isinstance(item, dict):
                _dropped_nondict += 1
                continue
            stem = str(item.get("table") or "").strip()
            sheet = str(item.get("sheet") or "").strip() or None
            act = str(item.get("action") or "add").strip().lower()
            if act not in ("add", "set", "delete", "get"):
                act = "add"
            fields = item.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            # §#2 dict 嵌套值治理：LLM 偶把子属性合并成对象塞进单列（如 ability 某
            # 列 = {cost:0, require_level:1}），dict 落到写盘 openpyxl 抛 "Cannot
            # convert dict to Excel" → 整行追加失败。这里在 LLM 产出后、return 前做
            # schema 驱动展开：子键命中真实表头 → 展开到对应列；非表头子键/列表dict →
            # 原列置空待 Step2 补。边界独立，不依赖写盘/_coerce 旧路径。
            _df_notes = self._flatten_dict_fields(fields, stem, sheet)
            if _df_notes:
                self.add_thinking("细分",
                    f"dict 字段展开/置空 {stem}/{sheet or '?'}：{_df_notes}")
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
            # §建议4 schema-first：surfacing 未知列（不在目标 sheet 表头列集合内的键）。
            # additive：只记可追踪 trace 供 Step2/诊断可见；不从 fields 移除（模糊/别名
            # 匹配交 agent ColumnMatcher，写前由 column_gate 处置），避免误伤。
            try:
                if stem and sheet and fields and self._cli is not None:
                    _idxc = getattr(self, "_table_index_cache", None) or {}
                    if not _idxc:
                        self._all_table_stems()
                        _idxc = getattr(self, "_table_index_cache", None) or {}
                    _pp = _idxc.get(stem) or _idxc.get(stem.lower())
                    if _pp is not None:
                        _hdrs, _ = self._read_schema_cached(_pp, stem, sheet)
                        if _hdrs:
                            from ..core.pipeline.field_partition import (
                                partition_fields_by_schema)
                            _known, _unknown = partition_fields_by_schema(fields, _hdrs)
                            if _unknown:
                                self.add_thinking("细分",
                                    f"schema-first：{stem}/{sheet} 未知列 "
                                    f"{list(_unknown.keys())}（不在表头列集合，"
                                    f"交 ColumnMatcher/写前闸门处置）")
            except Exception:  # noqa: BLE001
                logger.debug("schema-first 未知列 surfacing 失败", exc_info=True)
        # §主线2：非 dict 元素被丢弃时留可追踪 trace（原静默 continue），供 Step1
        # metrics / 诊断定位 LLM 产出结构异常，禁止静默吞。
        if _dropped_nondict:
            self.add_thinking("细分",
                f"_to_split_intents 丢弃 {_dropped_nondict} 个非 dict 元素"
                f"（LLM JSON 数组含非对象项，结构异常）")
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
