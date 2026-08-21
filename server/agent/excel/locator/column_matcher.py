"""列名匹配器：自然语言 → Excel 列名。

策略：
  1. 先查 YAML skill 词典（column_aliases.yaml），精确命中
  2. 精确子串匹配（用户输入完整出现在表头中，或反过来）
  3. 否则用余弦相似度 + Jaccard 混合评分在表头候选列上找最佳匹配

不再使用 _DEFAULT_DICT 硬编码；列别名全部由 column_aliases.yaml 提供。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

try:
    from rapidfuzz import fuzz as _rfz
    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False
    _rfz = None

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """中英混合分词。中文按单字+双字bigram切，英文/数字按单词切。

    中文除单字外增加相邻双字bigram提升短列区分度。
    "神通描述" -> ["神","通","描","述","神通","通描","描述"]
    """
    if not text:
        return []
    tokens = []
    for chunk in _CJK_RE.findall(text):
        if re.fullmatch(r"[A-Za-z0-9_]+", chunk):
            tokens.append(chunk.lower())
        else:
            tokens.extend(chunk)
            if len(chunk) >= 2:
                for j in range(len(chunk) - 1):
                    tokens.append(chunk[j:j + 2])
    return tokens


def _bow(tokens: Iterable[str]) -> dict[str, int]:
    """词袋模型。"""
    v: dict[str, int] = {}
    for t in tokens:
        v[t] = v.get(t, 0) + 1
    return v


def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
    """余弦相似度。"""
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in a.keys() & b.keys())
    import math
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a: frozenset, b: frozenset) -> float:
    """Jaccard 相似度：|A&B| / |A|B|。短输入区分度更好。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clean_header(h: str | None) -> str:
    """清洗表头：去类型后缀(:type)、去换行尾注(\\n...)、去内联括号注释(（...）)、strip。

    真实表头常含换行备注（如 '编号\\n（按序递增，不要分段）'）或类型标注
    （如 'prefab_id:int' / '3006: 对话ID'），或内联括号说明（如
    '获得奖励1（多个奖励用英文逗号隔开）'），直接匹配会因尾部噪声导致
    短键（如 '编号'）因长度差过大被 phase2 短输入约束拒掉，或 alias 目标
    '获得奖励1' 无法精确命中完整表头。取 ':' / '\\n' / '（' / '(' 之前的主名。
    """
    if not h:
        return ""
    s = str(h)
    s = s.split(":")[0] if ":" in s else s
    s = s.split("\n")[0]
    # 内联括号注释（全角（ / 半角(）：取括号前主名
    for paren in ("（", "("):
        if paren in s:
            s = s.split(paren)[0]
    return s.strip()


@dataclass
class ColumnMatch:
    column: str
    score: float
    index: int
    source: str  # "dict" | "exact_substr" | "similarity"
    semantic_warning: str = ""  # D3: 语义校验提示（int 列拒字符串等），供 agent 决策


class ColumnMatcher:
    """列名匹配器。"""

    _SHORT_INPUT_THRESHOLD = 0.3
    _DEFAULT_THRESHOLD = 0.05

    def __init__(self, headers: list[str],
                 yaml_aliases: dict[str, dict[str, str]] | None = None,
                 short_forms: dict[str, str] | None = None):
        self.headers = [str(h) if h is not None else "" for h in headers]
        self.yaml_aliases = yaml_aliases or {}
        # 短形式反向映射 {short_form: real_col}，命中时扩展为真实列名变体再匹配
        self.short_forms = short_forms or {}
        self._header_bows = [self._header_bow(h) for h in self.headers]
        self._header_tokens = [frozenset(_tokenize(_clean_header(h))) for h in self.headers]

    @staticmethod
    def _header_bow(header: str) -> dict[str, int]:
        name = _clean_header(header)
        return _bow(_tokenize(name))

    def match(self, nl: str) -> ColumnMatch | None:
        """三阶段列名匹配（带短形式扩展）。

        先把输入按短形式库扩展为变体（如"名"→["名","名称"]），
        再对每个变体跑阶段1/2/3，取最高分。

        阶段1: YAML别名精确匹配 (source="dict")
        阶段2: 精确子串匹配——用户输入完整出现在表头或其反向；
                短输入(≤2字符)要求列名长度接近(容差≤1)
        阶段3: 混合相似度——余弦(0.6) + Jaccard(0.4)，短输入更高阈值
        """
        if not nl:
            return None
        key = nl.strip()
        best: ColumnMatch | None = None
        for v in self._expand_variants(key):
            m = self._match_one(v)
            if m is not None and (best is None or m.score > best.score):
                best = m
        return best

    def _expand_variants(self, key: str) -> list[str]:
        """短形式扩展：key 命中短形式库则追加真实列名变体。"""
        variants = [key]
        real = self.short_forms.get(key)
        if real and real not in variants:
            variants.append(real)
        return variants

    def _match_one(self, key: str) -> ColumnMatch | None:
        """单变体的三阶段匹配（阶段1别名/阶段2子串/阶段3相似度）。"""
        if not key:
            return None

        # 阶段 1：YAML 别名精确命中（G11 修复：精确优先，避免 endswith 过宽误命中）
        # 先扫 alias == key 精确命中（如 option_function.data.1.reward_id 精确匹配），
        # 再扫 key.endswith(alias)/alias.endswith(key) 兜底。
        # 原行为单趟遍历遇到 alias='id' 时 key.endswith('id') 先于精确长 alias 命中，
        # 导致 option_function.data.1.reward_id 误命中全局 id→编号 主键列。
        for alias, col_name in self.yaml_aliases.items():
            if alias == key:
                for idx, h in enumerate(self.headers, start=1):
                    h_name = _clean_header(h)
                    if h_name == col_name or col_name == (h or ""):
                        return ColumnMatch(column=h, score=1.0, index=idx, source="dict")
        # 精确未命中 → endswith 兜底（短 alias 如 id/名 仍可命中长 key）
        for alias, col_name in self.yaml_aliases.items():
            if key.endswith(alias) or alias.endswith(key):
                for idx, h in enumerate(self.headers, start=1):
                    h_name = _clean_header(h)
                    if h_name == col_name or col_name == (h or ""):
                        return ColumnMatch(column=h, score=1.0, index=idx, source="dict")

        # 阶段 2：精确子串匹配（取最长子串匹配）
        key_l = key.lower()
        best_h, best_h_name = None, ""
        best_len = 0
        for idx, h in enumerate(self.headers, start=1):
            if not h:
                continue
            h_name = _clean_header(h)
            h_l = h_name.lower()
            if not h_l or not key_l:
                continue
            if key_l in h_l or h_l in key_l:
                match_len = min(len(key_l), len(h_l))
                # 短输入（≤2字符）要求长度相当
                if len(key_l) <= 2:
                    if abs(len(key_l) - len(h_l)) <= 1 and match_len > best_len:
                        best_h, best_h_name, best_len = h, h_name, match_len
                elif match_len > best_len:
                    best_h, best_h_name, best_len = h, h_name, match_len
        if best_h is not None:
            return ColumnMatch(column=best_h, score=0.90, index=self.headers.index(best_h) + 1, source="exact_substr")

        # 阶段 3：语义加权匹配
        # rapidfuzz 多算法混合（WRatio + token_set + partial）替换纯 cosine+Jaccard，
        # 覆盖口语化/长句指代优于字符串统计；rapidfuzz 不可用回退 cosine+Jaccard。
        q_set = frozenset(_tokenize(key))
        input_token_count = len(q_set)
        threshold = self._SHORT_INPUT_THRESHOLD if input_token_count <= 3 else self._DEFAULT_THRESHOLD

        best_idx, best_score = -1, 0.0
        source = "similarity"
        if _HAS_RAPIDFUZZ:
            for i, h in enumerate(self.headers):
                if not h:
                    continue
                h_name = _clean_header(h)
                wr = _rfz.WRatio(key, h_name)
                ts = _rfz.token_set_ratio(key, h_name)
                pr = _rfz.partial_ratio(key, h_name)
                combined = (wr * 0.5 + ts * 0.3 + pr * 0.2) / 100.0
                if combined > best_score:
                    best_score, best_idx = combined, i
            source = "rapidfuzz"
        else:
            q_bow = _bow(_tokenize(key))
            for i, hb in enumerate(self._header_bows):
                cos_sim = _cosine(q_bow, hb)
                jac_sim = _jaccard(q_set, self._header_tokens[i])
                combined = cos_sim * 0.6 + jac_sim * 0.4
                if combined > best_score:
                    best_score, best_idx = combined, i

        if best_idx < 0 or best_score < threshold:
            return None
        return ColumnMatch(
            column=self.headers[best_idx],
            score=round(best_score, 4),
            index=best_idx + 1,
            source=source,
        )

    def match_topk(self, nl: str, k: int = 5,
                   min_score: float = 0.30) -> list[ColumnMatch]:
        """返回 topK 列名匹配候选（按 score 降序），不提前收敛单值。

        复用 _match_one 的三阶段打分，但对每个 header 都保留得分而非取 best。
        用途：Step1 列名提取阶段——用户输入"活动类型"等列名 token 时，topK 反查
        该列出现在哪些表/sheet，据此收敛候选表（修案例三 spirit 误路由）。
        与 _match_one 的差异：_match_one 命中 phase1/phase2 即 return 单值；
        本方法扫完全部 header 取前 K，覆盖多候选并列场景。

        Args:
            nl: 自然语言片段（如"活动类型"/"名称"/"法宝描述"）
            k: 返回候选数上限
            min_score: 最低得分阈值，低于则丢弃
        Returns:
            按 score 降序的 ColumnMatch 列表（可能为空）
        """
        if not nl or k <= 0:
            return []
        key = nl.strip()
        if not key:
            return []
        # 阶段1：YAML 别名精确命中（直接置顶，score=1.0），命中即收
        alias_hits: list[ColumnMatch] = []
        for alias, col_name in self.yaml_aliases.items():
            if alias == key or key.endswith(alias) or alias.endswith(key):
                for idx, h in enumerate(self.headers, start=1):
                    h_name = _clean_header(h)
                    if h_name == col_name or col_name == (h or ""):
                        alias_hits.append(ColumnMatch(
                            column=h, score=1.0, index=idx, source="dict"))
        if alias_hits:
            alias_hits.sort(key=lambda m: m.score, reverse=True)
            if alias_hits[0].score >= 1.0:
                return alias_hits[:k]

        # 阶段2+3：逐 header 打分，收集 topK
        key_l = key.lower()
        scored: list[ColumnMatch] = list(alias_hits)
        q_set = frozenset(_tokenize(key))
        input_token_count = len(q_set)
        threshold = self._SHORT_INPUT_THRESHOLD if input_token_count <= 3 else self._DEFAULT_THRESHOLD
        threshold = max(threshold, min_score)
        for idx, h in enumerate(self.headers, start=1):
            if not h:
                continue
            h_name = _clean_header(h)
            h_l = h_name.lower()
            if not h_l:
                continue
            # 阶段2 子串
            s2 = 0.0
            if key_l in h_l or h_l in key_l:
                s2 = 0.90
            # 阶段3 语义
            if _HAS_RAPIDFUZZ:
                wr = _rfz.WRatio(key, h_name)
                ts = _rfz.token_set_ratio(key, h_name)
                pr = _rfz.partial_ratio(key, h_name)
                s3 = (wr * 0.5 + ts * 0.3 + pr * 0.2) / 100.0
            else:
                q_bow = _bow(_tokenize(key))
                cos_sim = _cosine(q_bow, self._header_bows[idx - 1])
                jac_sim = _jaccard(q_set, self._header_tokens[idx - 1])
                s3 = cos_sim * 0.6 + jac_sim * 0.4
            score = max(s2, s3)
            if score >= threshold:
                scored.append(ColumnMatch(
                    column=h, score=round(score, 4), index=idx,
                    source="exact_substr" if s2 >= s3 and s2 > 0 else "similarity"))
        scored.sort(key=lambda m: m.score, reverse=True)
        # 同 column 去重保留最高分
        seen_cols: set[str] = set()
        out: list[ColumnMatch] = []
        for m in scored:
            if m.column in seen_cols:
                continue
            seen_cols.add(m.column)
            out.append(m)
            if len(out) >= k:
                break
        return out

    def match_best(self, nl: str, value=None, stem: str = "", sheet: str = "",
                   col_type_fn=None, enum_resolver=None) -> ColumnMatch | None:
        """多分词候选匹配：对输入做分词生成多个候选串，每个各 match 一次，取最高分。

        解决单一噪声串匹配失败问题（如"属性这一"→剥离"这一"后用"属性"命中）。
        命中多个候选时选 score 最高者；同分保留更早（更可信）的候选。

        D3 语义校验（value + col_type_fn + enum_resolver 提供时）：
          1. int 列 + 字符串值 → 降权 0.5（score *= 0.5）
          2. int 列 + 字符串值 + 枚举映射命中 → 不降权
          3. str 列 + int 值 → 不降权
          4. 主名称列 + 值含中文且非枚举 → 提权 1.2
        返回的 ColumnMatch.semantic_warning 附提示供 agent 决策。
        """
        if not nl:
            return None
        from ..parser.segmenter import candidate_terms
        cands = candidate_terms(nl) or [nl.strip()]
        best: ColumnMatch | None = None
        for c in cands:
            m = self.match(c)
            if m is not None and (best is None or m.score > best.score):
                best = m
        if best is None:
            return None
        # D3 语义校验（需 value + col_type_fn + stem/sheet 上下文）
        if value is not None and col_type_fn is not None and stem and sheet:
            best = self._apply_semantic_check(
                best, value, stem, sheet, col_type_fn, enum_resolver)
        return best

    @staticmethod
    def _apply_semantic_check(m: "ColumnMatch", value, stem: str, sheet: str,
                              col_type_fn, enum_resolver) -> "ColumnMatch":
        """D3: 对最佳候选做 key↔value 语义校验，调整 score + 设 semantic_warning。"""
        col_name = _clean_header(m.column)
        try:
            ct = (col_type_fn(stem, sheet, col_name) or "").lower()
        except Exception:
            ct = ""
        sv = str(value).strip() if value is not None else ""
        is_int_val = False
        try:
            int(sv)
            is_int_val = True
        except (ValueError, TypeError):
            is_int_val = False
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", sv))
        enum_hit = False
        if enum_resolver is not None and ct in ("int", "integer") and not is_int_val:
            try:
                enum_hit = enum_resolver.resolve_label(stem, sheet, col_name, sv) is not None
            except Exception:
                enum_hit = False
        is_name_col = any(k in col_name for k in ("名字", "名称", "Name", "name")) \
            and ct in ("str", "string", "")
        warning = ""
        # 1. int 列 + 字符串值 → 降权 0.5（枚举命中不降权）
        if ct in ("int", "integer") and not is_int_val and not enum_hit:
            m.score = round(m.score * 0.5, 4)
            warning = f"int 列[{col_name}]不接受字符串值'{sv}'，已降权"
        # 4. 主名称列 + 值含中文且非枚举 → 提权 1.2
        if is_name_col and has_cjk and not enum_hit:
            m.score = round(min(m.score * 1.2, 1.0), 4)
        m.semantic_warning = warning
        return m
