"""主线1 resolution 台账单测（纯函数 + 数据结构，0 LLM，确定性）。"""
from agent.excel.core.pipeline.resolution_ledger import (
    make_issue_id,
    Resolution,
    ResolutionLedger,
)


def test_issue_id_is_content_derived_and_stable():
    a = make_issue_id(kind="pk_conflict", table="reward", sheet="Reward",
                      col="reward_id", value="100603")
    b = make_issue_id(kind="pk_conflict", table="reward", sheet="Reward",
                      col="reward_id", value="100603")
    assert a == b            # 同内容→同 id（跨调用稳定）
    assert a.startswith("pkconflict:")


def test_issue_id_normalizes_col_suffix_and_separators():
    a = make_issue_id(kind="type", table="fabao", sheet="FabaoLevel",
                      col="法宝id:int", value=1)
    b = make_issue_id(kind="type", table="fabao", sheet="FabaoLevel",
                      col=" 法宝 id ", value=1)
    assert a == b


def test_issue_id_differs_on_different_content():
    a = make_issue_id(kind="pk_conflict", table="reward", sheet="Reward",
                      col="reward_id", value="1")
    b = make_issue_id(kind="pk_conflict", table="reward", sheet="Reward",
                      col="reward_id", value="2")
    assert a != b


def test_ledger_record_is_idempotent():
    led = ResolutionLedger()
    # 同一冲突（reward_id=100603 已占用）：value=主体(100603)，resolved=用户改用的新值
    iid = led.record_kv(kind="pk_conflict", table="reward", sheet="Reward",
                        col="reward_id", value="100603", resolved="100604",
                        source="user")
    assert len(led) == 1
    # 同一 issue（同主体 100603）再记（不覆盖）→ 不新增、保留首个决策
    again = led.record_kv(kind="pk_conflict", table="reward", sheet="Reward",
                          col="reward_id", value="100603", resolved="999",
                          source="user")
    assert again == iid
    assert len(led) == 1
    assert led.get(iid).resolved == "100604"
    assert led.is_resolved(iid)


def test_ledger_overwrite_updates():
    led = ResolutionLedger()
    iid = led.record_kv(kind="pk_conflict", table="r", col="c", value="1")
    led.record_kv(kind="pk_conflict", table="r", col="c", value="1",
                  status="skipped", overwrite=True)
    assert led.get(iid).status == "skipped"


def test_ledger_roundtrip_persist():
    led = ResolutionLedger()
    led.record_kv(kind="pk_conflict", table="r", col="c", value="1", source="user")
    led.record_kv(kind="type", table="r", col="d", value="x", status="skipped")
    d = led.to_dict()
    led2 = ResolutionLedger.from_dict(d)
    assert len(led2) == 2
    assert led2.to_dict() == d


def test_ledger_merge():
    a = ResolutionLedger()
    a.record_kv(kind="pk_conflict", table="r", col="c", value="1")
    b = ResolutionLedger()
    b.record_kv(kind="type", table="r", col="d", value="x")
    a.merge(b)
    assert len(a) == 2


def test_is_resolved_false_for_pending():
    led = ResolutionLedger()
    iid = led.record_kv(kind="x", table="t", col="c", value="v", status="pending")
    assert led.has(iid)
    assert not led.is_resolved(iid)
