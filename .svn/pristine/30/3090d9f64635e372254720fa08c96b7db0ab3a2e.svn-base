"""engine_core: 双骨架统一共享层。

收口 CRUD 链与 Pipeline 共用的现代化基础设施（零行为变更迁移）：
- dispatcher: SubAgent 并行派发 + 超时 + 失败隔离（asyncio.gather）
- roles: 角色化 SubAgent（Dialog/ItemNpc/ButterflyEvent/Generic）
- checkpoint: 7 步断点续跑 CheckpointManager
- operation_orchestrator: 拓扑排序 + produces 推断 + 占位符替换
- verifier: 规则终检（value_constraints/cascade_rules/anti_patterns/symbol_closure）

迁移自 subagent/core/pipeline 子包，原位置保留 shim re-export 兼容入口。
后续 CRUD 链复杂意图接入 dispatcher 多 Agent 并行（方案 A2）在此层落点。
"""
