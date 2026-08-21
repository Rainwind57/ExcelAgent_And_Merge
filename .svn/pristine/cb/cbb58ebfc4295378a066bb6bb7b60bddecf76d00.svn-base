"""T6 运行证据层：把 agent 每次定位/操作的证据追加写盘，供 skill 自动更新消费。

证据按 table_stem 分文件 → evidence/{table_stem}.jsonl，每行一条 JSON。
单文件超 10MB 轮转，保留最近 5 份。

写入是旁路：失败只 warn 不抛，绝不阻断 agent 主流程。

并发安全：多 session 并发写同文件 → 跨平台文件锁
  - Linux: fcntl.flock
  - Windows: msvcrt.locking（锁 offset 0 的 1 字节作 mutex；空文件首次降级无锁）
  - 无锁模块：降级裸 append（单条 < 4KB，OS 基本保证原子）
"""

from __future__ import annotations

import json
import warnings
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False


_EVIDENCE_DIR = Path(__file__).parent / "evidence"
_MAX_BYTES = 10 * 1024 * 1024  # 10MB 触发轮转
_KEEP_ROTATED = 5              # 保留最近 5 份轮转


class EvidenceLogger:
    """证据写盘器。单例复用，所有 TableAgent 实例共用一把。"""

    def __init__(self, evidence_dir: Path = _EVIDENCE_DIR,
                 max_bytes: int = _MAX_BYTES,
                 keep_rotated: int = _KEEP_ROTATED):
        self.evidence_dir = Path(evidence_dir)
        self.max_bytes = max_bytes
        self.keep_rotated = keep_rotated

    def log(self, record: dict) -> None:
        """追加写一条证据。失败只 warn，不抛。"""
        try:
            stem = record.get("table_stem") or "_unknown"
            path = self.evidence_dir / f"{stem}.jsonl"
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(path)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self._file_lock(path) as f:
                f.write(line)
        except Exception as e:
            warnings.warn(f"evidence log failed: {e}", RuntimeWarning)

    def _rotate_if_needed(self, path: Path) -> None:
        """超 max_bytes → 重命名带时间戳，保留最近 keep_rotated 份。"""
        if not path.exists() or path.stat().st_size < self.max_bytes:
            return
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        rotated = path.with_name(f"{path.stem}.{ts}.jsonl")
        try:
            path.rename(rotated)
        except OSError:
            return  # 并发轮转竞争，放弃本次轮转
        # 清理旧轮转
        olds = sorted(path.parent.glob(f"{path.stem}.*.jsonl"))
        for old in olds[:-self.keep_rotated]:
            try:
                old.unlink()
            except OSError:
                pass

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[object]:
        """跨平台文件锁。锁失败降级裸 append。"""
        f = open(path, "a", encoding="utf-8")
        locked = False
        try:
            if _HAS_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = True
            elif _HAS_MSVCRT:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                except OSError:
                    pass  # 空文件或竞争，降级
            yield f
        finally:
            if locked:
                try:
                    if _HAS_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    elif _HAS_MSVCRT:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            f.close()


_logger: EvidenceLogger | None = None


def get_evidence_logger() -> EvidenceLogger:
    """模块级单例。首次调用惰性初始化。"""
    global _logger
    if _logger is None:
        _logger = EvidenceLogger()
    return _logger
