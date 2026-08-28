# combat 表填表知识

## 表结构（resources/combat/combat.xlsx，sheet: CombatData）

- `combat_id`（id:int）主键，用户显式给时直接用，未给时取段内 max+1。
- `space_id`（space_id:int）战场 ID，FK→space.space_id。用户说「战场 10050」→ 10050。
- `win_reward`（win_reward:int）打赢给的奖励包 ID，FK→reward.reward_id。用户说「打赢给奖励包 30010」→ 30010。
- `lose_reward`（lose_reward:int）打输给的奖励包 ID。用户说「输了不给」→ 留空或 0。
- `draw_reward`（draw_reward:int）平局给的奖励包 ID。用户说「平局不给」→ 留空或 0。
- `npc_ids[0..11]`（npc_ids[N]:int）战斗 NPC 位置，最多 12 个位置，FK→pve_combat_npc.npc_id。用户说「刷一只 PVE 世界 BOSS」→ npc_ids[0] 填该 BOSS 的 npc_id。
- 其余列（buff/trigger 等）可选，未提及留空。

## 填值规则

- combat_id 用户显式给时直接用。
- win/lose/draw reward 三栏独立，用户说「打赢给 X，输了和平局都不给」→ win_reward=X, lose_reward=空, draw_reward=空。
- npc_ids 是位置数组，[0] 是首位，填 BOSS 的 npc_id；未提及的位置留空。
- 奖励 ID 引用他表时若本批产出，用 `<new_reward_id>` 占位符 + consumes 标注。
