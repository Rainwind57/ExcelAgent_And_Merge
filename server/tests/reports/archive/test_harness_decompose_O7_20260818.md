# O7 test harness — DecomposeAgent 9 预存红修复

> 轮次：O7（2026-08-18）
> 范围：`test_decompose_agent.py` 9 预存红（`decompose` 返 0）。非 validator 代码 bug，是测试桩 cwd 依赖。解封 DecomposeAgent→ValidatorAgent e2e 路径。
> LLM e2e（`table_case_eval.py`）仍被 R7 阻断（ledger §2.1）。

## 根因

`test_decompose_agent.py:104` `make_cli()` 用 `StubCodeMakerCLI(workspace=Path("resources"))`：
- 从 repo 根跑（cwd=testtest/）：`Path("resources")` → `testtest/resources` ✅ 存在
- 从 server/ 跑（workflow §三 标准入口）：`Path("resources")` → `server/resources` ❌ 不存在

`list_tables()` rglob 空 → `_build_schema_block` 返 "" → `jobs` 空 → `decompose` 返 []。所有 9 测因 decompose 返 0 而 fail。

## 改动

| 项 | 文件 | 改动 |
|---|---|---|
| cwd 无关 workspace | `server/tests/test_decompose_agent.py:103` | `Path("resources")` → `Path(__file__).resolve().parents[2] / "resources"`（repo 根 resources/，cwd 无关）。仅此一文件用相对路径，其余测试均用 `tmp_path`（pytest fixture，绝对）。 |

## 确定性验证

```
# 从 server/ cwd 跑（workflow 标准）
python -m pytest server/tests/test_decompose_agent.py -q
=> 11 passed in 20.70s   # 9 预存红全绿

python -m pytest server/tests/test_validate_agent_two_layer.py \
               server/tests/test_parse_agent.py \
               server/tests/test_decompose_agent.py \
               server/tests/test_produces_inference_p0.py \
               server/tests/test_validator_forward_refs_p13.py \
               server/tests/test_multi_table_orchestration.py \
               server/tests/test_execute_agent.py \
               server/tests/test_subagent_roles.py \
               server/tests/test_schema_bundle.py \
               server/tests/test_optimizations_e2e.py -q
=> 189 passed in 32.23s   # 零红
```

## 量化

| 指标 | before | after | delta |
|---|---|---|---|
| test_decompose_agent 红测 | 9 | 0 | -9（全绿） |
| 全相关回归红测 | 9 | 0 | -9 |
| 全相关回归 PASS | 128 | 189 | +61（decompose 解封后 e2e 路径被 exercise） |

## 根因归因

| 修复项 | 贡献 | 证据 |
|---|---|---|
| cwd 无关 workspace | 9 预存红全绿，且 O5/O6 produces_inference 改动经解封的 decompose→validator e2e 路径验证无回归 | 189 passed / 0 failed |

## 注意事项

- O5/O6 的 produces_inference + validator 改动此前因 decompose 返 0 未被 e2e 路径 exercise；O7 解封后 e2e 跑通，验证 O5/O6 改动在 DecomposeAgent→ValidatorAgent 完整链路下无回归（189 passed）。
- 仅 `test_decompose_agent.py` 一文件 cwd 脆弱；其余测试用 `tmp_path`（pytest fixture，绝对路径）无此问题。
- LLM e2e `table_case_eval.py` 仍被 R7 阻断未跑。
- validator P0 第一批（P10/P11/P12/P13/P17）+ P9 降级 + P14 待决；O5/O6/O7 收尾后，validator 第一批除 P9/P14 外全部落地 + e2e 验证通过。
