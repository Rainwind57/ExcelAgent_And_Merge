"""ParseAgent 单测（§2.11）。

mock LocatorAgent.loc.decompose + infer_produces_consumes，
验证 ParseAgent 整合逻辑（不依赖 codemaker serve）：
  - parse 正常路径：SplitIntent[] → NLIntent[] (source=llm_decompose +
    produces_label 同步 + extras["source"]=llm_chain 兼容旧下游)
  - parse_baseline 兜底：source=splitter_baseline + ai_check_skipped=True +
    extras["source"]=splitter 兼容旧下游
  - parse 空输入/无候选/decompose 产空 → 返回 []
  - produces_inference 接入：调用 + extras["produces"] → NLIntent.produces_label 同步

端到端「封印魔龙」8+ SubTask（§2.11 阶段目标用例）依赖 codemaker serve LLM
实调，R7 serve 端 143.8k token/156s 卡死未解，归 §2.12 A/B 待 serve 落地。

运行: python -m pytest server/tests/test_parse_agent.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.excel.subagent.parse_agent as pa_mod
from agent.excel.core.cross_table_splitter import SplitIntent
from agent.excel.subagent.parse_agent import ParseAgent
from agent.excel.parser.nl_parser import NLIntent
from agent.excel.subagent.locator_agent import CandidateTable, LocatorResult


# ── 桩组件 ────────────────────────────────────────────────────


class _FakeLocator:
    """桩 LocatorAgent：locate 返回固定 LocatorResult。"""

    def __init__(self, result: LocatorResult):
        self._result = result
        self.called_with: list[str] = []

    def locate(self, text: str) -> LocatorResult:
        self.called_with.append(text)
        return self._result


class _FakeDecomposer:
    """桩 DecomposeAgent：decompose 返回固定 SplitIntent[]。"""

    def __init__(self, intents):
        self._intents = list(intents)
        self.called: list[tuple] = []

    def decompose(self, text, locator_result, force_single=False):
        self.called.append((text, locator_result))
        return list(self._intents)

    def _col_type_for(self, stem, sheet, col_name):
        # 桩：默认 str 列（中文值合法保留，不触发灌值守卫清空）
        return "string"


def _no_infer(intents):
    """no-op infer_produces_consumes 桩（隔离 RelationGraph 依赖）。"""
    return intents


# ── 正常路径 ──────────────────────────────────────────────────


class TestParseAgentNormal:
    def test_parse_produces_llm_decompose_source(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        lr = LocatorResult(candidates=[
            CandidateTable(stem="quest"), CandidateTable(stem="reward")])
        sis = [
            SplitIntent(text="加主线任务封印魔龙", table_hint="quest",
                        action="add",
                        fields={"quest_id": 1, "name": "封印魔龙"},
                        produces="new_quest_id"),
            SplitIntent(text="加主线任务封印魔龙", table_hint="reward",
                        action="add",
                        fields={"reward_id": "<new_quest_id>", "name": "魔龙奖励"}),
        ]
        pa = ParseAgent(locator_agent=_FakeLocator(lr),
                        decompose_agent=_FakeDecomposer(sis))
        out = pa.parse("加一个主线任务叫封印魔龙")
        assert len(out) == 2
        # 全部 source=llm_decompose
        assert all(i.source == "llm_decompose" for i in out)
        # produces_label 同步
        assert out[0].produces_label == "new_quest_id"
        assert out[1].produces_label is None
        # fields 经 extras 透传
        assert out[0].extras["fields"]["quest_id"] == 1
        assert out[0].extras["fields"]["name"] == "封印魔龙"
        # consumes 占位符（DecomposeAgent._to_split_intents 已替换为 <label>）
        assert out[1].extras["fields"]["reward_id"] == "<new_quest_id>"
        # extras["source"] 兼容旧下游（"llm_chain"）
        assert out[0].extras["source"] == "llm_chain"
        # raw 取 SplitIntent.text
        assert out[0].raw == "加主线任务封印魔龙"

    def test_parse_locator_called_with_text(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        loc = _FakeLocator(LocatorResult(candidates=[CandidateTable(stem="quest")]))
        pa = ParseAgent(locator_agent=loc,
                        decompose_agent=_FakeDecomposer([]))
        pa.parse("加任务")
        assert loc.called_with == ["加任务"]

    def test_parse_decompose_called_with_text_and_lr(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        dec = _FakeDecomposer([SplitIntent(text="x", table_hint="quest")])
        pa = ParseAgent(locator_agent=_FakeLocator(lr), decompose_agent=dec)
        pa.parse("加任务")
        assert dec.called[0][0] == "加任务"
        assert dec.called[0][1] is lr


# ── 空输入/无候选/decompose 产空 ───────────────────────────────


class TestParseAgentEmptyPaths:
    def test_parse_empty_input(self):
        pa = ParseAgent()
        assert pa.parse("") == []
        assert pa.parse("   ") == []

    def test_parse_no_candidates(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        pa = ParseAgent(locator_agent=_FakeLocator(LocatorResult()))
        assert pa.parse("无候选输入") == []

    def test_parse_decompose_empty_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        pa = ParseAgent(locator_agent=_FakeLocator(lr),
                        decompose_agent=_FakeDecomposer([]))
        assert pa.parse("xxx") == []

    def test_parse_locator_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)

        class _Boom:
            def locate(self, text):
                raise RuntimeError("locator boom")

        pa = ParseAgent(locator_agent=_Boom())
        assert pa.parse("x") == []

    def test_parse_decompose_exception_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)

        class _Boom:
            def decompose(self, text, lr, force_single=False):
                raise RuntimeError("decompose boom")

        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        pa = ParseAgent(locator_agent=_FakeLocator(lr), decompose_agent=_Boom())
        assert pa.parse("x") == []


# ── baseline 兜底 ─────────────────────────────────────────────


class TestParseAgentBaseline:
    def test_parse_baseline_source_and_skip(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        sis = [SplitIntent(text="加灵兽", table_hint="pet", action="add",
                          fields={"pet_id": 1}, produces="new_pet_id")]
        pa = ParseAgent()
        out = pa.parse_baseline("加灵兽", sis)
        assert len(out) == 1
        assert out[0].source == "splitter_baseline"
        assert out[0].ai_check_skipped is True
        assert out[0].produces_label == "new_pet_id"
        # extras["source"] 兼容旧下游（"splitter"）
        assert out[0].extras["source"] == "splitter"
        assert out[0].extras["fields"]["pet_id"] == 1

    def test_parse_baseline_empty(self, monkeypatch):
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        pa = ParseAgent()
        assert pa.parse_baseline("x", []) == []

    def test_parse_baseline_preserves_splitter_produces(self, monkeypatch):
        """splitter 模板 produces 精确标签保留（不覆盖）。"""
        monkeypatch.setattr(pa_mod, "infer_produces_consumes", _no_infer)
        sis = [SplitIntent(text="x", table_hint="interaction",
                          action="add", fields={"编号": 3002},
                          produces="new_interaction_id")]
        pa = ParseAgent()
        out = pa.parse_baseline("x", sis)
        assert out[0].produces_label == "new_interaction_id"
        assert out[0].source == "splitter_baseline"


# ── produces_inference 接入 ────────────────────────────────────


class TestParseAgentProducesInference:
    def test_infer_produces_consumes_called_once(self, monkeypatch):
        calls = [0]

        def spy(intents):
            calls[0] += 1
            return intents

        monkeypatch.setattr(pa_mod, "infer_produces_consumes", spy)
        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        sis = [SplitIntent(text="x", table_hint="quest", action="add",
                          fields={"q": 1})]
        pa = ParseAgent(locator_agent=_FakeLocator(lr),
                        decompose_agent=_FakeDecomposer(sis))
        pa.parse("x")
        assert calls[0] == 1

    def test_produces_label_synced_from_infer_extras(self, monkeypatch):
        """infer_produces_consumes 在 extras["produces"] 补 → ParseAgent 同步到 produces_label。"""

        def fake_infer(intents):
            for it in intents:
                if it.action == "add" and not it.extras.get("produces"):
                    it.extras["produces"] = "new_synced_id"
            return intents

        monkeypatch.setattr(pa_mod, "infer_produces_consumes", fake_infer)
        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        sis = [SplitIntent(text="x", table_hint="quest", action="add",
                          fields={})]  # 无显式 produces
        pa = ParseAgent(locator_agent=_FakeLocator(lr),
                        decompose_agent=_FakeDecomposer(sis))
        out = pa.parse("加任务")
        assert len(out) == 1
        assert out[0].produces_label == "new_synced_id"

    def test_infer_exception_does_not_break_parse(self, monkeypatch):
        """infer_produces_consumes 抛错时 ParseAgent 保留原 intent 不崩。"""

        def boom(intents):
            raise RuntimeError("infer boom")

        monkeypatch.setattr(pa_mod, "infer_produces_consumes", boom)
        lr = LocatorResult(candidates=[CandidateTable(stem="quest")])
        sis = [SplitIntent(text="x", table_hint="quest", action="add",
                          fields={"q": 1}, produces="new_quest_id")]
        pa = ParseAgent(locator_agent=_FakeLocator(lr),
                        decompose_agent=_FakeDecomposer(sis))
        out = pa.parse("x")
        assert len(out) == 1  # 不崩,intent 保留
        assert out[0].produces_label == "new_quest_id"  # 原 SplitIntent.produces 已设

    def test_baseline_also_runs_infer(self, monkeypatch):
        """parse_baseline 也调 infer_produces_consumes（splitter 模板 produces 精确但新链型可能缺）。"""
        calls = [0]

        def spy(intents):
            calls[0] += 1
            return intents

        monkeypatch.setattr(pa_mod, "infer_produces_consumes", spy)
        pa = ParseAgent()
        pa.parse_baseline("x", [SplitIntent(text="x", table_hint="pet",
                                            action="add", fields={"p": 1})])
        assert calls[0] == 1


# ── NLIntent 超集字段 ──────────────────────────────────────────


class TestNLIntentSupersetFields:
    """§2.9 路线 A：NLIntent 扩展字段默认值不破坏现有构造。"""

    def test_default_source_is_nl(self):
        it = NLIntent(action="add", table_hint="quest")
        assert it.source == "nl"
        assert it.produces_label is None
        assert it.consumes_labels == []
        assert it.ai_check_skipped is False
        assert it.validation is None
        assert it.execution is None

    def test_existing_fields_unchanged(self):
        """旧字段全部保留（#22/#25 splitter 保护语义不变）。"""
        it = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                     locator_field="名称", locator_value="朱雀",
                     target_field="成长率", value="1.5",
                     raw="修改朱雀成长率为1.5", row_override=None,
                     extras={"col_name": "成长率"})
        assert it.action == "set"
        assert it.locator_value == "朱雀"
        assert it.value == "1.5"
        assert it.extras["col_name"] == "成长率"
        # 新字段默认
        assert it.source == "nl"
        assert it.produces_label is None


# ── 孤立空壳 add 过滤（strict） ───────────────────────────────


def _mk_add(stem: str, sheet: str, fields: dict, produces: str = "",
            consumes: list | None = None) -> NLIntent:
    extras = {"fields": fields}
    if produces:
        extras["produces"] = produces
    return NLIntent(action="add", table_hint=stem, sheet_hint=sheet,
                    extras=extras, produces_label=produces or None,
                    consumes_labels=list(consumes or []))


class TestPartitionOrphanEmptyAdds:
    """_partition_orphan_empty_adds 的 strict 过滤（serve 空壳污染根因）。

    场景：serve 后端 LLM 面对大候选池给每个候选表幻觉产一条
    add+produces 占位符空壳（fields 全空/数字索引键），第一道非 strict
    过滤因 produces 豁免全放行 → Step3 写 N 个空行污染数据。
    strict=True（引用编译后）过滤只删真孤立幻觉行，被消费 producer 保留。
    """

    def _partition(self, intents, strict=False):
        pa = ParseAgent()
        return pa._partition_orphan_empty_adds(intents, strict=strict)

    def test_strict_drops_orphan_produces_shell(self):
        """挂 produces 但字段全空、无 consumer → strict 删，非 strict 保留。"""
        shell = _mk_add("reward", "Reward", {}, produces="new_reward_id")
        real = _mk_add("mail", "GlobalMail",
                       {"global_id": 21, "title": "月华庆典开启"})
        # 非 strict：produces 豁免 → 保留
        kept, dropped = self._partition([shell, real], strict=False)
        assert len(kept) == 2 and not dropped
        # strict：孤立 produces 空壳 → 删
        kept, dropped = self._partition([shell, real], strict=True)
        assert kept == [real] and dropped == [shell]

    def test_strict_keeps_consumed_producer(self):
        """被本批消费的 producer 占位行保留（FK 链前置，_capture_produced 回填）。"""
        producer = _mk_add("mail", "MailTemplate",
                           {"template_id": "<new_mailtemplate_id>"},
                           produces="new_mailtemplate_id")
        consumer = _mk_add("mail", "GlobalMail",
                           {"template_id": "<new_mailtemplate_id>",
                            "global_id": 21},
                           consumes=["new_mailtemplate_id"])
        kept, dropped = self._partition([producer, consumer], strict=True)
        assert len(kept) == 2 and not dropped

    def test_strict_keeps_real_fields(self):
        """有实值字段的 add 即使挂 produces 也保留（非空壳）。"""
        real = _mk_add("reward", "Reward", {"reward_id": 10001,
                                            "name": "全服邮件奖励"},
                       produces="new_reward_id_10001")
        kept, dropped = self._partition([real], strict=True)
        assert kept == [real] and not dropped

    def test_strict_drops_numeric_index_key_shell(self):
        """数字索引键（LLM 退化列号当键，如 {42:'10001'}）不算实值 → strict 删。

        对应月华样例 serve 报告：reward {42: '10001'}、combat {6: 10001}、
        residence_building {13: 10001} 三条幻觉空壳。
        """
        shell = _mk_add("reward", "Reward", {42: "10001"},
                        produces="new_reward_id")
        real = _mk_add("mail", "GlobalMail", {"global_id": 21})
        kept, dropped = self._partition([shell, real], strict=True)
        assert kept == [real] and dropped == [shell]

    def test_single_intent_never_dropped(self):
        """单条空 add 可能是用户单表新增（空 fields 交 Step2 补），不删。"""
        solo = _mk_add("quest", "Quest", {}, produces="new_quest_id")
        kept, dropped = self._partition([solo], strict=True)
        assert kept == [solo] and not dropped
