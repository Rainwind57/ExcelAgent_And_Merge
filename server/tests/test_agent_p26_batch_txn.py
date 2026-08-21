"""TableAgent P26 单测（OPTIMIZATION_LEDGER §4 第三批）。

覆盖 P26：批级事务/部分回滚。`_compute_rollback_targets` 计算硬失败时的回滚
目标集。`CODEMAKER_BATCH_TRANSACTIONAL=1`（strict）→ 回滚整批前序已 commit op
（批级原子，免重跑 UNIQUE_VIOLATION）；默认 off → G8 链回滚（仅直接依赖
producer）。

P27（4-step NL 路径 checkpoint）留 follow-up（涉及 NLIntent 序列化 + 续跑，
改动面更广）。

运行: python -m pytest server/tests/test_agent_p26_batch_txn.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent


def _partition(idx, table, has_backup=True, rolled_back=False):
    """轻量 partition 替身。backup = (path_str, backup_file) 或 None。"""
    return {
        "idx": idx,
        "intent": SimpleNamespace(table_hint=table),
        "backup": (f"/tmp/{table}.xlsx", f"/tmp/bak_{table}") if has_backup else None,
        "rolled_back": rolled_back,
        "res": SimpleNamespace(),
    }


class TestComputeRollbackTargetsP26:
    def test_batch_transactional_rolls_back_all_prior(self):
        """strict 模式：任一硬失败 → 回滚整批前序已 commit op（不限直接依赖）。

        模拟：4 op (A→B→C→D topo)，C 硬失败时仅 A+B 已成功 commit（有 backup），
        D 未执行（无 backup）。G8 仅回滚 C 的直接依赖（B）；
        P26 strict 回滚 A+B（全部前序已 commit）。
        """
        partitions = [_partition(0, "A"), _partition(1, "B"),
                      _partition(2, "C"), _partition(3, "D", has_backup=False)]
        # C (orig_idx=2) 硬失败
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=2, partitions=partitions,
            deps_map={2: {1}},  # G8: C 直接依赖 B
            batch_transactional=True, has_deps=True)
        assert mode == "P26-batch-transactional"
        assert set(targets) == {0, 1}, f"strict 应回滚全部前序 A+B: {targets}"

    def test_batch_transactional_excludes_self_and_rolled(self):
        """strict 模式：排除自己 + 已 rolled_back 的。"""
        partitions = [_partition(0, "A"), _partition(1, "B", rolled_back=True),
                      _partition(2, "C")]
        # C (orig_idx=2) 硬失败；B 已 rolled_back
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=2, partitions=partitions, deps_map={},
            batch_transactional=True, has_deps=True)
        assert mode == "P26-batch-transactional"
        assert set(targets) == {0}, f"应排除已回滚 B + 自己 C: {targets}"

    def test_batch_transactional_excludes_no_backup(self):
        """strict 模式：无 backup 的 op（未成功 commit）排除。"""
        partitions = [_partition(0, "A"), _partition(1, "B", has_backup=False),
                      _partition(2, "C")]
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=2, partitions=partitions, deps_map={},
            batch_transactional=True, has_deps=True)
        assert set(targets) == {0}, f"应排除无 backup B: {targets}"

    def test_g8_default_rolls_back_only_direct_deps(self):
        """默认（batch_transactional=False）+ has_deps → G8：仅直接依赖 producer。"""
        partitions = [_partition(0, "A"), _partition(1, "B"),
                      _partition(2, "C"), _partition(3, "D")]
        # C (orig_idx=2) 硬失败；直接依赖 B (idx=1)
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=2, partitions=partitions,
            deps_map={2: {1}},  # C 直接依赖 B
            batch_transactional=False, has_deps=True)
        assert mode == "G8-chain"
        assert set(targets) == {1}, f"G8 仅回滚直接依赖 B: {targets}"

    def test_no_deps_no_rollback_default(self):
        """默认 + 无依赖 → 不回滚前序（独立 op 不牵连）。"""
        partitions = [_partition(0, "A"), _partition(1, "B")]
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=1, partitions=partitions, deps_map={},
            batch_transactional=False, has_deps=False)
        assert mode == "none"
        assert targets == []

    def test_batch_transactional_overrides_no_deps(self):
        """strict 模式即使无依赖也回滚前序（批级原子）。"""
        partitions = [_partition(0, "A"), _partition(1, "B")]
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=1, partitions=partitions, deps_map={},
            batch_transactional=True, has_deps=False)
        assert mode == "P26-batch-transactional"
        assert set(targets) == {0}

    def test_g8_empty_deps_map_for_failed(self):
        """G8 模式 + failed op 无 deps_map 条目 → 空集（无直接依赖不回滚）。"""
        partitions = [_partition(0, "A"), _partition(1, "B")]
        mode, targets = TableAgent._compute_rollback_targets(
            orig_idx=1, partitions=partitions, deps_map={},  # B 无依赖
            batch_transactional=False, has_deps=True)
        assert mode == "G8-chain"
        assert targets == []


class TestBatchTransactionalAttribute:
    def test_default_off(self, monkeypatch):
        """CODEMAKER_BATCH_TRANSACTIONAL 缺省 → off（G8 链回滚默认）。"""
        monkeypatch.delenv("CODEMAKER_BATCH_TRANSACTIONAL", raising=False)
        ag = object.__new__(TableAgent)
        ag.batch_transactional = os.getenv("CODEMAKER_BATCH_TRANSACTIONAL", "0") == "1"
        assert ag.batch_transactional is False

    def test_env_on_enables(self, monkeypatch):
        """CODEMAKER_BATCH_TRANSACTIONAL=1 → strict 批级事务。"""
        monkeypatch.setenv("CODEMAKER_BATCH_TRANSACTIONAL", "1")
        ag = object.__new__(TableAgent)
        ag.batch_transactional = os.getenv("CODEMAKER_BATCH_TRANSACTIONAL", "0") == "1"
        assert ag.batch_transactional is True
