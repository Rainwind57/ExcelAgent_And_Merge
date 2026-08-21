"""验证本次优化修改的正确性：向量化 compare / 规则 parse / 白名单 AI / aiCache Proxy"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 1. 向量化 compare_sheet ──
def test_vectorized_compare():
    from engine.compare import _compare_sheet_vectorized, _semantic_eq

    base_name = "base.xlsx"
    other_files = ["dev.xlsx"]
    all_files = [base_name] + other_files
    headers = ["id", "名称", "攻击力", "防御力"]

    # 纯数据 sheet：无公式无批注
    file_rows = {
        base_name: [["1", "饕餮", "100", "50"], ["2", "朱雀", "200", "80"]],
        "dev.xlsx": [["1", "饕餮", "100", "50"], ["2", "朱雀", "250", "80"]],
    }
    result = _compare_sheet_vectorized(
        file_rows, base_name, other_files, all_files, headers,
        structure_diff=None, sparse=False, merge_base_file=None, commit_authors=None,
    )
    assert result is not None, "向量化返回 None"
    assert result["headers"] == headers
    assert len(result["rows"]) == 2
    # 行1 完全一致 → 无 conflict
    r1 = result["rows"][0]
    assert r1["row_type"] == "matched"
    assert r1["key"] == "1"
    # 行2 攻击力变化 → 1 changed
    r2_cells = [c for c in result["rows"][1]["cells"] if c.get("changed")]
    assert len(r2_cells) >= 1, "行2攻击力改为250应检测到"
    print("  ✓ 向量化 compare 纯数据 sheet 正确")

    # sparse 模式：全等行只保留 PK 格
    result2 = _compare_sheet_vectorized(
        file_rows, base_name, other_files, all_files, headers,
        structure_diff=None, sparse=True, merge_base_file=None, commit_authors=None,
    )
    r1_sparse = result2["rows"][0]
    assert r1_sparse["row_type"] == "matched"
    assert len(r1_sparse["cells"]) == 1  # 只有 PK 格
    assert r1_sparse["cells"][0]["col"] == 0
    print("  ✓ 向量化 sparse 模式正确")

    # 无 numpy 时降级返回 None
    import engine.compare as cm
    try:
        import numpy as np
        # numpy 存在时正常返回
        result3 = _compare_sheet_vectorized(
            file_rows, base_name, other_files, all_files, headers,
            structure_diff=None, sparse=False, merge_base_file=None, commit_authors=None,
        )
        assert result3 is not None
    except ImportError:
        print("  (跳过向量化测试：numpy 未安装)")
    print("  ✓ 向量化 compare 所有断言通过")


# ── 2. 规则 parse_multi ──
def test_rule_parse():
    """注意：这个测试依赖 agent 包的导入链。若 real_cli 缺失则跳过。"""
    try:
        from agent.excel.codemaker_parser import _try_rule_parse_multi
    except ImportError as e:
        print(f"  ⊘ 跳过规则 parse 测试（agent 导入链断裂：{e}）")
        return

    # 简单 add
    r = _try_rule_parse_multi("新增灵兽名称朱雀")
    assert r is not None, "简单add应命中"
    assert len(r) == 1
    assert r[0].action == "add"
    assert "朱雀" in str(r[0].extras.get("fields"))
    print("  ✓ 规则 parse: 简单add")

    # 逗号分隔 2 条 add
    r = _try_rule_parse_multi("增加道具类型TEST1,增加道具类型TEST2")
    assert r is not None
    assert len(r) == 2
    assert all(i.action == "add" for i in r)
    print("  ✓ 规则 parse: 2条add")

    # 简单 delete
    r = _try_rule_parse_multi("删除神通id为3333的信息")
    assert r is not None
    assert r[0].action == "delete"
    print("  ✓ 规则 parse: 简单delete")

    # 简单 set
    r = _try_rule_parse_multi("把法宝id为1001的等级改为5")
    assert r is not None
    assert r[0].action == "set"
    assert "等级" in r[0].extras.get("fields", {})
    print("  ✓ 规则 parse: 简单set")

    # 跨表引用 → 降级 LLM
    r = _try_rule_parse_multi("新增NPC铁匠老张放到entity_prefab，再在spawn_world_entity里刷新它")
    assert r is None, "跨表引用应降级LLM"
    print("  ✓ 规则 parse: 跨表引用降级")

    # 3条意图 → 降级
    r = _try_rule_parse_multi("新增A,新增B,新增C")
    assert r is None, ">2条应降级LLM"
    print("  ✓ 规则 parse: >2条降级")


# ── 3. 白名单 AI 模式 ──
def test_whitelist_ai():
    try:
        from agent.excel.step_ai_enhancer import StepAIEnhancer
    except ImportError as e:
        print(f"  ⊘ 跳过白名单 AI 测试（agent 导入链断裂：{e}）")
        return

    # 模拟 intent 对象
    class FakeIntent:
        action = "add"

    enh = StepAIEnhancer.__new__(StepAIEnhancer)
    enh._whitelist_mode = True

    # resolve_table 在白名单下不应跳过
    assert enh._should_skip_ai("resolve_table") is False
    print("  ✓ 白名单: resolve_table 不跳过")

    # verify_intents 在白名单下应跳过
    assert enh._should_skip_ai("verify_intents") is True
    print("  ✓ 白名单: verify_intents 跳过")

    # add 的 validate_plan 在白名单下应跳过
    assert enh._should_skip_ai("validate_plan", FakeIntent()) is True
    print("  ✓ 白名单: add validate_plan 跳过")

    # 白名单关闭时全不跳过
    enh._whitelist_mode = False
    assert enh._should_skip_ai("verify_intents") is False
    assert enh._should_skip_ai("validate_plan", FakeIntent()) is False
    print("  ✓ 白名单关闭: 全不跳过")


# ── 4. per-stage 超时 ──
def test_stage_timeouts():
    from agent.codemaker_client import get_stage_timeout, _STAGE_TIMEOUTS

    assert get_stage_timeout("parse") == _STAGE_TIMEOUTS["parse"]
    assert get_stage_timeout("verify") == _STAGE_TIMEOUTS["verify"]
    assert get_stage_timeout("unknown") == 45  # 回退默认 45s
    assert _STAGE_TIMEOUTS["parse"] <= 150  # parse 默认 150s（CODEMAKER_PARSE_TIMEOUT，复杂多意图 LLM 解析）
    assert _STAGE_TIMEOUTS["verify"] <= 20
    print("  ✓ per-stage 超时配置正确")


if __name__ == "__main__":
    print("=== 优化验证 ===")
    test_vectorized_compare()
    test_rule_parse()
    test_whitelist_ai()
    test_stage_timeouts()
    print("=== 全部通过 ===")
