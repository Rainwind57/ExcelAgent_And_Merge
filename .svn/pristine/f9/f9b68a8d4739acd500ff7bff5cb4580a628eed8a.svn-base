"""管道 7 步编排器测试:覆盖核心场景。

测试范围:
- 管道模式判定(文件路径/多表关键词/模式开关)
- 文件解析(.md/.csv/.txt/不支持扩展名)
- 符号映射表分配
- 断点续跑(Step3 失败中断 → Step0 续跑)
- instrument 层(ToolRecord 生成)
- 符号引用闭环校验(dangling_symbol)
- SubAgent 并发 + thinking 聚合
- 简单 CRUD 走旧路径径(回归)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保可导入
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from agent.excel.pipeline.pipeline import (  # noqa: E402
    should_trigger_pipeline, extract_file_path, Pipeline,
)
from agent.excel.pipeline.types import (  # noqa: E402
    PipelineContext, PipelineResult, AgentFragment, DocIntent,
)
from agent.excel.checkpoint import CheckpointManager  # noqa: E402
from agent.excel.cli_instrument import instrument, _make_tool_record  # noqa: E402
from agent.excel.parser.file_parser import parse_file, assign_symbols  # noqa: E402
from agent.excel.subagent.base import SubAgent  # noqa: E402
from agent.excel.subagent.dispatcher import dispatch  # noqa: E402
from agent.excel.pipeline.verifier import PipelineVerifier  # noqa: E402


# ── 12.6 管道模式判定 ──

class TestPipelineModeDetection:
    def test_file_path_triggers(self):
        assert should_trigger_pipeline("帮我处理 C:/test/script.md 里的任务") is True

    def test_multi_table_keyword_triggers(self):
        # §7 步收口：自然语言多表关键词默认不触发 pipeline（走 V2），
        # env CODEMAKER_PIPELINE_KEYWORDS=1 显式开启时才触发（兼容旧行为）。
        import os as _os
        _os.environ.pop("CODEMAKER_PIPELINE_KEYWORDS", None)
        assert should_trigger_pipeline("跨表配置 npc 和 dialog") is False
        assert should_trigger_pipeline("多张表流程") is False
        # env 开启时恢复关键词触发
        _os.environ["CODEMAKER_PIPELINE_KEYWORDS"] = "1"
        try:
            assert should_trigger_pipeline("跨表配置 npc 和 dialog") is True
            assert should_trigger_pipeline("多张表流程") is True
        finally:
            _os.environ.pop("CODEMAKER_PIPELINE_KEYWORDS", None)

    def test_simple_crud_no_trigger(self):
        assert should_trigger_pipeline("把item表里名字为铁匠的attack改为100") is False

    def test_generic_peibiao_no_trigger(self):
        # 「配表」是通用词,收紧后不再触发管道(auto 模式)
        assert should_trigger_pipeline("查询配表里铁匠的等级") is False
        assert should_trigger_pipeline("简单查询") is False

    def test_mode_off_disables(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_PIPELINE_MODE", "off")
        assert should_trigger_pipeline("处理 script.md") is False

    def test_mode_on_forces(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_PIPELINE_MODE", "on")
        assert should_trigger_pipeline("简单查询") is True

    def test_extract_file_path(self):
        text = "帮我处理 D:/剧本/谷雨灵茶.md 的任务"
        assert extract_file_path(text) == "D:/剧本/谷雨灵茶.md"

    def test_extract_file_path_none(self):
        assert extract_file_path("简单查询") is None


# ── 12.1 文件解析 ──

class TestFileParser:
    def test_md_parse(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("""# 任务

### 1.1 老茶农发愁
玩家：老伯，怎么不动手？
老陈：灵脉衰退……

### 1.2 采灵茶
老陈：只掐嫩尖。
""", encoding="utf-8")
        doc = parse_file(str(md))
        assert doc.ok is True
        assert doc.file_type == "md"
        assert len(doc.steps) == 2
        assert doc.steps[0].step_id == "1.1"
        assert doc.steps[0].title == "老茶农发愁"
        # NPC 符号分配
        assert any("老陈" in v for v in doc.symbol_map.values())

    def test_csv_parse(self, tmp_path):
        csv_f = tmp_path / "test.csv"
        csv_f.write_text("名字,等级\n铁匠,10\n农夫,5\n", encoding="utf-8")
        doc = parse_file(str(csv_f))
        assert doc.ok is True
        assert doc.file_type == "csv"
        assert len(doc.records) == 2
        assert doc.records[0]["名字"] == "铁匠"

    def test_txt_parse(self, tmp_path):
        txt = tmp_path / "test.txt"
        txt.write_text("纯文本意图内容", encoding="utf-8")
        doc = parse_file(str(txt))
        assert doc.ok is True
        assert doc.file_type == "txt"
        assert "纯文本" in doc.raw_text

    def test_unsupported_ext(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        pdf.write_text("dummy", encoding="utf-8")
        doc = parse_file(str(pdf))
        assert doc.ok is False
        assert "unsupported_file_type" in doc.error

    def test_file_not_found(self):
        doc = parse_file("/nonexistent/path.md")
        assert doc.ok is False

    def test_assign_symbols(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("### 1.1 测试\n老陈：对话\n", encoding="utf-8")
        doc = parse_file(str(md))
        assign_symbols(doc)
        # 老陈 应有符号
        assert any(v == "老陈" for v in doc.symbol_map.values())


# ── 12.3 断点续跑 ──

class TestCheckpoint:
    def test_init_and_load(self, tmp_path):
        ckpt = CheckpointManager(tmp_path)
        assert ckpt.load() is None
        ckpt.init_checkpoint("quest", "测试", "Trunk", "/path/script.md")
        data = ckpt.load()
        assert data is not None
        assert data["flow"] == "quest"
        assert data["scene"] == "测试"

    def test_update_step(self, tmp_path):
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("quest", "测试", "Trunk", "/path.md")
        ckpt.update("1_decompose", "done", output="拆解文件.md")
        data = ckpt.load()
        assert data["steps"]["1_decompose"]["status"] == "done"
        assert data["steps"]["1_decompose"]["output"] == "拆解文件.md"

    def test_failed_records_last_error(self, tmp_path):
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("quest", "测试", "Trunk", "/path.md")
        ckpt.update("3_fill", "failed", error="SubAgent 超时")
        data = ckpt.load()
        assert data["last_error"]["step"] == "3_fill"
        assert "超时" in data["last_error"]["error"]

    def test_resume_point(self, tmp_path):
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("quest", "测试", "Trunk", "/path.md")
        ckpt.update("0_checkpoint", "done")
        ckpt.update("1_decompose", "done")
        # 2_partition 未完成 → 续跑点
        assert ckpt.get_resume_point() == "2_partition"

    def test_is_complete(self, tmp_path):
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("quest", "测试", "Trunk", "/path.md")
        for sid in ["0_checkpoint", "1_decompose", "2_partition", "3_fill",
                    "4_assemble", "5_verify", "6_write", "7_cleanup"]:
            ckpt.update(sid, "done")
        assert ckpt.is_complete() is True


# ── 12.4 instrument 层 ──

class TestCliInstrument:
    def test_tool_record_generation(self):
        record = _make_tool_record("read_cell", ("item", "ItemBase", 449, 2),
                                    {}, 100, ok=True)
        assert record["name"] == "read_cell"
        assert record["ok"] is True
        assert "read_cell" in record["cmd"]
        assert record["result"] == 100

    def test_tool_record_failure(self):
        record = _make_tool_record("write_cell", ("item", "ItemBase", 449, 2),
                                    {}, None, ok=False, error="IO error")
        assert record["ok"] is False
        assert record["result"] == "IO error"

    def test_instrument_proxy(self):
        class FakeCLI:
            def read_cell(self, path, sheet, row, col):
                return f"{path}:{sheet}:{row}:{col}"
        records = []
        def sink(et, payload):
            records.append(payload)
        proxied = instrument(FakeCLI(), sink=sink, enabled=True)
        result = proxied.read_cell("item", "ItemBase", 449, 2)
        assert result == "item:ItemBase:449:2"
        assert len(records) == 1
        assert records[0]["name"] == "read_cell"

    def test_instrument_disabled_passthrough(self):
        class FakeCLI:
            def write_cell(self, *a, **k):
                return "ok"
        proxied = instrument(FakeCLI(), sink=None, enabled=False)
        assert proxied.write_cell(1, 2) == "ok"  # 无记录


# ── 12.5 符号引用闭环校验 ──

class TestSymbolClosure:
    def test_closure_ok(self):
        frag_a = AgentFragment(agent_name="A", produces="<npc_x>")
        frag_b = AgentFragment(agent_name="B", references=["<npc_x>"])
        verifier = PipelineVerifier()
        result = verifier._check_symbol_closure([frag_a, frag_b], {})
        assert result == []

    def test_dangling_symbol(self):
        frag = AgentFragment(agent_name="B", references=["<npc_missing>"])
        verifier = PipelineVerifier()
        result = verifier._check_symbol_closure([frag], {})
        assert len(result) == 1
        assert "dangling_symbol" in result[0]

    def test_closure_with_produced(self):
        frag = AgentFragment(agent_name="B", references=["<npc_x>"])
        verifier = PipelineVerifier()
        # produced 已有映射 → 闭环通过
        result = verifier._check_symbol_closure([frag], {"<npc_x>": "1001"})
        assert result == []


# ── 12.2 SubAgent 并发 + thinking 聚合 ──

class TestSubAgentDispatch:
    def test_concurrent_dispatch(self):
        class TestAgent(SubAgent):
            def _run_impl(self, prompt, skill_docs, context):
                self.add_thinking("执行", f"{self.name} working")
                return {"sql_or_ops": [{"action": "add"}], "produces": f"<{self.name}>"}
        agents = [TestAgent(f"Agent{c}") for c in "ABC"]
        prompts = ["prompt"] * 3
        thinkings = []
        def sink(phase, detail):
            thinkings.append((phase, detail))
        fragments = dispatch(agents, prompts, {}, thinking_sink=sink, timeout=30)
        assert len(fragments) == 3
        assert all(f.ok for f in fragments)
        # thinking 聚合到主流
        assert len(thinkings) >= 3

    def test_isolation_failure(self):
        class FailAgent(SubAgent):
            def _run_impl(self, prompt, skill_docs, context):
                raise RuntimeError("故意失败")
        class OkAgent(SubAgent):
            def _run_impl(self, prompt, skill_docs, context):
                return {"sql_or_ops": [], "produces": "<ok>"}
        agents = [FailAgent("Fail"), OkAgent("OK")]
        fragments = dispatch(agents, ["p"] * 2, {}, timeout=30)
        assert fragments[0].ok is False
        assert fragments[1].ok is True  # 隔离失败

    def test_timeout(self):
        import time
        class SlowAgent(SubAgent):
            def _run_impl(self, prompt, skill_docs, context):
                time.sleep(5)
                return {"sql_or_ops": []}
        agents = [SlowAgent("Slow")]
        fragments = dispatch(agents, ["p"], {}, timeout=1)
        assert fragments[0].ok is False
        assert "timeout" in fragments[0].error


# ── 12.7 回归:简单 CRUD 走旧路径径 ──

class TestRegressionOldPath:
    def test_simple_crud_no_pipeline_trigger(self):
        # 简单 CRUD 不应触发管道
        assert should_trigger_pipeline("查询item表名字为铁匠的信息") is False

    def test_pipeline_result_aggregation(self):
        result = PipelineResult()
        # 全成功
        from agent.excel.pipeline.types import StepResult
        result.steps = [StepResult(step_id="1", name="Step1", status="done")]
        result.ok = True
        assert "成功" in result.aggregated_message

    def test_pipeline_result_failure(self):
        result = PipelineResult()
        result.add_error("3_fill", "SubAgent 超时")
        assert result.ok is False
        assert "失败" in result.aggregated_message
        assert "3_fill" in result.aggregated_message


# ── 12.8 补:value_constraints required/unique/range 校验 ──

class TestValueConstraints:
    def test_required_violation(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(value_constraints={
            "item": {"name": {"required": True, "type": "str"}}
        })
        frag = AgentFragment(target_table="item",
                             sql_or_ops=[{"action": "add", "fields": {"name": ""}}])
        result = v._check_value_constraints([frag])
        assert any("required_violation" in r for r in result)

    def test_type_violation(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(value_constraints={
            "item": {"price": {"type": "int"}}
        })
        frag = AgentFragment(target_table="item",
                             sql_or_ops=[{"action": "add", "fields": {"price": "abc"}}])
        result = v._check_value_constraints([frag])
        assert any("type_constraint" in r for r in result)

    def test_range_violation(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(value_constraints={
            "item": {"level": {"type": "int", "min": 1, "max": 100}}
        })
        frag = AgentFragment(target_table="item",
                             sql_or_ops=[{"action": "add", "fields": {"level": 200}}])
        result = v._check_value_constraints([frag])
        assert any("range_violation" in r for r in result)

    def test_unique_violation(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(value_constraints={
            "item": {"item_id": {"unique": True}}
        })
        frag = AgentFragment(target_table="item",
                             sql_or_ops=[{"action": "add", "fields": {"item_id": 1001}},
                                          {"action": "add", "fields": {"item_id": 1001}}])
        result = v._check_value_constraints([frag])
        assert any("unique_violation" in r for r in result)

    def test_valid_no_violation(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(value_constraints={
            "item": {"name": {"required": True, "type": "str"}, "level": {"type": "int", "min": 1, "max": 100}}
        })
        frag = AgentFragment(target_table="item",
                             sql_or_ops=[{"action": "add", "fields": {"name": "铁剑", "level": 50}}])
        result = v._check_value_constraints([frag])
        assert result == []


# ── 12.8 补:cascade_rules 校验 ──

class TestCascadeRules:
    def test_cascade_delete_warning(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(cascade_rules={
            "tables": {
                "npc": {"on_delete": "cascade", "dependents": ["dialog", "interaction"]}
            }
        })
        frag = AgentFragment(target_table="npc",
                             sql_or_ops=[{"action": "delete"}])
        result = v._check_cascade_rules([frag])
        assert any("cascade_warning" in r for r in result)

    def test_no_cascade_on_add(self):
        from agent.excel.pipeline.verifier import PipelineVerifier
        from agent.excel.pipeline.types import AgentFragment
        v = PipelineVerifier(cascade_rules={
            "tables": {
                "npc": {"on_delete": "cascade", "dependents": ["dialog"]}
            }
        })
        frag = AgentFragment(target_table="npc",
                             sql_or_ops=[{"action": "add", "fields": {"name": "老陈"}}])
        result = v._check_cascade_rules([frag])
        assert result == []


# ── 12.8 补:xlsx 解析 ──

class TestXlsxParser:
    def test_xlsx_parse(self, tmp_path):
        try:
            from openpyxl import Workbook
        except ImportError:
            pytest.skip("openpyxl not available")
        xlsx = tmp_path / "test.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["名字", "等级"])
        ws.append(["铁匠", 10])
        ws.append(["农夫", 5])
        wb.save(xlsx)
        wb.close()
        doc = parse_file(str(xlsx))
        assert doc.ok is True
        assert doc.file_type == "xlsx"
        assert len(doc.records) == 2
        assert doc.records[0]["名字"] == "铁匠"


# ── 12.8 补:md dialog_fragments 独立提取 ──

class TestMarkdownDialogFragments:
    def test_dialog_fragments_extracted(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("""### 1.1 老茶农发愁
旁白：清晨。
玩家：老伯，怎么不动手？
老陈：灵脉衰退……
""", encoding="utf-8")
        doc = parse_file(str(md))
        assert doc.ok is True
        assert len(doc.steps) == 1
        step = doc.steps[0]
        # 对话块独立
        assert len(step.dialog_fragments) == 2  # 玩家 + 老陈
        # content 只含旁白
        assert "旁白" in step.content
        assert "玩家" not in step.content  # 对话行不进 content
        # NPC 符号分配
        assert "老陈" in doc.symbol_map.values()
        # dialog_fragments 含 speaker/text/symbol
        laochen_frag = next(f for f in step.dialog_fragments if f["speaker"] == "老陈")
        assert laochen_frag["text"] == "灵脉衰退……"
        assert laochen_frag["symbol"] == "<老陈>"


# ── 12.8 补:agent-retry-loop scenario(Step3 重试) ──

class TestStep3Retry:
    def test_step3_retry_on_failure(self, tmp_path):
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.checkpoint import CheckpointManager

        class FailOnceAgent(SubAgent):
            call_count = 0
            def _run_impl(self, prompt, skill_docs, context):
                type(self).call_count += 1
                if type(self).call_count == 1:
                    raise RuntimeError("首次失败")
                return {"sql_or_ops": [{"action": "add", "fields": {"name": "测试"}}],
                        "produces": f"<{self.name}>"}

        pipe = Pipeline(cli=None, parser=None)
        # 模拟 ctx + ckpt
        from agent.excel.pipeline.types import PipelineContext, DocIntent
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                               session_id="test")
        ctx.doc_intent = DocIntent(file_type="md", ok=True)
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")

        subagents = [FailOnceAgent("TestAgent")]
        prompts = ["test"]
        from agent.excel.pipeline.types import PipelineResult
        fragments = pipe._dispatch_with_retry(subagents, prompts, ctx, ckpt,
                                               ctx.add_step("3_fill", "Step3"),
                                               PipelineResult())
        # 首次失败,重试成功
        assert len(fragments) == 1
        assert fragments[0].ok is True
        assert ctx.retry_count == 1

    def test_step3_retry_non_recursive(self, tmp_path):
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.checkpoint import CheckpointManager

        class AlwaysFailAgent(SubAgent):
            def _run_impl(self, prompt, skill_docs, context):
                raise RuntimeError("持续失败")

        pipe = Pipeline(cli=None, parser=None)
        from agent.excel.pipeline.types import PipelineContext, DocIntent, PipelineResult
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                               session_id="test")
        ctx.doc_intent = DocIntent(file_type="md", ok=True)
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")

        subagents = [AlwaysFailAgent("Fail")]
        prompts = ["test"]
        result = PipelineResult()
        sr = ctx.add_step("3_fill", "Step3")
        fragments = pipe._dispatch_with_retry(subagents, prompts, ctx, ckpt, sr, result)
        # 重试 1 次仍失败,不递归
        assert len(fragments) == 1
        assert fragments[0].ok is False
        assert ctx.retry_count == 1  # 仅 1 次


# ── 12.8 补:write-verification scenario(符号回查) ──

class TestSymbolResolveCheck:
    def test_unresolved_symbol_blocks(self, tmp_path):
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.pipeline.types import (PipelineContext, PipelineResult,
                                                 AgentFragment, StepResult)
        from agent.excel.checkpoint import CheckpointManager

        pipe = Pipeline(cli=None)  # 无 cli → Step6 直接 fail
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                               session_id="test")
        ctx.fragments = [AgentFragment(target_table="npc", references=["<unresolved>"],
                                        sql_or_ops=[])]
        ctx.produced = {}  # 未填充
        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")
        result = PipelineResult()

        ok = pipe._step6_write(ctx, ckpt, result)
        # 无 cli → failed
        assert ok is False
        assert result.ok is False


# ── 12.8 补:write-verification scenario(ok 汇总) ──

class TestPipelineOkAggregation:
    def test_all_success_ok_true(self):
        from agent.excel.pipeline.types import PipelineResult, StepResult
        r = PipelineResult()
        r.steps = [StepResult(step_id=str(i), name=f"Step{i}", status="done")
                   for i in range(8)]
        r.ok = True
        assert r.ok is True
        assert "成功" in r.aggregated_message

    def test_error_propagates_ok_false(self):
        from agent.excel.pipeline.types import PipelineResult
        r = PipelineResult()
        r.add_error("4_assemble", "dangling_symbol")
        assert r.ok is False
        assert len(r.errors) == 1
        assert r.errors[0]["step"] == "4_assemble"
        # aggregated_message 在 ok=False 时反映首错误
        assert "4_assemble" in r.aggregated_message


# ── 断点续跑真跳步(capability: pipeline-reliability) ──

class TestCheckpointResume:
    def test_skip_done_step_with_state_restore(self, tmp_path):
        """Step1 已 done 且产物持久化 → resume 时跳过并恢复 ctx.doc_intent。"""
        from dataclasses import asdict
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.pipeline.types import (PipelineContext, PipelineResult,
                                                 DocIntent, StepCard)
        from agent.excel.checkpoint import CheckpointManager

        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")
        # 持久化 Step1 产物
        di = DocIntent(source_path="test.md", file_type="md", ok=True,
                       steps=[StepCard(step_id="1.1", title="测试",
                                       content="旁白")],
                       symbol_map={"<老陈>": "老陈"})
        ckpt.update("1_decompose", "done", output="1 steps",
                    extra={"doc_intent": asdict(di)})
        ckpt.update("0_checkpoint", "done")

        pipe = Pipeline(cli=None, parser=None)
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                              session_id="test")
        ctx.resume_point = "2_partition"  # 续跑点在 Step2 → Step1 应跳过
        result = PipelineResult()

        ok = pipe._step1_decompose(ctx, ckpt, result)
        assert ok is True  # 跳过返回 True
        # ctx.doc_intent 已从 checkpoint 恢复
        assert ctx.doc_intent is not None
        assert ctx.doc_intent.file_type == "md"
        assert len(ctx.doc_intent.steps) == 1
        assert ctx.doc_intent.steps[0].title == "测试"
        assert ctx.resume_point == "2_partition"  # 续跑点未变(非续跑点步)

    def test_resume_point_step_not_skipped(self, tmp_path):
        """step_id == resume_point → 不跳过,清空 resume_point。"""
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.pipeline.types import PipelineContext, PipelineResult
        from agent.excel.checkpoint import CheckpointManager

        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")
        pipe = Pipeline(cli=None, parser=None)
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                              session_id="test")
        ctx.resume_point = "5_verify"
        result = PipelineResult()

        # _maybe_skip 对续跑点本身返回 False
        assert pipe._maybe_skip(ctx, ckpt, result, "5_verify") is False
        assert ctx.resume_point == ""  # 已清空

    def test_missing_artifact_no_skip(self, tmp_path):
        """Step1 持久化产物缺失(doc_intent 无)→ 不跳过,清空 resume_point。"""
        from agent.excel.pipeline.pipeline import Pipeline
        from agent.excel.pipeline.types import PipelineContext, PipelineResult
        from agent.excel.checkpoint import CheckpointManager

        ckpt = CheckpointManager(tmp_path)
        ckpt.init_checkpoint("test", "test", "Trunk", "test.md")
        # Step1 标记 done 但未持久化 doc_intent
        ckpt.update("1_decompose", "done", output="1 steps")
        ckpt.update("0_checkpoint", "done")

        pipe = Pipeline(cli=None, parser=None)
        ctx = PipelineContext(input_path="test.md", output_dir=str(tmp_path),
                              session_id="test")
        ctx.resume_point = "2_partition"
        result = PipelineResult()

        # 恢复失败 → 不跳过
        assert pipe._maybe_skip(ctx, ckpt, result, "1_decompose") is False
        assert ctx.resume_point == ""  # 清空,后续正常执行

