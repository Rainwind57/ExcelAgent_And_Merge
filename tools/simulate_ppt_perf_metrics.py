from __future__ import annotations

import json
import re
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "server" / "agent" / "excel" / "_table_index.json"
OUT_JSON = ROOT / "bench" / "ppt_module_perf_simulated.json"
OUT_MD = ROOT / "bench" / "ppt_module_perf_simulated.md"

SAMPLE_INPUT = "新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10008的场景坐标(60,0,30)，玩家点击后弹出对话，对话内容为'欢迎来到铁匠铺，我可以帮你锻造装备。'，选项为'好的，我要锻造'和'离开'"
RAW_LOG = """
→ crud: 新增NPC铁匠老张并配置对话
▶ run_v2 入口（4-Step 硬隔离）
Step1 解析 开始
LocatorAgent 开始探测候选表
ColumnExtractor 提取列名 20 个，反查候选表 5 个（guild,interaction,combat,assistant,city），补进候选池供 LLM 参考
候选池超上限(12→12)，按置信度裁剪保留 12 个
歧义候选含 58 条 FK 边，判定为跨表链非纯噪声歧义，保留 12 候选交 DecomposeAgent 拆分阶段做表选择，不在定位层 LLM 收敛单表（防跨表链被短路成单表）
歧义候选含 58 条 FK 边，判定为跨表链非纯噪声歧义，保留 12 候选交 DecomposeAgent 拆分阶段做表选择，不在定位层 LLM 收敛单表（防跨表链被短路成单表）
LocatorAgent 产出 12 候选表, 58 FK 边, cross=True (复杂输入汇集)
DecomposeAgent 单 prompt主路径(12 表,阈值 3,timeout=120s)
意图解析中：已调用 0 次模型
疑似 serve hang（LLM 调用计数长时间未变化），建议点击停止
DecomposeAgent 过滤幻觉表 intent(单 prompt): ['interaction', 'interaction', 'interaction', 'interaction']
DecomposeAgent 单 prompt 产出 1 条意图（丢弃 0 叙述灌值 stem）
意图解析中：已调用 1 次模型
DecomposeAgent 过滤幻觉表 intent(单 prompt): ['entity_prefab']
DecomposeAgent 单 prompt 产出 4 条意图（丢弃 0 叙述灌值 stem）
意图解析中：已调用 2 次模型
DecomposeAgent 过滤幻觉表 intent(单 prompt): ['entity_prefab', 'interaction', 'interaction', 'interaction', 'interaction']
DecomposeAgent 单 prompt 产出 0 条意图（丢弃 0 叙述灌值 stem）
DecomposeAgent 链式分组 12 表→3 组单 prompt，产出 5 条意图
DecomposeAgent 产出 5 条意图(单 prompt)
DecomposeAgent 缺表对账：expected 48 sheet，produced 4 sheet，缺 ['combat', 'equipment', 'guild']
意图解析中：已调用 3 次模型
DecomposeAgent 过滤幻觉表 intent(单 prompt): ['entity_prefab', 'interaction', 'interaction', 'interaction', 'interaction']
DecomposeAgent 单 prompt 产出 0 条意图（丢弃 0 叙述灌值 stem）
缺表重拆 combat 仍产空（该表可能非动作主语，如 FK 引用目标）
意图解析中：已调用 4 次模型
DecomposeAgent 单 prompt 非 JSON 数组
缺表重拆 equipment 仍产空（该表可能非动作主语，如 FK 引用目标）
DecomposeAgent 单 prompt 非 JSON 数组
缺表重拆 guild 仍产空（该表可能非动作主语，如 FK 引用目标）
意图解析中：已调用 7 次模型
Step1 schema field cleanup changed 6 fields
Step1 FK placeholder targets repaired: 1
Step1 schema field cleanup changed 6 fields
ParseAgent 产出 5 条 NLIntent(source=llm_decompose)
Step2 校验 开始
核心4 PK 前移检查:intents=5,data_getter=有,ask_cb=有
Step2 预分配 PK: entity_prefab/Base 编号=10013112016（label=new_entity_prefab_id）
Step2 预分配 PK: interaction/InteractionConv 编号=10071（label=conv_main_id）
Step2 预分配 PK: interaction/InteractionConvOption 编号=10071（label=opt_forge_id）
Step2 预分配 PK: interaction/InteractionConvOption 编号=10072（label=opt_leave_id）
核心4 PK 检查:列[编号] 值[10013112016] 占用=False
核心4 PK 检查:列[编号] 值[3006] 占用=False
核心4 PK 检查:列[编号] 值[10071] 占用=False
核心4 PK 检查:列[编号] 值[10071] 占用=False
核心4 PK 检查:列[编号] 值[10072] 占用=False
Step2 ValidateAgent: 4 issues
P23 4 条 tips 转软失败上报
step2_validate 未通过（6 个问题）
""".strip()


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - chinese
    return int(chinese * 0.8 + other / 4)


def load_index_metrics() -> dict:
    size = INDEX.stat().st_size
    text = INDEX.read_text(encoding="utf-8", errors="ignore")
    stems = len(set(re.findall(r'"stem"\s*:\s*"([^"]+)"', text)))
    sheets = len(re.findall(r'"sheets"\s*:', text))
    columns = len(re.findall(r'"headers"\s*:', text))
    return {
        "file_size_mb": round(size / 1024 / 1024, 2),
        "stems_found": stems,
        "sheet_blocks_found": sheets,
        "header_blocks_found": columns,
    }


def prompt_metrics(index: dict) -> dict:
    full_chars = INDEX.stat().st_size
    focused_schema = {
        "entity_prefab.Base": ["prefab_id", "entity_class", "model_id", "entity_name", "interaction_id"],
        "interaction.Interaction": ["interaction_id", "effect.key", "effect.data.3006.conv_id"],
        "interaction.InteractionConv": ["conv_id", "prompt_text", "options[0]", "options[1]"],
        "interaction.InteractionConvOption": ["option_id", "option_text", "option_function.data.1.conv_id"],
    }
    focused_prompt = json.dumps({"user": SAMPLE_INPUT, "schema": focused_schema}, ensure_ascii=False)
    top12_prompt_chars = len(focused_prompt) * 3
    return {
        "full_index_mb": index["file_size_mb"],
        "full_context_est_tokens": estimate_tokens("x" * full_chars),
        "focused_schema_chars": len(focused_prompt),
        "focused_schema_est_tokens": estimate_tokens(focused_prompt),
        "top12_schema_est_tokens": estimate_tokens("x" * top12_prompt_chars),
        "token_reduction_vs_full_pct": round((1 - estimate_tokens(focused_prompt) / estimate_tokens("x" * full_chars)) * 100, 2),
    }


def log_metrics() -> dict:
    raw_lines = [x for x in RAW_LOG.splitlines() if x.strip()]
    summary_lines = [
        "Step1 解析完成：识别 5 个子任务",
        "命中表：entity_prefab.Base、interaction.Interaction、InteractionConv、InteractionConvOption",
        "模型调用：7 次；过滤幻觉表：interaction、entity_prefab",
        "Step2 校验未通过：6 个问题，其中 4 个 tips 转软失败",
        "建议：展开任务表检查字段，按校验卡片修正后继续",
    ]
    return {
        "raw_lines": len(raw_lines),
        "summary_lines": len(summary_lines),
        "line_reduction_pct": round((1 - len(summary_lines) / len(raw_lines)) * 100, 2),
        "raw_chars": len(RAW_LOG),
        "summary_chars": len("\n".join(summary_lines)),
        "char_reduction_pct": round((1 - len("\n".join(summary_lines)) / len(RAW_LOG)) * 100, 2),
    }


def main() -> None:
    t0 = perf_counter()
    index = load_index_metrics()
    data = {
        "note": "PPT 补充模拟压测：用于估算展示层/Prompt 结构优化收益；不替代真实端到端评测。",
        "input": SAMPLE_INPUT,
        "index": index,
        "prompt": prompt_metrics(index),
        "log_view": log_metrics(),
        "elapsed_ms": round((perf_counter() - t0) * 1000, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# PPT 模块化性能补充模拟压测",
        "",
        "> 说明：本报告为模拟压测/静态估算，用来补足 PPT 中 Prompt、展示层等缺少独立真实 A/B 的模块；真实准确率仍引用归档评测。",
        "",
        "## Prompt 上下文瘦身",
        "",
        "| 指标 | 全量上下文 | 聚焦 schema | 收益 |",
        "|---|---:|---:|---:|",
        f"| 上下文规模 | {data['prompt']['full_index_mb']} MB | {data['prompt']['focused_schema_chars']} 字符 | — |",
        f"| 估算 token | {data['prompt']['full_context_est_tokens']} | {data['prompt']['focused_schema_est_tokens']} | -{data['prompt']['token_reduction_vs_full_pct']}% |",
        "",
        "## 用户日志压缩",
        "",
        "| 指标 | 优化前 | 优化后 | 收益 |",
        "|---|---:|---:|---:|",
        f"| 展示行数 | {data['log_view']['raw_lines']} | {data['log_view']['summary_lines']} | -{data['log_view']['line_reduction_pct']}% |",
        f"| 展示字符 | {data['log_view']['raw_chars']} | {data['log_view']['summary_chars']} | -{data['log_view']['char_reduction_pct']}% |",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()
