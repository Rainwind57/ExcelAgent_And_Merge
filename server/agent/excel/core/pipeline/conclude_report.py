"""建议7：Step4 透明化（纯函数，0 LLM）。

两件事（docs §P4 "最终消息区分用户/开发者视角，不要只报泛化失败"）：
  1. is_clean_success：只要有 真失败 / 跳过 / 部分写入 / 漏解析 / 任何 failure 记录，
     就**不能**报"干净成功"。（信息性诊断 warning 不在此列——那走 warnings 通道。）
  2. render_bucketed_failures：失败按来源 Step1/2/3 **分桶**输出、**不截断**（原
     failures[:5] 会把后续失败吞掉，用户看不到全貌）。

纯函数、无 IO、无 LLM、确定性。
"""
from __future__ import annotations

from typing import Iterable

__all__ = ["is_clean_success", "bucket_failures", "render_bucketed_failures",
           "STEP_BUCKET_ORDER"]

STEP_BUCKET_ORDER = ["step1_parse", "step2_validate", "step3_execute", "other"]
_STEP_TITLE = {
    "step1_parse": "Step1 解析",
    "step2_validate": "Step2 校验",
    "step3_execute": "Step3 执行",
    "other": "其它",
}


def is_clean_success(*, prior_ok: bool, n_ok: int, n_fail: int, n_skipped: int,
                     n_partial: int, has_incomplete: bool,
                     n_failures: int) -> bool:
    """判定是否"干净成功"。

    干净成功 = 前序全 ok 且 至少完成 1 条 且 无真失败/跳过/部分写入/漏解析/failure 记录。
    只要任一问题信号存在即非干净成功（宁可如实报部分/失败，也不掩盖）。
    """
    return bool(
        prior_ok
        and n_ok > 0
        and n_fail == 0
        and n_skipped == 0
        and n_partial == 0
        and not has_incomplete
        and n_failures == 0
    )


def _step_of(f: dict) -> str:
    """从 failure dict 推断来源 step 桶。"""
    sid = str(f.get("step_id") or "").strip()
    if sid in _STEP_TITLE:
        return sid
    # 兼容：_prior_step_failures 把 step_id 放进 attempted_strategies
    strat = str(f.get("attempted_strategies") or "").strip()
    if strat in _STEP_TITLE:
        return strat
    return "step3_execute" if not sid else "other"


def bucket_failures(failures: Iterable[dict]) -> dict[str, list[dict]]:
    """把 failures 按来源 step 分桶（保序）。返回 {step_id: [failure,...]}。"""
    buckets: dict[str, list[dict]] = {}
    for f in (failures or []):
        if not isinstance(f, dict):
            continue
        buckets.setdefault(_step_of(f), []).append(f)
    return buckets


def render_bucketed_failures(failures: Iterable[dict]) -> str:
    """按 Step1/2/3 分桶渲染**全部**失败（不截断）。空则返回 ""。"""
    buckets = bucket_failures(failures)
    if not buckets:
        return ""
    lines: list[str] = []
    ordered = [s for s in STEP_BUCKET_ORDER if s in buckets]
    for s in ordered:
        items = buckets[s]
        lines.append(f"【{_STEP_TITLE.get(s, s)}】{len(items)} 项")
        for f in items:
            loc = f"{f.get('table') or '?'}/{f.get('sheet') or '?'}"
            col = f" 列[{f.get('col')}]" if f.get("col") else ""
            rc = f.get("root_cause") or f.get("message") or "未知"
            status = f.get("status") or ""
            tag = f"（{status}）" if status else ""
            lines.append(f"  - {loc}{col}{tag}：{rc}")
    return "\n".join(lines)
