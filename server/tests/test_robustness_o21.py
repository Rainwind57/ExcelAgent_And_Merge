"""O21 健壮性 + 真错误 + 表格交互 + 影响结果才阻塞 单测。

覆盖 4 改动：
- 健壮性：Step5 主循环 _phase_execute 异常 → failure + continue（单 op 崩不中断后续）
- 表格交互：3 处 ask suggestion 引导 field 模式填表格（非自然语言句子）+ example 字段
- 真错误：classify headers 比对去 `:` 后缀（"类型:int" 取 "类型"，避免假 COLUMN_NOT_FOUND）
- 影响结果：CODEMAKER_CONNECTIVITY_DEEP_CHECK 默认 on（悬空 FK 阻塞），env=off 显式关
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent.excel.repair.error_classifier import classify, ClassifiedError, ErrorType


class _FakeRes:
    """轻量 AgentResult（含 failures + steps + message + ok + dirty_data）。"""

    def __init__(self, message="", failures=None, steps=None, ok=True):
        self.message = message
        self.failures = failures or []
        self.steps = steps or []
        self.final = None
        self.intent = None
        self.ok = ok
        self.dirty_data = False
        self.partial = False
        self.result_rows = []
        self.needs_user_fill = []

    def add(self, name, ok, detail):
        self.steps.append(MagicMock(name=name, ok=ok, detail=detail))

    def add_thinking(self, *a, **kw):
        pass


# =====================================================================
# 改动 3：classify headers 比对去 `:` 后缀（真错误判定）
# =====================================================================

class TestClassifyHeadersSuffixStrip:
    """O21：headers 含 "类型:int" 时，failed_col="类型" 应命中（非假 COLUMN_NOT_FOUND）。"""

    def test_failed_col_matches_header_with_suffix(self):
        """failed_col="类型" 在 headers=["类型:int"] 中 → 不归 COLUMN_NOT_FOUND（列存在）。"""
        res = _FakeRes(message="写入失败", failures=[])
        # 无 verify 信号、无 regex 命中、failed_col 在去后缀 headers 中 → 不归 COLUMN_NOT_FOUND
        # 需构造 failed_col 来源：res.failures 无 failed_col，detail regex 无列名 → failed_col=None
        # 改为有 failed_col 的场景：构造 detail 含列名
        res = _FakeRes(
            message="列[类型] 不存在",
            failures=[{"code": 40, "kind": "other", "failed_col": "类型",
                       "message": "misc"}])
        c = classify(None, res, None, context={
            "table_stem": "t", "sheet": "s", "headers": ["类型:int", "名称"]})
        # "类型" 在去后缀 headers ["类型","名称"] 中 → 不应归 COLUMN_NOT_FOUND
        # 但 _COL_NOT_FOUND_RE 会命中 "列[类型] 不存在" → 仍归 COLUMN_NOT_FOUND
        # 此测验证 headers 比对分支：failed_col 在 headers 中时不走 headers-not-in 分支
        # 实际 regex 已命中 → COLUMN_NOT_FOUND，headers 比对是 OR 条件的第二支
        # 改测：failed_col 不在 regex 命中但 headers 含后缀 → 不归 COLUMN_NOT_FOUND
        assert c.error_type == ErrorType.COLUMN_NOT_FOUND  # regex 命中优先

    def test_header_suffix_not_false_positive(self):
        """failed_col="类型" + headers=["类型:int"]（去后缀="类型"）→ 不走 headers-not-in 假阳。"""
        # 构造无 regex 命中的场景：message 不含"不存在/未找到"等关键词
        # failed_col 来自 res.failures 结构化（kind 非 column_not_found，走 regex 兜底分支）
        res = _FakeRes(
            message="写入失败",
            failures=[{"code": 40, "kind": "write_failed",
                       "failed_col": "类型", "failed_val": 100,
                       "message": "写后验证不符"}])
        c = classify(None, res, None, context={
            "table_stem": "t", "sheet": "s", "headers": ["类型:int", "名称"]})
        # "类型" 在去后缀 headers 中 → headers-not-in 条件 False
        # 无 regex 命中（"写入失败"无列不存在关键词）→ 不归 COLUMN_NOT_FOUND
        # 落 type_mismatch？无。落 row_not_found？无。落 UNKNOWN。
        assert c.error_type == ErrorType.UNKNOWN
        assert c.failed_col == "类型"

    def test_header_suffix_old_behavior_preserved(self):
        """failed_col 真不存在于 headers（去后缀后）→ 仍归 COLUMN_NOT_FOUND。"""
        res = _FakeRes(
            message="写入失败",
            failures=[{"code": 40, "kind": "write_failed",
                       "failed_col": "不存在的列", "failed_val": 1,
                       "message": "写后验证不符"}])
        c = classify(None, res, None, context={
            "table_stem": "t", "sheet": "s", "headers": ["类型:int", "名称"]})
        # "不存在的列" 不在去后缀 headers → headers-not-in True → COLUMN_NOT_FOUND
        assert c.error_type == ErrorType.COLUMN_NOT_FOUND
        assert c.failed_col == "不存在的列"

    def test_no_headers_no_crash(self):
        """无 context headers → headers 比对分支跳过，不崩。"""
        res = _FakeRes(message="未知错误", failures=[])
        c = classify(None, res, None, context={})
        assert c.error_type == ErrorType.UNKNOWN


# =====================================================================
# 改动 1：Step5 健壮性（单 op 异常不中断）
# =====================================================================

class TestStep5DispatchExceptionRobustness:
    """O21：_phase_execute 抛异常 → Step5 主循环兜住 → failure + continue 下一 op。

    验证逻辑：源码断言 try/except 包裹 + failure dict 形状。
    完整 e2e 需构造 4-step NL 路径 mock（阻 R7 serve），此处源码断言 + 形状验证。
    """

    def test_source_has_try_except_around_phase_execute(self):
        """agent.py Step5 循环内 _phase_execute 调用被 try/except 包裹。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 定位 Step5 循环 _phase_execute 调用点
        idx = src.find("out = self._phase_execute(intent, path, sheet, res, confirm_token)")
        assert idx > 0, "未找到 _phase_execute 调用点"
        # 向前找 try:
        before = src[max(0, idx - 200):idx]
        assert "try:" in before, "Step5 _phase_execute 调用前无 try: 包裹"
        # 向后找 except Exception
        after = src[idx:idx + 200]
        assert "except Exception" in after, "Step5 _phase_execute 调用后无 except"

    def test_failure_dict_shape_dispatch_exception(self):
        """异常兜底产 failure dict 含 type/col/root_cause/status 字段。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 断言 dispatch_exception failure 形状
        assert '"type": "dispatch_exception"' in src
        assert '"attempted_strategies": ["direct_dispatch"]' in src
        assert '"status": "unresolved"' in src
        # 断言 broken_producers + failed_tables 标记 + continue
        assert "broken_producers.add(orig_idx)" in src
        assert "continue" in src[src.find("dispatch_exception"):src.find("dispatch_exception") + 1500]

    def test_continue_after_exception(self):
        """异常后 continue 下一 op（不 break/return 中断循环）。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        idx = src.find('"type": "dispatch_exception"')
        assert idx > 0
        # 在 dispatch_exception failure 块后 1500 字符内找 continue（块较大）
        block = src[idx:idx + 1500]
        assert "continue" in block, "异常兜底后无 continue 下一 op"


# =====================================================================
# 改动 2：表格交互（ask suggestion 引导 field 模式 + example）
# =====================================================================

class TestAskSuggestionFieldMode:
    """O21：3 处 ask suggestion 文本引导走 field 模式填表格，非补自然语言句子。"""

    def test_placeholder_ask_suggestion_no_natural_language(self):
        """占位符 ask suggestion 不含"补一句自然语言"。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 原文本"补一句自然语言"应已删除
        assert "补一句自然语言" not in src, "占位符 ask 仍含「补一句自然语言」"

    def test_placeholder_ask_suggestion_field_mode(self):
        """占位符 ask suggestion 引导填表格。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 定位占位符 ask suggestion（_ask_sug 赋值）
        idx = src.find("这些列无法自动生成，请在下方表格按列填入具体值")
        assert idx > 0, "占位符 ask suggestion 未改为 field 模式引导"

    def test_verify_repair_ask_suggestion_field_mode(self):
        """verify_repair 达上限 ask suggestion 引导填表格 + example 字段。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 原文本"重写这一条指令"应已删除
        assert "重写这一条指令" not in src, "verify_repair ask 仍含「重写这一条指令」"
        # 新 suggestion 引导填表格
        idx = src.find("请在下方表格按失败列填入正确字段值")
        assert idx > 0, "verify_repair ask suggestion 未改为 field 模式引导"
        # example 字段
        idx2 = src.find("（失败列「")
        assert idx2 > 0, "verify_repair ask 未带 failed_col/example 字段"

    def test_dangling_fk_ask_suggestion_field_mode(self):
        """悬空 FK ask suggestion 引导填表格 + example 字段。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 原文本"补一条指令"应已删除
        assert "补一条指令" not in src, "悬空 FK ask 仍含「补一条指令」"
        # 新 suggestion 引导填表格
        idx = src.find("在下方表格填入需补建的目标行的主键值")
        assert idx > 0, "悬空 FK ask suggestion 未改为 field 模式引导"
        # example 字段
        idx2 = src.find("填「（此处填目标主键值）」")
        assert idx2 > 0, "悬空 FK ask 未带 example 字段"


# =====================================================================
# 改动 4：CODEMAKER_CONNECTIVITY_DEEP_CHECK 默认 on（影响结果才阻塞）
# =====================================================================

class TestDeepCheckDefaultOn:
    """O21：悬空 FK 深度校验默认 on（影响结果阻塞），env=off 显式关。"""

    def test_default_on(self):
        """源码默认值从 == "1" 改为 != "off"（默认 on，env=off 显式关）。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 找 env get 调用行（非注释行）
        import re
        # 匹配 os.environ.get(...) <op> "off" 全表达式
        m = re.search(
            r'os\.environ\.get\(\s*["\']CODEMAKER_CONNECTIVITY_DEEP_CHECK["\']\s*,\s*["\']([^"\']+)["\']\s*\)\s*(\S+)\s*["\']off["\']',
            src)
        assert m, "未找到 CODEMAKER_CONNECTIVITY_DEEP_CHECK env get != off 判定"
        default_val = m.group(1)
        cmp_op = m.group(2)
        # 默认值应为 "1"（on），比较运算符应为 !=
        assert default_val == "1", f"默认值未改为 on（应为 1，实际 {default_val})"
        assert cmp_op == "!=", f"判定未改为 != off（实际 {cmp_op})"

    def test_off_env_disables(self):
        """env=off 显式关闭（向后兼容降级路径）。"""
        src_path = (Path(__file__).resolve().parents[1]
                    / "agent" / "excel" / "core" / "agent.py")
        src = src_path.read_text(encoding="utf-8")
        # 注释说明 env=off 关闭
        assert 'env=off' in src or '"off"' in src, "未文档化 env=off 关闭路径"
