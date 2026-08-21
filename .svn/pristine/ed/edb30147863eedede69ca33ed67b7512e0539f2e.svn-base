"""通用文件解析器:多模态输入 → 结构化 DocIntent。

按扩展名分发:
- .md → markdown 结构解析(章节/步骤编号/对话块/选项分支)→ StepCard 列表
- .xlsx → read_sheet → records 列表
- .csv → 行记录 → records 列表
- .txt → 纯文本意图(降级走 codemaker_parser)
不支持扩展名 → DocIntent.ok=False error="unsupported_file_type"

符号映射表分配:为 NPC/Dialog/Item/Task 分配唯一 placeholder <symbol>,
复用 CrossTableIntentSplitter produces 模式。
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _types():
    """延迟导入 pipeline.types 避免循环 import(parser ← pipeline ← parser)。"""
    from ..pipeline.types import DocIntent, StepCard
    return DocIntent, StepCard

# 实体类型 → placeholder 前缀(对齐 cross_table_splitter produces 模式)
_ENTITY_PREFIXES = {
    "npc": "npc", "NPC": "npc", "老陈": "npc", "芸娘": "npc",
    "dialog": "dlg", "对话": "dlg",
    "item": "item", "道具": "item",
    "task": "task", "任务": "task",
    "qiyu": "qiyu", "奇遇": "qiyu",
    "showhide": "showhide", "显隐": "showhide",
}

# 步骤编号正则:### 1.1 / #### 1.2.3 / ## 1
_STEP_RE = re.compile(r'^#{2,5}\s*((?:\d+\.)*\d+)[\s.、]*(.*)$')

# 对话行正则:玩家:/老陈:/芸娘: 等(显式 NPC 名列表,避免误匹配"旁白：")
# group(1)=speaker,group(2)=冒号后对话文本(已去除冒号)
_DIALOG_RE = re.compile(r'^(玩家|老陈|芸娘|沈鹤亭|沈夫人|茶童|沈飞|沈炼|老伯|老农|NPC)\s*[:：]\s*(.*)$')

# 选项分支正则:【选项1:xxx】/→ 跳转【1.4】
_OPTION_RE = re.compile(r'【选项\d+[：:](.+?)】')
_JUMP_RE = re.compile(r'→?\s*跳转【([\d.]+)】')


def parse_file(path: str, cli=None):
    """入口:按扩展名分发解析,返回 DocIntent。

    Args:
        path: 输入文件路径
        cli: CodeMakerCLI 实例(.xlsx 解析用 read_sheet;可选)
    """
    DocIntent, StepCard = _types()
    p = Path(path)
    if not p.exists():
        return DocIntent(source_path=path, ok=False, error=f"file not found: {path}")

    ext = p.suffix.lower()
    if ext == ".md":
        return _parse_markdown(p)
    if ext == ".xlsx":
        return _parse_xlsx(p, cli)
    if ext == ".csv":
        return _parse_csv(p)
    if ext == ".txt":
        return _parse_txt(p)
    return DocIntent(source_path=path, ok=False,
                     error=f"unsupported_file_type: {ext}")


def _parse_markdown(path: Path):
    """markdown 结构解析:章节/步骤编号/对话块/选项分支 → StepCard。

    识别 ### 1.1 步骤标题,提取对话行(玩家:/NPC:)独立为 dialog_fragments,
    非对话行(旁白/交互描述)进 content。选项分支,涉及元素。
    分配符号映射表(NPC/Dialog 等)。
    """
    DocIntent, StepCard = _types()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    doc = DocIntent(source_path=str(path), file_type="md", raw_text=text)

    current_step: Optional[StepCard] = None
    content_buffer: list[str] = []  # 非对话行(旁白/描述)
    npc_seen: set[str] = set()

    def _flush_step():
        nonlocal current_step
        if current_step is not None:
            current_step.content = "\n".join(content_buffer).strip()
            doc.steps.append(current_step)
            content_buffer.clear()
        current_step = None

    for line in lines:
        line = line.rstrip()
        # 步骤标题
        m = _STEP_RE.match(line)
        if m:
            _flush_step()
            current_step = StepCard(step_id=m.group(1), title=m.group(2).strip())
            continue
        # 选项分支
        om = _OPTION_RE.search(line)
        if om and current_step is not None:
            current_step.branches.append({"option": om.group(1), "raw": line})
            continue
        # 跳转
        jm = _JUMP_RE.search(line)
        if jm and current_step is not None:
            current_step.branches.append({"jump_to": jm.group(1), "raw": line})
            continue
        # 对话行 → 独立 dialog_fragments
        dm = _DIALOG_RE.match(line)
        if dm and current_step is not None:
            speaker = dm.group(1).strip().rstrip(":：")
            dialog_text = dm.group(2).strip()
            symbol = ""
            if speaker and speaker != "玩家":
                if speaker not in npc_seen:
                    npc_seen.add(speaker)
                    ph = f"<{speaker}>"
                    doc.add_symbol(ph, speaker)
                symbol = f"<{speaker}>"
                # 记录涉及元素
                if not any(e.get("name") == speaker
                           for e in current_step.involved_elements):
                    current_step.involved_elements.append({
                        "type": "npc", "name": speaker,
                        "symbol": f"<{speaker}>",
                    })
            current_step.dialog_fragments.append({
                "speaker": speaker, "text": dialog_text, "symbol": symbol,
            })
            continue
        # 非对话行(旁白/描述)进 content
        if current_step is not None and line.strip():
            content_buffer.append(line)

    _flush_step()
    doc.ok = True
    return doc


def _parse_xlsx(path: Path, cli=None):
    """xlsx 解析:read_sheet → records 列表(每行一 record)。"""
    DocIntent, _ = _types()
    doc = DocIntent(source_path=str(path), file_type="xlsx")
    if cli is None:
        # 无 cli 时降级用 openpyxl 直读
        try:
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if rows:
                headers = [str(h or "") for h in rows[0]]
                for row in rows[1:]:
                    rec = {headers[i]: row[i] for i in range(len(headers))}
                    doc.records.append(rec)
            wb.close()
            doc.ok = True
        except Exception as e:
            doc.ok = False
            doc.error = f"xlsx parse error: {e}"
        return doc
    # 有 cli 时用 read_sheet
    try:
        sheets = cli.get_sheets(path)
        sheet = sheets[0] if sheets else ""
        data = cli.read_sheet(path, sheet)
        headers = cli.read_header(path, sheet)
        for row in data:
            rec = {headers[i]: row[i] for i in range(len(headers))}
            doc.records.append(rec)
        doc.ok = True
    except Exception as e:
        doc.ok = False
        doc.error = f"xlsx parse error: {e}"
    return doc


def _parse_csv(path: Path):
    """csv 解析:行记录 → records 列表。"""
    DocIntent, _ = _types()
    doc = DocIntent(source_path=str(path), file_type="csv")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
            if rows:
                headers = [h.strip() for h in rows[0]]
                for row in rows[1:]:
                    rec = {headers[i]: (row[i] if i < len(row) else "")
                           for i in range(len(headers))}
                    doc.records.append(rec)
        doc.ok = True
    except Exception as e:
        doc.ok = False
        doc.error = f"csv parse error: {e}"
    return doc


def _parse_txt(path: Path):
    """txt 降级:纯文本,走 codemaker_parser HTTP 通道由 Step1 SubAgent 处理。"""
    DocIntent, _ = _types()
    text = path.read_text(encoding="utf-8")
    return DocIntent(source_path=str(path), file_type="txt",
                     raw_text=text, ok=True)


def assign_symbols(doc) -> None:
    """为 DocIntent 补充分配符号映射表(若 parse 阶段未完整分配)。

    扫描步骤卡片涉及元素 + records 中的 NPC/Dialog 字段,分配 placeholder。
    复用 CrossTableIntentSplitter produces 模式。
    """
    if not doc.ok:
        return
    # 从步骤卡片提取
    for step in doc.steps:
        for elem in step.involved_elements:
            name = elem.get("name", "")
            etype = elem.get("type", "npc")
            if name and not elem.get("symbol"):
                ph = f"<{etype}_{name}>"
                doc.add_symbol(ph, name)
                elem["symbol"] = ph
