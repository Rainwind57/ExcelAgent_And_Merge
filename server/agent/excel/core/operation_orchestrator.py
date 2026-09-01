"""跨表多步操作编排器（P2 + P5.3 并发）。

在 LLM `parse_multi` 输出多条意图之后、执行之前介入，处理**意图间依赖**：
前一步（如"新增 NPC"）产出的新 ID，作为后一步（如"刷新配置引用该 NPC"/
"新增对话引用该 NPC"）的输入。

依赖表达：沿用 expected_answer 的占位符约定——后续意图字段里出现
`<new_id>` / `<prev_id>` / `<上一个id>` 等占位符，编排器在执行到该意图前，
用前序 add 真实产出的新 ID 替换。无占位符的意图视为独立。

执行策略（5.3）：
  - 默认 `CODEMAKER_ORCH_MAX_WORKERS=1`：逐条顺序执行（向后兼容，行为不变）。
  - 调高该值时：按依赖**分层**，同层无依赖意图用 `ThreadPoolExecutor` 并发，
    层间等前层完成、合并 `produced` 后再进下一层（保证依赖正确）。
  - 同层 `produced` 写入按输入下标序归并，确保 `seq_counter` 有序键
    （`option_1_id`/`option_2_id`）在并发下仍确定。
  - 单 worker 异常不崩溃整批：并发路径隔离为失败结果，其余意图继续。
  - `CODEMAKER_ORCH_MAX_RETRIES`（默认 0=不重试）：失败按指数退避重试。
    默认 0 时与原行为完全一致（异常向上抛）。

设计要点：
  - 与执行器解耦：仅依赖注入的 `run_single(intent, confirm_token, session_id)`。
  - 逐条/分层执行，前序结果写入 `produced` 上下文供后续替换。
  - 只做安全的字符串占位符替换，不猜测隐式引用，避免误伤。
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from ..parser.nl_parser import NLIntent

logger = logging.getLogger(__name__)

# 通用命名占位符：`<new_id>` / `<new_prefab_id>` / `<new_conv_id>` /
# `<option_1_id>` / `<上一个id>` 等，捕获尖括号内的名字。
_PLACEHOLDER_RE = re.compile(r"<\s*([^>]+?)\s*>")

# 通用"最近一个新 ID"别名：这些占位符名统一解析到 produced['new_id']
_GENERIC_NAMES = {
    "new_id", "prev_id", "last_id", "prev_prefab_id",
    "新id", "上一个id", "上一步id", "上一个新id",
}

# 识别 ID 列（从 add 结果里提取新产出的主键值）
_ID_COL_RE = re.compile(r"(id|编号)$", re.IGNORECASE)

# 5.3 并发配置（默认 1=顺序，向后兼容；调高启用分层并发）
_ORCH_MAX_WORKERS = max(1, int(os.getenv("CODEMAKER_ORCH_MAX_WORKERS", "1") or "1"))
# 5.3 退避重试（默认 0=不重试，异常直接抛/隔离，与原行为一致）
_ORCH_MAX_RETRIES = max(0, int(os.getenv("CODEMAKER_ORCH_MAX_RETRIES", "0") or "0"))
_ORCH_BACKOFF_BASE = max(0.0, float(os.getenv("CODEMAKER_ORCH_BACKOFF_BASE", "0.5") or "0.5"))


class _FailedResult:
    """并发路径下 worker 异常的兜底结果（与 AgentResult 鸭子兼容）。

    顺序路径（max_workers=1）不构造此对象——异常向上抛，保留原 fail-fast 语义。
    """

    __slots__ = ("ok", "intent", "steps", "final", "message", "result_rows",
                 "table_stem", "table_sheet", "session_id", "sub_tasks",
                 "failed_tables", "dirty_data", "index_dirty")

    def __init__(self, intent: Optional[NLIntent], message: str,
                 session_id: str = "") -> None:
        self.ok = False
        self.intent = intent
        self.steps = []
        self.final = None
        self.message = message
        self.result_rows = []
        self.table_stem = ""
        self.table_sheet = ""
        self.session_id = session_id
        self.sub_tasks = []
        self.failed_tables: list = []
        self.dirty_data = False
        self.index_dirty = False


class OperationOrchestrator:
    """多意图依赖编排：顺序/分层并发执行 + 前序新 ID 占位符替换。"""

    # G9 环检测：最近一次 _topo_order 遇到的环成员下标（None=无环）。
    # 供上层诊断/告警，不阻断执行（环回退原序）。
    _last_cycle: Optional[list] = None

    def __init__(self, run_single: Callable[[NLIntent, Optional[str], str], "object"]):
        self._run_single = run_single

    def run(self, intents: list[NLIntent], confirm_token: Optional[str] = None,
            session_id: str = "") -> list:
        """按依赖顺序执行意图，返回各步结果列表（顺序与 intents 对应）。

        `produced` 累积前序 add 产出的新 ID，支持多命名占位符：
          - `new_id`：最近一次产出的 ID（向后兼容）
          - `new_<col>` / `<col>`：按产出 ID 的列名派生（如 prefab_id→new_prefab_id）
          - `<base>_<n>_id`：同名列多次产出时的有序键（如 option_1_id/option_2_id）
          - 意图显式 `extras['produces']` 指定的标签（解析器可给出的最强信号）

        并发（5.3）：`CODEMAKER_ORCH_MAX_WORKERS>1` 时按拓扑分层，同层并发执行。
        返回顺序始终与输入 intents 对齐（不受拓扑/并发执行顺序影响）。
        """
        produced: dict[str, str] = {}
        seq_counter: dict[str, int] = {}
        results_by_id: dict[int, object] = {}

        if _ORCH_MAX_WORKERS <= 1:
            # 顺序路径：原行为，逐条执行（占位符即用即替换，produced 逐条累积）
            order = self._topo_order(intents)
            import os as _os
            _dbg = bool(_os.getenv("CODEMAKER_ORCH_DEBUG"))
            if _dbg:
                print(f"[orch] seq run: order={order}")
            # D4 跨表事务：任一写步骤失败 → 中断后续 + 聚合 failed_tables + dirty_data
            committed: list[int] = []   # 已成功意图下标
            failed_tables: list[str] = []
            transaction_failed = False
            for i in order:
                intent = intents[i]
                if intent is None:
                    results_by_id[i] = None
                    continue
                if transaction_failed:
                    # 事务已失败：后续意图不执行，标记为跳过
                    results_by_id[i] = _FailedResult(
                        intent, "事务已失败，此步骤被跳过（跨表事务回滚）", session_id)
                    continue
                if produced:
                    if _dbg:
                        print(f"[orch] before resolve {i}: fields={(intent.extras or {}).get('fields')} produced={produced}")
                    self._resolve_placeholders(intent, produced)
                    if _dbg:
                        print(f"[orch] after  resolve {i}: fields={(intent.extras or {}).get('fields')}")
                res = self._run_with_retry(intent, confirm_token, session_id)
                if _dbg:
                    print(f"[orch] {i} done: ok={getattr(res, 'ok', None)} result_rows={[(r.get('col_name'), r.get('new_value')) if isinstance(r, dict) else r for r in (getattr(res, 'result_rows', None) or [])]}")
                res_ok = getattr(res, "ok", None)
                # D2: ok=None（未完成验证）视为失败（事务语义：未确认成功=失败）
                if res_ok is not True:
                    transaction_failed = True
                    tbl = getattr(intent, "table_hint", "") or f"intent_{i}"
                    failed_tables.append(tbl)
                    # 标记 dirty_data（若有 res 对象）：半成品残留供诊断
                    if hasattr(res, "dirty_data"):
                        res.dirty_data = True
                    if hasattr(res, "add"):
                        res.add("transaction_rollback", False,
                                f"跨表事务失败：{tbl} 写后验证未通过，已中断后续意图")
                else:
                    committed.append(i)
                self._capture_produced(res, intent, produced, seq_counter)
                if _dbg:
                    print(f"[orch] produced now={produced}")
                results_by_id[i] = res
            # 事务失败：在首个失败结果记录 failed_tables（供上层诊断）
            if transaction_failed and failed_tables:
                for i in order:
                    r = results_by_id.get(i)
                    if r is not None and getattr(r, "ok", None) is not True:
                        if hasattr(r, "failed_tables"):
                            r.failed_tables = failed_tables
                        break
            return [results_by_id.get(i) for i in range(len(intents))]

        # 并发路径：分层执行，层内 ThreadPoolExecutor，层间合并 produced
        levels = self._topo_levels(intents)
        for level in levels:
            ready = [(i, intents[i]) for i in level if intents[i] is not None]
            if not ready:
                continue
            # 占位符替换：用前层累积的 produced（层内 produced 只读，互不依赖）
            if produced:
                for _, intent in ready:
                    self._resolve_placeholders(intent, produced)
            if len(ready) == 1:
                i, intent = ready[0]
                res = self._run_with_retry(intent, confirm_token, session_id)
                results_by_id[i] = res
            else:
                workers = min(_ORCH_MAX_WORKERS, len(ready))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    fut_to_i = {
                        pool.submit(self._run_with_retry, intent, confirm_token, session_id): i
                        for i, intent in ready
                    }
                    level_results: dict[int, object] = {}
                    for fut in as_completed(fut_to_i):
                        i = fut_to_i[fut]
                        try:
                            level_results[i] = fut.result()
                        except Exception as exc:  # 隔离：单 worker 异常不崩整批
                            level_results[i] = _FailedResult(
                                intents[i], f"并发执行异常：{exc}", session_id)
                for i in level_results:
                    results_by_id[i] = level_results[i]
            # produced 按 index 序归并：保证 seq_counter 有序键确定
            for i in sorted(idx for idx, _ in ready):
                self._capture_produced(results_by_id[i], intents[i], produced, seq_counter)
        return [results_by_id.get(i) for i in range(len(intents))]

    def _run_with_retry(self, intent: NLIntent, confirm_token: Optional[str],
                        session_id: str):
        """5.3：执行单意图，失败按指数退避重试。

        `CODEMAKER_ORCH_MAX_RETRIES=0`（默认）时单次执行、异常直接抛 —— 与原
        `_run_single` 调用语义完全一致。>0 时按 `base * 2^attempt` 退避后重试。
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(_ORCH_MAX_RETRIES + 1):
            try:
                return self._run_single(intent, confirm_token, session_id)
            except Exception as exc:
                last_exc = exc
                if attempt < _ORCH_MAX_RETRIES:
                    time.sleep(_ORCH_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise
        # 理论不可达（上面循环要么 return 要么 raise）
        raise last_exc  # pragma: no cover

    @classmethod
    def _compute_deps(cls, intents: list[NLIntent]) -> dict[int, set]:
        """计算意图间占位符依赖：consumer i 依赖 producer j（j 须先执行）。

        仅当意图显式标注 `extras['produces']=<占位符名>` 时才建依赖边；
        无标注则全空（零依赖，保持原序）。
        """
        n = len(intents)
        producer_of: dict[str, int] = {}
        for i, it in enumerate(intents):
            if it is None:
                continue
            label = (getattr(it, "produces_label", None)
                     or (it.extras or {}).get("produces"))
            if isinstance(label, str) and label.strip():
                ln = cls._norm_name(label)
                producer_of.setdefault(ln, i)
                if ln.startswith("new_"):
                    producer_of.setdefault(ln[4:], i)
                else:
                    producer_of.setdefault("new_" + ln, i)
        deps: dict[int, set] = {i: set() for i in range(n)}
        if not producer_of:
            return deps
        for i, it in enumerate(intents):
            if it is None:
                continue
            for v in cls._iter_values(it):
                if not isinstance(v, str) or "<" not in v:
                    continue
                for m in _PLACEHOLDER_RE.finditer(v):
                    nm = cls._norm_name(m.group(1))
                    if nm.startswith("consume:"):
                        nm = nm.split(":", 1)[1].strip()
                    j = producer_of.get(nm)
                    if j is None and nm.startswith("new_"):
                        j = producer_of.get(nm[4:])
                    if j is not None and j != i:
                        deps[i].add(j)
        return deps

    @classmethod
    def _topo_order(cls, intents: list[NLIntent]) -> list[int]:
        """按占位符依赖拓扑排序意图下标（生产者先于消费者）。

        稳定拓扑（Kahn）：每次取入度归零中**最小下标**，逐个输出 → 保留原序。
        无 `produces` 标注时全空依赖，返回原序；存在环时回退原序并告警。

        环检测（G9）：占位符依赖图含环时（如 conv→option→conv 循环引用），
        Kahn 无 ready 节点。原行为静默回退原序，此处显式记录环成员供上层告警，
        避免 task_chain 用例1 的循环引用链被静默吞掉导致执行顺序错误无据可查。
        环本身不阻断执行（回退原序 = 按 input 声明顺序执行，依赖 input 已正确排序），
        仅提供可观测性。
        """
        n = len(intents)
        deps = cls._compute_deps(intents)
        cls._last_cycle = None
        if not any(deps[i] for i in range(n)):
            return list(range(n))
        order: list[int] = []
        done: set = set()
        remaining = list(range(n))
        while remaining:
            ready = [i for i in remaining if deps[i] <= done]
            if not ready:  # 环 → 最小破环继续（不再整体回退原序）
                # 原实现遇环直接 return 原序：producer 常落在 consumer 之后 →
                # 前向引用占位符在写盘时查不到值 → 悬空报错。改为最小破环：
                # 取剩余中最小下标强制输出，打断一条回边后继续拓扑，尽量让
                # producer 先于 consumer。残留的真·环回边交 backfill 兜底。
                cyclic = sorted(remaining)
                if cls._last_cycle is None:
                    cls._last_cycle = cyclic
                logger.warning(
                    "占位符依赖存在环（cyclic=%s），最小破环继续拓扑。"
                    "若结果异常请检查 input 中相关 op 的声明顺序。", cyclic)
                k = cyclic[0]
                order.append(k)
                done.add(k)
                remaining.remove(k)
                continue
            k = ready[0]
            order.append(k)
            done.add(k)
            remaining.remove(k)
        return order

    @classmethod
    def _topo_levels(cls, intents: list[NLIntent]) -> list[list[int]]:
        """按依赖**分层**（5.3 并发用）：每层为入度归零的一批，层内无相互依赖。

        同层按下标升序（确定性）。环时把剩余全部并入一层（回退原序的分层等价）。
        `_topo_order` 的逐个 Kahn 与本方法的分层 flatten 在无依赖/简单依赖下
        结果一致；差异仅在"同层多元素"的输出次序，但同层互不依赖故不影响
        `produced` 最终状态（`seq_counter` 归并按下标序，亦确定）。
        """
        n = len(intents)
        deps = cls._compute_deps(intents)
        levels: list[list[int]] = []
        done: set = set()
        remaining = list(range(n))
        while remaining:
            ready = sorted(i for i in remaining if deps[i] <= done)
            if not ready:  # 环 → 剩余全部并入一层（回退原序）
                levels.append(sorted(remaining))
                break
            levels.append(ready)
            done.update(ready)
            for i in ready:
                remaining.remove(i)
        return levels

    @staticmethod
    def has_dependencies(intents: list[NLIntent]) -> bool:
        """意图链中是否存在占位符依赖（决定是否需要编排而非并发）。"""
        for it in intents:
            if it is None:
                continue
            for v in OperationOrchestrator._iter_values(it):
                if isinstance(v, str) and _PLACEHOLDER_RE.search(v):
                    return True
        return False

    @classmethod
    def last_cycle(cls) -> Optional[list]:
        """G9: 返回最近一次 _topo_order 检测到的环成员下标（None=无环）。

        供上层诊断：环存在时执行回退原序，若结果异常可据此定位循环引用 op。
        注意：类属性，多次并发调用会互相覆盖，仅作最近一次诊断用。
        """
        return cls._last_cycle

    @staticmethod
    def _iter_values(intent: NLIntent):
        def _walk(v):
            yield v
            if isinstance(v, dict):
                for vv in v.values():
                    yield from _walk(vv)
            elif isinstance(v, (list, tuple)):
                for vv in v:
                    yield from _walk(vv)

        yield from _walk(intent.locator_value)
        yield from _walk(intent.value)
        # §P0 复合主键列表值（locator_values 含占位符待替换）
        if getattr(intent, "locator_values", None):
            for v in intent.locator_values:
                yield from _walk(v)
        fields = (intent.extras or {}).get("fields") or {}
        if isinstance(fields, dict):
            for v in fields.values():
                yield from _walk(v)

    @staticmethod
    def _norm_name(name: str) -> str:
        """占位符名归一：小写去空白，剥首尾 <>。

        task_chain 的 produces 声明可能带尖括号（如 "<new_prefab_id>"），
        而占位符正则捕获的是括号内裸名（如 "new_prefab_id"）；统一剥尖括号
        保证 produces 声明侧与消费侧 _lookup 查询键一致，避免漏匹配。
        """
        s = str(name or "").strip().lower()
        if s.startswith("<") and s.endswith(">") and len(s) >= 2:
            s = s[1:-1].strip()
        return s

    @classmethod
    def _lookup(cls, name: str, produced: dict[str, str]) -> Optional[str]:
        """按占位符名在 produced 中查值；未命中返回 None（不乱替换）。

        O8 增强:在原 3 步(精确/通用别名/new_前缀)后追加
        4) 去分隔符归一(下划线/横线/空格 → 空串)再查
        5) 剥数字后缀/_id 后缀(如 new_prefabid → new_prefab, prefab_id_new → prefab)
        缓解 LLM 自由命名致 produces key 与 consumes label 不匹配的漏配。
        """
        n = cls._norm_name(name)
        if not n:
            return None
        # 1) 精确命中
        if n in produced:
            return produced[n]
        # 2) 通用别名 → 最近一个 new_id
        if n in _GENERIC_NAMES:
            return produced.get("new_id")
        # 3) 去/补 new_ 前缀再试（<prefab_id> ↔ <new_prefab_id>）
        if n.startswith("new_") and n[4:] in produced:
            return produced[n[4:]]
        if ("new_" + n) in produced:
            return produced["new_" + n]
        # 4) O8 去分隔符归一(下划线/横线/空格 → 空)
        n_compact = re.sub(r"[\-_\s]+", "", n)
        if n_compact and n_compact != n:
            if n_compact in produced:
                return produced[n_compact]
            for k, v in produced.items():
                if re.sub(r"[\-_\s]+", "", k) == n_compact:
                    return v
        # 5) O8 剥常见后缀(_id / id / _new)缓解命名差异
        n_stem = n_compact
        for suf in ("_id", "id", "_new", "new"):
            if n_compact.endswith(suf) and len(n_compact) > len(suf):
                n_stem = n_compact[:-len(suf)]
                break
        if n_stem and n_stem != n_compact:
            if n_stem in produced:
                return produced[n_stem]
            for k, v in produced.items():
                k_compact = re.sub(r"[\-_\s]+", "", k)
                for suf in ("_id", "id", "_new", "new"):
                    if k_compact.endswith(suf) and len(k_compact) > len(suf):
                        if k_compact[:-len(suf)] == n_stem:
                            return v
                        break
        # 6) sheet 中缀剥离匹配：producer label 为 sheet-aware（如
        #    new_combat_combat_data_id，中缀含 sheet 名 combat_data），consumer
        #    占位符无中缀（<new_combat_id>）→ 前 5 级（去分隔/剥尾缀）均覆盖不到
        #    夹在中间的 sheet 段。按段序做子序列匹配：查询名各段（new/combat/id）
        #    若按序全部出现在 produced 键的段序列（new/combat/combat/data/id）中，
        #    视为同一产出（中间被插入 sheet 中缀），命中即返回。放最末且要求首尾
        #    段一致 + 段数≥2，避免误配其他 producer。通用判据，不绑表名/测例。
        def _segs(s: str) -> list[str]:
            return [p for p in re.split(r"[\-_\s]+", s) if p]

        def _is_subseq(sub: list[str], full: list[str]) -> bool:
            it = iter(full)
            return all(any(x == y for y in it) for x in sub)

        q_segs = _segs(n)
        if len(q_segs) >= 2:
            for k, v in produced.items():
                k_segs = _segs(cls._norm_name(k))
                if (len(k_segs) >= len(q_segs)
                        and k_segs[0] == q_segs[0]
                        and k_segs[-1] == q_segs[-1]
                        and _is_subseq(q_segs, k_segs)):
                    return v
        return None

    @classmethod
    def _resolve_placeholders(cls, intent: NLIntent, produced: dict[str, str]) -> None:
        """把 intent 中所有 `<name>` 占位符按名替换为 produced 里对应的新 ID。

        仅替换能查到的占位符，查不到的原样保留（避免误伤，交执行层报错）。

        §P0 替换面补全：原只替 locator_value/value/顶层 fields.values()，
        漏 locator_fields/locator_values（复合主键列表）→ 复合主键定位值里
        占位符永远悬空（case5 ResidenceEntry 双键等场景）。同步补列表遍历。
        """
        def _sub(v):
            if isinstance(v, dict):
                return {k: _sub(vv) for k, vv in v.items()}
            if isinstance(v, list):
                return [_sub(vv) for vv in v]
            if isinstance(v, tuple):
                return tuple(_sub(vv) for vv in v)
            if not isinstance(v, str) or "<" not in v:
                return v
            def _repl(m):
                val = cls._lookup(m.group(1), produced)
                if val is None and str(m.group(1)).strip().lower().startswith("consume:"):
                    val = cls._lookup(str(m.group(1)).split(":", 1)[1], produced)
                return str(val) if val is not None else m.group(0)
            return _PLACEHOLDER_RE.sub(_repl, v)

        intent.locator_value = _sub(intent.locator_value)
        intent.value = _sub(intent.value)
        # 复合主键列表（locator_fields 是列名通常无占位符，但 values 必含）
        if getattr(intent, "locator_fields", None):
            intent.locator_fields = [_sub(x) if isinstance(x, str) else x
                                     for x in intent.locator_fields]
        if getattr(intent, "locator_values", None):
            intent.locator_values = [_sub(x) if isinstance(x, str) else x
                                     for x in intent.locator_values]
        fields = (intent.extras or {}).get("fields")
        if isinstance(fields, dict):
            for k in list(fields.keys()):
                fields[k] = _sub(fields[k])

    @classmethod
    def _capture_produced(cls, res, intent, produced: dict[str, str],
                          seq_counter: dict[str, int]) -> None:
        """从 add 结果提取新产出主键 ID，按多命名键写入 produced。

        门控放宽：ok=True 或 ok=None（验证未结论但行可能已写入）均尝试捕获；
        仅 ok=False（硬失败）跳过。result_rows 无新 PK 时自然 return，不会误捕。
        原 `not getattr(res,"ok",False)` 把 None 当失败 → 复杂链嵌套字段验证
        不结论时 produced 全空 → 下游占位符全悬空 → 整链卡死。
        """
        if res is None or getattr(res, "ok", None) is False:
            return
        rows = getattr(res, "result_rows", None) or []
        # 提取主键 ID 策略：
        # 1) 优先取第一列（col==1，PK_COL=1）——项目所有表主键在第1列，
        #    agent._append_row 已把自动分配的主键回传到 result_rows (col=1)。
        # 2) 回退：列名匹配 _ID_COL_RE 的候选，按列号升序取最小。
        # 必须优先 col==1：主键列名常带说明后缀（如 entity_prefab 的
        # "编号\n（按序递增，不要分段）"），不以 id/编号 结尾 → _ID_COL_RE
        # 的 $ 锚定会漏掉它，first-match 误取"交互id"等非主键列（铁匠老张 bug）。
        pk_row = None
        candidates: list[tuple[int, str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cn = str(row.get("col_name") or "")
            nv = row.get("new_value")
            if nv in (None, ""):
                continue
            col = row.get("col", 9999)
            if col == 1 and pk_row is None:
                pk_row = (col, cn, nv)
            if _ID_COL_RE.search(cn):
                candidates.append((col, cn, nv))
        if pk_row is not None:
            col_name, new_val = pk_row[1], pk_row[2]
        elif candidates:
            candidates.sort(key=lambda x: x[0])
            col_name, new_val = candidates[0][1], candidates[0][2]
        else:
            return
        if new_val in (None, ""):
            return
        val = str(new_val)
        # 向后兼容：最近一个新 ID
        produced["new_id"] = val
        # 显式 produces 标签（解析器可提供的最强信号）
        label = (getattr(intent, "produces_label", None)
                 or ((intent.extras or {}).get("produces") if intent is not None else None))
        if isinstance(label, str) and label.strip():
            ln = cls._norm_name(label)
            produced[ln] = val
            if ln.startswith("new_"):
                produced[ln[4:]] = val
        # 按列名派生英文键（如 prefab_id→{prefab_id,new_prefab_id}）
        cn = cls._norm_name(col_name)
        cn_ascii = re.sub(r"[^a-z0-9_]", "", cn)
        base = None
        if cn_ascii and cn_ascii.endswith("id"):
            produced[cn_ascii] = val
            produced["new_" + cn_ascii] = val
            base = cn_ascii[:-3].rstrip("_") or cn_ascii  # 去尾 _id
        # 有序键：同基名多次产出 → <base>_<n>_id（如 option_1_id/option_2_id）
        if base:
            n = seq_counter.get(base, 0) + 1
            seq_counter[base] = n
            produced[f"{base}_{n}_id"] = val
