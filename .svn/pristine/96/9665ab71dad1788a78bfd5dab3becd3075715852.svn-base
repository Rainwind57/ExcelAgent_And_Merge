# activity 表填表知识

## 表结构（resources/activity.xlsx，sheet: Activity）

- `活动id`（id:int）主键，新增时取当前段最大值 +1，禁止硬编码。
- `活动类型`（activity_type:int）枚举列，填数字码：
  - 1 = 主线活动
  - 2 = 节日活动（春节/中秋/端午等节日类）
  - 3 = 限时活动（世界BOSS/限时挑战等）
  - 4 = 日常活动
  - 5 = 周常活动
  - 6 = 赛季活动
  - 用户说「节日」→ 填 2；「限时」→ 填 3；「日常」→ 填 4；「周常」→ 填 5。
  - ⚠ 若用户口述的活动类型不在上列，按最接近的语义归类，不要留空、不要填中文。
- `活动名称`（name:string）用户给了就填中文，未给留空。
- `活动描述`（desc:string）用户给了就填中文，未给留空。
- `活动图标`（icon:string）可选，填图标资源名（如 `icon_spring_festival`），未提及留空。
- `活动开始时间`（start_time:str）格式 `YYYY-MM-DD HH:MM:SS`，用户给了原样填，未给留空。
- `活动结束时间`（end_time:str）格式 `YYYY-MM-DD HH:MM:SS`，用户给了原样填，未给留空。
- `活动展示开始时间`（open_time:str）可选，未提及留空（默认与开始时间一致由程序处理）。
- `活动展示结束时间`（close_time:str）可选，未提及留空。
- `开启条件`（open_condition:str）可选，未提及留空。

## 填值规则

- 时间列必须是字符串格式 `YYYY-MM-DD HH:MM:SS`，不要写 datetime 对象、不要写时间戳。
- 活动类型是 int 列，禁止填中文「节日」「限时」，必须先归一为数字码。
- activity_id 用户显式给（如 3001）时直接用，未给时取段内 max+1。
