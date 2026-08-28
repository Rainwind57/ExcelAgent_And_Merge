"""断点管理器:管道断点续跑。

每步完成写 {output_dir}/_checkpoint.json,Step0 读取判断续跑/重跑/从头。
复用 agent_service.py:120 _session_checkpoints 模式做多任务隔离。

结构:
{
  "flow": "quest",          # 管道类型
  "scene": "谷雨灵茶",       # 场景名
  "workspace": "Trunk",     # 工作区
  "script_path": "...",    # 输入文件
  "output_dir": "...",     # 输出目录
  "steps": {               # 各步状态
    "1_decompose": {"status":"done","output":"..."},
    "2_partition": {"status":"done","partitions":{...}},
    "3_fill": {"status":"failed","error":"..."},
    ...
  },
  "last_error": null | {"step":"3_fill","error":"..."}
}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CHECKPOINT_FILE = "_checkpoint.json"


class CheckpointManager:
    """断点管理器:原子写 _checkpoint.json,支持断点续跑。

    Attributes:
        output_dir: 输出目录(checkpoint 文件所在)
        path: checkpoint 文件完整路径
    """

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.path = self.output_dir / _CHECKPOINT_FILE

    def init_checkpoint(self, flow: str, scene: str, workspace: str,
                         script_path: str) -> dict:
        """首次创建完整 checkpoint 结构。

        Returns:
            完整 checkpoint dict
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "flow": flow,
            "scene": scene,
            "workspace": workspace,
            "script_path": script_path,
            "output_dir": str(self.output_dir),
            "steps": {},
            "last_error": None,
        }
        self._atomic_write(data)
        return data

    def load(self) -> Optional[dict]:
        """读取 checkpoint,不存在返回 None。"""
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(f"checkpoint 读取失败: {self.path}", exc_info=True)
            return None

    def update(self, step_id: str, status: str, output: str = "",
               error: str = "", extra: dict = None) -> None:
        """更新单步状态,原子写(临时文件+rename)。

        Args:
            step_id: 步骤标识(如 "1_decompose")
            status: done/failed/pending
            output: 产物路径
            error: 失败详情
            extra: 额外字段(如 partitions/produces)
        """
        data = self.load() or {}
        steps = data.setdefault("steps", {})
        step_entry = {"status": status, "output": output}
        if error:
            step_entry["error"] = error
        if extra:
            step_entry.update(extra)
        steps[step_id] = step_entry

        if status == "failed":
            data["last_error"] = {"step": step_id, "error": error}
        elif status == "done":
            le = data.get("last_error") or {}
            if isinstance(le, dict) and le.get("step") == step_id:
                data["last_error"] = None

        self._atomic_write(data)

    def _atomic_write(self, data: dict) -> None:
        """原子写:临时文件 + rename,避免半成品。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.output_dir), suffix=".tmp", prefix="_ckpt_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(self.path))
        except Exception:
            logger.warning(f"checkpoint 原子写失败: {self.path}", exc_info=True)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def get_resume_point(self) -> Optional[str]:
        """判断续跑点:返回首个 failed/pending 步骤 id,全 done 返回 None。"""
        data = self.load()
        if not data:
            return None
        steps = data.get("steps", {})
        # 7 步顺序
        order = ["0_checkpoint", "1_decompose", "2_partition", "3_fill",
                 "4_assemble", "5_verify", "6_write", "7_cleanup"]
        for sid in order:
            entry = steps.get(sid)
            if not entry or entry.get("status") != "done":
                return sid
        return None

    def is_complete(self) -> bool:
        """所有步骤 done。"""
        return self.get_resume_point() is None
