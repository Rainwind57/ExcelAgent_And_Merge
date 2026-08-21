# O16 方法 F 编号账本跨分支

> 轮次：O16（2026-08-18）
> 范围：F1-F5 闭环 — 多分支编号账本校验 + id-claim 查询 + agent 写 ID 列前跨分支查重。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §3 + `docs/archive/事前预防优化TODO.md` 第三波 F。
> 前置：方法 B（`precommit_hold.py` 通道）+ `PreCommitHoldEvent`（kind=id_conflict reserved）就位。

## 设计

扩 `IdScopeValidator` 支持多分支根目录扫描，location 加 branch 维度，冲突判定从"文件"改"分支"（同分支同 stem 跨 sheet 白名单）。agent 写 ID 列前调 `claim_id` 跨分支查重，命中冲突 → pre_commit_hold 事件 + 建议下一空闲号，不静默改编号。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| F1 多分支报告 | `server/engine/id_scope.py` | 新增 `CrossBranchConflict` + `CrossBranchReport` dataclass（location 加 branch 维度，含 cross_branch_conflicts/reserved_segments/scanned_tables/branches_scanned + to_dict）。 |
| F1 多分支扫描 | `server/engine/id_scope.py` `IdScopeValidator` | 新增 `_build_multi_branch_index(branch_roots)` — 对每 branch_root 重复单根扫描逻辑，location 加 branch 字段；`find_cross_branch_conflicts` — 冲突判定 `len(branches)>=2`（同分支白名单）；`validate_multi_branch(branch_roots) → CrossBranchReport`。 |
| F2 向后兼容 | `server/engine/id_scope.py` `get_id_scope_validator` | 单例不动，多根用独立 `validate_multi_branch` 调用不复用单例活实例（单例含内存态 `_index`）。 |
| F4 claim_id | `server/engine/id_scope.py` `IdScopeValidator.claim_id`（新方法） | 单/多分支查重 + suggested_next（已用最大+1，跳过占用）。单分支同 stem 白名单不算冲突。 |
| F3 id-scope 路由 | `server/routers/validate.py` `GET /id-scope` | 加 `mode=single|multibranch` 参数（multibranch 扫 RESOURCES_DIR + 子目录各分支根）。 |
| F3 id-claim 路由 | `server/routers/validate.py` `GET /id-claim`（新） | `?id=xxx&mode=multibranch` → claim_id 查询。FastAPI Query 导入。 |
| F4 agent 接线 | `server/agent/excel/core/agent.py` `_validate_id_scope` | 段校验通过后加 id-claim 跨分支查重：命中 → `PreCommitHoldEvent(kind=id_conflict, severity=hold)` + SSE 推送（`_agent_subtask_sink`）+ 建议换号（reason 含 suggested_next）+ 返 False 阻断。env `CODEMAKER_ID_SCOPE_BRANCHES=path1;path2` 多分支模式。失败静默不阻断（ok=True）。 |
| 测试 | `server/tests/test_id_scope_multi_branch_o16.py`（新增 10） | TestF1ValidateMultiBranch 4（空根/单分支无冲突/多分支冲突/同 stem 白名单）+ TestF4ClaimId 3（无冲突/多分支冲突+建议/单分支白名单）+ TestF4AgentInjection 3（无 id_mgr ok/跨分支冲突 hold 事件/无冲突 ok）。autouse fixture 重置单例防污染。 |

## 确定性验证

```
python -m pytest server/tests/test_id_scope_multi_branch_o16.py -q
=> 10 passed in 2.32s

python -m pytest server/tests/test_id_scope_multi_branch_o16.py \
               server/tests/test_validate_agent_two_layer.py -q
=> 71 passed in 2.19s   # 无单例污染

python -m pytest server/tests/ -q
=> 961 passed, 1 failed, 1 skipped in 211.77s
   # 1 预存红：test_column_matcher_semantic::test_fuzzy_simplified_colname_hits
   #   与 O16 无关（列匹配器优先级，未触及 column_matcher.py）
```

## 残留 follow-up

- **真 SVN 分支目录对接**：生产环境 `branch_roots` 换 `ca/dev/*/` + `ca/cappedbranch/` + `ca/testbranch/` + `ca/` 根（archive TODO §4 生产迁移注意点 1）。
- **前端 id-claim 预检弹窗**：`MergeGuideView.vue` 接 `pre_commit_hold` kind=id_conflict 事件渲染 + override 弹窗（minified 需重建）。
- **方法 E/G/H**：八方法残留（见 ledger §3）。
