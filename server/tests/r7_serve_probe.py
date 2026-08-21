"""R7 codemaker serve agentic LLM 诊断探针。

R7 根因在 codemaker serve 侧（非 excel-agent 仓库可修）：serve 对 `/session/{id}/message`
端点做 auto-context-grounding（自动读项目 xlsx 文件作上下文），导致 excel-agent 发送
~9.6-20k token 的聚焦 prompt 后，serve 内部膨胀到 143.8k token + 90-180s 超时返空。

本探针不修复 R7（根治需 serve 侧改：关 auto-context / 提供纯文本补全端点 / 非 agentic 通道），
仅**表征 + 验证**：
  - 发送一个极小聚焦 prompt（~500 字符，无表 stem，避免触发 auto-context 读 xlsx）
  - 测墙钟 + 响应是否空
  - 判定 R7 状态：healthy（<30s 有响应）/ R7-suspected（30-90s 或响应短）/ R7-confirmed（>90s 或空响应）

运行（需 serve 可达 + CODEMAKER_R7_PROBE=1）:
    cd server && python tests/r7_serve_probe.py
    或 python -m tests.r7_serve_probe

退出码：0=healthy / 1=R7-suspected / 2=R7-confirmed / 3=serve 不可达 / 4=未启用探针
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _probe_once(parser, prompt: str, timeout: int = 90) -> tuple[float, str, str]:
    """单次 LLM 探针。返 (elapsed_s, response_text, error)。"""
    sid = parser._ensure_session()
    if not sid:
        return 0.0, "", "无 session（serve 不可达或 _ensure_session 失败）"
    t0 = time.perf_counter()
    try:
        resp = parser.client.prompt(sid, prompt, timeout=timeout,
                                     model=getattr(parser, "model", ""),
                                     cancel_event=None)
    except Exception as e:
        return time.perf_counter() - t0, "", f"prompt 异常: {type(e).__name__}: {e}"
    elapsed = time.perf_counter() - t0
    raw = getattr(resp, "response_text", "") or ""
    if not getattr(resp, "ok", False):
        return elapsed, raw, f"resp.ok=False err={getattr(resp,'error','')}"
    return elapsed, raw, ""


def diagnose_r7() -> int:
    """主诊断。返退出码。"""
    if os.getenv("CODEMAKER_R7_PROBE", "0") != "1":
        print("[R7-probe] 未启用。设 CODEMAKER_R7_PROBE=1 运行。")
        return 4

    try:
        from agent.excel.parser.codemaker_parser import CodemakerNLParser
        from agent.codemaker_client import CodemakerClient
    except Exception as e:
        print(f"[R7-probe] import 失败: {e}")
        return 3

    try:
        client = CodemakerClient()  # 连 codemaker serve
        parser = CodemakerNLParser(client=client)
    except Exception as e:
        print(f"[R7-probe] parser/client 初始化失败（serve 不可达?）: {e}")
        return 3

    # 健康检查：serve 可达？
    try:
        if not client.health_check():
            print("[R7-probe] serve health_check 失败（不可达）")
            return 3
    except Exception as e:
        print(f"[R7-probe] serve health_check 异常: {e}")
        return 3

    # 极小聚焦 prompt：无表 stem，纯文本问答，避免触发 serve auto-context 读 xlsx。
    prompt = "回答一个字：是。仅输出「是」，不要读任何文件，不要调用工具。"
    print(f"[R7-probe] 发送极小 prompt（{len(prompt)} 字符），timeout=90s ...")
    elapsed, raw, err = _probe_once(parser, prompt, timeout=90)

    print(f"[R7-probe] 墙钟: {elapsed:.1f}s")
    print(f"[R7-probe] 响应长度: {len(raw)} 字符")
    if err:
        print(f"[R7-probe] 错误: {err}")
    if raw:
        print(f"[R7-probe] 响应预览: {raw[:120]}")

    # 判定
    if err and "无 session" in err:
        print("[R7-probe] 结论: serve 不可达（无法建会话）")
        return 3
    if elapsed > 90 or (not raw and not err):
        print("[R7-probe] 结论: R7-confirmed（>90s 超时 或 空响应）—— serve auto-context 卡死")
        print("[R7-probe] 根治需 serve 侧: ① 关 auto-context-grounding ② 提供纯文本补全端点 ③ 非 agentic 通道")
        return 2
    if elapsed > 30 or len(raw) < 5:
        print("[R7-probe] 结论: R7-suspected（30-90s 或 响应极短）—— serve 慢/不稳定")
        return 1
    print("[R7-probe] 结论: healthy（<30s 有响应）—— serve 正常，可跑 LLM e2e")
    return 0


if __name__ == "__main__":
    sys.exit(diagnose_r7())
