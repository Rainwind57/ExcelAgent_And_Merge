# -*- coding: utf-8 -*-
"""通用修复单测（不绑业务/测例）：

§6 validator._get_pk_cols 表头兜底：无显式声明时推**单列**主键（row2 规范名/
    首列启发式），杜绝"多 id 列凑复合键"的假唯一冲突（如 model_id+combat_model_id）。
§7 Step4 all_ok：Step1 段级漏解析（segment_no_intent / segment_partial_coverage
    soft error）不再被当作干净成功，Step4 报"部分完成"且 ok=False。
"""
from __future__ import annotations

from agent.excel.core.pipeline import (
    Step4ConcludeSubAgent, StepContext, StepResult,
    STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE,
)
from agent.excel.core.pipeline.contracts import StepError
from agent.excel.subagent.validator_agent import ValidatorAgent


class _FakeIntent:
    def __init__(self, stem, sheet):
        self.table_hint = stem
        self.sheet_hint = sheet
        self.action = "add"
        self.raw = ""
        self.validation = None


# ── §6 ────────────────────────────────────────────────────────────────

def test_pk_fallback_single_col_row1_no_id():
    """真实 PK 列 row1 表头不含 'id'（如「门派」），但 row2 规范名 == stem。

    应返回单列 ['门派']，且不把 model_id/combat_model_id 凑成复合键。
    """
    v = ValidatorAgent(); v._cli = None
    v._pk_cols_cache = {}  # 强制走表头兜底（无显式声明）
    it = _FakeIntent("school", "School")

    def _schema_getter(_intent):
        headers = ["门派", "名字", "大世界模型", "战斗模型"]
        row2 = ["school:int", "name:str", "model_id:int", "combat_model_id:int"]
        return headers, row2

    cols = v._get_pk_cols(it, _schema_getter)
    assert cols == ["门派"], f"应推单列主键「门派」，实际 {cols}"


def test_pk_fallback_not_composite_from_multiple_ids():
    """多个含 id 的外键列不应被凑成复合主键（避免假 unique_violation）。"""
    v = ValidatorAgent(); v._cli = None
    v._pk_cols_cache = {}
    it = _FakeIntent("mytable", "MySheet")

    def _schema_getter(_intent):
        # 首列即主键（编号），另有两个外键 id 列
        headers = ["编号", "关联模型id", "战斗模型id"]
        row2 = ["mytable_id:int", "model_id:int", "combat_id:int"]
        return headers, row2

    cols = v._get_pk_cols(it, _schema_getter)
    assert len(cols) == 1, f"表头兜底只应返回单列主键，实际复合 {cols}"
    assert cols == ["编号"], f"首列「编号」应作主键，实际 {cols}"


def test_pk_fallback_first_col_when_id_like():
    """首列像主键（含 id）时直接取首列。"""
    v = ValidatorAgent(); v._cli = None
    v._pk_cols_cache = {}
    it = _FakeIntent("pet", "Pet")

    def _schema_getter(_intent):
        headers = ["灵兽id", "灵兽名称", "元素"]
        row2 = ["pet_id:int", "name:str", "element:int"]
        return headers, row2

    cols = v._get_pk_cols(it, _schema_getter)
    assert cols == ["灵兽id"], f"应取首列 id 主键，实际 {cols}"


def test_pk_explicit_declaration_composite_preserved():
    """显式声明的复合主键（rules/relations 缓存）仍原样返回。"""
    v = ValidatorAgent(); v._cli = None
    v._pk_cols_cache = {"fabao": {"FabaoLevel": ["法宝id", "法宝等级"]}}
    it = _FakeIntent("fabao", "FabaoLevel")
    cols = v._get_pk_cols(it, None)
    assert cols == ["法宝id", "法宝等级"], f"显式复合键应保留，实际 {cols}"


# ── §7 ────────────────────────────────────────────────────────────────
def _make_ctx_with_dropped_segment():
    ctx = StepContext(session_id="s", user_text="加A，然后加B，再加C，还有删D")
    # Step1：产了 3 条 intent（ok=True），但第4段漏解析 → soft segment_no_intent
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE, ok=True,
        errors=[StepError(
            step_id=STEP1_PARSE, error_type="segment_no_intent",
            message="第4段「删D」未能解析出意图", is_hard=False, segment_idx=3)],
    ))
    ctx.set_result(STEP2_VALIDATE, StepResult(STEP2_VALIDATE, ok=True))
    ctx.set_result(STEP3_EXECUTE, StepResult(
        STEP3_EXECUTE, ok=True,
        artifacts={"subtasks": [
            {"ok": True}, {"ok": True}, {"ok": True}], "failures": []}))
    return ctx


def test_step4_reports_partial_when_segment_dropped():
    """Step1 漏解析一段 → Step4 不报干净成功，ok=False + '部分完成'。"""
    ctx = _make_ctx_with_dropped_segment()
    r = Step4ConcludeSubAgent().execute(ctx)
    assert r.ok is False, "有意图被丢弃时 Step4 不应报成功"
    assert "部分完成" in r.artifacts["summary"], \
        f"summary 应显式点出部分完成，实际：{r.artifacts['summary']}"
    assert "完成 3 个子任务" != r.artifacts["summary"], "不应报干净成功文案"


def test_step4_clean_success_when_no_drop():
    """无漏解析、全部子任务成功 → 干净成功文案。"""
    ctx = StepContext(session_id="s", user_text="加A")
    ctx.set_result(STEP1_PARSE, StepResult(STEP1_PARSE, ok=True))
    ctx.set_result(STEP2_VALIDATE, StepResult(STEP2_VALIDATE, ok=True))
    ctx.set_result(STEP3_EXECUTE, StepResult(
        STEP3_EXECUTE, ok=True,
        artifacts={"subtasks": [{"ok": True}], "failures": []}))
    r = Step4ConcludeSubAgent().execute(ctx)
    assert r.ok is True
    assert r.artifacts["summary"].startswith("完成 1 个子任务")


# ── §8 ────────────────────────────────────────────────────────────────

def _check_biz_required(headers, row2, fields, raw, existing):
    v = ValidatorAgent(); v._cli = None
    it = _FakeIntent("pet_evolve", "PetEvolveData")
    it.raw = raw
    return v._check_business_required_pre_add(
        it, headers, fields, raw, existing_values=existing, type_row=row2)


def test_biz_required_exempts_denormalized_fk_mirror_name_col():
    """row2 规范名为空的名称列（FK 反规范化镜像，如 pet_evolve 的「宠物名称」）
    不应被当作用户漏填的业务必填列。"""
    headers = ["进化id", "宠物名称", "进化后的灵兽名称", "进化等级"]
    row2 = ["evolve_id:int", "", "", "evolve_level:int"]
    fields = {"进化id": "<new_evolve_id>", "进化等级": 10}
    existing = {"宠物名称": {"饕餮"}, "进化后的灵兽名称": {"金身饕餮"},
                "进化id": {"10001"}, "进化等级": {"10"}}
    raw = "进化为 pet_id 20999 叫'焚天赤龙·涅槃'"
    issues = _check_biz_required(headers, row2, fields, raw, existing)
    flagged = {getattr(i, "col", "") for i in issues}
    assert "宠物名称" not in flagged, f"FK 镜像列不应报缺，实际 {flagged}"
    assert "进化后的灵兽名称" not in flagged, f"FK 镜像列不应报缺，实际 {flagged}"


def test_biz_required_still_flags_real_name_col_with_row2():
    """带 row2 规范名的真实名称列（name:string）若用户给了值但 LLM 漏产，仍报缺。"""
    headers = ["活动id", "活动名称", "活动描述"]
    row2 = ["id:int", "name:string", "desc:string"]
    fields = {"活动id": 3060}
    existing = {"活动名称": {"旧活动"}, "活动描述": {"旧描述"}, "活动id": {"3000"}}
    raw = "开一个活动叫'九霄论剑'"
    issues = _check_biz_required(headers, row2, fields, raw, existing)
    flagged = {getattr(i, "col", "") for i in issues}
    assert "活动名称" in flagged, f"真实名称列漏填应报缺，实际 {flagged}"


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("ALL PASS")
