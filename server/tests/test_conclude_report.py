"""建议7 Step4 透明化单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.conclude_report import (
    is_clean_success, bucket_failures, render_bucketed_failures,
)


def _base(**kw):
    d = dict(prior_ok=True, n_ok=3, n_fail=0, n_skipped=0, n_partial=0,
             has_incomplete=False, n_failures=0)
    d.update(kw)
    return d


def test_clean_success_true_when_all_good():
    assert is_clean_success(**_base()) is True


def test_partial_blocks_clean_success():
    assert is_clean_success(**_base(n_partial=1)) is False


def test_skipped_blocks_clean_success():
    assert is_clean_success(**_base(n_skipped=1)) is False


def test_failures_record_blocks_clean_success():
    # 即使 n_fail=0，只要有 failure 记录（含 soft/warning）也不算干净成功
    assert is_clean_success(**_base(n_failures=2)) is False


def test_incomplete_blocks_clean_success():
    assert is_clean_success(**_base(has_incomplete=True)) is False


def test_no_work_is_not_clean_success():
    assert is_clean_success(**_base(n_ok=0)) is False


def test_prior_not_ok_blocks():
    assert is_clean_success(**_base(prior_ok=False)) is False


def test_bucket_by_step_id_and_attempted_strategies():
    fs = [
        {"step_id": "step1_parse", "root_cause": "缺表"},
        {"attempted_strategies": "step2_validate", "root_cause": "类型错"},
        {"type": "partial_write", "root_cause": "部分写入"},  # 无 step → step3
    ]
    b = bucket_failures(fs)
    assert len(b["step1_parse"]) == 1
    assert len(b["step2_validate"]) == 1
    assert len(b["step3_execute"]) == 1


def test_render_not_truncated_and_grouped():
    fs = [{"step_id": "step3_execute", "table": f"t{i}", "sheet": "S",
           "root_cause": f"err{i}"} for i in range(8)]
    fs.append({"step_id": "step1_parse", "table": "x", "root_cause": "缺表"})
    txt = render_bucketed_failures(fs)
    # 8 条都在（不截前5）
    for i in range(8):
        assert f"err{i}" in txt
    # 分桶：Step1 在 Step3 前
    assert txt.index("Step1 解析") < txt.index("Step3 执行")


def test_render_empty():
    assert render_bucketed_failures([]) == ""
