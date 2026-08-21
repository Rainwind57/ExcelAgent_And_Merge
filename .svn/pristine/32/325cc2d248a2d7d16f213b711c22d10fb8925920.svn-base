"""对话记录保留层：完整对话 + 质量评分 + 自动留优。

数据流:
  agent._log_evidence → DialogLogger.log(record)
    ├─ 全量写 dialogs/{session_id}.jsonl
    ├─ 算 quality score（0-100）
    ├─ score >= EXCELLENT_THRESH → 复制到 dialog_examples/{table_stem}.jsonl
    ├─ score <  FAILURE_THRESH   → 复制到 dialog_failures/{table_stem}.jsonl
    └─ 衰减扫描：30天无引用降权，60天移除

质量评分维度（自动，无需人工标注）:
  成功 ok=True                 +40
  一次命中无纠正 !user_corrected +20 / 纠正 -20
  列定位置信度 col.score>=0.8   +15 / <0.5 -10
  无行歧义 !row.ambiguous      +10 / 歧义 -15
  无需确认 !needs_confirm      +10
  步骤精简 len(steps)<=3       +5
  写操作且校验通过             +10

分级:
  EXCELLENT (>=70) → 入案例库，供后续检索增强 / few-shot 参考
  NORMAL   (40-69) → 仅全量留存
  FAILURE  (<40)   → 入失败案例库，供反模式学习 / 回归测试种子

自动留优（top-N）:
  案例库每 table_stem 保留前 EXAMPLE_KEEP_PER_TABLE（默认 50），
  按 score 主序 + last_seen 次序排序，超出裁剪。
  衰减：weight = score_norm * recency，recency 同 skill_updater（<30d=1.0/30-60d=0.5/>=60d=0）

并发安全：复用 evidence_logger 的跨平台文件锁策略。
失败只 warn 不抛，绝不阻断 agent 主流程。
"""
from __future__ import annotations

import json
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

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


_DIALOG_DIR = Path(__file__).parent / "dialogs"
_EXAMPLES_DIR = Path(__file__).parent / "dialog_examples"
_FAILURES_DIR = Path(__file__).parent / "dialog_failures"

# 评分阈值
EXCELLENT_THRESH = 70
FAILURE_THRESH = 40

# 每个 table_stem 保留的案例数上限
EXAMPLE_KEEP_PER_TABLE = 50
FAILURE_KEEP_PER_TABLE = 30

# 轮转
_MAX_BYTES = 10 * 1024 * 1024
_KEEP_ROTATED = 5

# 衰减
DECAY_DORMANT_DAYS = 30
DECAY_REMOVE_DAYS = 60


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def score_dialog(record: dict) -> int:
    """根据证据字段算质量分（0-100）。缺字段按中性处理。

    评分维度见模块 docstring。返回整数分。
    """
    score = 0
    # 成功
    if record.get("ok"):
        score += 40
    # 纠正
    if record.get("user_corrected"):
        score -= 20
    else:
        score += 20
    # 列定位置信度
    col = record.get("col") or {}
    cs = col.get("score")
    if isinstance(cs, (int, float)):
        if cs >= 0.8:
            score += 15
        elif cs < 0.5:
            score -= 10
    # 行歧义
    row = record.get("row") or {}
    if row.get("ambiguous"):
        score -= 15
    else:
        score += 10
    # 确认
    if not record.get("needs_confirm"):
        score += 10
    # 步骤精简
    steps = record.get("steps") or []
    if len(steps) <= 3:
        score += 5
    # 写操作校验通过（set/add/delete 成功且 ok）
    if record.get("ok") and record.get("intent_action") in ("set", "add", "delete"):
        score += 10
    return max(0, min(100, score))


def quality_grade(score: int) -> str:
    """分数 → 分级标签。"""
    if score >= EXCELLENT_THRESH:
        return "excellent"
    if score < FAILURE_THRESH:
        return "failure"
    return "normal"


class DialogLogger:
    """对话记录写盘器。单例复用（get_dialog_logger）。

    三路写入:
      1. dialogs/{session_id}.jsonl       全量
      2. dialog_examples/{table_stem}.jsonl  优秀案例（top-N）
      3. dialog_failures/{table_stem}.jsonl  失败案例（top-N）
    """

    def __init__(self, dialog_dir: Path = _DIALOG_DIR,
                 examples_dir: Path = _EXAMPLES_DIR,
                 failures_dir: Path = _FAILURES_DIR,
                 max_bytes: int = _MAX_BYTES,
                 keep_rotated: int = _KEEP_ROTATED):
        self.dialog_dir = Path(dialog_dir)
        self.examples_dir = Path(examples_dir)
        self.failures_dir = Path(failures_dir)
        self.max_bytes = max_bytes
        self.keep_rotated = keep_rotated

    def log(self, record: dict) -> None:
        """写一条对话记录。失败只 warn 不抛。

        record 由 agent._log_evidence 组装，至少含:
          ts, session_id, table_stem, sheet, intent_action,
          user_text, agent_message, steps, col, row,
          ok, needs_confirm, user_corrected
        本方法补 quality 分与 grade 后分发写盘。
        """
        try:
            score = score_dialog(record)
            grade = quality_grade(score)
            record = {**record, "quality_score": score, "quality_grade": grade}
            # 1. 全量
            self._append(self.dialog_dir / f"{record.get('session_id') or '_default'}.jsonl",
                         record)
            # 2. 优秀案例
            stem = record.get("table_stem") or "_unknown"
            if grade == "excellent":
                self._append_topn(self.examples_dir / f"{stem}.jsonl", record,
                                  EXAMPLE_KEEP_PER_TABLE)
            # 3. 失败案例
            elif grade == "failure":
                self._append_topn(self.failures_dir / f"{stem}.jsonl", record,
                                  FAILURE_KEEP_PER_TABLE)
        except Exception as e:
            warnings.warn(f"dialog log failed: {e}", RuntimeWarning)

    def _append(self, path: Path, record: dict) -> None:
        """追加写一条 jsonl，带轮转与文件锁。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed(path)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._file_lock(path) as f:
            f.write(line)

    def _append_topn(self, path: Path, record: dict, keep_n: int) -> None:
        """写 top-N 案例库：读现有 → 追加 → 按 score 降序+last_seen 降序裁剪 → 原子写回。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        items = self._read_jsonl(path)
        items.append(record)
        items.sort(key=lambda r: (r.get("quality_score", 0),
                                  _parse_iso(r.get("ts", "")) or datetime.min.astimezone()),
                   reverse=True)
        items = items[:keep_n]
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items)
        with self._file_lock(path) as f:
            f.seek(0)
            f.truncate()
            f.write(text)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def _rotate_if_needed(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size < self.max_bytes:
            return
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        rotated = path.with_name(f"{path.stem}.{ts}.jsonl")
        try:
            path.rename(rotated)
        except OSError:
            return
        olds = sorted(path.parent.glob(f"{path.stem}.*.jsonl"))
        for old in olds[:-self.keep_rotated]:
            try:
                old.unlink()
            except OSError:
                pass

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[object]:
        """跨平台文件锁。锁失败降级裸写。"""
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
                    pass
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

    # ── 衰减清理 ──
    def decay_scan(self, now: datetime | None = None) -> dict:
        """扫案例库/失败库，按 last_seen 衰减裁剪。

        - 60 天无新写入的条目移除
        - 返回 {"examples_removed": N, "failures_removed": M}
        """
        if now is None:
            now = datetime.now().astimezone()
        stats = {"examples_removed": 0, "failures_removed": 0}
        for d, key in ((self.examples_dir, "examples_removed"),
                       (self.failures_dir, "failures_removed")):
            if not d.exists():
                continue
            for p in d.glob("*.jsonl"):
                stats[key] += self._decay_file(p, now)
        return stats

    def _decay_file(self, path: Path, now: datetime) -> int:
        """裁剪单个案例文件，返回移除条数。"""
        items = self._read_jsonl(path)
        keep: list[dict] = []
        removed = 0
        for r in items:
            last = _parse_iso(r.get("ts", ""))
            if last is None:
                removed += 1
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            days = (now - last).days
            if days >= DECAY_REMOVE_DAYS:
                removed += 1
                continue
            keep.append(r)
        if removed == 0:
            return 0
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep)
        with self._file_lock(path) as f:
            f.seek(0)
            f.truncate()
            f.write(text)
        return removed

    # ── 检索（供后续 agent 检索增强用）──
    def query_examples(self, table_stem: str, limit: int = 5,
                       grade: str = "excellent") -> list[dict]:
        """查某表的优秀/失败案例，按 score 降序返回前 limit 条。"""
        d = self.examples_dir if grade == "excellent" else self.failures_dir
        path = d / f"{table_stem}.jsonl"
        items = self._read_jsonl(path)
        items.sort(key=lambda r: r.get("quality_score", 0), reverse=True)
        return items[:limit]


_logger: DialogLogger | None = None


def get_dialog_logger() -> DialogLogger:
    """模块级单例，惰性初始化。"""
    global _logger
    if _logger is None:
        _logger = DialogLogger()
    return _logger
