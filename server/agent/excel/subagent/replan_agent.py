"""ReplanAgent（§9.1 Plan-Execute 显式化）。

失败后离线重规划：Step5 失败子任务 + remaining 未执行子任务 → LLM ≤1 次 → 修订 SubTask[]
回 Step5 重跑（补建/跳过/改字段值）。等价综述 PlanExecuteAgent._replan。

与 §D4 不冲突：D4 否决"执行阶段现场 LLM 推理"，replan 是"失败后离线重规划"，
属 ConcludeAgent 之外的增量闭环，默认开（CODEMAKER_REPLAN_ON_FAILURE=1，准确率优先）。

接入点：Step5 主循环 + backfill 后、Step6 前扫前（agent.py:_run_replan_phase）。
上限 N=2（防 LLM 死循环，每轮 Step5 失败后最多 replan 2 次）。

职责边界:
  - 输入: failures(失败 op root_cause/col/val) + remaining_intents(未成功 op) +
          produced(已成功产出 ID 供占位符替换) + user_text
  - 输出: list[NLIntent] 修订 op（补建缺失行/改字段值/跳过+标 forward_ref）
  - 安全网: LLM 失败/空响应/畸形 → 返回空 list，调用方降级走原 Step6 上报

不与 verify-repair 重复升 LLM：
  verify-repair 是单 op 写后失败 → 规则/LLM 修字段重写（agent 内 _run_verify_repair_loop）。
  replan 是批级失败聚合后 → LLM 重规划剩余 op（跨 op 依赖调整，如补建被引用的目标行）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from ..parser.nl_parser import NLIntent

logger = logging.getLogger(__name__)

_REPLAN_MAX_ROUNDS = 2


class ReplanAgent:
    """失败后离线重规划 Agent。LLM ≤1 次/轮，产修订 SubTask[] 回 Step5 重跑。

    门控 CODEMAKER_REPLAN_ON_FAILURE=0 默认关（与 §D4 一致，增量 LLM 触发点默认关）。
    """

    def __init__(self, parser=None, thinking_sink=None):
        self.parser = parser
        self._thinking_sink = thinking_sink

    def _add_thinking(self, phase: str, msg: str) -> None:
        if self._thinking_sink is not None:
            try:
                self._thinking_sink(phase, msg)
            except Exception:
                logger.debug("ReplanAgent thinking_sink 推送失败", exc_info=True)

    def replan(self, failures: list[dict], remaining_intents: list[NLIntent],
               produced: dict[str, str], user_text: str,
               cli=None) -> list[NLIntent]:
        """主入口：failures + remaining → 修订 NLIntent[]。

        Args:
            failures: 失败 op 的结构化 failure dict（root_cause/col/table/sheet/type）
            remaining_intents: 未成功/未执行的 NLIntent（replan 的输入候选）
            produced: 已成功产出 ID {label: value}（供 LLM 知道哪些已建）
            user_text: 用户原始指令（供 LLM 理解意图）
            cli: CLIInterface（可选，供读表头丰富 prompt）

        Returns:
            修订 NLIntent 列表。失败返回空 list（调用方降级走原 Step6 上报）。
        """
        if not failures or not remaining_intents:
            return []
        if not self.parser:
            logger.warning("ReplanAgent 无 parser,跳过")
            return []
        client = getattr(self.parser, "client", None)
        if client is None:
            logger.warning("ReplanAgent 无 client,跳过")
            return []

        prompt = self._build_prompt(failures, remaining_intents, produced, user_text, cli)
        if not prompt:
            return []

        # LLM 调用（单次，≤1 LLM 往返，防死循环）
        per_to = int(os.environ.get("CODEMAKER_REPLAN_TIMEOUT", "45"))
        raw = self._call_llm(prompt, timeout=per_to)
        if not raw:
            self._add_thinking("重规划", "ReplanAgent LLM 空响应,降级走原上报")
            return []

        arr = self._parse_json_array(raw)
        if not arr:
            self._add_thinking("重规划", "ReplanAgent LLM 非 JSON 数组,降级走原上报")
            return []

        intents = self._to_nl_intents(arr, user_text, remaining_intents)
        if not intents:
            self._add_thinking("重规划", "ReplanAgent 无有效修订 op,降级走原上报")
            return []

        self._add_thinking("重规划",
            f"ReplanAgent 产出 {len(intents)} 条修订 op（补建/改字段/跳过）")
        return intents

    def _call_llm(self, prompt: str, timeout: int = 45) -> str:
        """调用 codemaker LLM（复用 DecomposeAgent 隔离 session 模式）。"""
        from .base import _isolated_empty_dir
        client = getattr(self.parser, "client", None)
        if client is None:
            return ""
        try:
            sr = client.create_session(
                directory=_isolated_empty_dir(),
                model=getattr(self.parser, "model", ""))
            if not getattr(sr, "ok", False):
                return ""
            cancel_event = getattr(self.parser, "_cancel_event", None)
            resp = client.prompt(sr.session_id, prompt, timeout=timeout,
                                  model=getattr(self.parser, "model", ""),
                                  cancel_event=cancel_event)
            return getattr(resp, "response_text", "") or ""
        except Exception:
            logger.warning("ReplanAgent LLM 调用失败", exc_info=True)
            return ""

    def _build_prompt(self, failures: list[dict], remaining: list[NLIntent],
                      produced: dict[str, str], user_text: str, cli=None) -> str:
        """构造 LLM prompt：failures + remaining schema + produced → 修订 JSON。"""
        # 失败清单摘要
        fail_lines = []
        for f in failures[:10]:  # 截断防 prompt 过长
            loc = f"{f.get('table','')}/{f.get('sheet','')}"
            col = f" 列[{f.get('col')}]" if f.get("col") else ""
            rc = f.get("root_cause") or f.get("type") or "未知"
            fail_lines.append(f"- {loc}{col} 原因：{rc}")
        fail_block = "\n".join(fail_lines) if fail_lines else "无失败信息"

        # remaining op schema（供 LLM 知道还有哪些未跑）
        rem_lines = []
        for i, it in enumerate(remaining[:15]):  # 截断
            tbl = it.table_hint or ""
            sht = it.sheet_hint or ""
            act = it.action or ""
            fields = (it.extras or {}).get("fields", {})
            produces = it.produces_label or getattr(it, "produces_label", "") or ""
            loc_f = it.locator_field or ""
            loc_v = it.locator_value or ""
            rem_lines.append(
                f"- op{i}: action={act} table={tbl} sheet={sht} "
                f"locator={loc_f}={loc_v} produces={produces} fields={fields}")
        rem_block = "\n".join(rem_lines) if rem_lines else "无剩余 op"

        # produced ID（已成功产出，供占位符替换参考）
        prod_lines = [f"- {k}={v}" for k, v in list(produced.items())[:15]]
        prod_block = "\n".join(prod_lines) if prod_lines else "无已产出 ID"

        return (
            "你是表格重规划助手。当前批量子任务执行部分失败，需基于失败原因 + 剩余子任务 "
            "重新规划修订操作，让功能链闭环。\n\n"
            f"用户原始指令：{user_text[:200]}\n\n"
            f"失败清单：\n{fail_block}\n\n"
            f"已成功产出 ID（可被后续 op 引用）：\n{prod_block}\n\n"
            f"剩余未成功子任务：\n{rem_block}\n\n"
            "重规划原则：\n"
            "1. 补建：若失败因跨表引用指向不存在的行 → 产 add op 补建目标行\n"
            "2. 改字段：若失败因列不存在/类型不符 → 产 set op 用正确列名/值重写\n"
            "3. 跳过：若失败不可恢复 → 不产该 op（交 Step6 上报）\n"
            "4. 不要重复已成功的 op\n"
            "5. consumes 引用已产出 ID 时用字面值（produced 里给的值）\n\n"
            "输出 ```json\n"
            "[{\"action\":\"add|set|delete\",\"table\":\"\",\"sheet\":\"\","
            "\"fields\":{},\"produces\":\"\",\"locator_field\":\"\","
            "\"locator_value\":\"\"}]\n"
            "```\n"
            "数组每元素一个修订 op。无修订产空数组 []。"
        )

    def _parse_json_array(self, raw: str) -> list:
        """从 LLM 响应提取 JSON 数组（fenced ```json ...``` 或裸数组）。"""
        m = re.search(r"```json\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(1))
                return arr if isinstance(arr, list) else []
            except ValueError:
                return []
        # 回退裸数组：找首个 [ 到末尾 ] 的子串
        start = raw.find("[")
        end = raw.rfind("]")
        if start < 0 or end < 0 or end <= start:
            return []
        try:
            arr = json.loads(raw[start:end + 1])
            return arr if isinstance(arr, list) else []
        except ValueError:
            return []

    def _to_nl_intents(self, arr: list, text: str,
                       remaining: list[NLIntent]) -> list[NLIntent]:
        """LLM JSON 数组 → NLIntent 列表（复用 DecomposeAgent._to_split_intents 语义）。"""
        out: list[NLIntent] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            act = str(item.get("action") or "add").strip().lower()
            if act not in ("add", "set", "delete", "get"):
                act = "add"
            stem = str(item.get("table") or "").strip()
            sheet = str(item.get("sheet") or "").strip() or None
            fields = item.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {}
            produces = str(item.get("produces") or "").strip() or None
            loc_field = str(item.get("locator_field") or "").strip() or None
            loc_value = str(item.get("locator_value") or "").strip() or None
            extras: dict = {"fields": fields, "source": "replan"}
            if produces:
                extras["produces"] = produces
            out.append(NLIntent(
                action=act, table_hint=stem or None, sheet_hint=sheet,
                locator_field=loc_field, locator_value=loc_value,
                raw=text, extras=extras,
            ))
        return out


def replan_enabled() -> bool:
    """门控：CODEMAKER_REPLAN_ON_FAILURE 默认开（准确率优先、不少指令）。"""
    return os.environ.get("CODEMAKER_REPLAN_ON_FAILURE", "1") != "0"


def replan_max_rounds() -> int:
    """重规划上限轮数（防 LLM 死循环）。"""
    return _REPLAN_MAX_ROUNDS
