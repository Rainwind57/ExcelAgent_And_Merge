"""Step1.5 契约校验层（准确率优先，LLM 深度参与）。

定位：Step1 parse 之后、Step2 validate 之前。
原则：LLM 产出一律视为不可信。规则做 grounding 硬门 + 成本控制，LLM 做语义裁决
      作为准确率主承担者。每层都是"规则先判 → LLM 复核/修正"，不是"规则判不了才
      问 LLM"。

5 层串行校验：
  1. 表/sheet grounding：规则判存在性 + LLM 判选对了吗/该不该存在
  2. 列名 grounding：规则判命中 + LLM 消歧/判该不该有
  3. 操作语义一致性：规则检测碎片化 + LLM 复核整批操作语义
  4. produces/consumes 闭环：规则集合比对 + LLM 裁决漏解析 vs 真错
  5. 覆盖完整性：全 LLM，按 entity_ledger 审计段内实体是否都有 intent

所有 LLM 调用受 LLMBudget 限制，耗尽降级为 soft warning，不无限叠。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from .contracts import STEP1_5_CONTRACT, StepContext, StepError, StepResult
from .llm_budget import LLMBudget

logger = logging.getLogger(__name__)

_IDX_RE = re.compile(r"\[\d+\]$")


def _norm_key(s) -> str:
    s = str(s or "").strip()
    s = _IDX_RE.sub("", s)
    s = re.sub(r"\[(?:PK|FK→[^\]]+|范围:[^\]]+|枚举:[^\]]+|候选来源:[^\]]+)\]", "", s).strip()
    return s.split(":", 1)[0].strip().lower()


def _intent_id(it) -> str:
    return f"{getattr(it, 'table_hint', '?')}/{getattr(it, 'sheet_hint', '?')}"


def _fields(it) -> dict:
    f = (getattr(it, "extras", None) or {}).get("fields")
    return f if isinstance(f, dict) else {}


def _produces(it) -> str:
    return str(getattr(it, "produces_label", "") or
              ((getattr(it, "extras", None) or {}).get("produces")) or "").strip()


def _consumes(it) -> list:
    out = list(getattr(it, "consumes_labels", []) or [])
    for v in _fields(it).values():
        if isinstance(v, str):
            for m in re.finditer(r"<\s*([^>]+?)\s*>", v):
                out.append(m.group(1).strip())
    return out


class ContractGate:
    """Step1.5 契约校验。注入 schema_getter/data_getter/call_llm_raw。"""

    def __init__(self, schema_getter=None, data_getter=None,
                 call_llm_raw=None, cli=None):
        self._schema_getter = schema_getter
        self._data_getter = data_getter
        self._call_llm_raw = call_llm_raw
        self._cli = cli
        self._budget = LLMBudget(int(os.getenv("CODEMAKER_CONTRACT_GATE_BUDGET", "6")))

    def execute(self, ctx: StepContext) -> StepResult:
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []
        report = {"layer1": {}, "layer2": {}, "layer3": {}, "layer4": {}, "layer5": {}}

        s1 = ctx.get_result("step1_parse")
        intents = (s1.artifacts.get("intents") if s1 else []) or []
        segments = (s1.artifacts.get("segments") if s1 else []) or []
        if not intents:
            return StepResult(
                step_id=STEP1_5_CONTRACT, ok=True,
                warnings=["Step1 无 intents，契约校验跳过"],
                metrics={"dur_ms": int((time.time() - t0) * 1000)},
                artifacts={"contract_report": report, "intents": intents})

        intents = self._layer1_table_sheet_grounding(intents, errors, warnings, report)
        if not intents:
            return self._fail(errors, warnings, report, t0, "表/工作表校验后无有效意图")

        intents = self._layer2_column_grounding(intents, errors, warnings, report)
        if not intents:
            return self._fail(errors, warnings, report, t0, "列名校验后无有效意图")

        intents = self._layer3_operation_semantics(intents, errors, warnings, report,
                                                   ctx.user_text)
        if not intents:
            return self._fail(errors, warnings, report, t0, "操作语义校验后无有效意图")

        intents = self._layer4_reference_closure(intents, errors, warnings, report,
                                                 ctx.user_text)
        if not intents:
            return self._fail(errors, warnings, report, t0, "引用闭环校验后无有效意图")

        intents = self._layer5_coverage_completeness(intents, segments, errors,
                                                     warnings, report, ctx.user_text)

        ok = not any(e.is_hard for e in errors)
        return StepResult(
            step_id=STEP1_5_CONTRACT, ok=ok,
            errors=errors, warnings=warnings,
            metrics={"dur_ms": int((time.time() - t0) * 1000),
                     "llm_calls": self._budget.limit - self._budget.remaining},
            artifacts={"contract_report": report, "intents": intents})

    def _fail(self, errors, warnings, report, t0, msg):
        errors.append(StepError(
            step_id=STEP1_5_CONTRACT, error_type="contract_gate_empty",
            message=msg, is_hard=True))
        return StepResult(
            step_id=STEP1_5_CONTRACT, ok=False, errors=errors, warnings=warnings,
            metrics={"dur_ms": int((time.time() - t0) * 1000)},
            artifacts={"contract_report": report, "intents": []})

    # ── 层 1：表/sheet grounding ──
    def _layer1_table_sheet_grounding(self, intents, errors, warnings, report):
        table_index = {}
        if self._cli:
            try:
                table_index = {p.stem: p for p in self._cli.list_tables()}
            except Exception:
                table_index = {}

        sheets_cache: dict[str, set[str]] = {}
        def _real_sheets(stem):
            if stem in sheets_cache:
                return sheets_cache[stem]
            p = table_index.get(stem) or table_index.get(stem.lower())
            ss = set()
            if p is not None:
                try:
                    ss = {s for s in (self._cli.get_sheets(p) or [])}
                except Exception:
                    ss = set()
            sheets_cache[stem] = ss
            return ss

        kept: list = []
        dropped: list = []
        for it in intents:
            stem = (getattr(it, "table_hint", "") or "").strip()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            if not stem or stem not in table_index:
                dropped.append(f"{stem}/{sheet}")
                errors.append(StepError(
                    step_id=STEP1_5_CONTRACT, error_type="invalid_table",
                    message=f"表「{stem}」不存在于真实表池，已丢弃",
                    table=stem, is_hard=True))
                continue
            real_sheets = _real_sheets(stem)
            if sheet and real_sheets and sheet not in real_sheets:
                # 尝试自动修正：按字段命中数选真实 sheet
                fixed = self._repair_sheet(stem, sheet, real_sheets, _fields(it))
                if fixed and fixed != sheet:
                    warnings.append(f"sheet 自修正：{stem}/{sheet} → {fixed}")
                    try:
                        it.sheet_hint = fixed
                    except Exception:
                        pass
                    sheet = fixed
                else:
                    dropped.append(f"{stem}/{sheet}")
                    errors.append(StepError(
                        step_id=STEP1_5_CONTRACT, error_type="invalid_sheet",
                        message=f"sheet「{sheet}」不存在于 {stem}，无法修正，已丢弃",
                        table=stem, sheet=sheet, is_hard=True))
                    continue
            kept.append(it)

        # LLM 复核：把通过的 intent 清单 + 原文给 LLM，判定幻觉表/选错表
        if self._budget.try_consume() and self._call_llm_raw and kept:
            reviewed = self._llm_review_table_selection(kept, table_index, sheets_cache)
            if reviewed is not None:
                # reviewed = {"drop":[idx...], "fix_sheet":[{idx,sheet}...]}
                drop_idx = set(reviewed.get("drop", []) or [])
                fix = {int(x.get("idx", 0)): x.get("sheet", "")
                       for x in (reviewed.get("fix_sheet", []) or [])
                       if isinstance(x, dict)}
                new_kept = []
                for i, it in enumerate(kept, start=1):
                    if i in drop_idx:
                        warnings.append(f"智能校验判定为幻觉表，已丢弃：{_intent_id(it)}")
                        continue
                    if i in fix and fix[i]:
                        old = getattr(it, "sheet_hint", "")
                        try:
                            it.sheet_hint = fix[i]
                        except Exception:
                            pass
                        warnings.append(f"智能校验修正工作表：{old} → {fix[i]}")
                    new_kept.append(it)
                kept = new_kept

        report["layer1"] = {"kept": len(kept), "dropped": len(dropped)}
        return kept

    def _repair_sheet(self, stem, sheet, real_sheets, fields):
        if not self._schema_getter:
            return sheet
        keys = {_norm_key(k) for k in (fields or {}).keys() if _norm_key(k)}
        if not keys:
            return sheet
        best, best_score = sheet, -1
        for sh in real_sheets:
            if "说明" in sh or "CONFIG" in sh.upper():
                continue
            try:
                hdrs, trow = self._schema_getter(stem, sh)
            except Exception:
                continue
            sk = set()
            for h, t in zip(hdrs or [], trow or []):
                hn = _norm_key(h)
                tn = _norm_key(t)
                if hn:
                    sk.add(hn)
                if tn:
                    sk.add(tn)
            score = len(keys & sk)
            if score > best_score:
                best, best_score = sh, score
        if best_score >= 1 and best != sheet:
            return best
        return sheet

    def _llm_review_table_selection(self, intents, table_index, sheets_cache):
        lines = []
        for i, it in enumerate(intents, start=1):
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            fields_keys = list(_fields(it).keys())[:8]
            lines.append(f"{i}. {stem}/{sheet} fields={fields_keys}")
        prompt = (
            "你是 Step1.5 表/sheet 选择复核员。下面是 LLM 拆出的 intent 清单。\n"
            "对每条判定：表选对了吗？sheet 选对了吗？是否是幻觉表（指令根本没要求）？\n"
            "只能从真实表池里选 sheet。不确定是否幻觉时不要轻易判 drop。\n\n"
            f"## intent 清单\n" + "\n".join(lines) + "\n\n"
            "## 输出 JSON\n"
            '{"drop":[intent序号], "fix_sheet":[{"idx":序号,"sheet":"正确sheet名"}]}\n'
            "只输出 JSON，不要解释。"
        )
        raw = self._call_llm_raw(prompt, timeout=25)
        return self._parse_json(raw)

    # ── 层 2：列名 grounding ──
    def _layer2_column_grounding(self, intents, errors, warnings, report):
        unknown_by_intent: dict[int, list] = {}
        for i, it in enumerate(intents):
            stem = (getattr(it, "table_hint", "") or "").strip()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            if not self._schema_getter:
                continue
            try:
                hdrs, trow = self._schema_getter(stem, sheet)
            except Exception:
                continue
            real_keys = set()
            for h, t in zip(hdrs or [], trow or []):
                hn = _norm_key(h)
                tn = _norm_key(t)
                if hn:
                    real_keys.add(hn)
                if tn:
                    real_keys.add(tn)
                    if "." in tn:
                        real_keys.add(tn.rsplit(".", 1)[-1])
            fields = _fields(it)
            unknown = []
            for k in fields.keys():
                if _norm_key(k) not in real_keys:
                    unknown.append(k)
            if unknown:
                unknown_by_intent[i] = unknown

        if not unknown_by_intent:
            report["layer2"] = {"unknown_columns": 0}
            return intents

        # LLM 列名消歧（复用 column_resolution 风格，一次调所有坏列）
        if self._budget.try_consume() and self._call_llm_raw:
            self._llm_resolve_columns(intents, unknown_by_intent, warnings, report)
        else:
            for i, cols in unknown_by_intent.items():
                it = intents[i]
                for c in cols:
                    errors.append(StepError(
                        step_id=STEP1_5_CONTRACT, error_type="col_not_found",
                        message=f"列「{c}」不存在于 {_intent_id(it)}",
                        table=getattr(it, "table_hint", ""),
                        sheet=getattr(it, "sheet_hint", ""),
                        column=c, is_hard=False))
        report["layer2"] = {"unknown_columns": sum(len(v) for v in unknown_by_intent.values())}
        return intents

    def _llm_resolve_columns(self, intents, unknown_by_intent, warnings, report):
        for i, cols in unknown_by_intent.items():
            it = intents[i]
            stem = (getattr(it, "table_hint", "") or "").strip()
            sheet = (getattr(it, "sheet_hint", "") or "").strip()
            try:
                hdrs, trow = self._schema_getter(stem, sheet)
            except Exception:
                continue
            schema_desc = "; ".join(f"{h}|{t}" for h, t in zip(hdrs or [], trow or [])
                                    if h or t)
            prompt = (
                f"intent {stem}/{sheet} 有下列列名不在真实表头里：{cols}\n"
                f"真实表头：{schema_desc[:2000]}\n"
                "为每个坏列名选最接近的真实列名；若该列确实不存在，返回 null。\n"
                '输出 JSON：[{"bad":"原列名","real":"真实列名或null"}]'
            )
            raw = self._call_llm_raw(prompt, timeout=25)
            mapping = self._parse_json(raw)
            if not isinstance(mapping, list):
                continue
            fields = _fields(it)
            changed = False
            for m in mapping:
                if not isinstance(m, dict):
                    continue
                bad = m.get("bad")
                real = m.get("real")
                if bad and real and bad in fields:
                    fields[real] = fields.pop(bad)
                    changed = True
                    warnings.append(f"列名消歧：{bad} → {real}")
                    changed = True
                elif bad and not real:
                    errors = getattr(it, "extras", {}).get("_contract_errors", [])
                    errors.append(f"列 {bad} 确认不存在")
                    getattr(it, "extras", {})["_contract_errors"] = errors
            if changed:
                try:
                    it.extras["fields"] = fields
                except Exception:
                    pass

    # ── 层 3：操作语义一致性 ──
    def _layer3_operation_semantics(self, intents, errors, warnings, report, raw_text):
        # 规则：add+set 碎片化合并（同表/sheet，set 的 locator 指向 add 的 produces）
        merged_count = 0
        i = 0
        while i < len(intents) - 1:
            cur, nxt = intents[i], intents[i + 1]
            if (getattr(cur, "action", "") == "add"
                    and getattr(nxt, "action", "") in ("set", "modify")
                    and getattr(cur, "table_hint", "") == getattr(nxt, "table_hint", "")
                    and getattr(cur, "sheet_hint", "") == getattr(nxt, "sheet_hint", "")):
                cur_prod = _produces(cur)
                nxt_loc = (getattr(nxt, "extras", {}) or {}).get("locator_field", "") or getattr(nxt, "locator_field", "") or ""
                nxt_loc_val = (getattr(nxt, "extras", {}) or {}).get("locator_value", "") or getattr(nxt, "locator_value", "") or ""
                if (cur_prod and nxt_loc_val and
                        (cur_prod == nxt_loc_val
                         or f"<{cur_prod}>" == nxt_loc_val)):
                    # 合并：set 的 fields 并入 add
                    cf = _fields(cur)
                    nf = _fields(nxt)
                    cf.update(nf)
                    try:
                        cur.extras["fields"] = cf
                    except Exception:
                        pass
                    intents.pop(i + 1)
                    merged_count += 1
                    warnings.append(f"合并碎片化的新增+修改：{_intent_id(cur)}")
                    continue
            i += 1

        # LLM 复核整批操作语义
        if self._budget.try_consume() and self._call_llm_raw and intents:
            self._llm_review_operation_semantics(intents, errors, warnings, raw_text)
        report["layer3"] = {"merged": merged_count}
        return intents

    def _llm_review_operation_semantics(self, intents, errors, warnings, raw_text):
        lines = []
        for i, it in enumerate(intents, start=1):
            act = getattr(it, "action", "")
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            fk = list(_fields(it).keys())[:8]
            lines.append(f"{i}. action={act} table={stem}/{sheet} fields={fk}")
        prompt = (
            "你是 Step1.5 操作语义复核员。下面是 LLM 拆出的 intent 清单和原文。\n"
            "判定：\n"
            "1) 这批 intent 覆盖了原文所有动作吗？\n"
            "2) 有没有同一实体被拆碎（该 add 却 add+set 分裂）？\n"
            "3) 有没有操作类型选错（该 add 却 set）？\n"
            "4) 有没有幻觉 intent（原文没要求的）？\n\n"
            f"## 原文\n{raw_text[:3000]}\n\n## intent 清单\n" + "\n".join(lines) + "\n\n"
            '输出 JSON：{"merge":[[idx1,idx2]], "fix_action":[{"idx":n,"action":"add|set|delete|get"}],'
            ' "hallucinate":[idx], "missing":["缺失的动作描述"]}\n只输出 JSON。'
        )
        raw = self._call_llm_raw(prompt, timeout=30)
        result = self._parse_json(raw)
        if not isinstance(result, dict):
            return
        for idx in (result.get("hallucinate") or []):
            if isinstance(idx, int) and 1 <= idx <= len(intents):
                warnings.append(f"智能校验疑似幻觉意图：{_intent_id(intents[idx-1])}")
        for m in (result.get("missing") or []):
            if isinstance(m, str) and m:
                warnings.append(f"智能校验疑似漏解析：{m}")
        # fix_action / merge 留后续迭代，先标 warning 不自动改

    # ── 层 4：produces/consumes 闭环 ──
    def _layer4_reference_closure(self, intents, errors, warnings, report, raw_text):
        produced_values: set[str] = set()
        produced_labels: set[str] = set()
        for it in intents:
            lbl = _produces(it)
            if lbl:
                produced_labels.add(lbl)
            for k, v in _fields(it).items():
                if _norm_key(k).endswith("id") or "编号" in str(k):
                    if isinstance(v, (int, str)) and str(v).strip().isdigit():
                        produced_values.add(str(v).strip())

        dangling = []
        for it in intents:
            for c in _consumes(it):
                if c in produced_labels:
                    continue
                if c.strip().isdigit() and c in produced_values:
                    continue
                dangling.append((it, c))

        if not dangling:
            report["layer4"] = {"dangling": 0}
            return intents

        # LLM 裁决：悬空引用是漏解析还是真错
        if self._budget.try_consume() and self._call_llm_raw:
            self._llm_judge_dangling(dangling, raw_text, warnings, report)
        else:
            for it, c in dangling:
                warnings.append(f"悬空引用 <{c}>（在 {_intent_id(it)} 中，可能漏解析）")
        report["layer4"] = {"dangling": len(dangling)}
        return intents

    def _llm_judge_dangling(self, dangling, raw_text, warnings, report):
        lines = []
        for it, c in dangling:
            lines.append(f"- {_intent_id(it)} 引用 <{c}>")
        prompt = (
            "你是 Step1.5 引用闭环裁决员。下面有引用找不到对应 produces。判定每个引用：\n"
            '1) missing_producer：原文确实要求该实体但漏解析了，需补建\n'
            '2) existing_row：引用已有表数据，非本批产出，放行\n'
            '3) hallucinated：LLM 臆造的引用，该丢弃\n\n'
            f"## 原文\n{raw_text[:3000]}\n\n## 悬空引用\n" + "\n".join(lines) + "\n\n"
            '输出 JSON：[{"ref":"引用名","verdict":"missing_producer|existing_row|hallucinated"}]\n只输出 JSON。'
        )
        raw = self._call_llm_raw(prompt, timeout=30)
        result = self._parse_json(raw)
        if not isinstance(result, list):
            for it, c in dangling:
                warnings.append(f"悬空引用 <{c}>（在 {_intent_id(it)} 中）")
            return
        for m in result:
            if not isinstance(m, dict):
                continue
            v = str(m.get("verdict", "")).lower()
            ref = m.get("ref", "")
            if v == "missing_producer":
                warnings.append(f"智能校验判定漏解析，需补建：<{ref}>")
            elif v == "hallucinated":
                warnings.append(f"智能校验判定为幻觉引用，建议丢弃：<{ref}>")
            elif v == "existing_row":
                pass
            else:
                warnings.append(f"悬空引用 <{ref}>（未裁决）")

    # ── 层 5：覆盖完整性 + 平行项模板复制补建（全 LLM）──
    def _layer5_coverage_completeness(self, intents, segments, errors, warnings, report, raw_text):
        # §段级计数对账：每段提到的实体数 vs 实际产出 intent 数，少 → 标 missing
        segment_gaps = self._segment_count_audit(intents, segments, warnings, report)

        if not self._budget.try_consume() or not self._call_llm_raw:
            report["layer5"] = {"skipped": True, "segment_gaps": segment_gaps}
            return intents
        lines = []
        for i, it in enumerate(intents, start=1):
            lines.append(f"{i}. {getattr(it,'action','')} {getattr(it,'table_hint','')}/{getattr(it,'sheet_hint','')} "
                        f"fields={list(_fields(it).keys())[:6]}")
        prompt = (
            "你是 Step1.5 覆盖完整性审计员。判定整批 intent 是否覆盖原文所有动作/实体/枚举项。\n"
            "特别检查：\n"
            "- 原文每个明确的新增/修改/删除/查询是否都有 intent？\n"
            "- '第N到第M天' 这种范围是否展开成对应数量？\n"
            "- 每个 id/名称/枚举项是否都有对应 intent？\n"
            "- 每条已产出 intent 的字段是否覆盖原文提到的该实体所有属性？漏列也要列出。\n"
            "- 平行相似项（如多个同表奖励/多环任务）必须逐个有 intent，数量与原文一致。\n"
            "  若缺失项与已产出 intent 是同类型（同表同 action），直接用模板复制：\n"
            "  指定 template_idx=已产出同类 intent 的序号，diff=只改不同的列（如 id/名称/数量）。\n"
            "  diff 里必须包含该实体的稳定标识（id/序号/名称），否则会产生重复行。\n\n"
            f"## 段级计数对账（规则预估，供参考）\n" + "\n".join(segment_gaps) + "\n\n"
            f"## 原文\n{raw_text[:3000]}\n\n## intent 清单\n" + "\n".join(lines) + "\n\n"
            '输出 JSON：{"covered":["已覆盖项"], '
            '"missing":[{"desc":"缺失描述","template_idx":已存在intent序号或0,"diff":{"列名":"新值"}}], '
            '"missing_fields":[{"idx":intent序号,"fields":["应填的列名"]}], '
            '"hallucinate":[{"idx":intent序号,"reason":"为何是幻觉"}], '
            '"needs_replan":["需补建的实体"]}\n只输出 JSON。'
        )
        raw = self._call_llm_raw(prompt, timeout=30)
        result = self._parse_json(raw)
        if not isinstance(result, dict):
            report["layer5"] = {"skipped": True, "segment_gaps": segment_gaps}
            return intents
        missing = result.get("missing") or []
        for m in missing:
            if isinstance(m, str) and m:
                warnings.append(f"覆盖审计疑似漏项：{m}")
        # 字段补全：LLM 指出某 intent 漏列 → 从原文/模板推断值补上
        self._backfill_missing_fields(intents, result.get("missing_fields") or [],
                                      warnings, report, raw_text)
        # 幻觉 intent 丢弃：LLM 判定与原文无关 → 丢弃
        self._drop_hallucinated_intents(intents, result.get("hallucinate") or [],
                                        warnings, report)
        # 模板复制补建
        added = self._copy_template_intents(intents, missing, warnings)
        # 补建后引用复验：新 intent 的 consumes 是否还悬空
        if added:
            self._revalidate_after_backfill(intents, warnings, report)
        report["layer5"] = {
            "missing": list(missing),
            "missing_fields": list(result.get("missing_fields") or []),
            "hallucinate": list(result.get("hallucinate") or []),
            "needs_replan": list(result.get("needs_replan") or []),
            "template_copied": added,
            "segment_gaps": segment_gaps,
        }
        return intents

    def _drop_hallucinated_intents(self, intents, hallucinate, warnings, report):
        """LLM 判定的幻觉 intent → 丢弃，防止污染下游。"""
        if not hallucinate:
            return
        drop_idx = set()
        for h in hallucinate:
            if isinstance(h, dict):
                idx = h.get("idx")
                if isinstance(idx, int) and 1 <= idx <= len(intents):
                    drop_idx.add(idx)
                    warnings.append(
                        f"智能校验判定为幻觉意图，已丢弃：{_intent_id(intents[idx-1])}"
                        f"（{h.get('reason','')}）")
            elif isinstance(h, int) and 1 <= h <= len(intents):
                drop_idx.add(h)
                warnings.append(f"智能校验判定为幻觉意图，已丢弃：{_intent_id(intents[h-1])}")
        if not drop_idx:
            return
        kept = [it for i, it in enumerate(intents, start=1) if i not in drop_idx]
        intents.clear()
        intents.extend(kept)

    def _segment_count_audit(self, intents, segments, warnings, report):
        """段级计数对账：每段规则预估实体数 vs 实际产出 intent 数。

        用纯规则估：数 id 出现次数、"第N"序号数、逗号分隔的枚举项数。
        少则标记 gap，供层5 LLM 参考。不硬判，只提示。
        """
        gaps: list[str] = []
        if not segments:
            return gaps
        # intent 按段归属（P2-2：原字符集重叠粗判——两段都含"任务/奖励"等高频
        # 中文词会错配。改双重判据：①任一段文本是另一方的子串（intent.raw 本
        # 就是段文本的清洗版，强命中）②否则 Jaccard=交集/并集 归一化，防高频
        # 词虚高重叠。仍粗粒度，只供层5 参考提示，不硬判。）
        seg_intents: list[list] = [[] for _ in segments]
        for it in intents:
            raw = str(getattr(it, "raw", "") or "")
            best_seg = -1
            best_score = 0.0
            for si, seg in enumerate(segments):
                seg_text = getattr(seg, "text", seg) if not isinstance(seg, str) else seg
                if not seg_text:
                    continue
                _seg_norm = str(seg_text).strip().rstrip("。;；，,").strip()
                _raw_norm = raw.strip().rstrip("。;；，,").strip()
                if _seg_norm and (_seg_norm in _raw_norm or _raw_norm in _seg_norm):
                    score = 2.0  # 子串包含 = 强归属
                else:
                    _a, _b = set(raw), set(seg_text)
                    if not _a or not _b:
                        score = 0.0
                    else:
                        score = len(_a & _b) / len(_a | _b)
                if score > best_score:
                    best_score = score
                    best_seg = si
            if best_seg >= 0:
                seg_intents[best_seg].append(it)

        for si, seg in enumerate(segments):
            seg_text = getattr(seg, "text", seg) if not isinstance(seg, str) else seg
            if not seg_text:
                continue
            # 规则预估实体数：数"第N"序号 + 数纯数字id + 数引号命名实体
            import re as _re
            ordinals = len(_re.findall(r"第[一二三四五六七八九十\d]+", seg_text))
            ids = len(_re.findall(r"\b\d{4,}\b", seg_text))
            quoted = len(_re.findall(r"[「」\"']", seg_text)) // 2
            estimated = max(ordinals, ids, quoted)
            actual = len(seg_intents[si])
            if estimated > actual and estimated >= 2:
                gap = f"第{si+1}段：预估{estimated}个实体，实际产出{actual}条，可能漏"
                gaps.append(gap)
                warnings.append(gap)
        report.setdefault("segment_gaps", gaps)
        return gaps

    def _backfill_missing_fields(self, intents, missing_fields, warnings, report, raw_text):
        """LLM 指出某 intent 漏列 → 让 LLM 从原文推断该列值补上。

        守卫：get 意图整行查询（"所有属性/全部信息"等）跳过补全——Step1 已按
        _is_whole_row_get 语义清空 fields（Step3 读整行全部列），此处若让 LLM
        "补漏列"会把「所有属性」等泛指词/实体名碎片补回成垃圾字段（如
        属性字段列表=所有属性、灵兽model_id=饕餮），Step2 校验不中触发重映射。
        """
        _whole_row_re = re.compile(
            r"(?:所有|全部|整个|整行|全)(?:属性|信息|字段|数据|列|内容|东西|值|情况)")
        if not missing_fields or not self._budget.try_consume() or not self._call_llm_raw:
            return
        backfill_items = []
        for mf in missing_fields:
            if not isinstance(mf, dict):
                continue
            idx = mf.get("idx")
            cols = mf.get("fields") or []
            if not isinstance(idx, int) or not isinstance(cols, list):
                continue
            if idx < 1 or idx > len(intents):
                continue
            it = intents[idx - 1]
            if getattr(it, "action", "") == "get" and _whole_row_re.search(raw_text or ""):
                continue  # get 整行查询：fields 无意义，跳过补全
            # §P2-1 补字段去重：Step1 已跑 _llm_complete_fields（对照原文+全量
            # schema 补漏），本层再补职责重复且无 schema grounding 会补幻觉列。
            # 有标记的 intent 跳过，省一次 LLM + 防覆盖。
            if (getattr(it, "extras", None) or {}).get("llm_fields_completed"):
                continue
            existing = set(_norm_key(k) for k in _fields(it).keys())
            need = [c for c in cols if _norm_key(c) not in existing]
            if need:
                backfill_items.append({"idx": idx, "table": getattr(it, "table_hint", ""),
                                       "sheet": getattr(it, "sheet_hint", ""),
                                       "missing_cols": need,
                                       "existing_fields": dict(list(_fields(it).items())[:8])})
        if not backfill_items:
            return
        prompt = (
            "下面有些 intent 漏填了列，请从原文推断该列应该填什么值。\n"
            "若原文确实没给该列的值，返回 null（不瞎填）。\n\n"
            f"## 原文\n{raw_text[:2000]}\n\n## 漏列清单\n{backfill_items}\n\n"
            '输出 JSON：[{"idx":序号,"fields":{"列名":"值或null"}}]\n只输出 JSON。'
        )
        raw = self._call_llm_raw(prompt, timeout=25)
        result = self._parse_json(raw)
        if not isinstance(result, list):
            return
        for item in result:
            if not isinstance(item, dict):
                continue
            idx = item.get("idx")
            new_fields = item.get("fields")
            if not isinstance(idx, int) or not isinstance(new_fields, dict):
                continue
            if idx < 1 or idx > len(intents):
                continue
            it = intents[idx - 1]
            extras = getattr(it, "extras", None) or {}
            fields = extras.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            for k, v in new_fields.items():
                if v is not None and k not in fields:
                    fields[k] = v
                    warnings.append(f"字段补全：第{idx}条 {k}={v}")
            extras["fields"] = fields

    def _revalidate_after_backfill(self, intents, warnings, report):
        """补建后轻量引用复验：新 intent 的 consumes 是否在 produced_labels 里。"""
        produced_labels = set()
        for it in intents:
            lbl = _produces(it)
            if lbl:
                produced_labels.add(lbl)
        still_dangling = 0
        for it in intents:
            for c in _consumes(it):
                if c not in produced_labels and not c.strip().isdigit():
                    still_dangling += 1
                    warnings.append(f"补建后仍悬空：<{c}>（在 {_intent_id(it)} 中）")
        if still_dangling:
            report["layer5"]["post_backfill_dangling"] = still_dangling

    def _copy_template_intents(self, intents, missing, warnings):
        """平行相似项模板复制：从已有 intent 复制，只改 diff 列值。

        LLM 判定"第4到第7天奖励"缺失 → 指定 template_idx（第3天奖励 intent）+
        diff（只改 id/必得道具/数量）→ 代码复制模板 4 份，各改对应 diff。
        """
        if not missing or not intents:
            return 0
        added = 0
        for m in missing:
            if not isinstance(m, dict):
                continue
            tidx = m.get("template_idx")
            diff = m.get("diff")
            desc = m.get("desc", "")
            if not isinstance(tidx, int) or not isinstance(diff, dict):
                continue
            if tidx < 1 or tidx > len(intents):
                continue
            template = intents[tidx - 1]
            new_it = self._clone_intent(template, diff, desc)
            if new_it is not None:
                intents.append(new_it)
                added += 1
                warnings.append(f"模板复制补建：{desc}（基于第{tidx}条）")
        return added

    @staticmethod
    def _clone_intent(template, diff: dict, desc: str = ""):
        """复制 template intent，覆盖 diff 里的列值。produces_label 递增保证唯一。"""
        import copy as _copy
        try:
            new_it = _copy.deepcopy(template)
        except Exception:
            return None
        # 覆盖 diff 列值
        extras = getattr(new_it, "extras", None)
        if extras is None:
            return None
        fields = extras.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        for k, v in diff.items():
            fields[k] = v
        extras["fields"] = fields
        # produces_label 递增（避免重复）
        old_prod = str(getattr(new_it, "produces_label", "") or
                       (extras.get("produces")) or "")
        if old_prod:
            import re as _re
            m = _re.search(r"_(\d+)$", old_prod)
            if m:
                new_prod = old_prod[:m.start()] + f"_{int(m.group(1)) + 1}"
            else:
                new_prod = old_prod + "_1"
            try:
                new_it.produces_label = new_prod
            except Exception:
                pass
            extras["produces"] = new_prod
        # raw 标记来源
        try:
            new_it.raw = f"{desc}（模板复制）"
        except Exception:
            pass
        return new_it

    # ── 工具 ──
    def _parse_json(self, raw):
        if not raw or not raw.strip():
            return None
        raw = raw.strip()
        mf = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if mf:
            raw = mf.group(1).strip()
        try:
            return json.loads(raw)
        except ValueError:
            pass
        for open_ch, close_ch in [("[", "]"), ("{", "}")]:
            arr = self._extract_balanced(raw, open_ch, close_ch)
            if arr:
                try:
                    return json.loads(arr)
                except ValueError:
                    pass
        return None

    def _extract_balanced(self, raw, open_ch="[", close_ch="]"):
        start = raw.find(open_ch)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
        return None


__all__ = ["ContractGate"]
