# -*- coding: utf-8 -*-
"""单元验证 Locator 通用收紧：对话树叙述不再被路由进 6 张附带表。"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    import os as _os
    if _k.strip() and _k.strip() not in _os.environ:
        _os.environ[_k.strip()] = _v.strip()
_os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.excel.subagent.locator_agent import LocatorAgent

TEXT = (
    "新建的长老叫守器长老·青 model_id 1025，放在 space_id 10030 坐标 (280,0,180)，"
    "点击后展开对话树：长老第一句话年轻人，神器非儿戏，你可有守护苍生之志？，"
    "给选项我愿一试跳第二句话很好，先去击败九幽守护者证明你的实力，"
    "这个第二句话再给一个选项我出发了选完什么都不做结束对话；"
    "还有一个选项容我再想想直接结束对话。"
)

ag = LocatorAgent()
res = ag.locate(TEXT)
print("complex_input:", ag._is_complex_input(TEXT))
print(f"候选数: {len(res.candidates)}")
print("candidates (stem/conf/level/matched_term):")
for c in res.candidates:
    print(f"  {c.stem:22s} conf={c.confidence:.2f} level={c.level:14s} term={c.matched_term}")
print("FK edges:", len(res.fk_edges))
