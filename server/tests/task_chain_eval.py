"""task_chain.json 复合任务链评估脚本（excel_LLM Agent 内环验证）。

针对 task_chain.json 每条复合指令（跨表多步 + produces 占位符引用链），
用真实 AgentService（excel_LLM Agent 全套：parse_multi / cross_table_splitter /
OperationOrchestrator / skill）在 resources/ 沙箱副本真实执行，以"跑前/跑后
xlsx 行级差异"为 ground truth，与 expected_answer 逐条比对，产出：

  - 链完整性：expected 每步是否真正产出对应行操作
  - 占位符引用一致性：consumer 步引用字段值 == producer 步实际产出的新 ID
    （task_chain 的核心质量指标——引用闭环是否成立）
  - 定位/覆盖/精准/多余写入（复用 table_case_eval 的判定口径）
  - 失败模式归类 → 直接驱动内环优化（解析/路由/定位/字段/引用/副作用）

复用 server/tests/table_case_eval.py 的沙箱、diff_sandbox、build_pristine_index、
_validate_fixture、_build_eval_sheet_aliases 等核心；新增链级 step↔op 绑定与
引用一致性分析（match_chain_steps / analyze_references）。

用法（在 server/ 目录下执行）:
    python -m tests.task_chain_eval --quick 3      # 冒烟：前 3 条链
    python -m tests.task_chain_eval                # 跑全部 10 条链（skill=on）
    python -m tests.task_chain_eval --skill both   # skill on/off A/B 对照
    python -m tests.task_chain_eval --cases-file tests/cases/task_chain.json

前提: codemaker serve 已启动（.env 配好 CODEMAKER_SERVER_URL 等）。
输出: server/tests/reports/task_chain_eval_latest.{md,json}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── 路径 & 环境（须在 import agent/services 之前）──────────
TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
ROOT = SERVER_DIR.parent
RES = ROOT / "resources"
REPORT_DIR = TESTS_DIR / "reports"
DEFAULT_CHAINS_FILE = TESTS_DIR / "cases" / "task_chain.json"


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import table_case_eval as tce  # noqa: E402  复用核心评估原语
from services.agent_service import AgentService  # noqa: E402


# ── 链级数据结构 ──────────────────────────────────────────

@dataclass
class ChainStepResult:
    """单步 expected 与 actual op 的绑定结果（比 tce.EntryResult 多 op 引用）。"""
    entry: tce.EntryResult
    matched_op: Optional[tce.ActualOp] = None
    produces: str = ""           # 该步 expected 标注的 produces 标签（无则空）


@dataclass
class RefCheck:
    """一条占位符引用的闭环校验。"""
    step_index: int             # consumer 步下标
    field: str                  # 引用字段名
    placeholder: str            # 原始占位符（如 <new_prefab_id>）
    producer_index: int         # producer 步下标（-1=未找到 producer）
    expected_value: Any = None  # producer 实际产出的 ID
    actual_value: Any = None    # consumer 步该字段实际写入值
    ok: bool = False
    reason: str = ""


@dataclass
class ChainRunResult:
    cid: int
    input_text: str
    skill_enabled: bool
    ok: bool
    error: str
    elapsed_ms: float
    needs_confirm_used: bool
    steps: list[ChainStepResult] = field(default_factory=list)
    ref_checks: list[RefCheck] = field(default_factory=list)
    extra_ops: int = 0
    extra_ops_off_table: int = 0
    n_expected: int = 0
    n_effective: int = 0
    locate_rate: float = 0.0
    coverage: float = 0.0
    field_accuracy: float = 0.0
    strict_pass: bool = False
    truth_ok: bool = False
    # 链级指标
    chain_complete: bool = False          # 所有 effective 步 status==matched
    ref_total: int = 0
    ref_ok: int = 0
    ref_consistency_rate: float = 0.0
    producers_total: int = 0
    producers_resolved: int = 0           # 产出 ID 成功提取的 producer 数
    fixture_errors: list = field(default_factory=list)
    fixture_error: bool = False
    # capability: error-type-distribution / llm-call-instrumentation
    error_type: str = "unknown"
    llm_stats: dict = field(default_factory=dict)


# ── step↔op 绑定匹配（复刻 tce.match_case，保留 op 引用）──

def _norm_name(name: str) -> str:
    return str(name or "").strip().lower()


def match_chain_steps(expected_answer: list[dict], actual_ops: list[tce.ActualOp],
                      pristine_idx: dict, sheet_alias_map: dict | None = None
                      ) -> tuple[list[ChainStepResult], list[tce.ActualOp]]:
    """逐条 expected 匹配 actual op，返回 (ChainStepResult 列表, 未消费 extra ops)。

    算法与 tce.match_case 一致（含 sheet 别名 / precondition / add-best-score），
    区别：每个 EntryResult 同时绑定命中的 ActualOp（matched_op），供引用一致性分析
    提取 producer 实际产出 ID 与 consumer 字段实际值。
    """
    alias_map = sheet_alias_map or {}
    results: list[ChainStepResult] = []

    for i, e in enumerate(expected_answer):
        table = tce._norm_table(e.get("table", ""))
        sheet = e.get("sheet", "")
        op = e.get("operation", "")
        row_key = e.get("row_key") or {}
        row_content = e.get("row_content") or {}
        produces = e.get("produces", "") or ""

        same_ts_ops = [o for o in actual_ops
                       if o.table == table
                       and tce._sheet_matches(sheet, o.sheet, tce._stem_from_table(table), alias_map)
                       and o.operation == op]
        table_sheet_hit = len(same_ts_ops) > 0

        if op in ("modify", "delete"):
            precondition_ok = True
            if row_key:
                key_field, key_val = next(iter(row_key.items()))
                existing = pristine_idx.get((table, sheet), {}).get(key_field, set())
                precondition_ok = any(tce._values_equal(x, key_val) for x in existing)
            if not precondition_ok:
                ent = tce.EntryResult(i, table, sheet, op, "precondition_missing",
                                      table_sheet_hit=table_sheet_hit,
                                      note="原始数据中未找到 row_key 对应的行，夹具与配表不一致")
                results.append(ChainStepResult(ent, None, produces))
                continue

            cand = None
            for o in same_ts_ops:
                if o.consumed:
                    continue
                base = o.row_before
                if all(tce._values_equal(base.get(k), v) for k, v in row_key.items()):
                    cand = o
                    break
            if cand is None:
                ent = tce.EntryResult(i, table, sheet, op, "missing",
                                      table_sheet_hit=table_sheet_hit,
                                      note="未定位到 row_key 匹配的行操作")
                results.append(ChainStepResult(ent, None, produces))
                continue
            cand.consumed = True
            if op == "delete":
                ent = tce.EntryResult(i, table, sheet, op, "matched",
                                      table_sheet_hit=True, row_located=True,
                                      field_score=1.0, concrete_total=1, concrete_matched=1)
                results.append(ChainStepResult(ent, cand, produces))
                continue
            # modify
            total = matched = 0
            for k, v in row_content.items():
                total += 1
                if tce._is_placeholder(v):
                    if k in cand.changed_fields and cand.changed_fields.get(k) not in (None, ""):
                        matched += 1
                else:
                    if k in cand.changed_fields and tce._values_equal(cand.changed_fields.get(k), v):
                        matched += 1
            score = (matched / total) if total else 1.0
            status = "matched" if score >= 0.999 else ("partial" if score > 0 else "located_only")
            ent = tce.EntryResult(i, table, sheet, op, status,
                                  table_sheet_hit=True, row_located=True,
                                  field_score=score, concrete_total=total, concrete_matched=matched)
            results.append(ChainStepResult(ent, cand, produces))
            continue

        # ── add ──
        concrete = {k: v for k, v in row_content.items() if not tce._is_placeholder(v)}
        best, best_score, best_matched = None, -1.0, 0
        for o in same_ts_ops:
            if o.consumed:
                continue
            matched = sum(1 for k, v in concrete.items()
                          if tce._values_equal(o.row_after.get(k), v))
            score = (matched / len(concrete)) if concrete else (1.0 if o.row_after else 0.0)
            if score > best_score:
                best, best_score, best_matched = o, score, matched
        if best is None:
            ent = tce.EntryResult(i, table, sheet, op, "missing",
                                  table_sheet_hit=table_sheet_hit,
                                  concrete_total=len(concrete),
                                  note="未找到新增行（该表/sheet 无新增记录，或已被其它 expected 项占用）")
            results.append(ChainStepResult(ent, None, produces))
            continue
        if concrete and best_score <= 0:
            best.consumed = True
            ent = tce.EntryResult(i, table, sheet, op, "located_only",
                                  table_sheet_hit=True, row_located=True,
                                  field_score=0.0, concrete_total=len(concrete), concrete_matched=0)
            results.append(ChainStepResult(ent, best, produces))
            continue
        best.consumed = True
        status = "matched" if (not concrete or best_score >= 0.999) else "partial"
        ent = tce.EntryResult(i, table, sheet, op, status,
                              table_sheet_hit=True, row_located=True,
                              field_score=best_score if concrete else 1.0,
                              concrete_total=len(concrete), concrete_matched=best_matched)
        results.append(ChainStepResult(ent, best, produces))

    extra_ops = [o for o in actual_ops if not o.consumed]
    return results, extra_ops


# ── 占位符引用闭环分析 ────────────────────────────────────

def _extract_placeholder_inner(v: Any) -> str:
    """`<new_prefab_id>` → `new_prefab_id`；非占位符返回空。"""
    if tce._is_placeholder(v):
        return str(v).strip()[1:-1].strip()
    return ""


def _producer_pk_value(step: ChainStepResult, expected_entry: dict,
                       pristine_idx: dict) -> Any:
    """从 producer 步绑定的 actual op 提取实际产出的主键 ID。

    优先 pristine 索引记录的主键字段名；回退 expected row_content 里的占位符字段名
    （其值即 <produces> 占位符，列名即主键列）；再回退 row_after 第一个非空值。
    """
    op = step.matched_op
    if op is None or not op.row_after:
        return None
    table = tce._norm_table(expected_entry.get("table", ""))
    sheet = expected_entry.get("sheet", "")
    pk_field = pristine_idx.get((table, sheet), {}).get("__pk__", "")
    if pk_field and pk_field in op.row_after:
        return op.row_after.get(pk_field)
    # 回退：expected row_content 中值为占位符的字段（即主键列）
    for k, v in (expected_entry.get("row_content") or {}).items():
        if tce._is_placeholder(v) and k in op.row_after:
            return op.row_after.get(k)
    # 再回退：row_after 第一个非空值
    for k, v in op.row_after.items():
        if v not in (None, ""):
            return v
    return None


def analyze_references(expected_answer: list[dict], steps: list[ChainStepResult],
                       pristine_idx: dict) -> tuple[list[RefCheck], dict[str, Any]]:
    """校验占位符引用闭环，返回 (RefCheck 列表, actual_produced 映射)。

    actual_produced: {produces_label_norm: producer 实际产出的真实 ID}
    """
    # 1. produces_map: label_norm -> step_idx
    produces_map: dict[str, int] = {}
    for i, e in enumerate(expected_answer):
        lbl = e.get("produces")
        if isinstance(lbl, str) and lbl.strip():
            ln = _norm_name(lbl)
            produces_map.setdefault(ln, i)
            if ln.startswith("new_"):
                produces_map.setdefault(ln[4:], i)
            else:
                produces_map.setdefault("new_" + ln, i)

    # 2. actual_produced: 从每个 producer 步提取真实产出 ID
    actual_produced: dict[str, Any] = {}
    for ln, idx in produces_map.items():
        if idx != produces_map.get(ln):  # 只在首次定义的下标上提取（避免重复）
            continue
        st = steps[idx] if idx < len(steps) else None
        if st is None:
            continue
        real = _producer_pk_value(st, expected_answer[idx], pristine_idx)
        if real is not None and real != "":
            actual_produced[ln] = real

    # 3. 遍历每步占位符引用，比对 consumer 字段实际值 == producer 真实 ID
    checks: list[RefCheck] = []
    for i, e in enumerate(expected_answer):
        st = steps[i] if i < len(steps) else None
        consumer_op = st.matched_op if st else None
        for fld, val in (e.get("row_content") or {}).items():
            inner = _extract_placeholder_inner(val)
            if not inner:
                continue
            ln = _norm_name(inner)
            producer_idx = produces_map.get(ln)
            if producer_idx is None and ln.startswith("new_"):
                producer_idx = produces_map.get(ln[4:])
            if producer_idx is None and not ln.startswith("new_"):
                producer_idx = produces_map.get("new_" + ln)
            expected_val = actual_produced.get(ln)
            if expected_val is None and ln.startswith("new_"):
                expected_val = actual_produced.get(ln[4:])
            if expected_val is None and not ln.startswith("new_"):
                expected_val = actual_produced.get("new_" + ln)
            actual_val = consumer_op.row_after.get(fld) if consumer_op else None
            if producer_idx is None:
                ok, reason = False, "未找到 produces 该占位符的 producer 步"
            elif expected_val is None:
                ok, reason = False, f"producer 步[{producer_idx}]未产出新 ID（行未生成或主键未回传）"
            elif consumer_op is None:
                ok, reason = False, "consumer 步未匹配到实际行操作"
            else:
                ok = tce._values_equal(actual_val, expected_val)
                reason = "引用闭环成立" if ok else (
                    f"引用断裂：期望 {expected_val!r}，实际 {actual_val!r}")
            checks.append(RefCheck(
                step_index=i, field=fld, placeholder=str(val),
                producer_index=producer_idx if producer_idx is not None else -1,
                expected_value=expected_val, actual_value=actual_val,
                ok=ok, reason=reason))
    return checks, actual_produced


# ── 单链执行 ──────────────────────────────────────────────

def _serve_alive() -> bool:
    url = os.environ.get("CODEMAKER_SERVER_URL", "").rstrip("/")
    if not url:
        return False
    try:
        req = urllib.request.Request(url + "/health", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        # 401/403 = serve 在线但需 Basic Auth，算活；5xx 才算 down
        return e.code < 500
    except Exception:
        return False


def run_one_chain(cid: int, chain: dict, enable_skill: bool) -> ChainRunResult:
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"chain_{cid}_{'on' if enable_skill else 'off'}_"))
    sandbox = tmp_dir / "resources"
    shutil.copytree(RES, sandbox)
    service = None
    n_expected = len(chain.get("expected_answer", []))
    try:
        try:
            service = AgentService(resources_dir=sandbox, enable_skill=enable_skill)

            # ── 实时进度打印：注入 step/thinking sink，复用 agent 同款事件流 ──
            # 终端按 6 步流程顺序输出 ✅/❌ + thinking phase，与前端 step 卡片一致。
            _STEP_LABEL = {
                "Step1解析": "Step1 解析", "Step2分区": "Step2 分区",
                "Step3计划": "Step3 计划", "Step4校验": "Step4 校验",
                "Step5应用": "Step5 应用", "Step6汇总": "Step6 汇总",
                "resolve_table": "定位表格", "resolve_sheet": "定位Sheet",
                "match_locator": "匹配定位列", "match_target": "匹配目标列",
                "locate_row": "定位行", "write": "写入", "read_cell": "读取",
                "add_values": "提取新增值", "append_row": "追加行",
                "delete_cell": "清空单元格", "delete_row": "删除行",
            }

            def _step_cb(payload: dict) -> None:
                name = _STEP_LABEL.get(payload.get("name", ""),
                                       payload.get("name", ""))
                mark = "✅" if payload.get("ok") else "❌"
                detail = (payload.get("detail") or "")[:80]
                print(f"        {mark} {name} {detail}".rstrip())

            def _think_cb(phase: str, detail: str = "") -> None:
                # 抑制噪声 phase（多指令聚合模式内部阶段），只打主线进度
                print(f"        → {phase}: {str(detail)[:90]}".rstrip())

            ag = getattr(service, "agent", None)
            if ag is not None:
                ag._agent_step_sink = _step_cb
                ag._agent_thinking_sink = _think_cb

            session_id = f"chain{cid}_{'on' if enable_skill else 'off'}"
            t0 = time.perf_counter()
            resp = service.chat(text=chain["input"], session_id=session_id, dry_run=False)
            needs_confirm_used = False
            if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
                needs_confirm_used = True
                resp = service.chat(text=chain["input"], session_id=session_id, dry_run=False,
                                    confirm_token=resp.confirm_token, confirm_cascade=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            actual_ops = tce.diff_sandbox(sandbox, RES)
            pristine_idx = tce.build_pristine_index(chain["expected_answer"])
            fixture_errors = tce._validate_fixture(chain["expected_answer"], pristine_idx)
            sheet_alias_map = tce._build_eval_sheet_aliases()
            steps, extra_ops_list = match_chain_steps(
                chain["expected_answer"], actual_ops, pristine_idx, sheet_alias_map)
            ref_checks, actual_produced = analyze_references(
                chain["expected_answer"], steps, pristine_idx)

            entries = [s.entry for s in steps]
            effective = [s.entry for s in steps if s.entry.status != "precondition_missing"]
            n_eff = len(effective)
            locate_rate = (sum(e.table_sheet_hit for e in effective) / n_eff) if n_eff else 0.0
            coverage = (sum(e.row_located for e in effective) / n_eff) if n_eff else 0.0
            located = [e for e in effective if e.row_located]
            field_accuracy = (sum(e.field_score for e in located) / len(located)) if located else 0.0

            expected_ts = {(tce._norm_table(e.get("table", "")), e.get("sheet", ""))
                           for e in chain.get("expected_answer", [])}
            extra_ops_off_table = sum(1 for o in extra_ops_list
                                      if (o.table, o.sheet) not in expected_ts)
            penalty = (extra_ops_off_table / n_eff) if n_eff else 0.0
            coverage = max(0.0, coverage - penalty)
            field_accuracy = max(0.0, field_accuracy - penalty)
            strict_pass = bool(effective) and all(e.status == "matched" for e in effective) \
                and len(extra_ops_list) == 0
            truth_ok = (n_eff > 0
                        and all(e.row_located and e.field_score >= 0.999 for e in effective)
                        and extra_ops_off_table == 0)

            # 链级指标
            chain_complete = bool(effective) and all(e.status == "matched" for e in effective)
            ref_total = len(ref_checks)
            ref_ok = sum(1 for c in ref_checks if c.ok)
            ref_consistency_rate = (ref_ok / ref_total) if ref_total else 1.0
            producers_total = len({e.get("produces") for e in chain["expected_answer"]
                                   if e.get("produces")})
            producers_resolved = len(actual_produced) // 2 if actual_produced else 0
            # actual_produced 每个产出写了 2 个键（new_x / x），折半；精确统计用 label 集
            producer_labels = {_norm_name(e.get("produces")) for e in chain["expected_answer"]
                               if e.get("produces")}
            producers_resolved = sum(1 for lbl in producer_labels
                                     if actual_produced.get(lbl) is not None)

            return ChainRunResult(
                cid=cid, input_text=chain["input"], skill_enabled=enable_skill,
                ok=bool(getattr(resp, "ok", False)),
                error=str(getattr(resp, "error", "") or ""),
                elapsed_ms=round(elapsed_ms, 1), needs_confirm_used=needs_confirm_used,
                steps=steps, ref_checks=ref_checks,
                extra_ops=len(extra_ops_list), extra_ops_off_table=extra_ops_off_table,
                n_expected=n_expected, n_effective=n_eff,
                locate_rate=round(locate_rate, 4), coverage=round(coverage, 4),
                field_accuracy=round(field_accuracy, 4), strict_pass=strict_pass,
                truth_ok=truth_ok, chain_complete=chain_complete,
                ref_total=ref_total, ref_ok=ref_ok,
                ref_consistency_rate=round(ref_consistency_rate, 4),
                producers_total=producers_total, producers_resolved=producers_resolved,
                fixture_errors=fixture_errors, fixture_error=bool(fixture_errors),
                error_type=_classify_chain_error(service, resp),
                llm_stats=_collect_chain_llm_stats(service))
        except Exception as e:
            import traceback
            print(f"        [error] chain {cid} ({'on' if enable_skill else 'off'}) 异常: {e}")
            print(traceback.format_exc())
            return ChainRunResult(
                cid=cid, input_text=chain["input"], skill_enabled=enable_skill,
                ok=False, error=f"{type(e).__name__}: {e}",
                elapsed_ms=0.0, needs_confirm_used=False,
                steps=[], n_expected=n_expected, n_effective=0)
    finally:
        try:
            if service is not None and getattr(service, "_file_watcher", None) is not None:
                service._file_watcher.stop()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _classify_chain_error(service, resp) -> str:
    """capability: error-type-distribution。"""
    if resp is None or getattr(resp, "ok", False):
        return "unknown"
    try:
        from agent.excel.repair.error_classifier import classify, VerifyResult
        res_like = type("R", (), {"steps": [], "message": getattr(resp, "message", "") or ""})()
        return classify(None, res_like, VerifyResult(), context={}).error_type.value
    except Exception:
        return "unknown"


def _collect_chain_llm_stats(service) -> dict:
    """capability: llm-call-instrumentation。"""
    try:
        agent = getattr(service, "agent", None)
        if agent is not None and getattr(agent, "_llm_counter", None) is not None:
            return agent._llm_counter.as_dict()
    except Exception:
        pass
    return {}


# ── 聚合 & 失败模式归类 ───────────────────────────────────

def _mean(xs: list) -> float:
    return statistics.mean(xs) if xs else 0.0


def aggregate_chains(results: list[ChainRunResult]) -> dict:
    if not results:
        return {}
    valid = [r for r in results if not r.fixture_error]
    n = len(valid)
    if n == 0:
        return {"n": 0, "n_total": len(results), "n_excluded": len(results),
                "fixture_error_rate": 1.0 if results else 0.0}
    elapsed = [r.elapsed_ms for r in valid]
    es = sorted(elapsed)

    def pct(p):
        return es[min(len(es) - 1, int(len(es) * p))] if es else 0.0

    return {
        "n": n, "n_total": len(results), "n_excluded": len(results) - n,
        "fixture_error_rate": round(sum(r.fixture_error for r in results) / len(results), 4),
        "ok_rate": round(sum(r.ok for r in valid) / n, 4),
        "truth_ok_rate": round(sum(r.truth_ok for r in valid) / n, 4),
        "chain_complete_rate": round(sum(r.chain_complete for r in valid) / n, 4),
        "strict_pass_rate": round(sum(r.strict_pass for r in valid) / n, 4),
        "locate_rate": round(_mean([r.locate_rate for r in valid]), 4),
        "coverage": round(_mean([r.coverage for r in valid]), 4),
        "field_accuracy": round(_mean([r.field_accuracy for r in valid]), 4),
        "ref_consistency_rate": round(_mean([r.ref_consistency_rate for r in valid]), 4),
        "producers_resolve_rate": round(
            _mean([(r.producers_resolved / r.producers_total) if r.producers_total else 1.0
                   for r in valid]), 4),
        "avg_extra_ops": round(_mean([r.extra_ops for r in valid]), 4),
        "avg_extra_off_table": round(_mean([r.extra_ops_off_table for r in valid]), 4),
        "avg_elapsed_ms": round(statistics.mean(elapsed), 1) if elapsed else 0.0,
        "p50_elapsed_ms": round(pct(0.50), 1),
        "p95_elapsed_ms": round(pct(0.95), 1),
        "total_elapsed_s": round(sum(elapsed) / 1000, 1),
        # capability: error-type-distribution / llm-call-instrumentation
        "error_type_distribution": _aggregate_error_types_chains(valid),
        "total_llm_calls": sum(r.llm_stats.get("total_calls", 0) for r in valid),
        "total_tokens": sum(r.llm_stats.get("total_tokens", 0) for r in valid),
        "avg_llm_calls": round(_mean([r.llm_stats.get("total_calls", 0) for r in valid]), 1),
        "success_path_calls": sum(r.llm_stats.get("success_path_calls", 0) for r in valid),
        "failure_path_calls": sum(r.llm_stats.get("failure_path_calls", 0) for r in valid),
    }


def _aggregate_error_types_chains(results) -> dict:
    """聚合失败链的 ErrorType 分布。"""
    dist: dict[str, int] = {}
    for r in results:
        if not r.ok:
            et = r.error_type or "unknown"
            dist[et] = dist.get(et, 0) + 1
    return dist


def classify_failures(results: list[ChainRunResult]) -> dict:
    """归类失败模式，供内环优化定位瓶颈。"""
    modes: dict[str, int] = {
        "parse_or_exec_failed": 0,   # agent 整体 ok=False / 异常
        "table_sheet_miss": 0,       # 路由到错误表/sheet
        "row_missing": 0,            # 行操作未产出
        "field_error": 0,            # 字段值不符
        "ref_broken": 0,             # 占位符引用断裂
        "producer_not_resolved": 0,  # producer 未产出新 ID
        "extra_writes": 0,           # 多余/异表写入
        "precondition_missing": 0,   # 夹具不一致
    }
    samples: dict[str, list[int]] = {k: [] for k in modes}
    for r in results:
        if r.fixture_error:
            for s in r.steps:
                if s.entry.status == "precondition_missing":
                    modes["precondition_missing"] += 1
            samples["precondition_missing"].append(r.cid)
            continue
        if not r.ok and not r.steps:
            modes["parse_or_exec_failed"] += 1
            samples["parse_or_exec_failed"].append(r.cid)
            continue
        for s in r.steps:
            st = s.entry
            if not st.table_sheet_hit:
                modes["table_sheet_miss"] += 1
                samples["table_sheet_miss"].append(r.cid)
            elif st.status == "missing":
                modes["row_missing"] += 1
                samples["row_missing"].append(r.cid)
            elif st.status in ("partial", "located_only"):
                modes["field_error"] += 1
                samples["field_error"].append(r.cid)
            elif st.status == "precondition_missing":
                modes["precondition_missing"] += 1
        for c in r.ref_checks:
            if not c.ok:
                if c.producer_index < 0 or c.expected_value is None:
                    modes["producer_not_resolved"] += 1
                    samples["producer_not_resolved"].append(r.cid)
                else:
                    modes["ref_broken"] += 1
                    samples["ref_broken"].append(r.cid)
        if r.extra_ops_off_table > 0:
            modes["extra_writes"] += r.extra_ops_off_table
            samples["extra_writes"].append(r.cid)
    return {"modes": modes, "samples": {k: sorted(set(v)) for k, v in samples.items() if v}}


# ── 报告渲染 ──────────────────────────────────────────────

_STATUS_LABEL = {
    "matched": "✅", "partial": "🟡", "located_only": "🟠",
    "missing": "❌", "precondition_missing": "⚪",
}


def _render_chain_detail(r: ChainRunResult) -> str:
    lines = [f"### 链 {r.cid}: {r.input_text}", ""]
    lines.append(f"- 响应ok: {r.ok} | 链完整: {r.chain_complete} | 严格通过: {r.strict_pass} | truth_ok: {r.truth_ok}")
    lines.append(f"- 定位 {r.locate_rate:.2f} | 覆盖 {r.coverage:.2f} | 精准 {r.field_accuracy:.2f}"
                 f" | 引用一致 {r.ref_consistency_rate:.2f} ({r.ref_ok}/{r.ref_total})"
                 f" | producer产出 {r.producers_resolved}/{r.producers_total}")
    lines.append(f"- 多余写入 {r.extra_ops} (异表 {r.extra_ops_off_table}) | 耗时 {r.elapsed_ms:.0f}ms")
    if r.error:
        lines.append(f"- 错误: {r.error}")
    lines.append("")
    lines.append("| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, s in enumerate(r.steps):
        e = s.entry
        ts = f"{e.table}.{e.sheet}"
        lab = _STATUS_LABEL.get(e.status, e.status)
        mo = "有" if s.matched_op else "无"
        lines.append(f"| {i+1} | {ts} | {e.operation} | {s.produces or '-'} | "
                     f"{lab} {e.status} | {e.field_score:.2f} | {mo} |")
    if r.ref_checks:
        lines.append("")
        lines.append("占位符引用闭环校验：")
        lines.append("| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in r.ref_checks:
            ok = "✅" if c.ok else "❌"
            lines.append(f"| {c.step_index+1} | {c.field} | {c.placeholder} | "
                         f"{c.producer_index+1 if c.producer_index >= 0 else '-'} | "
                         f"{str(c.expected_value)[:24]} | {str(c.actual_value)[:24]} | {ok} |")
    if r.fixture_errors:
        lines.append("")
        lines.append("夹具错误：")
        for fe in r.fixture_errors:
            lines.append(f"- [{fe.get('kind')}] {fe.get('detail', '')[:80]}")
    lines.append("")
    return "\n".join(lines)


def render_report(chains: list[dict], results: list[ChainRunResult],
                  skill_label: str, failures: dict) -> str:
    agg = aggregate_chains(results)
    lines = [
        "# task_chain 复合任务链评估报告（excel_LLM Agent 内环验证）",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样例来源: task_chain.json（{agg.get('n', 0)}/{agg.get('n_total', 0)} 条有效，"
        f"{agg.get('n_excluded', 0)} 条夹具排除）",
        f"- 评估对象: skill={skill_label}（TableAgent 全套：parse_multi + cross_table_splitter"
        f" + OperationOrchestrator 占位符编排 + skill 配置）",
        "- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条链在"
        " resources/ 临时沙箱副本真实执行，跑前/跑后 xlsx 行级差异作为 ground truth",
        "- 核心增量: 链完整性 + 占位符引用一致性（consumer 引用字段 == producer 实际产出 ID）",
        "",
        "## 一、总体指标",
        "",
        "| 指标 | 说明 | 值 |",
        "|---|---|---|",
        f"| 链完整率 | 整链所有 expected 步 status==matched | {agg.get('chain_complete_rate', 0):.4f} |",
        f"| truth_ok率 | 全步 row_located+字段满分+无异表多余写入 | {agg.get('truth_ok_rate', 0):.4f} |",
        f"| 引用一致率 | 占位符引用闭环成立的比例（task_chain 核心） | {agg.get('ref_consistency_rate', 0):.4f} |",
        f"| producer产出率 | produces 标注的步实际回传新 ID 的比例 | {agg.get('producers_resolve_rate', 0):.4f} |",
        f"| 定位功能 | 命中正确 table+sheet+操作类型 | {agg.get('locate_rate', 0):.4f} |",
        f"| 覆盖度 | expected 行操作真正产出比例（扣异表多余） | {agg.get('coverage', 0):.4f} |",
        f"| 精准程度 | 被定位行字段值完全正确比例 | {agg.get('field_accuracy', 0):.4f} |",
        f"| 严格通过率 | 整链 100%命中且无多余写入 | {agg.get('strict_pass_rate', 0):.4f} |",
        f"| 响应ok率 | Agent 自报告执行成功 | {agg.get('ok_rate', 0):.4f} |",
        f"| 平均多余写入 | 未被 expected 认领的行改动 | {agg.get('avg_extra_ops', 0):.4f} |",
        f"| 平均异表写入 | 写到 expected 之外的表 | {agg.get('avg_extra_off_table', 0):.4f} |",
        f"| 平均耗时(ms) | 单链端到端 | {agg.get('avg_elapsed_ms', 0):.1f} |",
        f"| P50/P95(ms) | | {agg.get('p50_elapsed_ms', 0):.1f} / {agg.get('p95_elapsed_ms', 0):.1f} |",
        f"| 总耗时(s) | | {agg.get('total_elapsed_s', 0):.1f} |",
        "",
        "## 二、失败模式归类（内环优化定位）",
        "",
        "| 失败模式 | 计数 | 涉及链 | 优化方向 |",
        "|---|---|---|---|",
    ]
    guide = {
        "parse_or_exec_failed": "parse_multi 超时/LLM 不可用 → 增大超时/降级 splitter 兜底",
        "table_sheet_miss": "路由或 sheet 别名缺失 → 补 table_context/sheet_aliases skill",
        "row_missing": "add 未落行/modify 未定位行 → 查列定位与主键自增逻辑",
        "field_error": "字段值写错/枚举未解析/类型不符 → 补 column_aliases/enum_mappings",
        "ref_broken": "占位符替换错误或 consumer 字段名错 → 修 OperationOrchestrator._capture_produced 列名派生",
        "producer_not_resolved": "producer 新 ID 未回传 result_rows → 修 _append_row 主键回传/produces 标注",
        "extra_writes": "过度级联/误改它表 → 收紧 cascade_rules/反模式拦截",
        "precondition_missing": "夹具与配表不一致（非 Agent 缺陷）→ 同步测试夹具或配表",
    }
    for mode, cnt in failures["modes"].items():
        samples = failures["samples"].get(mode, [])
        sm = ",".join(str(x) for x in samples[:8]) + ("..." if len(samples) > 8 else "")
        lines.append(f"| {mode} | {cnt} | {sm or '-'} | {guide[mode]} |")

    lines += ["", "## 三、每条链详情", ""]
    for i, chain in enumerate(chains):
        if i < len(results):
            lines.append(_render_chain_detail(results[i]))

    # 表现最差链
    valid = [r for r in results if not r.fixture_error]
    worst = sorted(valid, key=lambda r: (r.chain_complete, r.ref_consistency_rate,
                                         r.coverage + r.field_accuracy))[:5]
    lines += ["## 四、表现最差链 Top5（优先优化目标）", "",
              "| cid | 链完整 | 引用一致 | 覆盖 | 精准 | input |",
              "|---|---|---|---|---|---|"]
    for r in worst:
        txt = r.input_text[:36].replace("|", "/")
        lines.append(f"| {r.cid} | {r.chain_complete} | {r.ref_consistency_rate:.2f} | "
                     f"{r.coverage:.2f} | {r.field_accuracy:.2f} | {txt} |")

    lines += ["", "## 五、内环优化建议", ""]
    m = failures["modes"]
    suggestions = []
    if m["parse_or_exec_failed"]:
        suggestions.append(f"- 解析/执行失败 {m['parse_or_exec_failed']} 处：检查 codemaker serve "
                           "可用性与 CODEMAKER_API_TIMEOUT，巨型指令优先走 splitter fast-path。")
    if m["table_sheet_miss"]:
        suggestions.append(f"- 表/sheet 路由失误 {m['table_sheet_miss']} 处：补 table_context.yaml "
                           "关键词与 sheet_aliases.yaml，覆盖 NPC/对话/刷新等跨表场景。")
    if m["row_missing"]:
        suggestions.append(f"- 行操作未产出 {m['row_missing']} 处：核查 add 主键自增与 modify 行定位"
                           "（首列空/前缀剥离）链路。")
    if m["field_error"]:
        suggestions.append(f"- 字段错误 {m['field_error']} 处：补 column_aliases / enum_mappings / "
                           "value_constraints，强化枚举值预解析与类型校验。")
    if m["ref_broken"] or m["producer_not_resolved"]:
        suggestions.append(f"- 引用断裂 {m['ref_broken']} 处 + producer 未产出 "
                           f"{m['producer_not_resolved']} 处：这是 task_chain 核心瓶颈。核查 "
                           "OperationOrchestrator._capture_produced 主键列名派生（首列 col==1 优先）"
                           "与 _resolve_placeholders 占位符替换覆盖；确保 add 结果 result_rows 回传"
                           "主键新值，produces 标签与占位符名对齐。")
    if m["extra_writes"]:
        suggestions.append(f"- 多余写入 {m['extra_writes']} 处：收紧 cascade_rules 与 anti_patterns，"
                           "防止过度级联改写 expected 之外的表。")
    if not suggestions:
        suggestions.append("- 全链通过，无明显失败模式；可考虑扩充 task_chain 用例集做回归基线。")
    lines += suggestions
    lines += [
        "",
        "注意事项：",
        "- ⚪ 夹具缺失表示 expected 的 row_key 在 resources/ 真实数据中不存在（非 Agent 缺陷），"
        "已排除出统计；若需评估该链请同步夹具或配表。",
        "- 引用一致性是 task_chain 区别于单表用例的核心指标：producer 步产出的新 ID 必须被"
        "consumer 步正确引用写入，否则跨表配置在运行期无法关联。",
        "- 每条链在独立临时沙箱执行，互不影响；跑完即删，不污染真实 resources/。",
    ]
    return "\n".join(lines)


# ── 序列化 & 保存 ─────────────────────────────────────────

def _ser_step(s: ChainStepResult) -> dict:
    d = s.entry.__dict__.copy()
    d["produces"] = s.produces
    d["matched_op"] = bool(s.matched_op)
    return d


def _ser_ref(c: RefCheck) -> dict:
    return c.__dict__.copy()


def _ser(r: ChainRunResult) -> dict:
    d = r.__dict__.copy()
    d["steps"] = [_ser_step(s) for s in r.steps]
    d["ref_checks"] = [_ser_ref(c) for c in r.ref_checks]
    return d


def _save(out_dir: Path, chains: list[dict], results: list[ChainRunResult],
          skill_label: str, meta: dict) -> None:
    try:
        failures = classify_failures(results)
        payload = {
            "meta": meta,
            "aggregate": aggregate_chains(results),
            "failures": failures,
            "skill": skill_label,
            "results": [_ser(r) for r in results],
        }
        (out_dir / "task_chain_eval_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (out_dir / "task_chain_eval_latest.md").write_text(
            render_report(chains, results, skill_label, failures), encoding="utf-8")
    except Exception as e:
        print(f"  [warn] 保存失败（不影响继续跑）: {e}")


# ── 主流程 ────────────────────────────────────────────────

def load_chains(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", type=int, default=0, help="只跑前 N 条冒烟")
    ap.add_argument("--start", type=int, default=1, help="从第 N 条开始（1-based）")
    ap.add_argument("--end", type=int, default=0, help="到第 N 条结束（含），0=到最后")
    ap.add_argument("--cases-file", type=str, default=str(DEFAULT_CHAINS_FILE))
    ap.add_argument("--out", type=str, default=str(REPORT_DIR))
    ap.add_argument("--skill", choices=["on", "both"], default="on",
                    help="on=仅 skill=on（当前 agent）；both=on/off A/B 对照")
    args = ap.parse_args()

    # 阻止 skill_updater 嵌套跑 mini 回归（同 table_case_eval）
    os.environ["TABLE_CASE_EVAL_RUNNING"] = "1"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not _serve_alive():
        print("⚠ codemaker serve 不可达（CODEMAKER_SERVER_URL="
              f"{os.environ.get('CODEMAKER_SERVER_URL','')}）。请先启动：codemaker serve")
        print("  脚本仍会尝试运行，但 LLM 调用将失败。")

    chains = load_chains(Path(args.cases_file))
    total = len(chains)
    start = max(1, min(args.start, total))
    end = args.end if args.end > 0 else total
    end = min(end, total)
    if args.quick and start == 1:
        end = min(end, args.quick)

    run_chains: list[dict] = []
    results: list[ChainRunResult] = []
    print(f"共 {total} 条链，本轮 [{start}-{end}]，skill={args.skill}，"
          "真实 AgentService 执行中...\n")

    for i, chain in enumerate(chains, start=1):
        if i < start or i > end:
            continue
        print(f"[{i}/{total}] (on) {chain['input'][:48]}")
        t0 = time.time()
        r = run_one_chain(i, chain, enable_skill=True)
        print(f"        ok={r.ok} 链完整={r.chain_complete} 引用一致={r.ref_consistency_rate:.2f}"
              f" ({r.ref_ok}/{r.ref_total}) cov={r.coverage:.2f} acc={r.field_accuracy:.2f}"
              f" {r.elapsed_ms:.0f}ms ({time.time()-t0:.1f}s)")
        results.append(r)
        run_chains.append(chain)
        done = i - start + 1
        span = end - start + 1
        _save(out_dir, run_chains, results, args.skill,
              {"start": start, "end": end, "done": done, "span": span,
               "total_all": total, "last_cid": i})
        print(f"        [已保存 {done}/{span} → task_chain_eval_latest.json/.md]\n")

    print("=" * 60)
    print(f"报告: {out_dir / 'task_chain_eval_latest.md'}")
    print(f"数据: {out_dir / 'task_chain_eval_latest.json'}")
    agg = aggregate_chains(results)
    if agg:
        print(f"链完整率={agg.get('chain_complete_rate', 0):.2f} "
              f"引用一致率={agg.get('ref_consistency_rate', 0):.2f} "
              f"覆盖={agg.get('coverage', 0):.2f} 精准={agg.get('field_accuracy', 0):.2f}")
        print(f"ErrorType 分布: {agg.get('error_type_distribution', {})}")
        print(f"LLM 调用: total={agg.get('total_llm_calls',0)} avg={agg.get('avg_llm_calls',0)} tokens={agg.get('total_tokens',0)}")

    # capability: eval-baseline-management —— 归档
    try:
        import json as _json
        from tests.eval_baseline import archive_run, make_run_id
        tag = os.environ.get("EVAL_BASELINE_TAG", "")
        rid = make_run_id(tag=tag or None)
        jf = out_dir / "task_chain_eval_latest.json"
        jd = _json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else {}
        if isinstance(jd, dict):
            jd["summary"] = agg
        else:
            jd = {"summary": agg}
        md = (out_dir / "task_chain_eval_latest.md").read_text(encoding="utf-8") if (out_dir / "task_chain_eval_latest.md").exists() else ""
        archive_run("task_chain_eval", jd, md, run_id=rid)
        print(f"归档: reports/archive/task_chain_eval_{rid}.json")
    except Exception as e:
        print(f"[warn] 归档失败: {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
