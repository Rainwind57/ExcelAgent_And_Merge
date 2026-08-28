"""验证 4-Step 边界修复（问题 A/B/C）生效。

不依赖 LLM/Excel，纯单元验证三个修复点逻辑：
  A. Step2 占位符预解析 + 同 label 共享冲突检测
  B. Step1 幻觉意图过滤（_apply_ai_intent_check 接收 extra 字段）
  C. Step2/Step3 coerce 口径对齐（_coerce_field_simple 不再拆段放行）
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "server"))


def _make_intent(action="add", table_hint="interaction", sheet_hint="Interaction",
                 fields=None, produces=None):
    """构造 NLIntent（绕过 __init__ 依赖）。"""
    from agent.excel.parser.nl_parser import NLIntent
    extras = {"fields": fields or {}}
    if produces:
        extras["produces"] = produces
    it = NLIntent(
        action=action, table_hint=table_hint, sheet_hint=sheet_hint,
        extras=extras)
    if produces:
        it.produces_label = produces
    return it


# ────────────────────────────────────────────────────────────────
# 问题 C 验证：coerce 口径对齐（最快验证，无外部依赖）
# ────────────────────────────────────────────────────────────────
def verify_c_coerce_aligned():
    """问题C：'200,0,150' 填 float 列，Step2 应报 TYPE_MISMATCH（与 Step3 写路径一致）。

    修复前：_coerce_field_simple 拆段 "200,0,150" → 200/0/150 每段 float() 成功 → 放行
    修复后：整串 float("200,0,150") → ValueError → 报 TYPE_MISMATCH
    """
    from agent.excel.subagent.validator_agent import ValidatorAgent
    # ValidatorAgent.__init__ 需要 parser/cli，用 __new__ 绕过
    va = ValidatorAgent.__new__(ValidatorAgent)
    ok, err = va._coerce_field_simple("float", "200,0,150")
    assert not ok, (
        f"[C] FAIL: '200,0,150' 填 float 列应报 TYPE_MISMATCH，实际放行 ok={ok}")
    assert "分隔符" in err or "无法转成" in err, (
        f"[C] FAIL: 错误文案应含类型不符信息，实际：{err}")
    print(f"[C] PASS: '200,0,150' 填 float 列 → 报 TYPE_MISMATCH：{err}")

    # 回归：正常单值仍通过
    ok2, _ = va._coerce_field_simple("float", "10050")
    assert ok2, "[C] FAIL: 正常单值 '10050' 应放行"
    ok3, _ = va._coerce_field_simple("int", "3001")
    assert ok3, "[C] FAIL: 正常单值 '3001' 应放行"
    print("[C] PASS: 正常单值 '10050'/'3001' 仍放行")

    # 回归：int 列含分隔符多值（如 spell_ids "9101,9102"）现也报 TYPE_MISMATCH
    # 注：这是预期的行为变更——int 标量列不该装多值，应标注 int[] 数组类型
    ok4, _ = va._coerce_field_simple("int", "9101,9102")
    assert not ok4, "[C] FAIL: int 标量列含分隔符应报 TYPE_MISMATCH（多值应标 int[]）"
    print("[C] PASS: int 标量列 '9101,9102' 报 TYPE_MISMATCH（多值应标 int[]）")

    # 回归：int[] 数组类型列放行
    ok5, _ = va._coerce_field_simple("int[]", "9101,9102")
    assert ok5, "[C] FAIL: int[] 数组列应放行"
    print("[C] PASS: int[] 数组列 '9101,9102' 放行")


# ────────────────────────────────────────────────────────────────
# 问题 A 验证：占位符预解析 + 同 label 共享冲突检测
# ────────────────────────────────────────────────────────────────
def verify_a_placeholder_pre_resolve():
    """问题A：意图7、意图15 共享 <new_interaction_id>，Step2 应预分配不同 PK。

    修复前：Step2 PK 检测读到 <new_interaction_id> 占位符 → existing_values 无 → 通过
            → Step3 意图7 自增 10082、意图15 自增 10082 → PK 冲突
    修复后：Step2 预分配 produces label 为 max+1 递增值，意图15 读到 10083 → 检测无冲突
            （或若分到相同值则报 UNIQUE_VIOLATION 前置拦截）
    """
    from agent.excel.subagent.validator_agent import ValidatorAgent
    from agent.excel.parser.nl_parser import NLIntent

    va = ValidatorAgent.__new__(ValidatorAgent)

    # 构造意图7、意图15：同表同 produces label，PK 列填占位符
    it7 = _make_intent(
        action="add", table_hint="interaction", sheet_hint="Interaction",
        fields={"编号": "<new_interaction_id>", "交互效果编号": 3005,
                "3005: 目标space ID": 10050},
        produces="new_interaction_id")
    it15 = _make_intent(
        action="add", table_hint="interaction", sheet_hint="Interaction",
        fields={"编号": "<new_interaction_id>", "effect.key": 3006,
                "effect.data.3006.conv_id": "<conv_root_id>"},
        produces="new_interaction_id")
    intents = [it7, it15]

    # 模拟 schema_getter：返回 interaction 表头
    def _sg(intent):
        return (["编号", "交互效果编号", "effect.key"], ["int", "int", "int"])

    # 模拟 data_getter：existing_values 无 10082/10083（新表空）
    _next_id = [10081]
    def _dg(intent):
        _ev = {"编号": set()}  # 空集，无占用
        return {"stem": "interaction", "sheet": "Interaction",
                "existing_values": _ev, "cli": None}

    # 模拟 _suggest_next_id：每次调用递增（模拟 max+1）
    def _fake_suggest(intent, col, dg):
        _next_id[0] += 1
        return _next_id[0]
    va._suggest_next_id = _fake_suggest
    va._cli = None
    va._ask_callback = None  # 无交互 callback
    va._pk_cols_cache = {"interaction": {"编号"}}

    # 模拟 _ask_pk_conflict：返回 skip（无 cb 场景）
    def _fake_ask(intent, col, val, sugg):
        return {"accept_suggest": False}
    va._ask_pk_conflict = _fake_ask

    # 模拟 _mark_intent_skipped：记录但不真改
    _skipped_log = []
    def _fake_skip(intent):
        _skipped_log.append(intent)
        v = getattr(intent, "validation", None)
        if v is not None:
            v.skipped = True
    va._mark_intent_skipped = _fake_skip

    # 模拟 _get_schema：调用注入的 _sg
    va._get_schema = lambda intent, sg: _sg(intent) if sg else ([], [])

    # 跑 validate_two_layer
    vr = va.validate_two_layer(
        intents, schema_getter=_sg, data_getter=_dg,
        locator_result=None, dry_run=False)

    # 验证：意图7、意图15 的 PK 列应被预分配真实值（不再是占位符）
    it7_pk = it7.extras["fields"]["编号"]
    it15_pk = it15.extras["fields"]["编号"]
    print(f"[A] 意图7 PK 列值: {it7_pk}")
    print(f"[A] 意图15 PK 列值: {it15_pk}")

    assert not (isinstance(it7_pk, str) and it7_pk.startswith("<")), (
        f"[A] FAIL: 意图7 PK 应被预分配真实值，仍为占位符 {it7_pk}")
    assert not (isinstance(it15_pk, str) and it15_pk.startswith("<")), (
        f"[A] FAIL: 意图15 PK 应被预分配真实值，仍为占位符 {it15_pk}")
    print("[A] PASS: 意图7、意图15 PK 列均被预分配真实值（不再是占位符）")

    # 验证：两个 PK 值不同（预分配递增，不撞号）
    assert it7_pk != it15_pk, (
        f"[A] FAIL: 意图7、意图15 预分配 PK 相同（{it7_pk}=={it15_pk}），应递增不同")
    print(f"[A] PASS: 意图7({it7_pk})、意图15({it15_pk}) 预分配 PK 不同，不撞号")


# ────────────────────────────────────────────────────────────────
# 问题 B 验证：幻觉意图过滤逻辑
# ────────────────────────────────────────────────────────────────
def verify_b_hallucination_filter():
    """问题B：ai_verify_intents 返回 extra 字段，_apply_ai_intent_check 过滤幻觉意图。

    修复前：ai_verify_intents 只返回 missing/corrections，无 extra → 幻觉意图进 Step3
    修复后：prompt 扩展产 extra 字段，_apply_ai_intent_check 过滤掉对应下标的意图
    """
    from agent.excel.core.agent import TableAgent

    # 验证 prompt 已扩展（含"幻觉检测"和"extra"字段）
    from agent.excel.core.step_ai_enhancer import AIEnhancer
    import inspect
    src = inspect.getsource(AIEnhancer.ai_verify_intents)
    assert "幻觉检测" in src, "[B] FAIL: ai_verify_intents prompt 未含'幻觉检测'"
    assert '"extra"' in src, "[B] FAIL: ai_verify_intents prompt 未含 extra 字段"
    print("[B] PASS: ai_verify_intents prompt 已扩展（含'幻觉检测'+ extra 字段）")

    # 验证 _apply_ai_intent_check 处理 extra 字段
    src2 = inspect.getsource(TableAgent._apply_ai_intent_check)
    assert "extra_idxs" in src2, "[B] FAIL: _apply_ai_intent_check 未处理 extra 字段"
    assert "AI 幻觉检测" in src2, "[B] FAIL: _apply_ai_intent_check 未输出幻觉过滤日志"
    print("[B] PASS: _apply_ai_intent_check 已处理 extra 字段（过滤幻觉意图）")

    # 单元验证：构造 extra 返回，确认过滤逻辑生效
    it0 = _make_intent(table_hint="activity", sheet_hint="Activity",
                       fields={"活动id": 3001})
    it1 = _make_intent(table_hint="guild", sheet_hint="Library",
                       fields={"model_id": 1200})  # 幻觉意图
    it2 = _make_intent(table_hint="reward", sheet_hint="Reward",
                       fields={"reward_id": 30010})
    intents = [it0, it1, it2]

    # 模拟 ai_enhancer：返回 extra=[1]（标记 it1 为幻觉）
    class _FakeEnh:
        def ai_verify_intents(self, text, rule_intents):
            return {"ok": False, "missing": [], "corrections": [],
                    "extra": [1]}
        @staticmethod
        def _should_skip_ai(name):
            return False

    # 构造 TableAgent 实例（绕过 __init__）+ 注入 fake enhancer
    ta = TableAgent.__new__(TableAgent)
    ta._ai_enhancer = _FakeEnh()

    class _FakeRes:
        def __init__(self):
            self.thoughts = []
            self.ai_suggestions = []
        def add_thinking(self, tag, msg):
            self.thoughts.append(f"{tag}: {msg}")
    _res = _FakeRes()

    result = ta._apply_ai_intent_check(intents, "限时BOSS活动+奖励+引导NPC", _res)

    # 验证：guild/Library 幻觉意图被过滤
    stems = [getattr(it, "table_hint", "") for it in result]
    assert "guild" not in stems, (
        f"[B] FAIL: 幻觉意图 guild/Library 未被过滤，result={stems}")
    assert "activity" in stems and "reward" in stems, (
        f"[B] FAIL: 正常意图被误过滤，result={stems}")
    assert len(result) == 2, f"[B] FAIL: 应剩 2 条意图，实际 {len(result)} 条"
    print(f"[B] PASS: 幻觉意图 guild/Library 被过滤，保留 {stems}")

    # 验证日志输出
    has_hallucination_log = any("AI 幻觉检测" in t for t in _res.thoughts)
    assert has_hallucination_log, "[B] FAIL: 未输出幻觉过滤日志"
    print("[B] PASS: 已输出'AI 幻觉检测'日志")


if __name__ == "__main__":
    print("=" * 60)
    print("验证 4-Step 边界修复（问题 A/B/C）")
    print("=" * 60)

    print("\n── 问题 C：coerce 口径对齐 ──")
    verify_c_coerce_aligned()

    print("\n── 问题 A：占位符预解析 + 同 label 冲突检测 ──")
    verify_a_placeholder_pre_resolve()

    print("\n── 问题 B：幻觉意图过滤 ──")
    verify_b_hallucination_filter()

    print("\n" + "=" * 60)
    print("全部验证通过 ✅")
    print("=" * 60)
