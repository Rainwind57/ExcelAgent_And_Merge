# -*- coding: utf-8 -*-
"""聚焦验证：用户原始「探寻失落神器」对话树任务，真 LLM 全链路。

复用 run_cross_table_fullchain_real 的驱动/自动应答/行数 diff 逻辑，
仅替换输入为用户原始指令（含 conv/option 对话树 + BOSS 战 + 新建 NPC + set 引用）。
"""
import os
import sys
from pathlib import Path

# 关键：先加载 .env，再 import codemaker_client（其模块级常量在 import 时读取
# CODEMAKER_USERNAME/PASSWORD/SERVER_URL；不加载则 auth 为空 → /api/health 401 →
# health_check=False → 所有 LLM 调用短路成"serve_down"假通过）。
_ROOT = Path(__file__).resolve().parents[2]
_env = _ROOT / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _k, _v = _k.strip(), _v.strip()
        if _k and _k not in os.environ:
            os.environ[_k] = _v

os.environ["PK_CMD"] = (
    "新增一个限时世界BOSS活动叫'焚天赤龙降临'，activity_id 3001，"
    "活动描述'赤龙现世，各路豪杰齐心讨伐，首杀可得传说法宝'，"
    "开始时间 2024-12-21 00:00:00，结束时间 2024-12-28 23:59:59。"
    "配套首杀奖励包也建一下，reward_id 30010，叫'焚天赤龙首杀奖励'，"
    "每日限领 1 次，必给道具 10001 共 20 个，概率 100% 给经验、100% 给金币、"
    "金币公式填 800；再加一个保底掉落池 pool_id 3001，属性修改含 "
    "PhyAtkCon 范围 100-200 权重 20、MagAtkCon 范围 100-200 权重 20、"
    "CritCon 范围 5-15 权重 10，能力类型都填 2。"
    "BOSS 战斗也一起配上：在战场 10050 的坐标 (200,0,150) 刷一只 PVE 世界 BOSS，"
    "怪物名字叫'焚天赤龙'，model_id 1200，技能列表 9101/9102/9103/9104，"
    "AI 用 boss_ai，等级公式 80，气血斜率 60、气血基础 500000，"
    "物攻斜率 40、物攻基础 30000，打赢给奖励包 30010，输了和平局都不给。"
    "再配一个引导 NPC 叫'赤龙指引人'，model_id 1020，放在战场 10050 坐标 (190,0,140)，"
    "玩家点击他弹出对话'焚天赤龙正在肆虐人间，请勇士速速前往讨伐，首杀者将获得传说奖励'，"
    "给两个选项：第一个'我这就去讨伐'选完后跳到第二段奖励介绍对话后结束对话；"
    "第二个'了解一下奖励'跳到新对话，内容为'首杀奖励包含 20 个精炼石、"
    "海量经验与金币，以及随机词条池加成'，再给一个选项'知道了'选完什么都不做结束对话。"
)

import runpy
from pathlib import Path

_runner = Path(__file__).with_name("run_cross_table_fullchain_real.py")
runpy.run_path(str(_runner), run_name="__main__")
