"""模拟压测：agent_service 用户可见进度过滤 + intent_list 摘要 + 展示行数上限。

目标文件：server/services/agent_service.py（AgentService.chat_stream 内部）

本次改动新增（见 git diff）：
  - `_is_user_visible_progress(phase, detail)`：黑名单正则过滤内部调试文案
    （DecomposeAgent/LocatorAgent/单 prompt/serve hang/timeout= 等）。
  - `_intent_list_summary(detail)`：把 `__json:intent_list` 结构化 detail 转成
    友好摘要（"已解析 N 个子任务 + 命中表/Sheet"）。
  - `_compose_stage_content` 输出行数上限 `lines[:8]`。

**重要说明（如实标注）**：这三个函数定义在 `AgentService.chat_stream` 方法体
内部（闭包，依赖外层 `_re`/`json`/`OrderedDict`/`_STAGE_TITLES` 等局部变量和
`import` 别名），不是模块级/类级可 import 符号，无法 `from ... import
_is_user_visible_progress` 直接调用。为了仍然做到"跑真代码"而不是"编造等效
实现"，本脚本从 `server/services/agent_service.py` 源文件里用 AST 精确提取
这两个函数定义的原始源码文本，再在本进程内 `exec` 装载为可调用对象——
函数体字节与生产代码逐字一致，只是提取方式非 import。若源码结构变化导致
提取失败，脚本会显式报错退出，不会静默退化为「简化实现」。
"""
from __future__ import annotations

import ast
import json
import re as _re
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "server" / "services" / "agent_service.py"
OUT_JSON = ROOT / "bench" / "ppt_agent_service_noise_filter.json"
OUT_MD = ROOT / "bench" / "ppt_agent_service_noise_filter.md"


def _extract_func_source(func_name: str) -> str:
    """从 agent_service.py 源码 AST 中精确提取指定函数定义的源码文本。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(SRC.read_text(encoding="utf-8"), node)
    raise RuntimeError(f"未在 {SRC} 中找到函数定义 {func_name}，源码结构可能已变化")


def _load_real_functions():
    """把提取到的真实函数源码 exec 到独立命名空间，返回可调用对象。

    命名空间里预先注入函数体依赖的外部名字（_re/json/OrderedDict），
    与生产代码 chat_stream 闭包内的可见名字一致。
    """
    ns = {"_re": _re, "json": json, "OrderedDict": OrderedDict}
    src_visible = _extract_func_source("_is_user_visible_progress")
    src_summary = _extract_func_source("_intent_list_summary")
    exec(compile(src_visible, str(SRC), "exec"), ns)
    exec(compile(src_summary, str(SRC), "exec"), ns)
    return ns["_is_user_visible_progress"], ns["_intent_list_summary"]


_is_user_visible_progress, _intent_list_summary = _load_real_functions()


# 混合 (phase, detail) 数据：内部调试噪音 + 用户可见内容，至少 25 条。
THINKING_LINES = [
    ("细分", "DecomposeAgent 单 prompt主路径(12 表,阈值 3,timeout=120s)"),
    ("细分", "LocatorAgent 开始探测候选表"),
    ("细分", "DecomposeAgent 过滤幻觉表 intent(单 prompt): ['interaction']"),
    ("细分", "疑似 serve hang（LLM 调用计数长时间未变化），建议点击停止"),
    ("细分", "DecomposeAgent 链式分组 12 表→3 组单 prompt，产出 5 条意图"),
    ("细分", "DecomposeAgent 缺表对账：expected 48 sheet，produced 4 sheet"),
    ("细分", "缺表重拆 combat 仍产空（该表可能非动作主语，如 FK 引用目标）"),
    ("细分", "候选池超上限(12→12)，按置信度裁剪保留 12 个"),
    ("细分", "歧义候选含 58 条 FK 边，判定为跨表链非纯噪声歧义"),
    ("细分", "Step1 schema field cleanup changed 6 fields"),
    ("细分", "Step1 FK placeholder targets repaired: 1"),
    ("心跳", "意图解析中：已调用 3 次模型"),
    ("意图分类", "规则短路优先命中 add 模板"),
    ("校验", "核心4 PK 前移检查:intents=5,data_getter=有,ask_cb=有"),
    ("校验", "Step2 预分配 PK: entity_prefab/Base 编号=10013112016"),
    ("校验", "P23 4 条 tips 转软失败上报"),
    ("校验", "ColumnExtractor 提取列名 20 个，反查候选表 5 个"),
    ("校验", "validator_agent.py:1752 命中前向引用归一化分支"),
    ("校验", "本次校验发现 2 个必填字段缺失，请补充【灵兽名称】"),
    ("执行", "已成功新增 NPC「铁匠老张」到 entity_prefab 表"),
    ("执行", "已修改 conv_id=1 的对话内容"),
    ("执行", "写入完成：interaction/InteractionConvOption 新增 2 行"),
    ("汇总", "完成 5 个子任务，0 个失败"),
    ("汇总", "本次操作已全部落盘，可在表格中查看结果"),
    ("__json:intent_list", json.dumps({
        "rows": [
            {"loc": "entity_prefab/Base", "action": "add"},
            {"loc": "interaction/InteractionConv", "action": "add"},
            {"loc": "interaction/InteractionConvOption", "action": "add"},
            {"loc": "interaction/InteractionConvOption", "action": "add"},
            {"loc": "interaction/Interaction", "action": "set"},
        ]
    }, ensure_ascii=False)),
    ("执行", "DecomposeAgent 单 prompt 非 JSON 数组"),
]

assert len(THINKING_LINES) >= 25, "至少需要 25 条混合数据"


def before_all_visible(lines: list[tuple[str, str]]) -> list[str]:
    """Before：假设全部展示，不过滤、不截断（旧行为等效：无 _is_user_visible_progress
    时 _compose_stage_content 走 else 分支把 thinking 全塞进去，仅排除
    "规则短路优先" 一条，见 diff 删除的 for 循环）。"""
    out = []
    for ph, d in lines:
        if str(ph or "").startswith("__json:"):
            continue  # 旧代码也不会把原始 JSON detail 直接展示为文本行
        if d.startswith("规则短路优先"):
            continue
        out.append(d)
    return out


def after_filtered(lines: list[tuple[str, str]]) -> list[str]:
    """After：真实 _is_user_visible_progress 过滤 + intent_list 摘要 + 截断到 8 行。"""
    out = []
    json_summaries = []
    for ph, d in lines:
        if str(ph or "").startswith("__json:intent_list"):
            summary = _intent_list_summary(d)
            if summary:
                json_summaries.append(summary)
    if json_summaries:
        out.extend(json_summaries[-1].split("\n"))
    for ph, d in lines:
        if str(ph or "").startswith("__json:"):
            continue
        if not _is_user_visible_progress(ph, d):
            continue
        text = str(d).strip()
        if text and text not in out:
            out.append(text)
    return out[:8]


NOISE_PATTERN = _re.compile(
    r"(DecomposeAgent|LocatorAgent|ParseAgent|ColumnExtractor|timeout=|"
    r"serve hang|候选池|歧义候选|缺表|schema field cleanup|FK placeholder|"
    r"核心4 PK|P23|Step1 schema|\.py:)", _re.I)


def _is_noise(text: str) -> bool:
    return bool(NOISE_PATTERN.search(text))


def main() -> None:
    before_lines = before_all_visible(THINKING_LINES)
    after_lines = after_filtered(THINKING_LINES)

    before_noise = sum(1 for x in before_lines if _is_noise(x))
    after_noise = sum(1 for x in after_lines if _is_noise(x))
    before_noise_ratio = round(before_noise / len(before_lines), 4) if before_lines else 0.0
    after_noise_ratio = round(after_noise / len(after_lines), 4) if after_lines else 0.0

    out = {
        "note": ("模拟压测：直接从 server/services/agent_service.py 源码 AST 提取 "
                 "_is_user_visible_progress / _intent_list_summary 真实函数体 exec 装载"
                 "（两函数为 chat_stream 内部闭包，无法直接 import，故用 AST 提取源码文本 "
                 "而非重写等效逻辑，函数字节与生产代码逐字一致）。"
                 "before 为未过滤/未截断的展示行为（旧代码等效，按 diff 还原）。"
                 "口径：构造 25 条混合 thinking 数据模拟，非真实端到端会话回放。"),
        "input_lines": len(THINKING_LINES),
        "before": {
            "displayed_lines": len(before_lines),
            "noise_lines": before_noise,
            "noise_ratio": before_noise_ratio,
        },
        "after": {
            "displayed_lines": len(after_lines),
            "noise_lines": after_noise,
            "noise_ratio": after_noise_ratio,
        },
        "before_lines": before_lines,
        "after_lines": after_lines,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# agent_service 用户可见进度过滤 + 摘要 + 截断 模拟压测",
        "",
        "> 口径：`_is_user_visible_progress`/`_intent_list_summary` 为 "
        "`chat_stream` 方法内部闭包，非模块级可 import 符号；本脚本用 AST 从"
        "生产源码精确提取这两个函数定义源码文本后在本进程内 exec 装载，"
        "函数体与生产代码逐字一致（非重写等效实现）。构造 25 条混合 "
        "(phase, detail) thinking 数据（内部调试噪音 + 用户可见内容）模拟一次会话，"
        "before 为「全部展示不过滤不截断」的旧行为等效（按 diff 还原）。"
        "不与真实端到端会话指标混用。",
        "",
        "## 汇总",
        "",
        "| 指标 | Before（全展示） | After（过滤+摘要+截断8行） |",
        "|---|---:|---:|",
        f"| 展示行数 | {out['before']['displayed_lines']} | {out['after']['displayed_lines']} |",
        f"| 噪音行数 | {out['before']['noise_lines']} | {out['after']['noise_lines']} |",
        f"| 噪音占比 | {out['before']['noise_ratio']:.1%} | {out['after']['noise_ratio']:.1%} |",
        "",
        "## Before 展示内容（全部，不截断）",
        "",
    ]
    for x in before_lines:
        md.append(f"- {x}")
    md += ["", "## After 展示内容（过滤 + 摘要 + 截断到 8 行）", ""]
    for x in after_lines:
        md.append(f"- {x}")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()
