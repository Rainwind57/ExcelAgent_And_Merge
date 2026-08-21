import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["PK_CMD"] = (
    "新增一个任务NPC叫寻宝老人，model_id 1019，放在space_id 10001坐标(120,0,80)，"
    "玩家点击后弹出对话：老人说年轻人，老朽有一事相求——我祖传的玉佩被山贼头目夺走，能否帮我寻回？"
    "选项1我帮你寻回，选项2我现在没空稍后再来。"
    "点击我帮你寻回后老人继续说多谢！那山贼头目盘踞在space_id 10008，请击杀他取回玉佩，必有重谢。"
    "再点我这就去接下任务。配置对应支线任务寻回玉佩，任务ID 250020，任务组group_id 250，"
    "描述帮寻宝老人从山贼头目处夺回祖传玉佩，目标类型Combat，"
    "目标数据combat_id:[25002001],npc_id:5025,count:1，完成奖励reward_id 10090。"
    "在space_id 10008坐标(50,0,60)刷新山贼头目(npc_id 5025)供玩家击杀。"
    "同时把reward_id 10090的名称改为寻回玉佩奖励。"
)

import run_pk_fullchain_real as m
sys.exit(m.main())
