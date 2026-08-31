"""跨段引用上下文继承（路线图 §2/§9.2，0 LLM）。

多段指令切分后，后段常出现「再把它绑定」「前面的活动」「这几个奖励」这类跨段
指代。规则切分把前文切走后，段级 decompose_segment 拿到的单段文本里没有指代
对象 → LLM 只能瞎猜目标实体/表，导致子任务丢失或路由错。

本模块在段级 decompose **之前**，对每段做纯规则的指代消解：
  - 从前序段提取显式声明实体（「新增/配/建/叫 X」「X 的名称」「名为 X」）。
  - 后段含回指词（它/他/她/这个/那个/前面那个/上述/这几个/这些/该/其）时，
    解析到最近的前序实体，生成一段「上文实体继承」上下文块，拼到该段文本前。
  - LLM 拿到上下文块后知道「它」=具体实体名 + 原句，再结合 schema 选表选列。

纯函数、0 LLM、通用（不绑业务词/表），失败降级返回原文本，不阻塞主流程。
"""

from __future__ import annotations

import re

# 实体声明动词：这些动作词后常跟实体名（新增X / 配一个X / 建一个NPC叫Y / 造一个Z）
_DECLARE_VERB_RE = re.compile(
    r"(?:新增|增加|添加|配|建|造|创建|生成|设|给|加)(?:一个|个|一位|一条|一批|一下)?"
)

# 名称引导词：声明动词后，实体名常由「叫/名为/名字叫/名称是/是」引出
_NAME_MARKER_RE = re.compile(r"(?:叫|名为|名字叫|名称(?:为|是)|命名为|是)")

# 回指词（代词/指示词/序数回指）
_ANAPHORA_RE = re.compile(
    r"(?:前面(?:那个|这些|几个|的)?|上述|上(?:面|文)|它|他|她|这个|那个|这些|"
    r"这几个|那几个|该(?:实体|对象|项|行)?|其|前者)"
)

# 绑定类动作（这些动作的主语常是回指对象）
_BIND_VERB_RE = re.compile(r"(?:绑定|绑到|挂|关联|引用|指向|接到|接到|填进|设置到|赋给)")


def extract_declared_entities(seg_text: str) -> list[dict]:
    """从单段文本提取显式声明的实体（纯规则）。

    覆盖常见策划口语：
      - 「新增灵兽叫朱雀」 / 「配一个活动叫春节活动」
      - 「建一个NPC，名字叫张三」
      - 「造一个物品：XXX」
    返回 [{name, clause}]，name 为空则忽略。不绑定表（表由 schema 层裁决）。
    """
    text = seg_text or ""
    entities: list[dict] = []
    for m in _DECLARE_VERB_RE.finditer(text):
        tail = text[m.end():]
        # 实体名候选：引号名优先，其次名称引导词后的词
        name = ""
        q = re.search(r"[\"'「『]([^\"'」』]{1,24})[\"'」』]", tail)
        if q:
            name = q.group(1).strip()
        else:
            nm = _NAME_MARKER_RE.search(tail)
            if nm:
                after = tail[nm.end():].strip()
                # 取到首个分隔符（标点/连接词/回指词）为止的词串
                cut = re.search(r"[,，。；;、\s]+|然后|并且|再|的", after)
                name = after[:cut.start()] if cut else after[:24]
                name = name.strip("，,。；;：: ")
        if name and len(name) <= 24:
            clause = text[m.start():m.start() + 40].strip()
            entities.append({"name": name, "clause": clause})
    return entities


def _resolve_anaphora(seg_text: str, known: list[dict]) -> dict | None:
    """本段回指词 → 最近的前序实体。无前序实体返回 None。

    触发条件（满足其一即尝试继承最近前序实体）：
      - 段内含回指词（它/他/她/这个/那个/前面那个/上述/这些/该/其等）
      - 段内含绑定类动作（绑定/挂/关联/引用/指向等，主语常是前文对象）
    """
    if not known:
        return None
    text = seg_text or ""
    has_pronoun = bool(_ANAPHORA_RE.search(text))
    has_bind = bool(_BIND_VERB_RE.search(text))
    if not has_pronoun and not has_bind:
        return None
    return known[-1]


def build_segment_context(seg_texts: list[str]) -> list[str]:
    """为每段生成「上文实体继承」上下文块（无则空串）。

    返回与输入等长的 list[str]。第 i 项是第 i 段文本应拼接的前置上下文。
    顺序推进 known 实体池：后段能指向前序任意段声明的实体（取最近）。
    """
    texts = [str(t or "") for t in seg_texts]
    known: list[dict] = []
    out: list[str] = []
    for text in texts:
        ctx = ""
        antecedent = _resolve_anaphora(text, known)
        if antecedent is not None:
            ctx = (
                "【上文实体继承】本段里的回指词（它/这个/前面那个等）指向上文实体："
                f"「{antecedent['name']}」（原句：{antecedent['clause']}）。"
                "若本段要引用/绑定/关联该实体，请沿用它的目标表与其新增行的"
                " produces 标签（引用列填 <produces_label>），不要另起新实体。\n"
            )
        out.append(ctx)
        # 提取本段声明实体，加入前序池（供后续段回指）
        for ent in extract_declared_entities(text):
            if not any(k["name"] == ent["name"] for k in known):
                known.append(ent)
    return out


def enrich_segments(seg_texts: list[str]) -> list[str]:
    """入口：给含回指的后段拼接上下文块。返回与输入等长的增强文本列表。"""
    ctxs = build_segment_context(seg_texts)
    return [
        (ctx + text) if ctx else text
        for ctx, text in zip(ctxs, seg_texts)
    ]


__all__ = ["build_segment_context", "enrich_segments", "extract_declared_entities"]
