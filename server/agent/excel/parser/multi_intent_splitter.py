"""通用多指令拆分器：将含多个独立操作的自然语言拆分为多条单指令。

与 cross_table_splitter 的分工：
  - cross_table_splitter：处理"单指令多表"（如进化链：一条指令涉及 pet+pet_evolve）
  - multi_intent_splitter：处理"多指令"（如"查A并改B再加C删D"：四条独立指令）

作为 parse_multi(LLM) 的规则化快速前置路径：
  - 命中多指令 → 各段独立 parse（单指令 LLM 调用，比 parse_multi 巨型调用快且不超时）
  - 单指令 → 交还 parse_multi / parse 正常路径

拆分策略：
  1. 引号保护：引号（""''「」）内的内容视为整体，不在内部切分
  2. 一次切分：按分隔符（句末标点 / 连接词 / 换行，仅引号外生效）
  3. 二次切分：段内含多个动作关键词时，按动作边界再切
  4. 动作识别：get/set/add/delete/unknown
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# 引号对（成对识别，内部内容不切分）
# 含中文全角括号（）【】：括号内常为复合实体/编号（如"宝箱（普通）"、"技能【破甲】"），
# 内部分隔符不应切断语义边界
_QUOTE_PAIRS = [
    ('"', '"'), ("'", "'"), ("「", "」"), ("“", "”"), ("‘", "’"),
    ("（", "）"), ("【", "】"),
]

# 分隔符（按优先级）：句末标点 > 连接词 > 换行 > 编号列表
# 编号列表（1. 2. 3. 或 一、 二、 三、）：用户用编号组织多指令时，按编号边界切分
# 前瞻断言：编号+标点后须紧跟空白+非数字（避免 "1.5" 小数、"(5000,10)" 括号误切）
#   1.5      → 不切（1. 后是 5，非空白）
#   (5000,10)→ 不切（10) 后是 ，，非空白）
#   1. 新增  → 切（1. 后是 空格+中文）
# 第4组是捕获组（编号列表），供 cuts 逻辑识别切点位置（start vs end）
# §复杂多指令支持：补全策划口语连接词，避免"再配/以及/还有"等漏识别致整段不拆
# → 单超长 prompt → LLM 空返 → hard error（审查 P0-1）
_SEPARATORS = re.compile(
    r'([。；;])\s*'
    r'|(\n+)'
    # §P0-1 连接词边界：避免"BOSS 战斗也一起配上"中"一起配"被误切（"一起配上/一起配着"
    # 是动词延续"配上/配着"，非独立连接词）。加负向前瞻：连接词后须接动作词/实体名/数量
    # 才算分隔，排除"一起配上/配着/来"等动词延续子串。
    r'|(然后|接着|之后|并且|同时|另外|而后|随后|以及|还有|还要|顺带)\s*'
    r'|(再配|再建|再加|再设)\s*'
    r'|(一起配(?!上|着|来|去)|一起建)\s*'
    r'|((?:\d+|[一二三四五六七八九十])[\.、)])(?=\s+[^\d])\s*'
)

# 动作关键词 → action（顺序敏感：长词优先，避免"删除"被"除"误匹配）
# §补全策划口语动作词："配/建/造/设/给"等，避免"配NPC""建奖励包"无动作边界不拆段
# §低危补全：新增"创建/生成"（常见口语 add 同义词，原漏收致该分段 action=unknown，
# decompose 拿不到动作提示）。纯白名单追加，不影响既有匹配。
_ACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?:查看|查询|查一下|有哪些|是什么|看看|查找|搜索|列出|显示)'), 'get'),
    (re.compile(r'(?:修改|改成|改为|设置成|设置为|设为|更新|调整)'), 'set'),
    (re.compile(r'(?:新增|增加|添加|加一个|加个|新增一个|添加一个|配一个|配个|建一个|建个|造一个|给一个|配一下|建一下|创建|生成)'), 'add'),
    (re.compile(r'(?:删除|删除掉|去掉|移除|清除|删掉|清掉|清空)'), 'delete'),
]


# 序数词模式：第一个/第二个/第三个... 第N个
_ORDINAL_RE = re.compile(r'第[一二三四五六七八九十\d]+个')

# 编号列表前缀模式：1. 2. 3. 或 一、 二、 三、 等（行首或空格后）
# 用于区分"多指令编号列表"和"单实体多列序数"
_NUMBERED_LIST_PREFIX_RE = re.compile(r'(?:^|\s)(?:\d+|[一二三四五六七八九十])[\.、)]\s')


def _has_ordinal_same_target(text: str) -> bool:
    """检测文本是否含"配一个X，第一个...第二个..."的同列序数结构。

    这类文本语义上是单行多列写入（如秘法池：秘法1/概率1/秘法2/概率2），
    不应按分号拆成多行。判定条件：
      1. 含"一个/新增一个/配一个"+ 实体名
      2. 含 >=2 个序数词（第一个/第二个...）
    """
    ordinals = _ORDINAL_RE.findall(text)
    if len(ordinals) < 2:
        return False
    # 须含"一个"或"配/新增"+ 数量词，表明是单实体多属性配置
    if not re.search(r'(?:一个|新增一个|配一个|添加一个|加一个)', text):
        return False
    return True


def _has_numbered_list_prefix(text: str) -> bool:
    """检测文本是否含编号列表前缀（1. 2. 3. 等多指令编号）。

    含编号列表 → 是多指令复合输入，序数词在各子段内处理，
    整段不应被序数保护折叠（否则多条指令被合并成一段）。
    """
    return bool(_NUMBERED_LIST_PREFIX_RE.search(text))


def _ordinal_protect_ranges(text: str) -> list[tuple[int, int]]:
    """计算序数同列保护区间：第一个序数词起始到最后一个序数词结束。

    该区间内的分隔符（分号/逗号）是同列属性分隔，不应作为指令切分点。
    仅当文本含"一个"+实体 + >=2 序数词时返回非空区间。
    """
    if not _has_ordinal_same_target(text):
        return []
    matches = list(_ORDINAL_RE.finditer(text))
    if len(matches) < 2:
        return []
    return [(matches[0].start(), matches[-1].end())]


def _quote_ranges(text: str) -> list[tuple[int, int]]:
    """计算引号/括号保护区间（已合并重叠、已排序）。

    引号对（含中文全角括号（）【】）内内容不切分。序数同列区间也并入。
    供 _split_respecting_quotes 与 _split_by_action_boundary 共用。
    """
    if not text:
        return []
    quote_ranges: list[tuple[int, int]] = []
    for oq, cq in _QUOTE_PAIRS:
        i = 0
        while i < len(text):
            if text[i] == oq:
                j = text.find(cq, i + 1)
                if j > i:
                    quote_ranges.append((i, j + 1))
                    i = j + 1
                    continue
            i += 1
    quote_ranges.extend(_ordinal_protect_ranges(text))
    quote_ranges.sort()
    merged: list[tuple[int, int]] = []
    for s, e in quote_ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _split_respecting_quotes(text: str, pattern: re.Pattern) -> list[str]:
    """按 pattern 切分 text，但跳过引号内 + 序数同列区间内的内容（均不切分）。

    引号内的内容（如对话文本"欢迎光临，要不要看看？"）含逗号/句号时不应被切断，
    否则破坏语义边界。本函数扫描 text，仅在引号外应用分隔符匹配。

    序数同列区间内的分隔符（如"第一个X；第二个Y"的分号）是同列属性分隔，
    不应切断（否则单行多列写入被拆成多行，主键重复、数据残缺）。
    """
    if not text:
        return []
    merged = _quote_ranges(text)

    def _in_quote(pos: int) -> bool:
        for s, e in merged:
            if s <= pos < e:
                return True
            if s > pos:
                break
        return False

    # 找所有分隔符匹配，仅保留引号外的
    # 切点位置规则：
    #   组1（句末标点 。；;）/组2（换行）：m.end()（标点本身是句末符号，前段保留）
    #   组3（连接词 然后/并且等）/组4（编号列表 1. 2.）：m.start()（连接词/编号归下段开头，供 strip 去除）
    cuts: list[int] = []
    for m in pattern.finditer(text):
        if not _in_quote(m.start()):
            if m.lastindex in (3, 4):
                cuts.append(m.start())
            else:
                cuts.append(m.end())
    if not cuts:
        return [text]
    # 按 cuts 切分
    pieces: list[str] = []
    prev = 0
    for c in cuts:
        piece = text[prev:c].strip()
        if piece:
            pieces.append(piece)
        prev = c
    if prev < len(text):
        tail = text[prev:].strip()
        if tail:
            pieces.append(tail)
    return pieces


@dataclass
class SplitSegment:
    """拆分后的单指令段。

    Attributes:
        text: 单指令文本（供 parser.parse 进一步解析为 NLIntent）
        action: 动作类型 get/set/add/delete/unknown
    """
    text: str
    action: str


def _detect_action(text: str, route: Optional[dict] = None) -> str:
    """识别文本的动作类型。返回 get/set/add/delete/unknown。

    §系统性重构 Phase1：route.ok=True 且 action 合法时采信 LLM 判断，
    跳过动作词白名单正则（同义词覆盖不全，如"放/摆/搁"等口语词漏判）。
    route 缺失/非法时走原正则兜底。
    """
    if route and route.get("ok"):
        act = route.get("action")
        if act in ("get", "set", "add", "delete", "unknown"):
            return act
    for pat, action in _ACTION_PATTERNS:
        if pat.search(text):
            return action
    return "unknown"


def _route_applies(candidate: str, original: str) -> bool:
    """route 是否适用于 candidate 文本（candidate 与整句 original 语义等同，
    仅相差首尾空白/句末标点）。

    route 由整句 LLM 分类产出，只能安全套用到"内容上就是整句"的 segment
    （单段场景，或多段切分后仍是唯一段）；真正被切成多个独立子段时，
    每段语义可能与整句分类结果不同，不应盲目套用同一个 route（保持规则判定，
    避免多段场景下 LLM 误分类被错误放大到不相关的子段）。
    """
    def _norm(s: str) -> str:
        return (s or "").strip().rstrip("。;；").strip()
    return bool(candidate) and _norm(candidate) == _norm(original)


def _split_by_action_boundary(seg: str) -> list[str]:
    """段内含多个动作关键词时，按动作边界再切。

    例："查看灵兽饕餮的属性并修改它的攻击为100"
       → ["查看灵兽饕餮的属性", "修改它的攻击为100"]

    单行多列保护：若所有动作关键词同属 set（如"把X的A改成1，B改成2，C改成3"），
    且相邻动作间仅用逗号/顿号分隔（无句号/分号/连接词），判定为同一目标实体的
    多列修改 → 不切，整段作一条 set 意图（fields 多列）。否则按动作边界切。
    跨实体多 set（如"修改A然后修改B"）因含连接词仍会切。
    """
    # 收集 (start, end, action) 三元组，跳过引号/括号内的动作词
    # （如"去掉（改成空列表）"括号内"改成"是补述，不作为切点）
    merged = _quote_ranges(seg)

    def _in_q(pos: int) -> bool:
        for s, e in merged:
            if s <= pos < e:
                return True
            if s > pos:
                break
        return False

    span_acts: list[tuple[int, int, str]] = []
    for pat, action in _ACTION_PATTERNS:
        for m in pat.finditer(seg):
            if _in_q(m.start()):
                continue
            span_acts.append((m.start(), m.end(), action))
    if len(span_acts) <= 1:
        return [seg]
    span_acts.sort()

    # 级联说明保护：段含"："(中文冒号引出补充说明，如"删X，连带清它的关联：把A去掉，
    # 删B，删C")时，冒号后多动作是对同一主操作的级联展开，语义上是单一复合意图，
    # 交 LLM decompose_segment 内部拆多 op 更准。规则切会把谓语动词(句末"删掉。")
    # 当切点切成残段。
    if "：" in seg:
        colon_idx = seg.index("：")
        # 冒号后存在 ≥2 个动作词 → 级联展开，整段不切
        post = span_acts[1:]  # 冒号后的动作词（粗略：取第2个起）
        post_after_colon = [s for s in post if s[0] > colon_idx]
        if len(post_after_colon) >= 2:
            return [seg]

    # 单行多列 set 保护：全 set + 相邻动作间仅逗号/顿号（含尾随空格）
    acts = {a for _, _, a in span_acts}
    if acts == {"set"}:
        only_comma_sep = True
        for i in range(len(span_acts) - 1):
            gap = seg[span_acts[i][1]:span_acts[i + 1][0]]
            # 允许：逗号、顿号、空格、数量值（0-9.%等残留）、及"X 描述"等列名文字
            # 不允许：句号分号(应已被一次切分切开)、连接词(然后/并且等引新实体)
            # §复用扩充后的连接词集（再配/以及/还有等），与 _SEPARATORS 一致
            if re.search(r'[。；;]|然后|接着|之后|并且|同时|另外|而后|随后|再配|再建|再加|再设|以及|还有|还要|顺带|一起配|一起建', gap):
                only_comma_sep = False
                break
        if only_comma_sep:
            return [seg]

    # 按动作起始位置去重排序，作为切点
    cuts = sorted({sp[0] for sp in span_acts})
    pieces: list[str] = []
    prev = 0
    for cut in cuts:
        if cut > prev:
            piece = seg[prev:cut].strip().rstrip('，,、并且然后接着而再配以及还有顺带')
            if piece:
                pieces.append(piece)
        prev = cut
    if prev < len(seg):
        piece = seg[prev:].strip()
        if piece:
            pieces.append(piece)
    return pieces or [seg]


def split_multi_intent(text: str, route: Optional[dict] = None) -> list[SplitSegment]:
    """拆分多指令文本 → 独立指令段列表。

    单指令返回 [单段]；空输入返回 []。
    每段含 text + action，供上层各段独立解析执行。

    跨表模式保护：整句若命中 detect_cross_table_action（NPC/进化链等），
    不拆分，整句返回单段交 cross_table_splitter 处理任务链。
    "同时/然后"切分后的子段也各过跨表检测，命中则该子段不进一步按动作切。

    §系统性重构 Phase1：route（LocatorAgent._llm_classify_route 产出的 LLM
    分类结果）只在"候选 segment 语义等同整句"时套用（见 _route_applies），
    真正被切成多个独立子段时仍用规则判定（LLM 分类是整句粒度，不能盲目下放
    到每个子段）。route=None 时行为与重构前完全一致。

    Args:
        text: 用户自然语言输入（可能含多个操作指令）
        route: 可选，整句 LLM 路由分类结果（cross_table_type/action 等）

    Returns:
        SplitSegment 列表。单指令或跨表模式时长度为 1。
    """
    if not text or not text.strip():
        return []
    text = text.strip()

    # §P0-0 修分段折叠（真根因）：原此处 _has_ordinal_same_target 整段早返回，
    # 把"配一个NPC，第一个选项X，第二个选项Y"这类对话链误判为"单行多列"折叠成 1 段
    # （ordinal 在对话链里指选项序号，非同列属性）。整段 700+ 字塞给单次 LLM decompose
    # → schema 过大 → LLM 退化灌值 → 全盘失败。
    # 修复：删整段早返回。ordinal 同列保护改由段内 _split_by_action_boundary 的
    # 单行多列 set 保护覆盖（已切好的单段内多逗号分隔 set 才保护，跨表链不保护）。
    # 这样对话链按分隔符正常切成多段，每段 1-2 表小 schema，LLM 不退化。

    # A. 前置跨表检测：整句命中跨表模式时不再整句折叠。
    # G7 修复：原行为命中跨表即 return 单段，导致同句内非跨表子句（如"修改reward名称"）
    # 被吞进跨表段无法独立拆分（task_chain 用例4 新增NPC+修改reward 混合句失败）。
    # 新行为：命中跨表仍按分隔符切分，跨表子段标记 cross_table 保持完整交 splitter，
    # 非跨表子段正常走动作边界拆分。整句无分隔符（纯跨表单指令）时才折叠单段。
    try:
        from ..core.cross_table_splitter import detect_cross_table_action
        has_cross = detect_cross_table_action(text, route=route)
    except Exception:
        has_cross = False

    # §A（framework 提升，去硬编码拟合）：原 _is_task_chain 用业务词"任务链"+"最后"/
    # "指向…新建"判整段折叠成单段 cross_table → force_single → force_grouped →
    # 大 prompt ≈19.6KB → serve 6×90s 全超时空返（实测 raw=0, 90→90.7s/180→180.5s
    # 翻倍仍卡）→ LLM 路径全空 → splitter_baseline 兜底产 5 碎片 → 漏 7 表 + 名称/描述
    # 缺失（baseline 正则提不到中文）。根因即此早返回把整链塞进单 prompt 触发 serve 病灶。
    # 判据本身违反"通用判据不绑业务词"原则。删之，走下方通用分隔符切段 → 每段小 prompt
    # （decompose_segment 实测：87 字段段产 2 条字段齐全 intent）+ 段级并发，避 serve 超时。
    # 跨段 produces/consumes 不靠"段看到前文"（切段时前文已切走），靠已有基础设施：
    #   1. 段级 locate FK 扩表 —— "指向上面新建的战斗"段仍命中 combat（FK 边扩）
    #   2. prompt 占位符命名规范 new_<stem>_id —— 各段独立产 produces/consumes 标签一致
    #   3. _assemble 后置 infer_produces_consumes —— 按 RelationGraph FK 边连跨段占位符

    # 一次切分：按分隔符（引号保护，不在引号内切分）
    raw_segs = _split_respecting_quotes(text, _SEPARATORS)
    if not raw_segs or (len(raw_segs) == 1 and raw_segs[0] == text):
        # §P0-1 补：无分隔符但含多动作词（"配NPC，建奖励包"逗号分隔无连接词），
        # 一次切分当单段返回会漏二次切分。补：单段含≥2动作词时仍走动作边界切分。
        _n_acts = 0
        for pat, _ in _ACTION_PATTERNS:
            _n_acts += sum(1 for _ in pat.finditer(text))
        if _n_acts >= 2:
            pieces = _split_by_action_boundary(text)
            if len(pieces) > 1:
                # 按动作切出多段，各段走下方跨表检测+组装
                raw_segs = pieces
            else:
                # 单指令。跨表则标记 cross_table，否则按动作识别
                action = "cross_table" if has_cross else _detect_action(text, route=route)
                return [SplitSegment(text=text, action=action)]
        else:
            # 无分隔符：单指令。跨表则标记 cross_table，否则按动作识别
            action = "cross_table" if has_cross else _detect_action(text, route=route)
            return [SplitSegment(text=text, action=action)]

    # D. 子段跨表检测：命中跨表模式的子段保持完整，不按动作边界再切
    segments: list[SplitSegment] = []
    for seg in raw_segs:
        # 去掉段首编号前缀（1. 2. 3. 或 一、 二、 等）+ 段首连接词（然后/并且等，切分后残留）
        # §复用扩充后的连接词集（再配/以及/还有等），与 _SEPARATORS 一致
        # §P0-1 同步：一起配(?!上|着|来|去) 边界（避免误清"一起配上"的"一起配"残留）
        seg = re.sub(r'^(?:(?:\d+|[一二三四五六七八九十])[\.、)]\s*|(?:然后|接着|之后|并且|同时|另外|而后|随后|再配|再建|再加|再设|以及|还有|还要|顺带|一起配(?!上|着|来|去)|一起建)\s*)', '', seg).strip()
        if not seg:
            continue
        _seg_route = route if _route_applies(seg, text) else None
        try:
            from ..core.cross_table_splitter import detect_cross_table_action as _d
            if _d(seg, route=_seg_route):
                segments.append(SplitSegment(text=seg, action="cross_table"))
                continue
        except Exception:
            pass
        # 二次切分：段内多动作 → 按动作边界再切
        pieces = _split_by_action_boundary(seg)
        for piece in pieces:
            piece = piece.strip()
            if piece:
                _piece_route = route if _route_applies(piece, text) else None
                segments.append(SplitSegment(
                    text=piece, action=_detect_action(piece, route=_piece_route)))

    segments = [s for s in segments if s.text]
    return segments if segments else [SplitSegment(text=text, action=_detect_action(text, route=route))]


def is_multi_intent(text: str) -> bool:
    """快速判定文本是否含多个独立指令（供 agent.run 决定是否走快速路径）。"""
    return len(split_multi_intent(text)) > 1
