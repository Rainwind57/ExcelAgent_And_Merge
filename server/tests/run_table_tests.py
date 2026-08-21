"""
AI 配表助手 — 表格操作综合测试套件。

混合模式全面覆盖：
  - 查询/预览类  → POST /api/agent/preview (dry-run，不写盘)
  - 写操作类     → POST /api/agent/chat (真实写盘) + 快照恢复保证可重复
  - 边界/异常    → preview 模式探测错误处理
  - 批量/快照    → /api/agent/batch + /api/workflow/snapshot/restore 闭环

每条用例：id + 指令 + 意图类型 + 理应结果(可程序化断言) + 判定规则。
跑完输出逐条 PASS/FAIL + 准确率统计。

用法：
    uv run python -m server.tests.run_table_tests
    或： python -m server.tests.run_table_tests --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


# ── HTTP 封装 ──────────────────────────────────────────────

class Api:
    def __init__(self, base: str, timeout: int = 120):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def preview(self, message: str, table_hint: Optional[str] = None) -> dict:
        body = {"message": message}
        if table_hint:
            body["table_hint"] = table_hint
        return self._call("POST", "/api/agent/preview", body)

    def chat(self, message: str, session_id: str = "default") -> dict:
        return self._call("POST", "/api/agent/chat",
                          {"message": message, "session_id": session_id})

    def batch(self, messages: list, session_id: str = "default",
              stop_on_error: bool = False) -> dict:
        return self._call("POST", "/api/agent/batch",
                          {"messages": messages, "session_id": session_id,
                           "stop_on_error": stop_on_error})

    def create_snapshot(self, name: str = "") -> dict:
        return self._call("POST", "/api/workflow/snapshot", {"name": name})

    def restore_snapshot(self, snap_id: str) -> dict:
        return self._call("POST", f"/api/workflow/snapshot/{snap_id}/restore")

    def health(self) -> bool:
        r = self._call("GET", "/api/tables")
        return isinstance(r, list)


# ── 断言助手 ────────────────────────────────────────────────

def _to_text(v) -> str:
    return "" if v is None else str(v)


def assert_ok(resp: dict) -> bool:
    """响应 ok 字段为 True。"""
    return bool(resp.get("ok"))


def assert_intent(resp: dict, expected: str) -> bool:
    """intent 字段等于预期。preview/chat 均适用。"""
    return resp.get("intent", "") == expected


def assert_intent_any(resp: dict, *expected: str) -> bool:
    """intent 字段等于预期集合中任一（新增类系统可能返回 add/insert）。"""
    return resp.get("intent", "") in expected


def assert_reply_type(resp: dict, expected: str) -> bool:
    return resp.get("reply_type", "") == expected


def assert_message_contains(resp: dict, *subs: str) -> bool:
    """message 文本包含全部子串（大小写不敏感）。"""
    msg = _to_text(resp.get("message", "")).lower()
    return all(s.lower() in msg for s in subs)


def assert_message_any(resp: dict, *subs: str) -> bool:
    """message 文本包含任一子串。"""
    msg = _to_text(resp.get("message", "")).lower()
    return any(s.lower() in msg for s in subs)


def assert_data_value(resp: dict, expected: str) -> bool:
    """data.value 等于预期（字符串比较，容空）。"""
    data = resp.get("data") or {}
    val = data.get("value")
    return _to_text(val) == str(expected)


def assert_data_any_value(resp: dict, *expected: str) -> bool:
    """data.value 等于预期集合中任一。"""
    data = resp.get("data") or {}
    val = _to_text(data.get("value"))
    return val in [str(e) for e in expected]


def assert_diff_has_changes(resp: dict) -> bool:
    """diff_preview 存在且 changes 非空（写操作生效）。"""
    dp = resp.get("diff_preview")
    if not dp:
        return False
    return bool(dp.get("changes"))


def assert_diff_change_value(resp: dict, expected: Any) -> bool:
    """diff_preview.changes[0].new_value 等于预期（写入校验）。"""
    dp = resp.get("diff_preview")
    if not dp:
        return False
    changes = dp.get("changes") or []
    if not changes:
        return False
    return _to_text(changes[0].get("new_value")) == str(expected)


def assert_not_ok(resp: dict) -> bool:
    """响应 ok 为 False（边界/异常用例预期失败）。"""
    return not bool(resp.get("ok"))


def assert_steps_contain(resp: dict, keyword: str) -> bool:
    """任一 step.detail 包含关键字（用于校验定位过程）。"""
    steps = resp.get("steps") or []
    kw = keyword.lower()
    return any(kw in _to_text(s.get("detail", "")).lower() for s in steps)


# ── 用例定义 ────────────────────────────────────────────────

@dataclass
class Case:
    id: str
    category: str            # 类别：单表查询/修改/新增/删除/复合/多表/边界/批量/快照
    instruction: str         # 自然语言指令
    mode: str                # preview | chat | batch
    intent: str              # 预期 intent: get/set/insert/delete/qa/unknown
    desc: str                # 用例说明（理应结果的人类描述）
    table_hint: Optional[str] = None
    batch_msgs: Optional[list] = None
    check: Optional[Callable[[dict], bool]] = None   # 判定函数
    setup_chat: Optional[list] = None  # 前置 chat 指令（写操作场景的前置数据）
    needs_snapshot: bool = False        # 是否需要写盘（影响快照策略）


def _all_cases() -> list[Case]:
    """全部测试用例。理实数据样本：
    pet.xlsx Pet sheet: 火焰犬(id1052 行5) 物攻资质=1500 成长率=1.15 体力资质=4500 速度资质=1000
                       烈焰犬(id2052 行6) 物攻资质=1550 成长率=1.2 体力资质=4700 速度资质=1200
                       炽焰獒(id3052 行7) 物攻资质=1600 成长率=1.25 速度资质=1400
                       白泽一/二/三阶(行38-40) 名称前缀同为"白泽" → 歧义
                       pet_evolve.xlsx 经 宠物id 外键引用上述灵兽 → 级联删除
    building.xlsx: 帮派基地/坊市/传送阵/城主府/青龙图腾/朱雀图腾/玄武图腾/瞭望塔 等20个
    hero.xlsx Hero: 初始角色(id5022 行5) 移动速度=6 面板气血上限=15000 初始门派id=1
                   男角色(id8001 行6) 初始门派id=2  女角色(id8002 行7) 初始门派id=3
    mail.xlsx MailTemplate: 10001 标题=测试邮件模板标题 内容=测试邮件模板内容
                           10002 内容=欢迎{},来到新大陆
    fabao.xlsx Fabao: 无敌法宝(id1 行5) 法宝描述=这是一个神奇的法宝
    """
    C: list[Case] = []

    # ── A. 单表查询（get）— preview 模式 ──
    C += [
        Case("A01", "单表查询", "查询灵兽火焰犬的物攻资质", "preview", "get",
             "火焰犬物攻资质=1500，应返回 1500",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "get")
                             and assert_message_contains(r, "1500")),
        Case("A02", "单表查询", "查询灵兽火焰犬的成长率", "preview", "get",
             "火焰犬成长率=1.15",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1.15")),
        Case("A03", "单表查询", "查询灵兽烈焰犬的物攻资质", "preview", "get",
             "烈焰犬物攻资质=1550",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1550")),
        Case("A04", "单表查询", "查询灵兽炽焰獒的成长率", "preview", "get",
             "炽焰獒成长率=1.25",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1.25")),
        Case("A05", "单表查询", "查询灵兽火焰犬的体力资质", "preview", "get",
             "火焰犬体力资质=4500",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "4500")),
        Case("A06", "单表查询", "查询灵兽火焰犬的所有属性", "preview", "get",
             "应返回该行数据，message 含火焰犬",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "get")
                             and assert_message_contains(r, "火焰犬")),
        # building 表查询
        Case("A07", "单表查询", "查询建筑帮派基地的建筑名称", "preview", "get",
             "帮派基地是真实建筑名，应能查到",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "帮派基地")),
        Case("A08", "单表查询", "查询建筑传送阵的描述", "preview", "get",
             "传送阵是真实建筑，应能查到",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "传送阵")),
        # ── 跨表覆盖：hero / mail / fabao + 更多 pet / building ──
        Case("A09", "单表查询", "查询人物初始角色的移动速度", "preview", "get",
             "hero 初始角色(row5) 移动速度=6",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_intent(r, "get")
                             and assert_data_value(r, "6")),
        Case("A10", "单表查询", "查询人物初始角色的面板气血上限", "preview", "get",
             "hero 初始角色 面板气血上限=15000",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_data_value(r, "15000")),
        Case("A11", "单表查询", "查询人物男角色的初始门派id", "preview", "get",
             "hero 男角色(row6) 初始门派id=2",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_data_value(r, "2")),
        Case("A12", "单表查询", "查询邮件模板10001的标题", "preview", "get",
             "mail 10001 标题=测试邮件模板标题",
             table_hint="mail",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "测试邮件模板标题")),
        Case("A13", "单表查询", "查询邮件模板10002的内容", "preview", "get",
             "mail 10002 内容=欢迎{},来到新大陆",
             table_hint="mail",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "欢迎")),
        Case("A14", "单表查询", "查询法宝无敌法宝的法宝描述", "preview", "get",
             "fabao 无敌法宝(row5) 法宝描述=这是一个神奇的法宝",
             table_hint="fabao",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "神奇的法宝")),
        Case("A15", "单表查询", "查询灵兽炽焰獒的物攻资质", "preview", "get",
             "炽焰獒(row7) 物攻资质=1600",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1600")),
        Case("A16", "单表查询", "查询灵兽烈焰犬的体力资质", "preview", "get",
             "烈焰犬(row6) 体力资质=4700",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "4700")),
        Case("A17", "单表查询", "查询灵兽炽焰獒的速度资质", "preview", "get",
             "炽焰獒(row7) 速度资质=1400",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_data_value(r, "1400")),
        Case("A18", "单表查询", "查询建筑城主府的建筑名称", "preview", "get",
             "城主府是真实建筑，应能查到",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "城主府")),
        Case("A19", "单表查询", "查询建筑瞭望塔的建筑名称", "preview", "get",
             "瞭望塔是真实建筑，应能查到",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "瞭望塔")),
    ]

    # ── B. 单表修改（set）— preview 模式（不写盘，仅验证解析+定位+diff生成）──
    C += [
        Case("B01", "单表修改", "将灵兽火焰犬的物攻资质改为 1500", "preview", "set",
             "值与原值相同(1500)，应生成 diff changes 含 1500",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "1500")),
        Case("B02", "单表修改", "将灵兽火焰犬的物攻资质改为 2000", "preview", "set",
             "应生成 diff changes new_value=2000",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "2000")),
        Case("B03", "单表修改", "把灵兽炽焰獒的成长率改成 1.5", "preview", "set",
             "应生成 diff new_value=1.5",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "1.5")),
        Case("B04", "单表修改", "将灵兽烈焰犬的速度资质改为 1800", "preview", "set",
             "烈焰犬速度资质原=1200，改后 diff new_value=1800",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "1800")),
        Case("B05", "单表修改", "将人物初始角色的移动速度改为 8", "preview", "set",
             "hero 初始角色 移动速度→8，diff new_value=8",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "8")),
        Case("B06", "单表修改", "将建筑帮派基地的描述改为 测试基地描述", "preview", "set",
             "building 帮派基地 描述→测试基地描述",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "测试基地描述")),
        Case("B07", "单表修改", "把灵兽炽焰獒的速度资质改为 2000", "preview", "set",
             "炽焰獒速度资质→2000，diff new_value=2000",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "2000")),
        Case("B08", "单表修改", "将邮件模板10001的标题改为 新标题测试", "preview", "set",
             "mail 10001 标题→新标题测试",
             table_hint="mail",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "新标题测试")),
    ]

    # ── C. 单表新增（insert）— preview 模式 ──
    C += [
        Case("C01", "单表新增", "新增一个灵兽，名称测试兽A，物攻资质1600", "preview", "insert",
             "应识别 insert，ok=True",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("C02", "单表新增", "新增一个灵兽，名称朱雀，品质3，成长率1.5", "preview", "insert",
             "复合字段新增，应 ok",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("C03", "单表新增", "增加建筑名称为测试塔，赋值它的建筑类型是99", "preview", "insert",
             "复合语句+代词消解，应识别 insert",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("C04", "单表新增", "新增一个人物，名称测试英雄，移动速度5", "preview", "insert",
             "hero 新增，应识别 insert",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("C05", "单表新增", "新增邮件模板，标题测试邮件X，内容测试内容X", "preview", "insert",
             "mail 新增，应识别 insert",
             table_hint="mail",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("C06", "单表新增", "新增一个法宝，名称测试法宝X，法宝描述测试描述", "preview", "insert",
             "fabao 新增，应识别 insert",
             table_hint="fabao",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
    ]

    # ── D. 单表删除（delete）— preview 模式 ──
    C += [
        Case("D01", "单表删除", "删除灵兽名称为火焰犬的行", "preview", "delete",
             "应识别 delete，ok=True（预览不真删）",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")),
        Case("D02", "单表删除", "删除灵兽烈焰犬", "preview", "delete",
             "简写删除，应识别 delete",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")),
        Case("D03", "单表删除", "删除建筑名称为瞭望塔的行", "preview", "delete",
             "building 瞭望塔唯一，预览在临时副本删除，应识别 delete",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")),
        Case("D04", "单表删除", "删除邮件模板10001", "preview", "delete",
             "mail 10001 唯一，预览删除，应识别 delete",
             table_hint="mail",
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")),
    ]

    # ── E. 复合语句 / 代词消解 — preview 模式 ──
    C += [
        Case("E01", "复合语句", "增加建筑名称为瞭望塔，赋值它的建筑类型是99999", "preview", "insert",
             "case-guide 原例：复合 INSERT + 代词'它'消解，应 ok",
             table_hint="building",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("E02", "复合语句", "新增灵兽名称朱雀，然后设置它的成长率是2.0", "preview", "insert",
             "复合语句含代词'它'，应识别 insert",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
        Case("E03", "复合语句", "把灵兽火焰犬的物攻资质改为2000，法攻资质改为1500", "preview", "set",
             "复合多字段修改，应识别 set",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_intent(r, "set")),
    ]

    # ── F. 多表 / 跨表查询 — preview/qa ──
    C += [
        Case("F01", "多表查询", "灵兽相关的表有哪些", "preview", "qa",
             "QA 意图，reply_type=qa，回答应含 pet 等表",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "pet", "灵兽", "宠物")),
        Case("F02", "多表查询", "邮件相关的表格有哪些", "preview", "qa",
             "QA，回答应含 mail",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "mail", "邮件")),
        Case("F03", "多表查询", "hero表有哪些列", "preview", "qa",
             "QA，回答应含 hero 表的列名（如名称/人物id）",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "名称", "hero", "人物id")),
        Case("F04", "多表查询", "有哪些配置表", "preview", "qa",
             "QA，应列举表名",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")),
        Case("F05", "多表查询", "建筑相关的表有哪些", "preview", "qa",
             "QA，应含 building",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "building", "建筑")),
        Case("F06", "多表查询", "法宝相关的表有哪些", "preview", "qa",
             "QA，应含 fabao",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "fabao", "法宝")),
        Case("F07", "多表查询", "人物角色表有哪些列", "preview", "qa",
             "QA，应含 hero 表的列名（如名称/人物id）",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "名称", "hero", "人物id")),
        Case("F08", "多表查询", "灵兽表有哪些列", "preview", "qa",
             "QA，应含 pet 表的列名（如灵兽id/物攻资质）",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")
                             and assert_message_any(r, "灵兽id", "物攻资质", "成长率")),
    ]

    # ── G. 边界 / 异常处理 — preview 模式 ──
    C += [
        Case("G01", "边界异常", "查询灵兽不存在的兽的物攻资质", "preview", "get",
             "查不到行，应 ok=False 或给出未找到提示",
             table_hint="pet",
             check=lambda r: (assert_not_ok(r)) or assert_message_any(r, "未找到", "不存在", "找不到", "无")),
        Case("G02", "边界异常", "查询灵兽火焰犬的不存在的列", "preview", "get",
             "列不存在，应 ok=False 或提示",
             table_hint="pet",
             check=lambda r: assert_not_ok(r) or assert_message_any(r, "未找到", "不存在", "找不到", "无法", "匹配")),
        Case("G03", "边界异常", "将灵兽火焰犬的物攻资质改为", "preview", "set",
             "缺失值，应 ok=False",
             table_hint="pet",
             check=lambda r: assert_not_ok(r)),
        Case("G04", "边界异常", "你好", "preview", "qa",
             "闲聊应走 qa 分支",
             check=lambda r: assert_ok(r) and assert_reply_type(r, "qa")),
        Case("G05", "边界异常", "删除灵兽完全不存在兽XYZ的行", "preview", "delete",
             "删除不存在的行，应 ok=False 或提示未找到",
             table_hint="pet",
             check=lambda r: assert_not_ok(r) or assert_message_any(r, "未找到", "不存在", "找不到")),
        Case("G06", "边界异常", "查询灵兽火焰犬", "preview", "get",
             "只给行名未给目标列且无泛指词，信息不足应反问/失败",
             table_hint="pet",
             check=lambda r: assert_not_ok(r)
                             or assert_message_any(r, "目标列", "属性", "明确", "列", "无法")),
        Case("G07", "边界异常", "删除灵兽", "preview", "delete",
             "删除指令缺定位行，信息不足应反问/失败",
             table_hint="pet",
             check=lambda r: assert_not_ok(r)
                             or assert_message_any(r, "定位", "明确", "未找到", "缺少", "哪")),
        Case("G08", "边界异常", "查询灵兽火焰犬的物攻资质是多少呢", "preview", "get",
             "口语化查询（含'是多少呢'），应能理解并返回 1500",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1500")),
    ]

    # ── H. 真实写盘闭环（chat 模式 + 快照恢复）──
    # 顺序：建快照 → 前置(可选) → 执行写 → 读回校验 → 恢复快照
    C += [
        Case("H01", "写盘闭环", "将灵兽火焰犬的物攻资质改为 2000", "chat", "set",
             "真实写盘后，再查询应为 2000（闭环）",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "2000")),
        Case("H02", "写盘闭环", "将灵兽火焰犬的成长率改为 2.0", "chat", "set",
             "真实写盘，diff new_value=2.0",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "2.0")),
        Case("H03", "写盘闭环", "新增一个灵兽，名称测试兽闭环，物攻资质1800", "chat", "insert",
             "真实新增，应 ok",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent_any(r, "insert", "add")),
    ]

    # ── I. 批量操作 — batch 模式 ──
    C += [
        Case("I01", "批量操作", "批量查询宠物属性", "batch", "batch",
             "3 条查询指令，应全部 ok，success_count=3",
             batch_msgs=[
                 "查询灵兽火焰犬的物攻资质",
                 "查询灵兽烈焰犬的物攻资质",
                 "查询灵兽炽焰獒的成长率",
             ],
             check=lambda r: assert_ok(r) and r.get("success_count", 0) == 3
                             and r.get("fail_count", 0) == 0),
        Case("I02", "批量操作", "批量含一条失败", "batch", "batch",
             "stop_on_error=False，第1条 ok + 第2条失败，总 success>=1",
             batch_msgs=[
                 "查询灵兽火焰犬的物攻资质",
                 "查询灵兽不存在的兽XYZABC的物攻资质",
             ],
             check=lambda r: assert_ok(r) and r.get("success_count", 0) >= 1
                             and r.get("fail_count", 0) >= 1),
    ]

    # ── J. 快照恢复闭环 ──
    C += [
        Case("J01", "快照闭环", "将灵兽火焰犬的物攻资质改为2000", "chat", "set",
             "改火焰犬物攻资质为2000→恢复快照→查回应回1500",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "set")
                             and assert_diff_change_value(r, "2000")),
    ]

    # ── K. 自然语言模糊输入对照 — preview 模式 ──
    # 面对模糊/口语化输入：能理解则执行（与精确表述的对照用例结果一致），
    # 信息不足则反问（ok=False 或 message 含澄清关键词），不得静默误执行。
    C += [
        # K01-K05：模糊但信息充足 → 应理解并执行（与 A 系列精确表述对照）
        Case("K01", "模糊对照", "火焰犬物攻资质多少", "preview", "get",
             "对照 A01 精确表述，口语'多少'应理解 → 1500",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1500")),
        Case("K02", "模糊对照", "看看炽焰獒的成长率", "preview", "get",
             "口语'看看'，应理解 → 1.25",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1.25")),
        Case("K03", "模糊对照", "烈焰犬速度资质", "preview", "get",
             "裸字段表述，应理解 → 1200",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "1200")),
        Case("K04", "模糊对照", "无敌法宝的法宝描述", "preview", "get",
             "跨表口语查询，应理解 → 神奇的法宝",
             table_hint="fabao",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "神奇的法宝")),
        Case("K05", "模糊对照", "初始角色的面板气血上限", "preview", "get",
             "hero 口语查询，应理解 → 15000",
             table_hint="hero",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "15000")),
        Case("K06", "模糊对照", "火焰犬的所有属性", "preview", "get",
             "泛指'所有属性' → 整行读取，message 含火焰犬",
             table_hint="pet",
             check=lambda r: assert_ok(r) and assert_message_contains(r, "火焰犬")),
        # K07-K11：信息不足 / 歧义 → 应反问，不得静默执行
        Case("K07", "模糊对照", "把火焰犬物攻资质调高一点", "preview", "set",
             "值模糊（'调高一点'），信息不足应反问/失败",
             table_hint="pet",
             check=lambda r: assert_not_ok(r)
                             or assert_message_any(r, "值", "明确", "具体", "无法", "缺少", "提供")),
        Case("K08", "模糊对照", "查询灵兽白泽的成长率", "preview", "get",
             "白泽命中多行(一/二/三阶)：应反问消歧，或理解执行其一（不得崩溃）",
             table_hint="pet",
             check=lambda r: assert_ok(r)
                             or assert_message_any(r, "多行", "候选", "明确", "确认", "歧义")),
        Case("K09", "模糊对照", "删除灵兽白泽", "preview", "delete",
             "删除歧义名(白泽一/二/三阶)，应反问要求明确指定",
             table_hint="pet",
             check=lambda r: assert_message_any(r, "多行", "候选", "明确", "确认", "级联", "歧义")),
        Case("K10", "模糊对照", "查询张三的属性", "preview", "get",
             "无法定位表格/行，信息不足应反问/失败",
             check=lambda r: assert_not_ok(r)
                             or assert_message_any(r, "表格", "表", "无法", "未找到", "明确", "找不到")),
        Case("K11", "模糊对照", "把烈焰犬物攻资质改成两千", "preview", "set",
             "中文数字'两千'，应理解 → diff new_value=2000 或反问",
             table_hint="pet",
             check=lambda r: (assert_ok(r) and assert_diff_change_value(r, "2000"))
                             or assert_message_any(r, "明确", "具体", "无法", "值")),
    ]

    # ── L. 级联删除预览 — chat 模式（真实写盘路径，但不实际删除）──
    # pet 主行被 pet_evolve 外键引用，删除前应触发级联预览并要求确认。
    # needs_confirm 路径不写盘，needs_snapshot 仅用于保证起点干净。
    C += [
        Case("L01", "级联删除", "删除灵兽火焰犬", "chat", "delete",
             "火焰犬(灵兽id=1052)被 pet_evolve 引用，应预览级联删除并要求确认",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")
                             and assert_message_contains(r, "级联删除", "确认")),
        Case("L02", "级联删除", "删除灵兽烈焰犬", "chat", "delete",
             "烈焰犬(灵兽id=2052)被 pet_evolve 引用，应预览级联删除并要求确认",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")
                             and assert_message_contains(r, "级联删除", "确认")),
        Case("L03", "级联删除", "删除灵兽炽焰獒", "chat", "delete",
             "炽焰獒(灵兽id=3052)被 pet_evolve 引用，应预览级联删除并要求确认",
             table_hint="pet",
             needs_snapshot=True,
             check=lambda r: assert_ok(r) and assert_intent(r, "delete")
                             and assert_message_contains(r, "级联删除", "确认")),
    ]

    return C


# ── 执行器 ──────────────────────────────────────────────────

@dataclass
class CaseResult:
    case: Case
    passed: bool
    detail: str = ""
    resp_snippet: str = ""
    elapsed: float = 0.0


def _run_one(case: Case, api: Api, snap_id: str | None) -> CaseResult:
    """执行单条用例。返回判定结果 + 详情。"""
    t0 = time.time()
    detail_parts: list[str] = []
    resp: dict = {}

    try:
        if case.mode == "preview":
            resp = api.preview(case.instruction, table_hint=case.table_hint)
        elif case.mode == "chat":
            # 写盘前确保有快照（H/J 类）
            if case.needs_snapshot and not snap_id:
                snap = api.create_snapshot(name=f"pre_{case.id}")
                if snap.get("id"):
                    snap_id = snap["id"]
                    detail_parts.append(f"建快照={snap_id}")
            resp = api.chat(case.instruction, session_id=f"test_{case.id}")
        elif case.mode == "batch":
            resp = api.batch(case.batch_msgs or [],
                             session_id=f"batch_{case.id}",
                             stop_on_error=False)
        else:
            return CaseResult(case, False, f"未知 mode={case.mode}", "", time.time() - t0)

        # 判定
        passed = bool(case.check(resp)) if case.check else bool(resp.get("ok"))

        # 写盘闭环：恢复快照 + 读回校验
        if case.mode == "chat" and case.needs_snapshot and snap_id:
            # J01 额外做恢复+读回校验
            if case.id == "J01" and passed:
                restored = api.restore_snapshot(snap_id)
                detail_parts.append(f"恢复快照={'OK' if restored.get('status')=='ok' else 'FAIL'}")
                # 读回：preview 查火焰犬物攻资质，应回 1500
                rb = api.preview("查询灵兽火焰犬的物攻资质", table_hint="pet")
                rb_val = _to_text((rb.get("data") or {}).get("value"))
                readback_ok = (rb_val == "1500")
                detail_parts.append(f"读回物攻资质={rb_val} (期望1500)")
                passed = passed and readback_ok
            elif case.id == "H01" and passed:
                # H01 写后读回校验：preview 查应为 2000
                rb = api.preview("查询灵兽火焰犬的物攻资质", table_hint="pet")
                rb_val = _to_text((rb.get("data") or {}).get("value"))
                readback_ok = (rb_val == "2000")
                detail_parts.append(f"写后读回物攻资质={rb_val} (期望2000)")
                passed = passed and readback_ok
                # 立即恢复，避免污染后续用例
                restored = api.restore_snapshot(snap_id)
                detail_parts.append(f"恢复快照={'OK' if restored.get('status')=='ok' else 'FAIL'}")
            else:
                # 其他写盘用例：测完即恢复
                restored = api.restore_snapshot(snap_id)
                detail_parts.append(f"恢复快照={'OK' if restored.get('status')=='ok' else 'FAIL'}")
                # snap 用完即弃，后续用例建新的
                snap_id = None

    except Exception as e:
        passed = False
        detail_parts.append(f"异常: {e}")
        resp = {"error": str(e)}

    elapsed = time.time() - t0

    # 响应摘要（便于人工核查）
    if case.mode == "batch":
        snippet = f"success={resp.get('success_count')} fail={resp.get('fail_count')}"
    else:
        msg = _to_text(resp.get("message", ""))[:120]
        intent = resp.get("intent", "")
        ok = resp.get("ok")
        data_val = _to_text((resp.get("data") or {}).get("value", ""))[:40]
        has_diff = bool((resp.get("diff_preview") or {}).get("changes"))
        snippet = f"ok={ok} intent={intent} msg={msg!r} data.value={data_val!r} diff={has_diff}"

    return CaseResult(case, passed, "; ".join(detail_parts), snippet, elapsed)


def run_all(base_url: str, only_category: str | None = None,
            only_id: str | None = None) -> tuple[list[CaseResult], dict]:
    api = Api(base_url)
    if not api.health():
        print(f"[FATAL] 后端不可达: {base_url}，请先启动后端")
        return [], {}

    cases = _all_cases()
    if only_category:
        cases = [c for c in cases if c.category == only_category]
    if only_id:
        cases = [c for c in cases if c.id == only_id]

    results: list[CaseResult] = []
    # 共享快照：所有 needs_snapshot 用例前建一次，最后统一恢复
    shared_snap = None
    # 为写盘类用例预先建一个共享快照
    has_write = any(c.mode == "chat" and c.needs_snapshot for c in cases)
    if has_write:
        print("[setup] 为写盘用例创建共享快照...")
        snap = api.create_snapshot(name="table_tests_baseline")
        if snap.get("id"):
            shared_snap = snap["id"]
            print(f"[setup] 共享快照={shared_snap}")
        else:
            print(f"[WARN] 建快照失败: {snap}，写盘用例将跳过恢复")

    for c in cases:
        print(f"\n[{c.id}] {c.category} | {c.instruction}")
        print(f"       预期: {c.desc}")
        # 共享快照模式下，每条写盘用例前恢复到基线，保证起点一致
        cur_snap = shared_snap
        if c.mode == "chat" and c.needs_snapshot and shared_snap:
            # 先恢复到基线，确保该用例起点干净
            api.restore_snapshot(shared_snap)
        r = _run_one(c, api, cur_snap)
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"       => {status} ({r.elapsed:.1f}s) {r.detail}")
        print(f"          resp: {r.resp_snippet}")
        # 失败时打印更多
        if not r.passed:
            print(f"          [FAIL详情] {r.resp_snippet}")

    # 收尾：恢复共享快照
    if shared_snap:
        print(f"\n[teardown] 恢复共享快照 {shared_snap} ...")
        api.restore_snapshot(shared_snap)
        print("[teardown] 已恢复，resources/ 回到测试前状态")

    # 统计
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r.case.category, []).append(r.passed)

    stats = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": round(passed / total * 100, 2) if total else 0.0,
        "by_category": {
            cat: {"total": len(v), "passed": sum(v),
                  "accuracy": round(sum(v) / len(v) * 100, 2) if v else 0.0}
            for cat, v in sorted(by_cat.items())
        },
    }
    return results, stats


def _print_report(results: list[CaseResult], stats: dict):
    print("\n" + "=" * 72)
    print("测试报告")
    print("=" * 72)
    print(f"总计 {stats['total']} 条 | 通过 {stats['passed']} | 失败 {stats['failed']}"
          f" | 准确率 {stats['accuracy']}%")
    print("-" * 72)
    print("按类别：")
    for cat, s in stats["by_category"].items():
        print(f"  {cat:<10} {s['passed']}/{s['total']}  ({s['accuracy']}%)")
    print("-" * 72)
    print("明细：")
    for r in results:
        mark = "✓" if r.passed else "✗"
        print(f"  {mark} [{r.case.id}] {r.case.category:<8} {r.case.instruction[:40]:<42}"
              f" {'PASS' if r.passed else 'FAIL'}")
        if not r.passed:
            print(f"        失败详情: {r.resp_snippet}")
            if r.detail:
                print(f"        步骤: {r.detail}")
    print("=" * 72)
    print(f"系统准确率: {stats['accuracy']}% ({stats['passed']}/{stats['total']})")


def _build_report_payload(results: list[CaseResult], stats: dict,
                          base_url: str) -> dict:
    """构建可序列化的报告 payload。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": base_url,
        "stats": stats,
        "cases": [
            {
                "id": r.case.id, "category": r.case.category,
                "instruction": r.case.instruction, "mode": r.case.mode,
                "expected_intent": r.case.intent, "desc": r.case.desc,
                "table_hint": r.case.table_hint,
                "passed": r.passed, "detail": r.detail,
                "resp": r.resp_snippet, "elapsed": round(r.elapsed, 2),
            }
            for r in results
        ],
    }


def _save_report_json(payload: dict, out_path: Path) -> Path:
    """写入 JSON 报告,并同步一份 latest.json。返回实际写入路径。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    latest = out_path.parent / "latest.json"
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def main():
    p = argparse.ArgumentParser(description="AI 配表助手 表格操作综合测试")
    p.add_argument("--base", default="http://127.0.0.1:8000",
                   help="后端 base URL")
    p.add_argument("--category", default=None, help="只跑某类别")
    p.add_argument("--id", default=None, help="只跑某 id")
    p.add_argument("--json-out", default=None,
                   help="把结果写入指定 json 文件(覆盖默认路径)")
    p.add_argument("--no-json", action="store_true",
                   help="不自动保存 json 报告")
    args = p.parse_args()

    results, stats = run_all(args.base, args.category, args.id)
    if not results:
        return 1
    _print_report(results, stats)

    if not args.no_json:
        payload = _build_report_payload(results, stats, args.base)
        if args.json_out:
            out_path = Path(args.json_out)
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = Path(__file__).parent / "reports" / f"run_table_tests_{ts}.json"
        try:
            _save_report_json(payload, out_path)
            latest = out_path.parent / "latest.json"
            print(f"\nJSON 报告已写入: {out_path}")
            print(f"JSON 最新副本: {latest}")
        except Exception as e:
            print(f"\n[WARN] JSON 报告写入失败: {e}")

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
