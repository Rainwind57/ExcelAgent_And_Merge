"""校验 SubAgent:引用闭环校验 + 修正,对齐 produces/consumes 标签。

解决 R8g 引用一致 0.00 最后一层失配:
  ① LLM produces/consumes 标签与 _capture_produced/_resolve_placeholders 的
     _norm_name 归一不对齐(如 "new_pet_id" vs "new_pet" 风格漂移)
  ② LLM 过产(每表产多 op 或 mail 3 intent)
  ③ eval consumer 字段读取漏(item 0.00 bug)

职责边界:
  - 输入: list[SplitIntent] + LocatorResult(FK 边供校验)
  - 输出: {ok, issues, fixes, intents}(修正后 intents)
  - 安全网: 失败时返回原 intents 不变(produces_inference 作兜底)

规则层校验为主(类型/required/unique 走 PipelineVerifier),本 Agent 专注:
  produces 闭环 + 字段一致性 + 抑制过产。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from .base import SubAgent
from .llm_agent import LLMSubAgent
from .locator_agent import LocatorResult, FKEdge
from ..parser.nl_parser import Issue, IssueType, ValidationResult, assemble_tips

logger = logging.getLogger(__name__)


def _norm_name(s: str) -> str:
    """归一标签名:对齐 produces_inference._norm + orchestrator._norm_name 风格。

    去尖括号/空白/下划线后缀/大小写,供 produces 标签与 consumes 占位符匹配。
    """
    if not s:
        return ""
    s = str(s).strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    s = s.split(":")[0]
    return re.sub(r"\s+", "", s).lower()


def _norm_col_name(s) -> str:
    """剥离表头注释段，取纯列名。

    表头常带注释（枚举/类型/说明），如：
      - 配方类型：\\n1：炼器\\n2：炼丹  → 配方类型（全角冒号+换行注释）
      - recipe_type:int                 → recipe_type（半角冒号注释）
      - 编号 （按序递增，不要分段）       → 保留原样（无分隔符）
    原代码仅 split(":")（半角），对全角"："和换行"\\n"注释不生效，导致 LLM 产
    裸列名「配方类型」匹配不到带注释的真实表头「配方类型：\\n1：炼器\\n2：炼丹」，
    误报 COL_NOT_FOUND。此处统一按 半角:/全角：/换行 任一作为注释起始切分。
    """
    if not s:
        return ""
    return re.split(r'[:\uff1a\n\r]', str(s))[0].strip()


def _is_id_col(col_name: str) -> bool:
    """ID/编号 列启发式（与 TableAgent._is_id_column 一致,供 O4 字段层段校验触发）。

    只匹配「id 结尾」和「编号 开头」两种形态：
      - prefab_id/item_id → id$ 命中
      - 编号\n（按序递增…） → ^编号 命中
    「交互效果编号」「技能编号」等「XXX编号」（编号作修饰后缀的枚举/普通列）
    不含 id、不以编号开头 → 不命中，避免把枚举列误判为主键唯一列。
    """
    if not col_name:
        return False
    name = _norm_col_name(col_name).lower()
    return bool(re.search(r"(^|_)(id)$|id$|^编号", name))


def _norm_col(c) -> str:
    """列名归一：去类型/注释后缀 + 空白 + 小写。复合键成员比对用。"""
    return _norm_col_name(c or "").lower()


def _is_any_placeholder(fields: dict, pk_norm_set: set) -> bool:
    """复合 PK 任一成员列值为占位符/空 → 跳过组合检测（交拓扑回填或留空）。"""
    if not fields or not pk_norm_set:
        return True
    for k, v in fields.items():
        if _norm_col(k) not in pk_norm_set:
            continue
        s = str(v).strip() if v is not None else ""
        if not s or s == "<auto>" or (s.startswith("<") and s.endswith(">")):
            return True
    return False


def _label_from_consumes(value) -> Optional[str]:
    """从 consumes 占位符值提取标签名:<new_pet_id> → "new_pet_id"。"""
    if value is None:
        return None
    s = str(value).strip()
    if s.startswith("<") and s.endswith(">") and len(s) >= 2:
        return s[1:-1].strip()
    return None


class ValidatorAgent(LLMSubAgent):
    """校验 Agent:produces 闭环 + 抑制过产 + 修正。

    规则为主,LLM 仅在规则无法判定时裁决(如语义层引用合理性)。
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("ValidatorAgent", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="引用闭环校验 + 修正")
        self._cli = cli
        # §4.5 交互反问通道：agent_service 注入 agent._ask_callback 时同步注入 validator
        # （或 agent __init__ validator 时传入）。None 时 ask_user 降级 skip（非交互场景/CI）。
        self._ask_callback = None
        # §4.1 ③ 必填性：required_fields.yaml 懒加载缓存（#30,当前空,配置填充后生效）
        self._required_fields = None
        # §P1-9 PK 列缓存：从 table_relations.json + rules/validate primary_key
        # 加载 {stem: {sheet: [pk_cols]}}（含复合主键）。替代旧单列 {stem: set}，
        # 旧模型无法表达 (法宝id, 法宝等级) 这类联合键。懒加载，首次 _get_pk_cols 时填。
        self._pk_cols_cache: dict = None

    def _load_pk_cols_cache(self) -> dict:
        """加载 {stem: {sheet: [pk_cols]}} 复合主键缓存（含单列主键）。

        优先级（后者覆盖前者）：
          1. table_relations.json 的 FK ``to_column`` 作为单列主键 fallback
             （仅作兜底，不可靠——如 fabao.FabaoLevel 被记成"技能id"而非"法宝id"）。
          2. ``rules/validate`` 的 sheet 级 ``primary_key`` 声明（硬规则，最可信）。

        缓存结构：``{stem_lower: {sheet_name: [col1, col2, ...]}}``
          - 单列主键 -> ``[col]``（行为等价旧 set，但保序、去重）。
          - 复合主键 -> ``[col1, col2]``，检测层按"组合值"判唯一/冲突。
          - 未声明该 sheet 时缺省为空 list（由消费者回退表头首列含 id 列兜底）。

        懒加载，首次 _get_pk_cols 调用时填充。
        """
        if self._pk_cols_cache is not None:
            return self._pk_cols_cache
        cache: dict = {}
        # 1) table_relations FK to_column 兜底（单列）
        try:
            from ..core.table_relations import RelationGraph
            rg = RelationGraph.load()
            for r in rg.relations:
                to_path = str(r.to_path).replace("\\", "/").rstrip("/")
                if to_path.endswith(".xlsx"):
                    to_path = to_path[:-5]
                to_stem = to_path.rsplit("/", 1)[-1].lower()
                col = (r.to_column or "").split(":")[0].strip()
                to_sheet = r.to_sheet or ""
                if to_stem and col:
                    sheets_map = cache.setdefault(to_stem, {})
                    cols = sheets_map.setdefault(to_sheet, [])
                    if col not in cols:
                        cols.append(col)
        except Exception:
            logger.debug("table_relations PK 列缓存加载失败", exc_info=True)
        # 2) rules/validate 的 primary_key 覆盖（硬规则，整列替换而非追加，
        #    避免 table_relations 的错误 to_column 污染复合键判定）
        try:
            from ..core.rules_loader import get_primary_key_overlay
            overlay = get_primary_key_overlay() or {}
            for stem_lower, sheets in overlay.items():
                if not isinstance(sheets, dict):
                    continue
                sheets_map = cache.setdefault(stem_lower, {})
                for sheet, cols in sheets.items():
                    sheets_map[sheet] = [c for c in (cols or [])]
        except Exception:
            logger.debug("rules primary_key overlay 加载失败", exc_info=True)
        self._pk_cols_cache = cache
        return cache

    def _get_pk_cols(self, intent, schema_getter=None) -> list:
        """取本 intent 目标 sheet 的主键列列表（复合/单列统一），消费者唯一入口。

        优先级：
          1. ``rules``/``table_relations`` 缓存（``_load_pk_cols_cache``）按
             (stem, sheet) 精确命中 -> 返回声明的复合/单列主键（新结构
             ``{stem:{sheet:[cols]}}``，旧结构 ``{stem:set(cols)}`` 也兼容单列）。
          2. 缓存未命中该 sheet -> 读表头，取【所有含 id/编号 的列】作复合候选
             （而非首列兜底，更能覆盖 (法宝id, 法宝等级) 这类联合键）。
          3. 表头无 id 列 -> 退回首列（惯例主键）。

        Args:
            schema_getter: 供 _get_schema 读表头时的 schema 注入器（可不传）。
        """
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").strip()
        cache = self._pk_cols_cache or self._load_pk_cols_cache() or {}
        # §兼容旧缓存结构 {stem: set([cols])}（单列主键，无 sheet 维度）
        if stem and isinstance(cache, dict):
            sheets_map = cache.get(stem)
            if isinstance(sheets_map, dict):
                cols = sheets_map.get(sheet)
                if cols is None:
                    for sn, cs in sheets_map.items():
                        if sn and sn.lower() == sheet.lower():
                            cols = cs
                            break
                if cols:
                    return [c for c in cols if c]
            elif isinstance(sheets_map, (set, list, tuple)):
                # 旧结构：整表单列 PK，不区分 sheet
                _legacy = [c for c in sheets_map if c]
                if _legacy:
                    return _legacy
        # 回退：读表头取所有 id/编号 列作复合候选
        try:
            _hdrs, _ = self._get_schema(intent, schema_getter)
            if _hdrs:
                id_cols = []
                for h in _hdrs:
                    if not h:
                        continue
                    name = str(h).split(":")[0].strip()
                    if not name:
                        continue
                    nl = name.lower()
                    if "id" in nl or "编号" in nl or "序号" in nl or "主键" in nl:
                        if name not in id_cols:
                            id_cols.append(name)
                if id_cols:
                    return id_cols
                # 无 id 列时首列兜底
                first = str(_hdrs[0] or "").split(":")[0].strip()
                return [first] if first else []
        except Exception:
            pass
        return []

    def _check_composite_unique(self, fields: dict, pk_cols: list,
                                existing_values: dict, stem: str = "",
                                sheet: str = "",
                                composite_existing: set = None) -> list:
        """复合主键组合唯一性检测（add only）。

        按 pk_cols 从 fields 取各列值组成组合键，查 composite_existing（整行组合值
        集合）严格定夺；无 composite_existing 时退化为列级弱判定（各列值都存在于
        各自 existing 集合）并标"疑似"，交 Step3 复合写盘校验最终定夺。

        组合重复才算 UNIQUE_VIOLATION；单列重复但组合不同（如 (法宝id=5, 法宝等级
        =1/2/3)）合法放行。

        返回 list[Issue]，空 = 无冲突。
        """
        out: list = []
        if not pk_cols or len(pk_cols) < 2 or not fields:
            return out
        # 取本 fields 各 PK 列值（列名大小写/后缀不敏感）
        pk_norm = [_norm_col(c) for c in pk_cols if c]
        col_vals: list = []  # [(norm_col, original_field_key, val)]
        for fk, fv in fields.items():
            nfk = _norm_col(fk)
            if nfk in pk_norm:
                col_vals.append((nfk, fk, fv))
        if len(col_vals) < 2:
            return out  # 复合键列未全列填值，交单列/必填逻辑处理
        combo = tuple(str(v).strip() if v is not None else "" for _, _, v in col_vals)
        combo_desc = ",".join(f"{fk}={v}" for _nfk, fk, v in col_vals)
        # 严格路径：composite_existing（整行组合值集合，schema_bundle 注入）
        if isinstance(composite_existing, (set, list, tuple)) and composite_existing:
            if combo in set(composite_existing):
                out.append(Issue(
                    col=",".join(pk_cols),
                    issue_type=IssueType.UNIQUE_VIOLATION.value,
                    expected=f"复合主键组合唯一（{combo_desc}）",
                    suggestion=f"组合「{combo_desc}」已存在,请改用其他组合或 modify",
                    value=combo_desc,
                ))
            return out
        # 退化路径：无 composite_existing，用列级 existing 集合做弱判定（保守报"疑似"
        # 不硬拦，交 Step3 _do_append 复合写盘校验最终定夺，避免误拦合法行）。
        col_existing: dict = {}
        for nfk, _fk, _v in col_vals:
            ev_set = None
            for k, v in existing_values.items():
                if _norm_col(k) == nfk and isinstance(v, (set, list, tuple)):
                    ev_set = {str(x) for x in v}
                    break
            if ev_set is None:
                return out  # 现有数据缺某 PK 列，无法判组合，放行交 Step3 兜底
            col_existing[nfk] = ev_set
        hit_each = all(combo[i] in list(col_existing.values())[i]
                       for i, (nfk, _fk, _v) in enumerate(col_vals))
        if not hit_each:
            return out
        out.append(Issue(
            col=",".join(pk_cols),
            issue_type=IssueType.UNIQUE_VIOLATION.value,
            expected=f"复合主键组合唯一（{combo_desc}）",
            suggestion=f"组合「{combo_desc}」疑似重复,请确认该行确为新组合",
            value=combo_desc,
        ))
        return out

    def _check_business_required_pre_add(self, intent, headers, fields, raw) -> list:
        """Pack 3：写前 business heuristic 必填列校验（agent.py:4561 同启发式前移）。

        策略：user_text 含引号(说明用户显式给了名字/描述值) 且列名含「名称/描述/名」
        字样的 string 列未在 LLM fields 提供值 → 报 MISSING_REQUIRED issue +
        mark intent.validation.skipped=True 让 Step3 跳写盘避免半成品落盘 + 隐瞒失败。

        与 P26 用户原则一致：非主键 MISSING_REQUIRED 仅 warning 上报，配合 skipped
        让 step4 induce_anti_patterns 标失败种；不强制走 _ask hard ask 循环（用户
        原则 "除主键缺失外不校验" 不变）。PK 自动分配列豁免（首列必非空时由
        _dedup_inter_pk_dup 等处理）。

        Returns:
            list[Issue]，空 = 无业务必填列缺失。
        """
        issues: list = []
        if not headers or not isinstance(fields, dict) or not fields:
            return issues
        try:
            if getattr(intent, "action", "") != "add":
                return issues
            raw_lower = (raw or "").lower()
            # 触发条件：指令含引号（用户显式给过名字/描述值）或含显式赋值关键词
            # （发送人/发送时间/奖励 等）。无此信号时用户可能本就没要求这些列，不报缺。
            quoted = any(q in (raw or "") for q in ("'", '"', "「", "」")) \
                or any(kw in (raw or "")
                       for kw in ("活动描述", "活动名称", "描述为", "名称为",
                                  "发送人", "发送时间", "有效期", "奖励",
                                  "开始时间", "结束时间", "图标", "邮件类型"))
            if not quoted:
                return issues
            written_norm = {(str(k) or "").split(":")[0].strip().lower()
                            for k in fields.keys() if k}
            # ① 名称/描述/名 类列：保持原 Pack3 契约——raw 含引号即视为用户给了
            #    名字/描述值，缺失即报（保守，宁可多报不可漏半成品）。
            name_kws = ("名称", "描述", "名")
            # ② 显式赋值列：raw 中出现列名（用户明确给了该列值）才报缺。
            explicit_kws = ("发送人", "发送时间", "时间", "奖励", "图标", "邮件类型",
                            "开始时间", "结束时间", "有效期")
            for h in headers:
                if not h:
                    continue
                name = str(h).split(":")[0].strip()
                if not name:
                    continue
                if name.lower() in written_norm:
                    continue
                _is_name_kw = any(kw in name for kw in name_kws)
                _is_explicit_kw = any(kw in name for kw in explicit_kws)
                if not _is_name_kw and not _is_explicit_kw:
                    continue
                # 显式赋值列需 raw 出现列名才报（防表头有「奖励」列但用户没提误报）
                if _is_explicit_kw and not _is_name_kw \
                        and name.lower() not in raw_lower:
                    continue
                issues.append(Issue(
                    col=name, issue_type=IssueType.MISSING_REQUIRED.value,
                    expected=f"业务必填列「{name}」（指令明确给出该列值，LLM 漏产）",
                    suggestion=f"补填 {name} 列值；或跳过让 Step4 induce_anti_patterns 标失败",
                ))
            if issues:
                self._mark_intent_skipped(intent)
        except Exception:
            logger.warning("_check_business_required_pre_add 失败", exc_info=True)
        return issues

    def _is_pk_like_col(self, col_clean: str, stem: str = "",
                        headers: list = None, sheet: str = "") -> bool:
        """PK 列判定（P1-9 读真实元数据，支持复合主键）。

        优先级：① table_relations/rules 声明的 PK 列（按 stem+sheet 精确命中）
                ② _is_id_col 启发式（含 id/编号 子串）
                ③ 表第一列（惯例主键）
        """
        if not col_clean:
            return False
        col_lower = col_clean.lower()
        # ① 真实 PK 列（复合键缓存，按 stem+sheet 精确查；兼容旧 {stem:set}）
        pk_cache = self._pk_cols_cache or self._load_pk_cols_cache()
        if stem and isinstance(pk_cache, dict):
            sheets_map = pk_cache.get(stem)
            if isinstance(sheets_map, dict):
                decl_cols: list = []
                if sheet and sheet in sheets_map:
                    decl_cols = sheets_map.get(sheet) or []
                elif sheet:
                    for sn, cs in sheets_map.items():
                        if sn and sn.lower() == sheet.lower():
                            decl_cols = cs or []
                            break
                if not decl_cols and "*" in sheets_map:
                    decl_cols = sheets_map.get("*") or []
                if decl_cols and any(_norm_col(c) == col_lower for c in decl_cols):
                    return True
            elif isinstance(sheets_map, (set, list, tuple)):
                if any(_norm_col(c) == col_lower for c in sheets_map if c):
                    return True
        # ② id/编号 启发式
        if _is_id_col(col_clean):
            return True
        # ③ 表第一列
        if headers and col_clean == (str(headers[0] or "").split(":")[0].strip().lower()
                                     if headers else False):
            return True
        return False

    def validate(self, intents: list, locator_result: LocatorResult = None,
                 schema_getter=None, data_getter=None) -> dict:
        """主入口:校验 + 修正 intents。

        Args:
            intents: SplitIntent 列表(原地修正)
            locator_result: LocatorAgent 产出(FK 边供校验)
            schema_getter: P21 可选，callable(intent) -> (headers, type_row)。
                提供时额外跑 validate_field_layer（字段层 6 项），让本入口与
                validate_two_layer 共享同一字段校验集合（统一校验管线）。
                缺省 None → 保留旧行为（不跑字段层，仅 produces/consumes/FK 覆盖）。
            data_getter: P21 可选，validate_field_layer 用（existing_values/enum_set 等）。

        Returns:
            {"ok": bool, "issues": [str], "fixes": [str], "intents": intents}
            失败时 ok=False 但 intents 保留原样(调用方降级走 produces_inference)。
        """
        if not intents:
            return {"ok": True, "issues": [], "fixes": [], "intents": intents}
        self.add_thinking("校验", f"ValidatorAgent 开始校验 {len(intents)} 条意图")
        issues: list[str] = []
        fixes: list[str] = []

        # 1. 抑制过产:每表仅一 op（env 开关,默认 on,便于 A/B 对比 LLM 裁决增益）
        if os.getenv("CODEMAKER_VALIDATOR_SUPPRESS_OVER_PRODUCE", "1") != "0":
            over_produced = self._suppress_over_produce(intents)
            if over_produced:
                fixes.append(f"抑制过产:合并/丢弃 {over_produced} 条冗余 op")

        # 2. produces 标签对齐 _norm_name（env 开关）
        if os.getenv("CODEMAKER_VALIDATOR_ALIGN_PRODUCES", "1") != "0":
            label_fixes = self._align_produces_labels(intents)
            fixes.extend(label_fixes)

        # 3. consumes 占位符匹配 produces 标签（env 开关）
        if os.getenv("CODEMAKER_VALIDATOR_CONSUMES_MATCH", "1") != "0":
            consume_issues = self._validate_consumes_match(intents)
            issues.extend(consume_issues)

        # 4. FK 边覆盖校验（env 开关,warning 级）
        if (os.getenv("CODEMAKER_VALIDATOR_FK_COVERAGE", "1") != "0"
                and locator_result and locator_result.fk_edges):
            fk_issues = self._validate_fk_coverage(intents, locator_result.fk_edges)
            issues.extend(fk_issues)

        # 5. LLM 前向引用裁决（opt-in,默认 off）:consumer FK 字段引用未在本批
        #    produces 的 concrete id → LLM 判「需补建还是已存在」。rule 路径不触及,
        #    开启后补充语义层校验。失败/不可达静默降级（不阻断）。
        #    O2：写前默认不调（串行 LLM 卡死 + 与写后 ref_integrity 重叠,design D2
        #    写前零 LLM）。docstring「默认 off」此前与 code（默认 "1"）矛盾,现对齐。
        #    仅 A/B 对比时显式 CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1 开启。
        if (os.getenv("CODEMAKER_VALIDATOR_LLM_FORWARD_REFS", "0") == "1"
                and locator_result and locator_result.fk_edges):
            fr_issues = self._validate_forward_refs_llm(intents, locator_result)
            issues.extend(fr_issues)

        # 6. P21：字段层 + FK 拓扑层（可选,schema_getter 提供时跑）。让 validate()
        #    与 validate_two_layer 共享同一字段/FK 校验集合（统一校验管线），
        #    消除「同输入不同路径结论不同」。Issues 转 str 合并（FORWARD_REF_BROKEN
        #    带「断链」关键字供 hard_issues 判定）。
        if schema_getter is not None:
            field_map = self.validate_field_layer(intents, schema_getter, data_getter)
            fk_map = self.validate_fk_layer(intents, locator_result)
            for sid in set(field_map) | set(fk_map):
                for iss in (field_map.get(sid, []) + fk_map.get(sid, [])):
                    itype = (iss.get("issue_type") if isinstance(iss, dict)
                             else getattr(iss, "issue_type", "")) or ""
                    col = (iss.get("col") if isinstance(iss, dict)
                           else getattr(iss, "col", "")) or ""
                    sug = (iss.get("suggestion") if isinstance(iss, dict)
                           else getattr(iss, "suggestion", "")) or ""
                    kw = " 断链" if itype == "forward_ref_broken" else ""
                    issues.append(f"[{itype}]{kw} {col}: {sug}".strip())

        # issues 含 warning/建议 不阻断(语义层提示),仅 issues 含"断链"/"未建"/"失败"等硬错时 ok=False
        hard_issues = [i for i in issues
                       if "断链" in i or "失败" in i or "未建" in i]
        ok = len(hard_issues) == 0
        self.add_thinking("校验",
                          f"Validator 完成:ok={ok}, {len(issues)} issues, {len(fixes)} fixes")
        return {"ok": ok, "issues": issues, "fixes": fixes, "intents": intents}

    # ── 字段层校验（§4.1）─────────────────────────────────────

    def validate_field_layer(self, intents: list,
                             schema_getter=None, data_getter=None) -> dict:
        """字段层校验（零 LLM，§4.1 ①②③④⑤⑥ 完整 6 项）。

        ① 列存在性 / ② 类型 coerce / ③ 必填性（required_fields.yaml）
        ④ 唯一性（data_getter.existing_values 或 cli.read_sheet）
        ⑤ 枚举白名单（_check_enum_whitelist 纯函数 或 data_getter.enum_set）
        ⑥ 范围分布（modify only, run_semantic_gate 纯函数, 需 result_rows+vc+cli）

        Args:
            intents: list[NLIntent]（已 schema-grounded by ParseAgent）
            schema_getter: callable(intent) -> (headers, type_row) 或 None
            data_getter: callable(intent) -> dict 或 None。dict 键（方案 B）：
              path/stem/sheet/vc/existing_values/enum_set/result_rows/cli
              （调用方注入数据,validator 保持无 agent 引用）

        Returns:
            {subtask_id: list[Issue]}。subtask_id 取 id(intent)。
            调用方经 assemble_tips() 序列化为前端 ask-card tips。
        """
        from ..semantic_gate import _check_enum_whitelist, run_semantic_gate
        issues_map: dict = {}
        for it in intents:
            sid = id(it)
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            if not fields:
                issues_map[sid] = []
                continue
            issues: list[Issue] = []
            headers, type_row = self._get_schema(it, schema_getter)
            if not headers:
                issues.append(Issue(
                    col="", issue_type=IssueType.SCHEMA_MISSING.value,
                    expected="sheet 表头",
                    suggestion=f"无法读取 {getattr(it, 'table_hint', '?')}/"
                               f"{getattr(it, 'sheet_hint', '?')} schema",
                ))
                issues_map[sid] = issues
                continue
            # data_getter 注入数据（方案 B：validator 保持无 agent 引用）
            data = data_getter(it) if data_getter else {}
            if not isinstance(data, dict):
                data = {}
            stem = data.get("stem") or getattr(it, "table_hint", "") or ""
            sheet = data.get("sheet") or getattr(it, "sheet_hint", "") or ""
            path = data.get("path")
            vc = data.get("vc") or {}
            existing_values = data.get("existing_values") or {}
            enum_set = data.get("enum_set") or {}
            result_rows = data.get("result_rows") or []
            cli = data.get("cli") or self._cli
            headers_norm = {_norm_col_name(h).lower() for h in headers if h}
            # §复合主键：本 intent 目标 sheet 的 PK 列列表（≥2 即复合键）。
            # 预算一次，单列唯一性循环据此跳过复合键成员（组合唯一性交末尾统一判），
            # 兼作 _check_composite_unique 的输入。
            # 优先 data_getter 注入的 pk_cols（rules 声明，含跨 sheet 算组合值集合），
            # 否则回退 validator 自有 _get_pk_cols。
            _pk_cols_this: list = list(data.get("pk_cols") or [])
            if not _pk_cols_this and hasattr(self, "_get_pk_cols"):
                try:
                    _pk_cols_this = self._get_pk_cols(it) or []
                except Exception:
                    _pk_cols_this = []
            _composite_pk_norm = {_norm_col(c) for c in _pk_cols_this if c}
            _is_composite = len(_composite_pk_norm) >= 2
            _composite_existing = data.get("composite_existing") or set()
            # ── 英文规范名(row2) ↔ 中文表头(row1) 桥接 ─────────────────
            # 根因：Excel 表头 row1 是中文（如「建筑类型编号」），row2 才是英文
            # 规范名（如 BuildingType/primary_class）。Step1/Step2 的 LLM 常直接
            # 产英文键，本层原用中文表头严格精确匹配 → 英文键全部误报「列不存在」。
            # 而写路径经 ColumnMatcher+type_aliases 能兜底成功，两端能力不对称。
            # 此处用 schema_getter 附带的 type_row(row2 英文规范名) 按列序桥接，
            # 与写路径对齐消除误报；命中的英文键改写回真实中文表头供下游一致。
            _norm_to_real: dict[str, str] = {}   # 中文表头 norm → 原始表头
            for _h in headers:
                if _h:
                    _norm_to_real.setdefault(_norm_col_name(_h).lower(), _h)
            _type_to_real: dict[str, str] = {}   # row2 英文规范名 norm → 中文表头
            for _h, _t in zip(headers, type_row or []):
                if not _h or not _t:
                    continue
                _tn = _norm_col_name(_t).lower()
                if not _tn:
                    continue
                _type_to_real.setdefault(_tn, _h)
                if "." in _tn:  # 点分规范名末段也登记
                    _type_to_real.setdefault(_tn.rsplit(".", 1)[-1], _h)
            _idx_re = re.compile(r"\[\d+\]$")   # 数组列元素下标 [3]
            _renames: dict[str, str] = {}       # 英文/别名键 → 真实中文表头
            for col, val in fields.items():
                col_clean = _norm_col_name(col)
                col_lower = col_clean.lower()
                col_base = _idx_re.sub("", col_lower).strip()  # 去尾部 [N]
                _had_idx = bool(_idx_re.search(col_lower))     # 原键是否带下标
                # ① 列存在性（多级解析，与写路径 matcher 能力对齐）
                _resolved = None  # 命中的真实中文表头
                if col_lower in _norm_to_real:
                    _resolved = _norm_to_real[col_lower]
                elif col_base in _norm_to_real:
                    _resolved = _norm_to_real[col_base]
                elif col_lower in _type_to_real:      # 英文规范名整名命中
                    _resolved = _type_to_real[col_lower]
                elif col_base in _type_to_real:        # 去下标后命中
                    _resolved = _type_to_real[col_base]
                elif "." in col_lower:                 # 点分规范键末段命中
                    seg_last = col_lower.rsplit(".", 1)[-1]
                    if seg_last in _norm_to_real:
                        _resolved = _norm_to_real[seg_last]
                    elif seg_last in _type_to_real:
                        _resolved = _type_to_real[seg_last]
                    elif any(col_lower in h or seg_last in h
                             for h in headers_norm if h):
                        _resolved = col  # 表头含点分全名/末段子串，保留原键
                if _resolved is None:
                    # 真找不到 → 幻觉/别名。给出该表真实列名清单 + 最相近猜测，
                    # 让用户一眼看懂该填什么。
                    _guess = self._closest_header(col_clean, headers, type_row)
                    _avail = "、".join(
                        _norm_col_name(h)
                        for h in headers if h)[:200]
                    _hint = (f"最相近的真实列可能是「{_guess}」，建议改填此列。"
                             if _guess else
                             "实在看不出与原列名相似的真实列名，建议填「删除此列」丢弃该字段。")
                    issues.append(Issue(
                        col=col, issue_type=IssueType.COL_NOT_FOUND.value,
                        expected=f"列存在于 {stem}/{sheet} 表头",
                        suggestion=(
                            f"LLM 写的列名「{col}」是英文/别名，在表「{stem}/{sheet}」"
                            f"的中文表头里找不到对应列。{_hint}"
                            f"该表真实列名有：{_avail}。"
                            f"请改填其中一个真实列名，或填「删除此列」丢弃该字段。"),
                        value=val,
                    ))
                    continue
                # 命中：归一到真实中文表头，后续类型/枚举/唯一/PK 检查按真实列进行
                if _resolved and _resolved != col:
                    _real_clean = _norm_col_name(_resolved)
                    if _real_clean:
                        col_clean = _real_clean
                        col_lower = _real_clean.lower()
                    if not _had_idx:
                        _renames[col] = _resolved
                # 占位符/空软跳过 ④⑤（待拓扑序前序产出替换或可选留空）
                _val_str = str(val).strip() if val is not None else ""
                _is_placeholder = (val is None or _val_str == ""
                                   or _val_str == "<auto>"
                                   or (_val_str.startswith("<") and _val_str.endswith(">")))
                # ② 类型 coerce（按解析后的真实中文表头查类型）
                col_type = self._lookup_col_type(col_clean, headers, type_row)
                # §P1-6 枚举转码前置：int 列填中文标签（如"节日"）先查 enum_resolver
                # 转数字码，命中则改写 fields 消除 TYPE_MISMATCH，避免硬阻断 ask 用户。
                # 写路径 agent._coerce_value 也会查 enum_resolver，但 Step2 前置转码
                # 更省——Step3 零 LLM 下直接写入成功，无需 ask 交互。
                _val_str = str(val).strip() if val is not None else ""
                _is_int_col = col_type and "int" in str(col_type).lower() and "id" not in str(col_type).lower()
                if (_is_int_col and _val_str and not _val_str.lstrip("-").isdigit()
                        and not _is_placeholder and stem and sheet):
                    try:
                        from ..core.enum_resolver import get_enum_resolver as _ger
                        _er = _ger()
                        _enum_val = _er.resolve_label(stem, sheet, col_clean, _val_str)
                        if _enum_val is not None:
                            # 转码成功，改写 fields 值为数字码
                            fields[col] = _enum_val
                            val = _enum_val
                            _val_str = str(val)
                    except Exception:
                        logger.debug("enum_resolver 转码失败 %s/%s/%s/%s",
                                     stem, sheet, col_clean, _val_str, exc_info=True)
                # §中文枚举列放行：列标注 int 但现有数据存中文（如 activity_type:int
                # 实际存「春节活动」）→ 该列实际是中文枚举列，中文值合法，跳过
                # TYPE_MISMATCH。判据：该列 existing_values 里已有中文值。
                _is_cn_enum_col = False
                if (_is_int_col and _val_str and not _val_str.lstrip("-").isdigit()
                        and not _is_placeholder and col_lower in existing_values):
                    _ev_vals = existing_values[col_lower]
                    if isinstance(_ev_vals, (set, list, tuple)):
                        try:
                            import re as _re_cn
                            if any(_re_cn.search(r"[\u4e00-\u9fff]", str(s))
                                   for s in _ev_vals):
                                _is_cn_enum_col = True
                        except Exception:
                            pass
                if not _is_cn_enum_col:
                    ok, err = self._coerce_field_simple(col_type, val)
                    if not ok:
                        issues.append(Issue(
                            col=col, issue_type=IssueType.TYPE_MISMATCH.value,
                            expected=f"{col_type or '未知类型'}",
                            suggestion=err or "类型不匹配",
                            value=val,
                        ))
                # ⑦ ID 段范围（O4）：ID/编号 列的 concrete 值是否落在 id_mgr 预留段。
                #    复用 engine.id_scope.get_id_scope_validator（与写路径同一单例，
                #    design D2 规则校验）；id_mgr 未加载/模块未注册/非 int → 不校验
                #    （validate_value 返 ok=True,不报 issue,不阻断）。
                if not _is_placeholder and _is_id_col(col_clean):
                    try:
                        from engine.id_scope import get_id_scope_validator as _gv
                        _iv = _gv()
                        if _iv._id_mgr_loaded:
                            _ok_id, _reason_id = _iv.validate_value(
                                f"{stem}.{sheet}", val)
                            if not _ok_id:
                                issues.append(Issue(
                                    col=col,
                                    issue_type=IssueType.ID_OUT_OF_SCOPE.value,
                                    expected=f"{stem}.{sheet} ID 预留段内",
                                    suggestion=_reason_id or f"值「{val}」越界 ID 段",
                                    value=val,
                                ))
                    except Exception:
                        logger.debug("id_scope 校验失败 col=%s", col, exc_info=True)
                # ⑤ 枚举白名单（data_getter.enum_set 优先,否则 _check_enum_whitelist 纯函数）
                if not _is_placeholder:
                    if col_lower in enum_set and isinstance(
                            enum_set[col_lower], (set, list, tuple)):
                        allowed = enum_set[col_lower]
                        if (val not in allowed
                                and _val_str not in {str(x) for x in allowed}):
                            issues.append(Issue(
                                col=col, issue_type=IssueType.ENUM_INVALID.value,
                                expected=f"枚举: {sorted(str(x) for x in allowed)[:10]}",
                                suggestion=f"值「{val}」不在白名单",
                                value=val,
                            ))
                    elif cli is not None and path is not None and stem and sheet:
                        try:
                            result = _check_enum_whitelist(stem, sheet, col_clean, val)
                            if result:
                                reason, suggested = result
                                issues.append(Issue(
                                    col=col, issue_type=IssueType.ENUM_INVALID.value,
                                    expected="枚举白名单",
                                    suggestion=reason or suggested or "值不在白名单",
                                    value=val,
                                ))
                        except Exception:
                            logger.debug("_check_enum", col,
                                         exc_info=True)
                # ④ 唯一性（data_getter.existing_values 预计算）
                # §PK 列限定：只对真正的主键/唯一约束列检查重复，不对所有列查。
                # §P1-9 改用 _is_pk_like_col 读 table_relations/rules 真实 PK。
                # §复合主键：本列属复合键成员时跳过单列唯一性（单列重复在复合键里
                # 可能合法，如 (法宝id=5, 法宝等级=1/2/3) 的法宝id 重复），组合唯一性
                # 交循环后的 _check_composite_unique 统一判定，避免误报。
                _is_pk_like = self._is_pk_like_col(
                    col_clean, stem=stem, headers=headers, sheet=sheet)
                _in_composite = col_lower in _composite_pk_norm and _is_composite
                if _is_pk_like and not _in_composite and not _is_placeholder \
                        and col_lower in existing_values \
                        and isinstance(existing_values[col_lower], (set, list, tuple)):
                    ev = existing_values[col_lower]
                    # 仅走 str 比较：val 可能是 list/dict（不可哈希），
                    # `val in ev`(set) 会抛 TypeError 中断整个 validate_field_layer
                    # → validate_two_layer 在 Core4 PK 前移检查前崩 → 冲突落 Step3。
                    _ev_strs = {str(x) for x in ev}
                    if _val_str in _ev_strs:
                        issues.append(Issue(
                            col=col, issue_type=IssueType.UNIQUE_VIOLATION.value,
                            expected=f"列「{col}」值唯一（主键）",
                            suggestion=f"值「{val}」已存在,请用其他值或 modify",
                            value=val,
                        ))
            # 命中但键名≠真实中文表头的字段：改写 fields 键为真实表头，
            # 使 Step3 写盘 / forward_ref 检测 / 后续校验按真实列名一致处理，
            # 消除「校验端英文键、写盘端中文列」的不对称。
            for _old, _new in _renames.items():
                if _old in fields and _new not in fields:
                    fields[_new] = fields.pop(_old)
            # ③ 必填性（required_fields.yaml,当前空,配置填充后生效,#30）
            # §action 限定：仅 add（新增行）校验必填列齐全。
            # set/delete 是修改/删除已有行——主键用于定位行，不在 fields 里是正常的，
            # 原实现对 set 也跑必填校验 → 主键列被误报 MISSING_REQUIRED → 硬 ask →
            # 默认接受建议 → 主键被一并改写（如"法宝id 3→6"这种与用户指令无关的错误修改）。
            required_fields = self._load_required_fields()
            if getattr(it, "action", "") == "add" and required_fields:
                t_cfg = required_fields.get(stem) or required_fields.get(stem.lower()) or {}
                # §P26 sheet 名大小写不敏感查找（yaml 用 "Activity"，运行时小写 "activity"）
                required_cols = None
                if isinstance(t_cfg, dict):
                    required_cols = (t_cfg.get(sheet) or t_cfg.get(sheet.lower())
                                     or t_cfg.get("") or [])
                required_cols = required_cols or []
                # §P26 fields_lower 也清洗 \n/空格，与过滤后 required_cols 一致比较
                fields_lower = {(c or "").split(":")[0].strip().replace("\n", "").replace(" ", "").lower()
                               for c in fields.keys()}
                # §P26 桥接：required_col 可能是中文表头（如"灵兽id"），
                # 但 LLM 产了英文规范名（如"pet_id"）或别名。
                # 字段层校验前面已做 row2 英文规范名 → 中文表头改名（_renames），
                # 但 required_fields 比较在改名之后，fields 键应已是真实表头。
                # 若仍不匹配（桥接未覆盖的别名），用"含 id/编号"启发式：
                # required_col 是主键列时，fields 里只要有任何含 id/编号 的键就算不缺。
                _has_any_id_key = any(
                    "id" in fl or "编号" in fl for fl in fields_lower if fl)
                for req_col in (required_cols or []):
                    _rc_norm = str(req_col or "").strip().replace("\n", "").replace(" ", "").lower()
                    if _rc_norm not in fields_lower:
                        # 主键列缺失时，若 fields 有任何 id/编号 键 → 桥接不算缺
                        _is_pk = "id" in _rc_norm or "编号" in _rc_norm
                        if _is_pk and _has_any_id_key:
                            continue  # 桥接：fields 有英文/别名主键，不算缺
                        issues.append(Issue(
                            col=req_col, issue_type=IssueType.MISSING_REQUIRED.value,
                            expected=f"必填列「{req_col}」",
                            suggestion=f"补充 {req_col} 字段值",
                        ))
            # Pack 3：业务必填列 heuristic 前移（agent.py:4561 同启发式）。
            # 指令含引号（用户显式给名称/描述值）但 LLM 漏产含 名称/描述/名 kw
            # 的列 → 报 MISSING_REQUIRED + 标 intent.validation.skipped=True 让
            # Step3 跳写盘（避免半成品行落盘才 step4 retro-active 标失败）。
            issues.extend(self._check_business_required_pre_add(
                it, headers, fields, getattr(it, "raw", "") or ""))
            # ⑥ 范围分布（modify only, run_semantic_gate 纯函数,需 path/cli/vc/result_rows）
            action = getattr(it, "action", "")
            if (action == "modify" and cli is not None and path is not None
                    and result_rows and vc):
                try:
                    headers_clean = [(h or "").split(":")[0] for h in headers if h]
                    sg_issues = run_semantic_gate(
                        stem, sheet, path, headers_clean, result_rows, cli, vc,
                        action="modify")
                    for si in (sg_issues or []):
                        if isinstance(si, dict):
                            issues.append(Issue(
                                col=str(si.get("column") or ""),
                                issue_type=IssueType.RANGE_OUTLIER.value,
                                expected="范围/分布",
                                suggestion=str(si.get("reason")
                                               or si.get("suggested_fix") or "离群"),
                                value=si.get("value"),
                            ))
                except Exception:
                    logger.debug("run_semantic_gate 失败 stem=%s sheet=%s",
                                 stem, sheet, exc_info=True)
            # ④' 复合主键组合唯一性（add only）：PK 为 ≥2 列复合键时，按组合值
            # 查 existing_values 对应各列已有行的组合，组合重复才算冲突。单列（如
            # 法宝id=5 重复但等级不同）合法放行，根治 FabaoLevel 这类"同实体多等级行"
            # 被单列唯一性误判冲突落 Step3 写盘才爆的漏检。
            if _is_composite and getattr(it, "action", "") == "add" \
                    and not _is_any_placeholder(fields, _composite_pk_norm):
                com_issues = self._check_composite_unique(
                    fields, _pk_cols_this, existing_values, stem=stem, sheet=sheet,
                    composite_existing=_composite_existing)
                issues.extend(com_issues)
            issues_map[sid] = issues
        return issues_map

    def _load_required_fields(self) -> dict:
        """懒加载 required_fields.yaml（§4.1 ③ 必填性,#30）。

        路径：skills/L1_derived/required_fields.yaml
        结构：{table_stem: {sheet: [field_aliases]}}
        用户规则（rules/validate/*.md 内嵌 yaml 的 required:true 列）合并覆盖。
        """
        if self._required_fields is not None:
            return self._required_fields
        self._required_fields = {}
        try:
            from pathlib import Path
            p = (Path(__file__).resolve().parent.parent
                 / "skills" / "L1_derived" / "required_fields.yaml")
            if p.exists():
                import yaml
                with open(p, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    # §P26 修复：yaml 顶层 key 是 "required_fields"，需取子 dict
                    # （原代码 self._required_fields = data 导致 .get(stem) 永远 None，
                    # required_fields 一直空配置，非主键必填从未生效，也从未触发 MISSING_REQUIRED）
                    self._required_fields = data.get("required_fields") or data
        except Exception:
            logger.debug("required_fields.yaml 加载失败", exc_info=True)
        # 用户校验规则 required overlay 合并（rules/validate/*.md，优先级高于 skills）
        try:
            from ..core.rules_loader import get_required_fields_overlay
            overlay = get_required_fields_overlay()
            for stem, sheets in overlay.items():
                base_stem = self._required_fields.setdefault(stem, {})
                for sheet, cols in sheets.items():
                    existing = base_stem.setdefault(sheet, [])
                    for c in cols:
                        if c not in existing:
                            existing.append(c)
        except Exception:
            logger.debug("合并用户必填规则失败", exc_info=True)
        # §P26 用户原则：除主键冲突/主键缺失/类型出错外不校验。
        # required_fields.yaml 由非空率派生，含大量非主键必填（如 activity.活动名称），
        # 用户要求"主键每个表每个 sheet 只设第一个 id 就可以，其他默认不是"。
        # 此处过滤：每个 sheet 只保留第一个 id 列（含 id/编号/主键 的首列），
        # 其余非主键必填一律删除，避免 MISSING_REQUIRED 阻断正常写盘。
        _pk_cols_map = self._load_pk_cols_cache() or {}
        _filtered: dict = {}
        for stem, sheets in (self._required_fields or {}).items():
            stem_lower = str(stem).lower()
            pk_set = set()
            if isinstance(_pk_cols_map, dict):
                # stem 大小写不敏感查 PK 列
                pk_raw = (_pk_cols_map.get(stem)
                          or _pk_cols_map.get(stem_lower)
                          or _pk_cols_map.get(str(stem).title())
                          or set())
                pk_set = set(str(c).lower() for c in pk_raw)
            stem_filtered: dict = {}
            for sheet, cols in (sheets or {}).items():
                sheet_lower = str(sheet).lower()
                # §P26 兼容 cols 可能是 list 或 dict（yaml 嵌套结构）
                if isinstance(cols, dict):
                    cols = list(cols.keys())
                if not isinstance(cols, (list, tuple)):
                    stem_filtered[sheet_lower] = []
                    continue
                # §P26 清洗列名：去 \n/空格（yaml 多行字符串如 "编号\n（按序递增，不要分段）"
                # 与运行时表头 "编号 （按序递增，不要分段）" 空格不一致，统一去空格比较）
                cols = [str(c).replace("\n", "").replace(" ", "").strip() for c in cols if c]
                if not cols:
                    stem_filtered[sheet_lower] = []
                    continue
                # 先按真实 PK 列集合筛
                kept = [c for c in cols if c.lower() in pk_set]
                if not kept:
                    # 无真实 PK 声明 → 取第一个含 id/编号 的列（首列兜底）
                    for c in cols:
                        _cl = c.lower()
                        if "id" in _cl or "编号" in _cl:
                            kept = [c]
                            break
                    if not kept:
                        kept = [cols[0]]  # 首列兜底
                stem_filtered[sheet_lower] = kept
            _filtered[stem_lower] = stem_filtered
        self._required_fields = _filtered
        return self._required_fields

    def _get_schema(self, intent, schema_getter):
        """拉取 (headers, type_row)。schema_getter 优先;None 时尝试 self._cli。

        §schema 缓存：按 (stem, sheet) 缓存，同 intent 多 issue 校验（PK+字段层+
        FK 层）重复拉同表头，缓存省 I/O。
        """
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").lower()
        cache_key = (stem, sheet)
        if not hasattr(self, "_schema_cache"):
            self._schema_cache = {}
        cached = self._schema_cache.get(cache_key)
        if cached is not None:
            return cached
        result = ([], [])
        if schema_getter is not None:
            try:
                result = schema_getter(intent)
            except Exception:
                logger.debug("schema_getter 抛错", exc_info=True)
                result = [], []
        self._schema_cache[cache_key] = result
        return result

    def _lookup_col_type(self, col, headers, type_row) -> str:
        """从 type_row（row2 规范名）找列类型。"""
        if not type_row:
            return ""
        col_clean = _norm_col_name(col).lower()
        for h, t in zip(headers, type_row):
            if h and _norm_col_name(h).lower() == col_clean:
                return str(t or "")
        return ""

    def _coerce_field_simple(self, col_type, val) -> tuple[bool, str]:
        """轻量标量类型校验：值能否强转成列类型。返回 (ok, err)。

        §边界对齐（与 Step3 写路径 _coerce_value 口径一致）：
          原 Step2 对"数值列 + 含分隔符值"做拆段放行（"200,0,150" 拆 200/0/150
          每段 float() 成功 → 放行），但 Step3 写路径 _coerce_value 对 float 列
          直接 float(sv) 整串强转 → "200,0,150" 抛 ValueError → 硬失败阻止写入。
          两端能力不对称致 Step2 误判通过、Step3 才爆类型错误（边界泄漏）。
          现统一：数值列（int/float/...）遇含分隔符的值，按写路径整串强转判定，
          不再拆段放行——分隔符多值场景应由 LLM 标注为数组类型列（int[]/list），
          标量数值列就是单值，含分隔符 = 类型不符。

        Step2 非阻断，仅产 TYPE_MISMATCH issue 供修复层参考，故策略从宽：
          - 占位符 <...> / <auto> / 空值 → 放行（交拓扑回填或可选留空）。
          - 未知类型 / 数组·复合类型（int[]/list/map/json…）→ 放行（不做标量校验）。
          - 仅对明确 int/float/bool 标量列且值为单一字面量时校验可转性。
        """
        if val is None:
            return True, ""
        s = str(val).strip()
        if not s or s == "<auto>" or (s.startswith("<") and s.endswith(">")):
            return True, ""
        t = (col_type or "").strip().lower()
        if not t:
            return True, ""
        if any(x in t for x in ("[]", "list", "array", "map", "dict",
                                "json", "vector", "tuple", "arr", "str",
                                "string", "text")):
            return True, ""
        _is_scalar_num = ("int" in t or "long" in t
                          or "float" in t or "double" in t
                          or "number" in t or "decimal" in t)
        # §边界对齐：数值标量列遇含分隔符值 → 按写路径整串强转判定（不拆段放行）。
        # 写路径 _coerce_value 对 int 列 int(sv)/float 列 float(sv) 整串转，
        # 含逗号/分号等分隔符的串会抛 ValueError → 硬失败。此处同步：不再拆段
        # 放行，直接落下方整串强转分支，分隔符值会自然报 TYPE_MISMATCH。
        try:
            if "float" in t or "double" in t or "number" in t or "decimal" in t:
                float(s)
                return True, ""
            if t in ("bool", "boolean"):
                if s.lower() in ("1", "0", "true", "false", "是", "否", "yes", "no"):
                    return True, ""
                return False, f"「{val}」不是布尔值（需 0/1/true/false）"
            if "int" in t or "long" in t:
                int(float(s))
                return True, ""
        except (ValueError, TypeError):
            if _is_scalar_num:
                return False, f"「{val}」无法转成 {col_type}（需数字，含分隔符的多值应填数组类型列）"
            return False, f"「{val}」无法转成 {col_type}（需数字）"
        return True, ""

    def _closest_header(self, col: str, headers: list, type_row: list = None) -> str:
        """从中文表头 + row2 英文规范名找与 col 最相近的真实列名。

        多策略兜底命中返回真实表头，否则空串。不参与硬匹配，仅供
        COL_NOT_FOUND 文案生成友好提示。三策略：
          ① difflib 字符序列相似度（cutoff 0.4）
          ② 通用 en/cn 同义词组重合（reward↔奖励 等 CA 数据表通用词，
             不绑业务表/测例）→ 跨语言别名桥接（difflib 在中英文混合列名
             上字符序列不重合→丢真实推荐，本策略补全语义同源命中）
          ③ row2 英文规范名 token 重合 → 回真实中文表头
        """
        if not col or not headers:
            return ""
        try:
            import difflib
            import re as _re_h
            _SYN_GROUPS = [
                ("reward", ("奖励", "酬劳", "奖品", "奖励包")),
                ("item",   ("物品", "道具", "材料")),
                ("name",   ("名称", "名字", "名")),
                ("desc",   ("描述", "简介", "说明")),
                ("note",   ("备注", "注释", "批注")),
                ("level",  ("等级", "级别")),
                ("type",   ("类型", "种类")),
                ("count",  ("数量", "个数", "数目")),
                ("weight", ("权重",)),
                ("cost",   ("消耗", "代价")),
                ("id",     ("编号", "序号")),
                ("key",    ("键", "键名")),
                ("data",   ("数据", "内容")),
            ]
            _W2G: dict = {}
            for _en, _cn_list in _SYN_GROUPS:
                for _w in [_en] + list(_cn_list):
                    _W2G.setdefault(_w.lower(), set()).add(_en)

            def _to_tokens(s) -> set:
                s = str(s or "")
                toks: set = set()
                toks.update(m.lower() for m in _re_h.findall(r"[a-zA-Z]+", s))
                for i in range(len(s) - 1):
                    b = s[i:i + 2]
                    if b and all(ord(c) > 127 for c in b):
                        toks.add(b)
                return toks

            def _groups_for(toks: set) -> set:
                g: set = set()
                for _t in toks:
                    if _t in _W2G:
                        g |= _W2G[_t]
                return g

            cands = [_norm_col_name(h) for h in headers if h]
            hit = difflib.get_close_matches(col, cands, n=1, cutoff=0.4)
            if hit:
                return hit[0]
            col_grps = _groups_for(_to_tokens(col))
            if col_grps:
                _best_c = ""
                _best_sc = 0
                for _c in cands:
                    _sc = len(col_grps & _groups_for(_to_tokens(_c)))
                    if _sc > _best_sc:
                        _best_sc, _best_c = _sc, _c
                for _h, _t in zip(headers, type_row or []):
                    if not (_h and _t):
                        continue
                    _tn = _norm_col_name(_t)
                    _sc = len(col_grps & _groups_for(_to_tokens(_tn)))
                    if _sc > _best_sc:
                        _best_sc, _best_c = _sc, _norm_col_name(_h) or _tn
                if _best_sc > 0:
                    return _best_c
            return ""
        except Exception:
            return ""


        """简化类型 coerce（int/float/bool/string）。占位符软跳过。

        返回 (ok, err_msg)。完整 coerce（含枚举映射/数组/date）留 agent._coerce_value
        写路径（validator 无 agent 引用，本版只做基础类型校验）。

        §P1-5 与写路径 coerce 能力对齐：原 int(s) 直接抛 ValueError，但写路径
        agent._coerce_value 对 "703.0"(浮点串)/"1×1"(面积串拆首整数)能容错转。
        能力不对称致 Step2 误报 TYPE_MISMATCH 硬阻断（写路径本可成功）。
        现对 int 列容错：① 浮点串 int(float()) ② 含×x面积串拆首整数。
        枚举中文标签(int列填"节日")仍报 TYPE_MISMATCH（交 Step2 ask，写路径也无枚举映射时失败）。
        """
        if val is None:
            return True, ""
        s = str(val).strip()
        if s == "" or s == "<auto>" or (s.startswith("<") and s.endswith(">")):
            return True, ""  # 占位符/空软跳过
        ct = (col_type or "").lower()
        if "int" in ct and "id" not in ct:
            try:
                int(s)
            except ValueError:
                # §P1-5 容错1：浮点串 "703.0" → int(float("703.0"))=703
                try:
                    int(float(s))
                except ValueError:
                    # §P1-5 容错2：面积串 "1×1"/"2x2" 拆首整数
                    import re as _re
                    m = _re.match(r'\s*(\d+)', s)
                    if m:
                        return True, ""  # 能拆出首整数，写路径会转
                    return False, f"期望 int,实际「{s}」"
        elif "float" in ct or "double" in ct:
            try:
                float(s)
            except ValueError:
                return False, f"期望 float,实际「{s}」"
        elif "bool" in ct:
            if s not in ("true", "false", "True", "False", "1", "0", "是", "否"):
                return False, f"期望 bool,实际「{s}」"
        return True, ""

    # ── FK 拓扑层校验（§4.2）────────────────────────────────

    def validate_fk_layer(self, intents: list,
                          locator_result: LocatorResult = None) -> dict:
        """FK/跨表引用层校验（拓扑序，§4.2）。

        按 OperationOrchestrator._topo_order 拓扑序推进 produced 集合：
          - <consume:label> 必须在 produced（前向引用否则 FORWARD_REF_BROKEN）
          - produces_label 产出后写 produced[label] = (stem, sheet, pk)
        FK 列字面值前向引用（LLM 裁决 build/exists）复用 _validate_forward_refs_llm
        （#19 opt-in CODEMAKER_VALIDATOR_LLM_FORWARD_REFS），本方法不重复
        （validate 主入口已调）。

        与字段层（validate_field_layer 并行）不同：FK 层需拓扑序
        （produced 集合依赖前序，§4.3 "L"形流水基础）。

        Args:
            intents: list[NLIntent]（已 produces_inference 标注 produces/consumes）
            locator_result: LocatorResult（FK 边，供 _validate_fk_coverage 已在 validate 调）

        Returns:
            {subtask_id: list[Issue]}。subtask_id 取 id(intent)。
        """
        issues_map: dict = {}
        if not intents:
            return issues_map
        # 拓扑序（复用 OperationOrchestrator._topo_order，Kahn + produces_inference）
        try:
            from ..core.operation_orchestrator import OperationOrchestrator
            ordered_idx = OperationOrchestrator._topo_order(intents)
        except Exception:
            logger.debug("_topo_order 失败,降级原序", exc_info=True)
            ordered_idx = list(range(len(intents)))
        produced: dict = {}  # label -> (stem, sheet, pk)
        for idx in ordered_idx:
            if not isinstance(idx, int) or idx < 0 or idx >= len(intents):
                continue
            it = intents[idx]
            sid = id(it)
            issues = issues_map.get(sid, [])
            fields = (getattr(it, "extras", None) or {}).get("fields") or {}
            # 本 intent 的 produces label（自引用 <new_xxx_id> 豁免用）
            prod_label_this = (getattr(it, "produces_label", None)
                               or (getattr(it, "extras", None) or {}).get("produces"))
            if prod_label_this:
                prod_label_this = str(prod_label_this).strip()
            # 读本表首列（主键位置），供主键列占位符豁免（LLM produces 标签名
            # 与占位符名常不一致，如 <new_entity_id> vs new_gate_prefab_id）
            _pk_header = ""
            if getattr(it, "action", "") == "add":
                try:
                    _hdrs, _ = self._get_schema(it, None)
                    if _hdrs:
                        _pk_header = str(_hdrs[0] or "").split(":")[0].strip()
                except Exception:
                    _pk_header = ""
            # consumes 占位符前向引用校验
            for col, val in fields.items():
                label = _label_from_consumes(val)
                if label is None:
                    continue
                # §主键列豁免：add 主键列（首列）占位符 = 自动分配主键，非前向引用
                _c_clean = (col or "").split(":")[0].strip()
                if _pk_header and _c_clean == _pk_header:
                    continue
                # §自引用豁免：add 主键 <new_xxx_id> 引用自身 produces = 自动分配主键
                # （Step3 _do_append 自增 + _capture_produced 捕获真实 id），非前向引用。
                # 原实现先校验后写 produced → 单表 add 的 <new_activity_id> 被误判
                # "前序产出未定义" → FORWARD_REF_BROKEN → 整条 intent 被 skip 不落盘。
                if prod_label_this and label == prod_label_this:
                    continue
                if label not in produced:
                    issues.append(Issue(
                        col=col, issue_type=IssueType.FORWARD_REF_BROKEN.value,
                        expected=f"前序产出 produces label「{label}」",
                        suggestion=f"检查 producer 是否漏建或 produces 标签拼写",
                        value=val,
                    ))
            # produces_label 写 produced（供下游 consumer 校验）
            if prod_label_this:
                stem = getattr(it, "table_hint", "") or ""
                sheet = getattr(it, "sheet_hint", "") or ""
                produced[prod_label_this] = (stem, sheet, None)
            issues_map[sid] = issues
        return issues_map

    # ── 交互反问接入（§4.5）──────────────────────────────────

    def set_ask_callback(self, cb) -> None:
        """注入 _ask_callback（agent_service 注入 agent 时同步注入 validator）。

        agent_service.py:1981 `self.agent._ask_callback = _ask_callback` 时，
        若 agent 有 _validator_agent 也调 `agent._validator_agent.set_ask_callback(cb)`。
        """
        self._ask_callback = cb

    def ask_user(self, tips: list) -> dict:
        """发 ask SSE 事件 + 等用户回复（§4.5）。

        复用 agent._ask_callback（agent_service.py:1970 闭包,#39,默认开
        CODEMAKER_INTERACTIVE_REPAIR=1）。validator 通过 set_ask_callback 接收。

        Args:
            tips: assemble_tips() 产出的
                  [{subtask_id, col, issue_type, expected, suggestion}]

        Returns:
            {mode: "field"|"nl"|"skip", text?: str, fix_payload?: {fields}}。
            无 callback（非交互场景/CI）→ {"mode": "skip"}。
        """
        cb = getattr(self, "_ask_callback", None)
        if cb is None:
            # 无 callback（CI/非交互场景）→ continue（不阻塞,不标 skipped）
            # 区别于用户真 skip（mode=skip 标 skipped 跳写盘）
            return {"mode": "continue"}
        question = {
            "reason": "字段/FK 校验发现问题,需用户确认",
            "tips": tips,
            "table": "", "sheet": "",
            "root_cause": f"{len(tips)} 条 issue",
            "attempted_strategies": "字段层 + FK 拓扑层校验",
            "suggestion": "修正字段值或补建前序产出,或跳过此项",
        }
        try:
            return cb(question) or {"mode": "skip"}
        except Exception:
            logger.warning("ask_user _ask_callback 失败,降级 skip", exc_info=True)
            return {"mode": "skip"}

    # ── 两段式整合（§4.1+4.2+4.5+4.6+4.7）────────────────────

    def validate_two_layer(self, intents: list, schema_getter=None,
                           locator_result: LocatorResult = None,
                           data_getter=None, dry_run: bool = False) -> dict:
        """两段式校验 + 硬阻断（要求A：据硬 issue 阻断 + ask 修正闭环）。

        字段层（validate_field_layer）+ FK 拓扑层（validate_fk_layer）→ 收集
        tips。再按硬集分类：
          - 硬 issue（UNIQUE_VIOLATION / TYPE_MISMATCH / 主键列 MISSING_REQUIRED）：
            ask 用户（_ask_pk_conflict / _ask_hard_issue）接受建议 ID / 自定义 /
            跳过 → 改写 intent.extras["fields"] → 标 _resolved；未解决（无 cb /
            用户 skip / 3 轮不符）→ _mark_intent_skipped 阻断 Step3 写盘。
            末尾 ok 据 _has_hard 返回 False（见 return @ end）。
          - warning 类（COL_NOT_FOUND / FORWARD_REF_BROKEN / SCHEMA_MISSING /
            RANGE_OUTLIER / 非主键 MISSING_REQUIRED）：降级放行，交 Step3 列映射
            （ColumnMatcher）/ 占位符代换 / 写盘 ref_integrity 兜底。
        交互回调 _ask_callback 由 agent_service.chat_stream 注入；非交互场景
        （CI / 无 cb）PK 冲突自动改号兜底，其余硬 issue 标 skipped 不写盘。

        Args:
            intents: list[NLIntent]（ParseAgent 产出）
            schema_getter: validate_field_layer 用
            locator_result: validate_fk_layer 用
            data_getter: 字段层 ④⑤⑥ + 核心4 PK 预算 max+1 用

        Returns:
            {ok, issues, fixes, intents, tips, user_reply}。
            ok = not _has_hard（无硬 issue 才 True）；tips 为收集到的展示项。
            已 ask 改写/标 skipped 的 intent 反映在 intents[i].validation 上。
        """
        # O20b：4-step 路径同表同 sheet 同字段去重（S1 Quest 18-23 6 条重复）。
        # _suppress_over_produce 只去 produces 过产；本方法去完全重复（fields 一致）。
        _n_dedup = self._dedup_intents(intents)
        if _n_dedup:
            try:
                self.add_thinking("校验",
                    f"O20b 去重：抑制 {_n_dedup} 条同表同字段重复 intent")
            except Exception:
                pass
        # Pack 2：批内同 PK 但 fields 不同的多 intent 改号/skip。bench 实证
        # reward_id=100608 复用 3 次、item_id=29012 复用 3 次（含 name 冲突）等
        # 真实 dup-PK 链路：_dedup_intents 不去（fields 不同 → sig 不同）→ Step3
        # first 写入，rest 撞 pk_conflict 入 failures。改前移 Step2 解决。
        _n_pk_dup = self._dedup_inter_pk_dup(
            intents, data_getter=data_getter, schema_getter=schema_getter)
        if _n_pk_dup:
            try:
                self.add_thinking("校验",
                    f"Pack 2 同 PK 互撞去重：处理 {_n_pk_dup} 条同 PK 多 intent"
                    f"（改号/ask/skip）")
            except Exception:
                pass
        field_map = self.validate_field_layer(intents, schema_getter, data_getter)
        fk_map = self.validate_fk_layer(intents, locator_result)
        # O2：写前不再调 _validate_forward_refs_llm（串行 LLM 卡死 source + 与写后
        # ref_integrity 重叠）。FK 层只校验 in-batch produces/consumes 闭环
        # （validate_fk_layer 已做，FORWARD_REF_BROKEN 仍产）；既存 FK 字面值引用
        # 交写后 verify_repair 的 ref_integrity.validate_sheet_references 真验证
        # （design D2：写前门控零 LLM）。CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1
        # 仅在 validate() 主入口（A/B opt-in）保留，本方法不触及。
        merged: dict = {}
        for sid in set(field_map) | set(fk_map):
            merged[sid] = list(field_map.get(sid, [])) + list(fk_map.get(sid, []))
        # 要求 A：placeholder 残留检测前移到 Step2。原在 Step5 _phase_execute
        # （agent.py:6068-6166）才抓 → 暂停写盘、半成品风险。现 Step2 对每个
        # add intent 的 fields 扫非 <auto> 占位符（<new_xxx> / <consume:label>），
        # 命中即报 FORWARD_REF_BROKEN issue 进 merged → 下方硬阻断逻辑标 skipped。
        # <auto> 视为可选留空，不报（与 _classify_placeholder_fields 一致）。
        #
        # §P0 可解析豁免：与 validate_fk_layer（:726-732）同构——按拓扑序推进
        # produced 集合，只对「label 不在本批 produces 内」的占位符报 FORWARD_REF_BROKEN。
        # 否则每条合法跨表链（<new_quest_id>）都被误报 → 交互模式假 ask 浪费轮次 /
        # 非交互带病落盘。复用 _topo_order + produces_label，不重复造轮子。
        _ph_auto_re = re.compile(r"<\s*auto\s*>")
        _ph_re = re.compile(r"<([^>]+)>")
        # 收集本批 produces label 集合（拓扑序推进，含前序已产出）
        try:
            from ..core.operation_orchestrator import OperationOrchestrator
            _ordered = OperationOrchestrator._topo_order(intents)
        except Exception:
            logger.debug("_topo_order 失败,降级原序", exc_info=True)
            _ordered = list(range(len(intents)))
        _produced_labels: set = set()
        for _idx in _ordered:
            if not isinstance(_idx, int) or _idx < 0 or _idx >= len(intents):
                continue
            _it = intents[_idx]
            _pl = (getattr(_it, "produces_label", None)
                   or (getattr(_it, "extras", None) or {}).get("produces"))
            if _pl:
                _produced_labels.add(str(_pl).strip())
        for it in intents:
            _fields = getattr(it, "extras", None) or {}
            _fields = _fields.get("fields") if isinstance(_fields, dict) else None
            if not isinstance(_fields, dict):
                continue
            _sid = id(it)
            _ph_cols: list[str] = []
            for _k, _v in _fields.items():
                if not isinstance(_v, str) or "<" not in _v:
                    continue
                if not _ph_re.search(_v):
                    continue
                if _ph_auto_re.fullmatch(_v.strip()):
                    continue  # <auto> 可选留空
                # §P0 可解析豁免：占位符 label 在本批 produces 内 → 可解析，不报
                _lbl = _label_from_consumes(_v)
                if _lbl and _lbl in _produced_labels:
                    continue
                _ph_cols.append(_k)
            if _ph_cols:
                # 占位符悬空 = 跨表前序产出未对上 → FORWARD_REF_BROKEN
                for _c in _ph_cols:
                    _ph_val = _fields.get(_c, "")
                    merged.setdefault(_sid, []).append(Issue(
                        col=_c,
                        issue_type=IssueType.FORWARD_REF_BROKEN.value,
                        expected="已解析的具体值（非占位符）",
                        suggestion=(
                            f"列「{_c}」的值现在是占位符「{_ph_val}」，还没变成真实数据。"
                            f"占位符（尖括号 <...> 包住的内容）本该由前面某个操作先执行、"
                            f"产出真实编号后自动回填，但当前那个前置操作没跑或没对上，"
                            f"所以这里悬空了。解决方式二选一：① 确保生成该编号的前置"
                            f"操作先执行；② 直接在此手动填入真实值（如具体 ID 数字），"
                            f"或点「跳过」放弃此字段。"),
                        value=_fields.get(_c, ""),
                    ))
        # 核心4:PK 冲突(UNIQUE_VIOLATION)前移到 validate 阶段阻断 + ask 用户
        # 原 O3 全软失败 → PK 冲突漏到 Step3 写盘才抓 + 误分类 unknown
        # 现对 UNIQUE_VIOLATION 预算建议 ID(max+1) → ask 接受/输入 → 改 intent
        # 补充:field_map 可能因 intent fields 键与表头不一致漏检 UNIQUE_VIOLATION
        # (如 LLM 产"ID"键而非"reward_id"),故主动扫 add intent 的 PK 值查占用
        _pk_resolved: set = set()  # 已处理的 sid,从 tips 移除
        _sid_to_intent = {id(it): it for it in intents}
        # 诊断:确认核心4进入 + callback 状态
        _cb = getattr(self, "_ask_callback", None)
        try:
            self.add_thinking("校验",
                f"核心4 PK 前移检查:intents={len(intents)},data_getter={'有' if data_getter else '无'},ask_cb={'有' if _cb else '无'}")
        except Exception:
            pass
        # §边界修复：占位符预解析前移。原 Step2 只检测"label 不在 produces"的
        # 悬空占位符，对"label 在 produces 内"的占位符一律视为可解析直接放行，
        # 不做真实值替换 → PK 检测读到的全是 <label> 字符串，existing_values 里
        # 当然没有 → 判占用=False → 通过。但同批次多条 intent 共享同一 produces
        # label（如意图7、意图15 共用 <new_interaction_id>）时，意图7 写盘自增
        # 分到 10082，意图15 不会自动复用 → 二次自增 → PK 冲突，且 Step2 漏检。
        # 现按拓扑序预分配每条 add 的 PK 值（复用 _suggest_next_id 预算 max+1，
        # 同表多 add 递增），写入 produced 字典，再 _resolve_placeholders 把
        # consumes 占位符替换成预分配的真实 ID，让下方 PK 检测看到真实值。
        _pre_produced: dict[str, str] = {}
        _pre_seq: dict[str, int] = {}
        if data_getter is not None:
            try:
                from ..core.operation_orchestrator import OperationOrchestrator as _OO
                _pre_order = _OO._topo_order(intents)
            except Exception:
                logger.debug("Step2 预解析 topo_order 失败,降级原序", exc_info=True)
                _pre_order = list(range(len(intents)))
            for _oi in _pre_order:
                if not isinstance(_oi, int) or _oi < 0 or _oi >= len(intents):
                    continue
                _pit = intents[_oi]
                if getattr(_pit, "action", "") != "add":
                    _OO._resolve_placeholders(_pit, _pre_produced)
                    continue
                # 预分配 PK：读表头找 PK 列名，预算 max+1（同表多 add 递增）
                _phdrs, _ = self._get_schema(_pit, schema_getter)
                _pk_cn = ""
                if _phdrs:
                    for _h in _phdrs:
                        if _h and "id" in str(_h).lower():
                            _pk_cn = str(_h).split(":")[0].strip()
                            break
                    if not _pk_cn and _phdrs:
                        _pk_cn = str(_phdrs[0] or "").split(":")[0].strip()
                # 先解析 consumes 占位符（用前序已预分配的 produced）
                _OO._resolve_placeholders(_pit, _pre_produced)
                # 提取本条 PK 值（解析后，占位符已替换或仍为 <label>）
                _pf = (getattr(_pit, "extras", None) or {}).get("fields") or {}
                _pk_v = None
                if _pk_cn:
                    for _k, _v in _pf.items():
                        if _k and str(_k).split(":")[0].strip().lower() == _pk_cn.lower():
                            _pk_v = _v
                            break
                if _pk_v is None and _pf:
                    _fi = next(iter(_pf.items()), None)
                    if _fi and _fi[1] is not None:
                        _pk_v = _fi[1]
                # PK 值仍是占位符 → 预分配（同表多 add 递增保证不撞）
                if _pk_v is not None and isinstance(_pk_v, str) \
                        and _pk_v.startswith("<") and _pk_v.endswith(">"):
                    _sugg = self._suggest_next_id(_pit, _pk_cn or "", data_getter)
                    if _sugg is not None:
                        # 预分配值写入 produced（按 produces label + 通用 new_id）
                        _lbl = (getattr(_pit, "produces_label", None)
                                or (getattr(_pit, "extras", None) or {}).get("produces"))
                        if _lbl:
                            _pre_produced[str(_lbl).strip()] = str(_sugg)
                        _pre_produced["new_id"] = str(_sugg)
                        # 同时改写 intent 的 PK 字段值（让下方 PK 检测读到真实值）
                        if _pk_cn:
                            for _k in list(_pf.keys()):
                                if _k and str(_k).split(":")[0].strip().lower() == _pk_cn.lower():
                                    _pf[_k] = _sugg
                                    break
                        try:
                            self.add_thinking("校验",
                                f"Step2 预分配 PK: {getattr(_pit,'table_hint','')}/"
                                f"{getattr(_pit,'sheet_hint','')} {_pk_cn}={_sugg}"
                                f"（label={_lbl}）")
                        except Exception:
                            pass
        # 检测与 ask 解耦：data_getter 可读 → 无条件跑检测；
        # 仅 ask 阶段门控 _cb。无 cb / 用户 skip → 标 validation.skipped=True
        # 让 _phase_execute 跳写盘（不落 Step3 半成品 + 误判成功路径）。
        _pk_skipped: set = set()  # 未解决(无 cb / 用户 skip)的 PK 冲突 sid
        # §预览也检测：原 dry_run=True 整段跳过 → 用户预览以为过了真执行才爆 PK
        # 冲突（29004 案例）。预览虽不写盘，但检测+ask 让用户提前解决，避免真执行失败。
        # dry_run 仍传给 _ask_pk_conflict / _suggest_next_id（不影响改 intent）。
        if data_getter is not None:
            # 1) 处理 field_map 已抓到的 UNIQUE_VIOLATION
            for sid, issues in list(merged.items()):
                _intent = _sid_to_intent.get(sid)
                if _intent is None:
                    continue
                for iss in issues:
                    if (getattr(iss, "issue_type", "") != IssueType.UNIQUE_VIOLATION.value
                            or sid in _pk_resolved or sid in _pk_skipped):
                        continue
                    _col = getattr(iss, "col", "") or ""
                    _val = getattr(iss, "value", "")
                    # §复合主键 UNIQUE_VIOLATION 特殊处理：col 形如"列1,列2"，不能走
                    # 单列 _suggest_next_id/_apply_pk_to_intent（会按 bogus 组合名改号→
                    # issue 被静默移除但 fields 未真正修正→Step2 漏报）。改走 skip 阻断
                    # Step3，由写盘 _do_append 复合键校验最终拦。
                    _comp_pk_now = self._get_pk_cols(_intent) \
                        if hasattr(self, "_get_pk_cols") else []
                    if len([c for c in (_comp_pk_now or []) if c]) >= 2:
                        self._mark_intent_skipped(_intent)
                        _pk_skipped.add(sid)
                        continue
                    _suggested = self._suggest_next_id(_intent, _col, data_getter)
                    _reply = self._ask_pk_conflict(
                        _intent, _col, _val, _suggested)
                    if _reply.get("accept_suggest") and _suggested is not None:
                        self._apply_pk_to_intent(_intent, _col, _suggested)
                        _pk_resolved.add(sid)
                    elif _reply.get("accept_suggest") and _suggested is None:
                        # 默认接受建议但无 suggested（如多状态行 idle/collect 非真 PK
                        # 冲突，或 _suggest_next_id 未命中）：放行不 skip，保留原值，
                        # 交写后 ref_integrity 真验证（防误拦致链路断）。
                        _pk_resolved.add(sid)
                    elif _reply.get("custom_id"):
                        self._apply_pk_to_intent(_intent, _col, _reply["custom_id"])
                        _pk_resolved.add(sid)
                    else:
                        # 无 cb 或用户主动 skip → 不落 Step3 写盘
                        self._mark_intent_skipped(_intent)
                        _pk_skipped.add(sid)
            # 2) 主动扫 add intent 的 PK 值(field_map 漏检兜底)
            # 用 schema_getter 拿表头,找 PK 列(含 id),从 intent fields 按列名 match
            # 取值,查 existing_values 占用。比只扫 fields 键含 id 更稳(应对 LLM 自由命名)
            for it in intents:
                sid = id(it)
                if sid in _pk_resolved:
                    continue
                if getattr(it, "action", "") != "add":
                    continue
                _fields = (getattr(it, "extras", None) or {}).get("fields") or {}
                if not _fields:
                    continue
                # 拿表头找 PK 列名
                _hdrs, _ = self._get_schema(it, schema_getter)
                # §复合主键短路：本表 sheet 声明复合主键时，单列占用判定会误报
                # （如 FabaoLevel 的法宝id=5 单看被占，但 (5,1) 组合才唯一）。交
                # validate_field_layer 的 _check_composite_unique 严格处理，本路径跳过，
                # 不做"首列 id 列"单列占用校验。
                _comp_pk = self._get_pk_cols(it) if hasattr(self, "_get_pk_cols") else []
                _comp_pk_n = len([c for c in (_comp_pk or []) if c])
                if _comp_pk_n >= 2:
                    continue
                _pk_col_name = ""
                if _hdrs:
                    for h in _hdrs:
                        if h and "id" in str(h).lower():
                            _pk_col_name = str(h).split(":")[0].strip()
                            break
                    if not _pk_col_name and _hdrs:
                        _pk_col_name = str(_hdrs[0] or "").split(":")[0].strip()
                # §PK 值提取按列位置对齐表头：Step3 写盘 _do_append 用 r[0]（首列）
                # 硬比对，必抓；Step2 原靠列名匹配，LLM 命名偏差（rewardId vs 表头 id）
                # 就漏（29004 案例）。改与写盘对齐——intent fields 第一项即 PK 列值。
                # 仍保留列名回退（多列场景字段顺序不保证 PK 在首），三路径兜底。
                _pk_val = None
                _pk_field_key = ""
                if _pk_col_name:
                    # 路径1：精确列名匹配（最稳）
                    for k, v in _fields.items():
                        if k and str(k).split(":")[0].strip().lower() == _pk_col_name.lower():
                            _pk_val = v
                            _pk_field_key = k
                            break
                if _pk_val is None:
                    # 路径2：按 fields 首项对齐表头首列（与写盘 r[0] 一致）
                    _first_item = next(iter(_fields.items()), None)
                    if _first_item and _first_item[1] is not None:
                        _pk_val = _first_item[1]
                        _pk_field_key = _first_item[0]
                if _pk_val is None:
                    # 路径3回退：fields 键含 id 子串
                    for k, v in _fields.items():
                        if k and "id" in str(k).lower() and v is not None:
                            _pk_val = v
                            _pk_field_key = k
                            break
                if _pk_val is None:
                    try:
                        self.add_thinking("校验",
                            f"核心4 intent(action={getattr(it,'action','')},table={getattr(it,'table_hint','')}) 未提取到 PK 值,fields_keys={list(_fields.keys())}")
                    except Exception:
                        pass
                    continue
                # §防误报（A 修复）：仅当 PK 值确来自"PK 列"才校验冲突。path2(取
                # fields 首项)可能抓到非 PK 列（如 effect.key/交互效果编号），再拿其值
                # 与真 PK 列的 existing 比对 → 假报"已被占用"（用户看到莫名冲突）。
                # 要求 _pk_field_key 精确等于 PK 列名，或本身是 id/编号型键；否则视为
                # 非 PK 列，跳过校验（未显式给 PK → _do_append 自增，天然无冲突）。
                # 通用判据（列名形式），不绑业务词/表/测例。
                _pfk = str(_pk_field_key or "").split(":")[0].strip()
                _pfk_l = _pfk.lower()
                _is_pk_key = (
                    (bool(_pk_col_name) and _pfk_l == _pk_col_name.lower())
                    or ("id" in _pfk_l)
                    or any(_k in _pfk for _k in ("编号", "序号", "主键")))
                if not _is_pk_key:
                    try:
                        self.add_thinking("校验",
                            f"核心4 跳过非 PK 列[{_pk_field_key}]冲突校验"
                            f"（非 id/编号型键,值不与 PK 列比对,避免假冲突）")
                    except Exception:
                        pass
                    continue
                # 查 existing_values 是否占用
                try:
                    data = data_getter(it) if callable(data_getter) else {}
                    ev = (data or {}).get("existing_values") or {}
                    _col_lower = _pk_col_name.lower()
                    _vals = ev.get(_col_lower)
                    if _vals is None:
                        for _k, _v in ev.items():
                            if _k and _k.lower() == _col_lower:
                                _vals = _v
                                break
                    if _vals is None:
                        try:
                            self.add_thinking("校验",
                                f"核心4 PK 列[{_pk_col_name}] 在 existing_values 无匹配(可用列={list(ev.keys())[:5]})")
                        except Exception:
                            pass
                        continue
                    _val_str = str(_pk_val).strip()
                    # 仅走 str 比较：_pk_val 可能不可哈希（list/dict），
                    # `_pk_val in _vals`(set) 会抛 TypeError 中断 Core4。
                    _occupied = _val_str in {str(x) for x in _vals}
                    try:
                        self.add_thinking("校验",
                            f"核心4 PK 检查:列[{_pk_col_name}] 值[{_pk_val}] 占用={_occupied}")
                    except Exception:
                        pass
                    if not _occupied:
                        continue
                except Exception:
                    continue
                _suggested = self._suggest_next_id(it, _pk_col_name or _pk_field_key, data_getter)
                _reply = self._ask_pk_conflict(it, _pk_col_name or _pk_field_key, _pk_val, _suggested)
                if _reply.get("accept_suggest") and _suggested is not None:
                    self._apply_pk_to_intent(it, _pk_col_name or _pk_field_key, _suggested)
                    _pk_resolved.add(sid)
                elif _reply.get("accept_suggest") and _suggested is None:
                    # 默认接受建议但无 suggested（多状态行/字段无 ID 值）：放行不 skip，
                    # 交写后 ref_integrity 真验（防误拦致链路断）。
                    _pk_resolved.add(sid)
                elif _reply.get("custom_id"):
                    self._apply_pk_to_intent(it, _pk_col_name or _pk_field_key, _reply["custom_id"])
                    _pk_resolved.add(sid)
                else:
                    # 无 cb 或用户主动 skip → 标 skipped 阻断 Step3 写盘
                    self._mark_intent_skipped(it)
                    _pk_skipped.add(sid)
        # 移除已处理的 PK issue（accept/custom 已改写 intent，冲突消除，不重复软失败）。
        # _pk_skipped 保留 issue 在 tips → 走软失败上报（Step4 显式列出未解决）。
        if _pk_resolved:
            for sid in list(merged.keys()):
                if sid in _pk_resolved:
                    merged[sid] = [i for i in merged[sid]
                                   if getattr(i, "issue_type", "") != IssueType.UNIQUE_VIOLATION.value]
        tips = assemble_tips(merged)
        # 要求 A：Step2 真阻断。原 O3 ok=True 恒（非阻断），PK/placeholder/FK
        # 全落 Step3/写后抓。现据 tips 是否含【硬 issue】决定 ok：
        # §P26 用户原则：除主键冲突、主键缺失、类型出错外不校验。
        # 收缩硬 issue 范围：
        #   UNIQUE_VIOLATION（PK 冲突）→ 硬阻断
        #   MISSING_REQUIRED（仅主键列缺失）→ 硬阻断
        #   TYPE_MISMATCH（类型无法 coerce）→ 硬阻断
        # 以下降级 warning（不阻断，Step3 照常写盘，tips 上报 Step4 提示）：
        #   FORWARD_REF_BROKEN → 降级 warning（占位符悬空时 Step3 写盘会保留占位符或留空）
        #   COL_NOT_FOUND → 升 Step2 ask 硬阻断（validate_field_layer 已做中文表头/
        #     英文规范名/去下标/点分末段多级规范化匹配，仍不命中=真正列名错）。
        #     原"降级 Step3 ColumnMatcher 兜底"是越界——Step3 职责=执行非校验，
        #     列名错应在 Step2 ask 让用户改/删字段，而非扔 Step3 兜底（兜底成功侥幸，
        #     失败产污染）。第1问归属判定：列名错归 Step2 非 Step1（Step1 只产不校验）。
        #   SCHEMA_MISSING → 降级 warning（表/sheet 不存在时 Step3 写盘失败由 Step4 报）
        #   RANGE_OUTLIER → 降级 warning（modify 场景离群，add 不影响）
        _hard_issue_types = {
            IssueType.UNIQUE_VIOLATION.value,
            IssueType.TYPE_MISMATCH.value,
            IssueType.COL_NOT_FOUND.value,
        }
        _pk_cols = self._pk_cols_cache or self._load_pk_cols_cache() or {}
        def _is_pk_missing(tip) -> bool:
            """MISSING_REQUIRED 仅在主键列缺失时才算硬 issue。"""
            if (tip.get("issue_type") if isinstance(tip, dict)
                    else getattr(tip, "issue_type", "")) != IssueType.MISSING_REQUIRED.value:
                return False
            stem = (tip.get("table") or tip.get("stem") or "") if isinstance(tip, dict) \
                else (getattr(tip, "table", "") or getattr(tip, "stem", ""))
            col = (tip.get("col") or "") if isinstance(tip, dict) \
                else getattr(tip, "col", "")
            pk_set = _pk_cols.get(stem, set()) if isinstance(_pk_cols, dict) else set()
            if pk_set and col in pk_set:
                return True
            # 无 pk 缓存时回退到列名启发式（含 id/编号 且为首列）
            return bool(col) and ("id" in str(col).lower() or "编号" in str(col))
        _has_hard = any(
            ((tip.get("issue_type") if isinstance(tip, dict)
              else getattr(tip, "issue_type", "")) in _hard_issue_types
             or _is_pk_missing(tip))
            for tip in (tips or [])
        )
        # §P1-7 防 PK accept 后重复 ask：核心4 accept 改写的 intent 已并入 _pk_resolved，
        # 此处把 _pk_resolved 的 sid 也并入 _resolved_sids，避免遗留的 TYPE_MISMATCH issue
        # 对同一 intent 再触发 _ask_hard_issue 二次提问（用户刚接受建议ID又弹"类型不符"）。
        _resolved_sids = set(_pk_resolved)  # 初始化含已 PK 解决的 sid
        # 核心4：PK 冲突已 accept 改写的 intent 标 ok=True（_pk_resolved 已改写
        # fields，不应再阻断）；未解决（skip / 无 cb）的 intent 标 skipped。
        self._mark_validation_ok(intents)
        if _has_hard:
            # §交互增强：对非 PK 已处理的硬 issue 通用 ask → 改写 fields → 标 ok
            # 已 accept 改写的 issue 从 tips 移除，未解决（skip/无 cb）标 skipped。
            # _resolved_sids 已在 _has_hard 前初始化含 _pk_resolved（P1-7 防重复 ask）
            # UX Pack: 批量 COL_NOT_FOUND ask——单 intent 有 N 个 COL_NOT_FOUND 时，
            # 原 per-tip 主 loop 弹 N 次单列 ask（疲劳）。改为一次批量 ask card：
            # 列出全部对不上的列 + Pack 1 _closest_header 推荐 + 按行单独填/勾选删除 +
            # "全部删除"/"全部接受推荐" 一键。Replay 见 _ask_col_not_found_batch。
            from collections import defaultdict as _dd
            _col_nf_by_sid = _dd(list)
            for _tip in (tips or []):
                _itype_t = (_tip.get("issue_type") if isinstance(_tip, dict)
                            else getattr(_tip, "issue_type", ""))
                if _itype_t != IssueType.COL_NOT_FOUND.value:
                    continue
                _sid_t = (_tip.get("subtask_id") if isinstance(_tip, dict)
                          else getattr(_tip, "subtask_id", ""))
                if _sid_t and _sid_t not in _resolved_sids:
                    _col_nf_by_sid[_sid_t].append(_tip)
            _batch_processed_sids: set = set()
            for _sid, _nf_tips in _col_nf_by_sid.items():
                _it = _sid_to_intent.get(_sid) if _sid else None
                if _it is None or _sid in _pk_resolved:
                    continue
                _reply_b = self._ask_col_not_found_batch(
                    _it, _nf_tips, schema_getter=schema_getter)
                _mode_b = _reply_b.get("mode") or ""
                if _mode_b in ("batch_field", "skip"):
                    _batch_processed_sids.add(_sid)
                    _resolved_sids.add(_sid)
            if _batch_processed_sids:
                tips = [t for t in (tips or [])
                        if not (
                            ((t.get("subtask_id") if isinstance(t, dict)
                              else getattr(t, "subtask_id", ""))
                             in _batch_processed_sids)
                            and (((t.get("issue_type") if isinstance(t, dict)
                                   else getattr(t, "issue_type", ""))
                                  == IssueType.COL_NOT_FOUND.value)))]
                _has_hard = any(
                    (tip.get("issue_type") if isinstance(tip, dict)
                     else getattr(tip, "issue_type", "")) in _hard_issue_types
                    for tip in (tips or []))
            for tip in (tips or []):
                _itype = (tip.get("issue_type") if isinstance(tip, dict)
                          else getattr(tip, "issue_type", ""))
                # §P25：MISSING_REQUIRED 仅主键列缺失才处理；非主键 MISSING_REQUIRED 降级 warning 跳过
                if _itype not in _hard_issue_types and not _is_pk_missing(tip):
                    continue
                if _itype == IssueType.UNIQUE_VIOLATION.value and \
                        (tip.get("subtask_id") if isinstance(tip, dict)
                         else getattr(tip, "subtask_id", "")) in _pk_resolved:
                    continue  # PK 已 accept，跳过
                sid = (tip.get("subtask_id") if isinstance(tip, dict)
                       else getattr(tip, "subtask_id", ""))
                it = _sid_to_intent.get(sid) if sid else None
                if it is None:
                    continue
                if sid in _resolved_sids:
                    continue  # 本 intent 已被前一个 issue ask 改过
                # 非交互（无 _ask_callback）直接标 skipped 走原逻辑
                if getattr(self, "_ask_callback", None) is None:
                    self._mark_intent_skipped(it)
                    continue
                # §交互 ask 循环：用户输入需校验，不符合类型再提醒（最多 3 轮）
                _col = (tip.get("col") if isinstance(tip, dict)
                        else getattr(tip, "col", "")) or ""
                _retry = 0
                while _retry < 3:
                    _reply = self._ask_hard_issue(it, tip, data_getter=data_getter)
                    _mode = _reply.get("mode") or ""
                    # §修复：accept_suggest 必须优先于 mode=="field" 判断。
                    # 前端「接受建议」回传 {mode:field, accept_suggest:true}，若先走
                    # field 分支，_apply_issue_fix_to_intent 只认 custom_id/value/text、
                    # 不认 accept_suggest → 返回 False → _retry+=1 循环重弹同一 ask
                    # （死循环）。提前判 accept_suggest，用 tip._suggested_id 回填即破。
                    if _reply.get("accept_suggest"):
                        _sug_val = (tip.get("_suggested_id")
                                    if isinstance(tip, dict) else None)
                        if _sug_val is not None:
                            self._apply_issue_fix_to_intent(
                                it, tip, {"mode": "field", "value": _sug_val})
                            try:
                                self.add_thinking("校验",
                                    f"接受建议：{getattr(it,'table_hint','')}/"
                                    f"{getattr(it,'sheet_hint','')} 列[{_col}]"
                                    f" → 填 {_sug_val}")
                            except Exception:
                                pass
                        _resolved_sids.add(sid)
                        break
                    if _mode == "field":
                        if self._apply_issue_fix_to_intent(it, tip, _reply):
                            _resolved_sids.add(sid)
                            _tc = (tip.get("col") if isinstance(tip, dict)
                                   else getattr(tip, "col", "")) or "?"
                            try:
                                self.add_thinking("校验",
                                    f"用户交互修正：{getattr(it,'table_hint','')}/"
                                    f"{getattr(it,'sheet_hint','')} 列[{_tc}]"
                                    f" → 已改写,重跑校验")
                            except Exception:
                                pass
                            break
                        # field 但校验失败（值仍不符合类型）→ 再提醒
                        _retry += 1
                        continue
                    # skip / 无回复 → 标 skipped 阻断
                    self._mark_intent_skipped(it)
                    break
                if _retry >= 3:
                    # 3 轮仍不符合 → 标 skipped
                    self._mark_intent_skipped(it)
            # 移除已交互解决的硬 issue（已改 fields，冲突消除）
            if _resolved_sids:
                tips = [t for t in (tips or [])
                        if not ((t.get("subtask_id") if isinstance(t, dict)
                                else getattr(t, "subtask_id", "")) in _resolved_sids
                                and ((t.get("issue_type") if isinstance(t, dict)
                                      else getattr(t, "issue_type", ""))
                                     in _hard_issue_types))]
                _has_hard = any(
                    (tip.get("issue_type") if isinstance(tip, dict)
                     else getattr(tip, "issue_type", "")) in _hard_issue_types
                    for tip in (tips or []))
            return {"ok": not _has_hard, "issues": tips, "fixes": [],
                    "intents": intents, "tips": tips, "user_reply": None}
        return {"ok": True, "issues": tips, "fixes": [], "intents": intents,
                "tips": tips, "user_reply": None}

    def _suggest_next_id(self, intent, col: str, data_getter) -> Optional[int]:
        """核心4:调 data_getter(intent) 拿 existing_values,算 PK 列下一个可用 ID。

        data_getter 返回 {existing_values: {col_lower: set/list 已用值}}。
        """
        try:
            data = data_getter(intent) if callable(data_getter) else {}
            if not isinstance(data, dict):
                return None
            ev = data.get("existing_values") or {}
            # ev: {col_lower: set/list},匹配 col(大小写不敏感)
            _col_lower = (col or "").lower()
            _vals = None
            for _k, _v in ev.items():
                if _k and _k.lower() == _col_lower:
                    _vals = _v
                    break
            if _vals is None:
                # PK 列名未匹配,取首个含 id 的列作回退
                for _k, _v in ev.items():
                    if _k and "id" in _k.lower():
                        _vals = _v
                        break
            if _vals is None:
                return None
            used = set()
            for x in _vals:
                try:
                    used.add(int(x))
                except (ValueError, TypeError):
                    continue
            if not used:
                return 1
            n = max(used) + 1
            while n in used:
                n += 1
            return n
        except Exception:
            logger.debug("_suggest_next_id 失败", exc_info=True)
            return None

    def _ask_pk_conflict(self, intent, col: str, conflict_val, suggested) -> dict:
        """核心4:PK 冲突 ask 用户接受建议 ID 或自定义输入。

        复用 _ask_callback,带 mode_hint=pk_conflict + suggested_id,
        前端走"接受/输入"简化交互(非通用 textarea)。
        """
        cb = getattr(self, "_ask_callback", None)
        # §取值不确定统一交互：有 cb → 弹 ask（建议 ID + 文字框，不填=接受建议，
        # 填了=自定义）。无 cb（CI/非交互）→ 自动改号兜底（suggested 非 None），
        # 算不出建议才 skip。保证交互场景用户可自定义，非交互场景不卡死。
        if cb is None:
            if suggested is not None:
                try:
                    self.add_thinking("校验",
                        f"PK/ID 冲突自动改号（无交互）：列[{col or 'ID'}] "
                        f"{conflict_val}→{suggested}")
                except Exception:
                    pass
                return {"mode": "field", "accept_suggest": True,
                        "custom_id": suggested, "_auto": True}
            return {"mode": "skip"}
        _tbl = getattr(intent, "table_hint", "") or ""
        _sht = getattr(intent, "sheet_hint", "") or ""
        _reason = (f"主键重复：列「{col or 'ID'}」值「{conflict_val}」已被占用"
                   if suggested is not None else "主键重复或 ID 冲突")
        _sug = (f"「{col or 'ID'}」值「{conflict_val}」已被占用,建议改为「{suggested}」"
                if suggested is not None else
                "请在下方填入正确的 ID 值")
        question = {
            "reason": _reason,
            "error_type": "id_conflict",
            "root_cause": "主键重复或 ID 冲突",
            "table": _tbl, "sheet": _sht,
            "failed_col": col, "failed_val": conflict_val,
            "attempted_strategies": "Step2 校验阶段检测到 PK 冲突",
            "suggestion": _sug,
            "snip": (getattr(intent, "raw", "") or "")[:120],
            "mode_hint": "pk_conflict" if suggested is not None else None,
            "suggested_id": suggested,
            # 要求 B：策划能懂的大白话 reason + action
            "user_friendly": {
                "reason": (f"你填的「{col or '编号'}」值「{conflict_val}」已经被别的数据用了，换个编号吧。"),
                "action": (f"建议改成「{suggested}」（系统自动找的下一个可用编号），点「接受」即可；也可手动填别的编号。"
                           if suggested is not None else
                           "请在下方输入一个新的编号（数字），或点「跳过」放弃此项。"),
            },
        }
        try:
            return cb(question) or {"mode": "skip"}
        except Exception:
            logger.warning("_ask_pk_conflict 失败,降级 skip", exc_info=True)
            return {"mode": "skip"}

    def _apply_pk_to_intent(self, intent, col: str, new_id) -> None:
        """核心4:把新 PK 值写入 intent.extras["fields"][col] + extras["pk_value"]。"""
        try:
            fields = intent.extras.setdefault("fields", {})
            if col:
                fields[col] = new_id
            # 同步更新 pk_value(若列名是首列 id 或匹配)
            _col_lower = (col or "").lower()
            if _col_lower in ("id", "编号", "主键") or "id" in _col_lower:
                intent.extras["pk_value"] = new_id
            # 标记 Core4 已解决 PK：下游 _apply_plan_fields(AI merge 路径) 据此
            # 强制保护，防止 AI 语义校验把改写后的值「按用户指令」回退成原冲突值。
            intent.extras["_pk_resolved"] = {"col": col, "value": new_id}
        except Exception:
            logger.warning("_apply_pk_to_intent 失败", exc_info=True)

    def _ask_hard_issue(self, intent, tip, data_getter=None) -> dict:
        """通用硬 issue 交互 ask（扩 _ask_pk_conflict 模式）。

        覆盖 unique_violation(非PK) / col_not_found / type_mismatch / schema_missing。
        按 issue_type 派生 question，用户可 mode=field 改值 / mode=skip 跳过。
        """
        cb = getattr(self, "_ask_callback", None)
        if cb is None:
            return {"mode": "skip"}
        _tbl = getattr(intent, "table_hint", "") or ""
        _sht = getattr(intent, "sheet_hint", "") or ""
        _col = (tip.get("col") if isinstance(tip, dict)
                else getattr(tip, "col", "")) or ""
        _itype = (tip.get("issue_type") if isinstance(tip, dict)
                  else getattr(tip, "issue_type", "")) or ""
        _val = (tip.get("value") if isinstance(tip, dict)
                else getattr(tip, "value", "")) or ""
        _sug = (tip.get("suggestion") if isinstance(tip, dict)
                else getattr(tip, "suggestion", "")) or ""
        _exp = (tip.get("expected") if isinstance(tip, dict)
                else getattr(tip, "expected", "")) or ""
        # §P26 初始化 _suggested_id 和 _enum_hint（避免分支未定义时 UnboundLocalError）
        _suggested_id = None
        _enum_hint = ""
        # 按 issue_type 派生文案。_reason/_action 保留技术描述（供日志/root_cause）；
        # _uf_reason/_uf_action 为面向策划的大白话（一句话说清 + 明确一键操作），
        # 只放进 user_friendly 供前端展示。
        if _itype == IssueType.UNIQUE_VIOLATION.value:
            _reason = f"值唯一冲突：列「{_col}」值「{_val}」已被占用"
            _action = (f"请输入新的「{_col}」值（原值「{_val}」重复），"
                       f"或点「跳过」放弃此项。建议：{_sug}")
            _mode = "field"
            _uf_reason = f"「{_col}」填的「{_val}」和已有数据重复了。"
            _uf_action = (f"换一个没用过的值{('，建议填「'+_sug+'」') if _sug else ''}；"
                          f"不改就点「跳过」。")
        elif _itype == IssueType.MISSING_REQUIRED.value:
            # §P26 MISSING_REQUIRED 主键缺失文案改进：
            # 像「无法确定 X 的取值，已暂停写入」那样友好，提供建议 ID（已有最大值+1）。
            # 交互形式同 TYPE_MISMATCH 枚举建议：不填=接受建议，填了=按用户值。
            _suggested_id = None
            if data_getter is not None:
                try:
                    _data = data_getter(intent) if callable(data_getter) else {}
                    _ev = (_data or {}).get("existing_values") or {}
                    _col_lower = (_col or "").split(":")[0].strip().lower()
                    for _k, _v in (_ev or {}).items():
                        if str(_k).lower() != _col_lower:
                            continue
                        if not isinstance(_v, (set, list, tuple)):
                            continue
                        _nums = []
                        for _x in _v:
                            try:
                                _nums.append(int(_x))
                            except (ValueError, TypeError):
                                pass
                        if _nums:
                            _suggested_id = max(_nums) + 1
                        break
                except Exception:
                    pass
            if isinstance(tip, dict):
                tip["_suggested_id"] = _suggested_id
            _reason = f"无法确定「{_col}」的取值，已暂停写入"
            _action = (_sug or
                       f"主键「{_col}」缺失，请填入一个未使用的编号。"
                       + (f"建议填 {_suggested_id}" if _suggested_id is not None else "")
                       + "；或点「跳过」放弃此项。")
            _mode = "field"
            _uf_reason = (f"「{_col}」是主键，但指令里没给具体编号，系统无法自动确定。")
            _uf_action = (f"填一个没被用过的编号"
                          + (f"，建议填「{_suggested_id}」" if _suggested_id is not None else "")
                          + "；不填就点「跳过」放弃这一项。"
                          + "（不填=接受建议值，填了=按你输入的编号写入）")
        elif _itype == IssueType.COL_NOT_FOUND.value:
            # §P26 COL_NOT_FOUND 降级 warning，但仍进 tips 上报。文案改进：
            # 说明 LLM 用的列名与真实表头对不上，列出真实表头供参考。
            # 该 issue 不再硬阻断，Step3 列映射兜底处理（可能写空或匹配近邻列）。
            _reason = (f"列名对不上：LLM 给的列名「{_col}」在表「{_tbl}/{_sht}」"
                       f"里没有对应的中文列")
            _action = (f"通常是 LLM 用了英文名或别名。可改成真实列名，"
                       f"或填「删除此列」丢弃该字段。{_sug}")
            _mode = "field"
            _uf_reason = (f"系统按「{_col}」找不到对应列，可能用了英文名或别名。"
                          f"（此项已降级为提示，不影响其他字段写入）")
            _uf_action = (f"如果想配这列，从表里真实列名中挑一个填进来"
                          f"{('：'+_sug) if _sug else ''}；"
                          f"如果本就不需要，填「删除此列」或直接跳过。")
        elif _itype == IssueType.FORWARD_REF_BROKEN.value:
            _reason = (f"列「{_col}」的值还是占位符「{_val}」，没变成真实数据")
            _action = (_sug or
                       f"占位符本该由前置操作产出真实编号后回填，但没对上。"
                       f"请手动填入真实值（如具体 ID），或点「跳过」。")
            _mode = "field"
            _uf_reason = (f"「{_col}」还没拿到真实编号（现在显示的「{_val}」是个"
                          f"临时占位符）。")
            _uf_action = ("这个编号本该由前面一步自动生成再填进来。多数情况"
                          "直接点「跳过」就行（不影响其它内容）；如果你知道具体"
                          "编号，也可以直接填数字。")
        elif _itype == IssueType.TYPE_MISMATCH.value:
            # §枚举列增强：int 列填中文标签(如"节日")/空值是常见场景，文案要说明
            # 当前值是什么（str 空 / str「节日」），无法转 int，并给枚举建议值。
            _is_int_col = "int" in str(_exp).lower()
            _val_disp = (str(_val) if _val not in (None, "") else "空")
            # 查枚举映射，给"中文=数字码"建议列表 + 建议值（中文标签对应数字码）
            _enum_hint = ""
            _suggested_id = None
            if _is_int_col and _tbl and _sht and _col:
                try:
                    from ..core.enum_resolver import get_enum_resolver as _ger
                    _er = _ger()
                    _mapping = _er.get_mapping(_tbl, _sht, _col)
                    if _mapping:
                        _pairs = [f"{k}={v}" for k, v in list(_mapping.items())[:6]]
                        _enum_hint = "可选值：" + "、".join(_pairs)
                        # 建议值 = 当前中文标签对应的数字码；不在映射则取首个
                        if _val:
                            _suggested_id = _mapping.get(str(_val))
                        if _suggested_id is None:
                            _suggested_id = next(iter(_mapping.values()))
                except Exception:
                    _enum_hint = ""
                # §无枚举映射 → 从表数据推断建议值（该列现有值里的最小数字），
                # 让用户看到"这列长什么样"，可接受建议或自行输入。
                if _suggested_id is None and data_getter is not None:
                    try:
                        _data = data_getter(intent) if callable(data_getter) else {}
                        _ev = (_data or {}).get("existing_values") or {}
                        _col_lower = (_col or "").split(":")[0].strip().lower()
                        for _k, _v in (_ev or {}).items():
                            if str(_k).lower() != _col_lower:
                                continue
                            if not isinstance(_v, (set, list, tuple)):
                                continue
                            _nums = []
                            for _x in _v:
                                try:
                                    _nums.append(int(_x))
                                except (ValueError, TypeError):
                                    pass
                            if _nums:
                                _suggested_id = min(_nums)
                                _enum_hint = (f"该列现有值如 {sorted(set(_nums))[:5]}")
                            break
                    except Exception:
                        pass
            # 建议值写回 tip（dict），供 accept_suggest 时回填 fields
            if isinstance(tip, dict):
                tip["_suggested_id"] = _suggested_id
            if _val in (None, "") or str(_val).strip() == "":
                _reason = f"列「{_col}」的值是空的，需要填数字（{_exp}）"
                _action = (f"请在下方填入数字"
                           + (f"；{_enum_hint}" if _enum_hint else "")
                           + "；或点「跳过」先不配这列")
                _uf_reason = f"「{_col}」这列现在没有值（str 空），它需要填数字。"
                _uf_action = (f"填一个数字{('，'+_enum_hint) if _enum_hint else ''}；"
                              f"不清楚填什么就点「跳过」先不配。")
            elif _is_int_col and any(ord(c) > 127 for c in str(_val)):
                _reason = (f"列「{_col}」是数字列（{_exp}），"
                           f"当前值「{_val}」是文字（str），无法转成数字")
                _action = (f"请把「{_val}」改成对应的数字码"
                           + (f"（{_enum_hint}）" if _enum_hint else "")
                           + "；或点「跳过」先不配这列")
                _uf_reason = f"「{_col}」填的「{_val}」是文字，这列只能填数字。"
                _uf_action = (f"改成数字码{('（'+_enum_hint+'）') if _enum_hint else ''}；"
                              f"不清楚编号就点「跳过」。")
            else:
                _reason = f"类型不符：列「{_col}」值「{_val_disp}」类型错（需 {_exp}）"
                _action = (f"请填符合类型的值"
                           + (f"；{_enum_hint}" if _enum_hint else "")
                           + f"；或点「跳过」")
                _uf_reason = f"「{_col}」填的「{_val_disp}」格式不对，这列要「{_exp}」类型。"
                _uf_action = (f"填符合类型的值{('，'+_enum_hint) if _enum_hint else ''}；"
                              f"或点「跳过」。")
            _mode = "field"
        elif _itype == IssueType.SCHEMA_MISSING.value:
            _reason = f"读不到表头：「{_tbl}/{_sht}」schema 缺失"
            _action = "表/sheet 可能不存在，请确认表名/sheet 名，或跳过"
            _mode = "field"
            _uf_reason = f"找不到表「{_tbl}/{_sht}」，读不出它有哪些列。"
            _uf_action = "请确认表名/分页名是否写对，或点「跳过」放弃此项。"
        else:
            _reason = f"校验问题：{_itype} 列「{_col}」"
            _action = _sug or "请处理或跳过"
            _mode = "field"
            _uf_reason = f"「{_col}」这项没通过校验。"
            _uf_action = _sug or "请按提示修正，或点「跳过」放弃此项。"
        question = {
            "reason": _reason,
            "error_type": _itype or "validation_issue",
            "root_cause": _reason,
            "table": _tbl, "sheet": _sht,
            "failed_col": _col, "failed_val": _val,
            "attempted_strategies": "Step2 校验阶段检测",
            "suggestion": _sug,
            "snip": (getattr(intent, "raw", "") or "")[:120],
            "mode_hint": _mode,
            "user_friendly": {"reason": _uf_reason, "action": _uf_action},
        }
        # §取值不确定统一交互：TYPE_MISMATCH/MISSING_REQUIRED 有建议值 →
        # 前端走「建议+输入框」（不填=接受建议，填了=按用户值），
        # 避免 textarea 被当自然语言重描述。
        if _suggested_id is not None and _itype in (
                IssueType.TYPE_MISMATCH.value,
                IssueType.MISSING_REQUIRED.value):
            question["mode_hint"] = "value_input"
            question["suggested_id"] = _suggested_id
            question["suggestion"] = (f"建议填 {_suggested_id}"
                                      + (f"（{_enum_hint}）" if _enum_hint else ""))
        try:
            return cb(question) or {"mode": "skip"}
        except Exception:
            logger.warning("_ask_hard_issue 失败,降级 skip", exc_info=True)
            return {"mode": "skip"}

    # ── 批量 COL_NOT_FOUND ask（UX Pack）──────────────────────

    def _ask_col_not_found_batch(self, intent, col_nf_tips: list,
                                 schema_getter=None) -> dict:
        """单/多 COL_NOT_FOUND 批量 ask card（单列也走此路径，统一自动解决）。

        痛点：原 validate_two_layer 主 loop 对每个 COL_NOT_FOUND tip 单独 _ask_hard_issue，
        同一 intent 有 N 个幻觉列 → 弹 N 次连续 ask card，用户疲劳；且单列 no-cb 会
        skip（丢整条 intent，失败收尾）。改成一张卡片批量 + 自动解决：
          - 列出全部对不上的列 + Pack 1 _closest_header 推荐列名
          - 每行单独填/勾选删除 + "全部删除" / "全部接受推荐" 一键

        Reply schema（前端 replyAskColNotFoundBatch → /api/agent/reply）：
          {mode:'batch_field',
           columns:[{col, fill_value, delete}],   # delete=true 删该列, fill_value=真列名改名
           delete_all: bool}                       # true 全部删

        无 _ask_callback / accept_suggest（dry_run 自动接受）→ 自动策略：
          - _closest_header 命中 → fill_value 建议列名（改名继续写，不丢字段）
          - 不命中 → 删该列（幻觉列，删除=正确收敛，不 skip 整条 intent）
        """
        if not col_nf_tips:
            return {"mode": "skip"}
        _hdrs, _type_row = self._get_schema(intent, schema_getter)
        if _hdrs is None:
            _hdrs = []
        _batch_rows: list = []
        for _tip in col_nf_tips:
            if isinstance(_tip, dict):
                _col = _tip.get("col", "") or ""
                _sug = _tip.get("suggestion", "") or ""
            else:
                _col = getattr(_tip, "col", "") or ""
                _sug = getattr(_tip, "suggestion", "") or ""
            _guess = self._closest_header(_col, _hdrs, _type_row)
            _batch_rows.append({
                "col": _col, "suggested": _guess, "suggestion": _sug,
            })
        _fields = (getattr(intent, "extras", None) or {}).get("fields")
        _editable_fields = []
        if isinstance(_fields, dict):
            _bad_cols = {str(x.get("col", "")) for x in _batch_rows if x.get("col")}
            for _col, _val in _fields.items():
                _col_s = str(_col)
                _suggested = ""
                for _b in _batch_rows:
                    if str(_b.get("col", "")) == _col_s:
                        _suggested = _b.get("suggested", "") or ""
                        break
                _editable_fields.append({
                    "col": _col_s,
                    "value": _val,
                    "suggested": _suggested,
                    "invalid": _col_s in _bad_cols,
                })

        def _auto_columns() -> list:
            _auto = []
            for _b in _batch_rows:
                if _b.get("suggested"):
                    _auto.append({"col": _b["col"], "fill_value": _b["suggested"],
                                  "delete": False})
                else:
                    _auto.append({"col": _b["col"], "fill_value": "",
                                  "delete": True})
            return _auto

        cb = getattr(self, "_ask_callback", None)
        if cb is None:
            _auto = _auto_columns()
            self._apply_batch_to_intent(intent, _auto, delete_all=False)
            try:
                self.add_thinking("校验",
                    f"COL_NOT_FOUND 自动处理（无 cb）：{len(_auto)} 列"
                    f"（{sum(1 for x in _auto if x['delete'])} 列删、"
                    f"{sum(1 for x in _auto if x['fill_value'])} 列改名）")
            except Exception:
                pass
            return {"mode": "batch_field", "_auto": True,
                    "columns": _auto, "delete_all": False}
        _tbl = getattr(intent, "table_hint", "") or ""
        _sht = getattr(intent, "sheet_hint", "") or ""
        question = {
            "reason": f"{len(_batch_rows)} 个列名在这张表里匹配不到。",
            "error_type": "col_not_found_batch",
            "table": _tbl, "sheet": _sht,
            "attempted_strategies": "Step2 validate_field_layer 多级中文表头/英文规范名/去下标/点分末段匹配未中",
            "suggestion": "可按行单独填真实列名、勾选删除、或一键全部删除/全部接受推荐。",
            "snip": (getattr(intent, "raw", "") or "")[:120],
            "mode_hint": "col_not_found_batch",
            "batch_columns": _batch_rows,
            "editable_fields": _editable_fields,
            "user_friendly": {
                "reason": (f"指令里提出的 {len(_batch_rows)} 个列名落不了地，"
                           f"可以从「该表真实列名」里挑一个填进去、或勾选删掉这列。"),
                "action": "按行填真实列名、勾选删除，或者一键「全部删除」/「全部接受推荐」。",
            },
        }
        try:
            _reply = cb(question) or {"mode": "skip"}
        except Exception:
            logger.warning("_ask_col_not_found_batch cb 失败", exc_info=True)
            return {"mode": "skip"}
        _mode = _reply.get("mode") or ""
        if _mode == "batch_field" and (_reply.get("columns") or _reply.get("fields")):
            self._apply_batch_to_intent(
                intent, _reply["columns"],
                delete_all=bool(_reply.get("delete_all", False)),
                fields_reply=_reply.get("fields"))
        elif _mode == "skip":
            self._mark_intent_skipped(intent)
        elif _reply.get("accept_suggest"):
            # dry_run 自动接受建议（CODEMAKER_DRY_RUN_ACCEPT_SUGGEST=1）：
            # 通用 _dry_ask_cb 回 accept_suggest=True + value=suggestion 无 columns，
            # 这里按每行 suggested 改名 / 无 suggested 删列，等价自动解决。
            self._apply_batch_to_intent(intent, _auto_columns(), delete_all=False)
        return _reply

    def _apply_batch_to_intent(self, intent, columns_reply: list,
                                delete_all: bool = False,
                                fields_reply: list | None = None) -> None:
        """按 batch reply 改写 intent fields：删除 col / 改名 col→fill_value。

        - delete_all=True → 全部 COL_NOT_FOUND 列从 fields.pop
        - 行级 delete=True → 该列 pop
        - 行级 fill_value 非空且不等于原 col → 改名（保留原值，键名换为真实列名）
        - fields_reply=[{col,value,delete}] → 全字段可编辑表格回写；col 为空且
          delete=false 时追加字段，适配前端手动补列。
        """
        try:
            _fields = (getattr(intent, "extras", None) or {}).get("fields")
            if not isinstance(_fields, dict):
                return
            for _entry in (columns_reply or []):
                _bc = (_entry or {}).get("col", "") or ""
                if not _bc:
                    continue
                if delete_all or bool(_entry.get("delete")):
                    _fields.pop(_bc, None)
                    continue
                _fv = (_entry or {}).get("fill_value", "") or ""
                if _fv and _fv != _bc:
                    if _fv not in _fields:
                        _fields[_fv] = _fields.pop(_bc, None)
                    else:
                        _fields.pop(_bc, None)
            if fields_reply is not None:
                _next: dict = {}
                for _entry in (fields_reply or []):
                    if not isinstance(_entry, dict):
                        continue
                    if bool(_entry.get("delete")):
                        continue
                    _col = str(_entry.get("col", "") or "").strip()
                    if not _col:
                        continue
                    _next[_col] = _entry.get("value", "")
                _fields.clear()
                _fields.update(_next)
        except Exception:
            logger.warning("_apply_batch_to_intent 失败", exc_info=True)

    def _apply_issue_fix_to_intent(self, intent, tip, reply) -> bool:
        """按用户回复改写 intent fields。返回是否改了。

        - mode=field + custom_id/value → 改 fields[col]
        - mode=field + text=删除此列 → 从 fields 删该列
        - 其余（skip/无）→ False

        §输入校验：新值需符合列类型（tip.expected），不符合不应用（返回 False），
        调用方据此保留 issue 再提醒用户，不静默接受错误值。
        """
        if not reply or reply.get("mode") != "field":
            return False
        _col = (tip.get("col") if isinstance(tip, dict)
                else getattr(tip, "col", "")) or ""
        if not _col:
            return False
        # 优先 custom_id（PK 场景兼容），其次 value，再次 text
        _new = reply.get("custom_id") or reply.get("value") or reply.get("text")
        if not _new:
            return False
        # §输入校验：新值需符合列类型
        _exp = (tip.get("expected") if isinstance(tip, dict)
                else getattr(tip, "expected", "")) or ""
        if _exp:
            _ok, _err = self._coerce_field_simple(str(_exp), _new)
            if not _ok:
                try:
                    self.add_thinking("校验",
                        f"用户输入「{_new}」仍不符合 {_exp}，拒绝应用，保留 issue 待再填")
                except Exception:
                    pass
                return False
        try:
            fields = intent.extras.setdefault("fields", {})
            if str(_new).strip() in ("删除此列", "删除", "delete"):
                fields.pop(_col, None)
                return True
            fields[_col] = _new
            return True
        except Exception:
            logger.warning("_apply_issue_fix_to_intent 失败", exc_info=True)
            return False

    def _mark_validation_ok(self, intents: list) -> None:
        """无 issue 时标 NLIntent.validation.ok=True（下游 ExecuteAgent 据此写盘）。"""
        for it in intents:
            if getattr(it, "validation", None) is None:
                it.validation = ValidationResult(ok=True)

    def _mark_intent_skipped(self, intent) -> None:
        """核心4：单条 intent 标 validation.skipped=True（Step2 检测到 PK 冲突
        但无法 ask/用户主动 skip 时）。下游 _phase_execute 据此跳写盘，
        避免落 Step3 半成品 + 误判成功路径。已有 validation 的覆盖 skipped 字段。
        """
        v = getattr(intent, "validation", None)
        if v is None:
            intent.validation = ValidationResult(ok=True, skipped=True)
        else:
            v.skipped = True

    def _mark_validation_skipped(self, intents: list,
                                 only_subtask_ids=None) -> None:
        """用户 skip 时标 validation.skipped=True（下游 ExecuteAgent 跳写盘）。

        O1：per-subtask skip。only_subtask_ids 给定时只标这些 sid（id(it) 集合）
        的 intent，其余不动；缺省（None）标全部（向后兼容）。已有 validation
        的也覆盖 skipped 字段。
        """
        only = set(only_subtask_ids) if only_subtask_ids else None
        for it in intents:
            if only is not None and id(it) not in only:
                continue
            v = getattr(it, "validation", None)
            if v is None:
                it.validation = ValidationResult(ok=True, skipped=True)
            else:
                v.skipped = True

    # ── 抑制过产 ───────────────────────────────────────────────

    def _suppress_over_produce(self, intents: list) -> int:
        """抑制 LLM 过产：同表同 sheet 且都声明了 produces 的 op 只留首个。

        P9 修复：原实现按 (stem, sheet) 一刀切去重，会误杀同表多行 op（如
        BuildingInteract 的 idle/collect 两条不同 state 行——它们是引用行，无
        produces）。改为：仅在"同 (stem, sheet) 且双方都声明 produces"时才判
        过产抑制（producer 一表一 op 契约）；无 produces 的引用/明细行允许多行，
        保留字段不同的多行 op。返回抑制条数，原地修改 intents 列表。
        """
        if not intents:
            return 0
        seen: set[tuple] = set()
        skip_idx: list[int] = []
        for i, it in enumerate(intents):
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            produces = (getattr(it, "produces", None)
                        or (getattr(it, "extras", None) or {}).get("produces"))
            # 无 produces（引用/明细行如 BuildingInteract）→ 不参与去重，允许多行
            if not (produces and str(produces).strip()):
                continue
            # P9：用户显式多 producer 同 sheet（multi_op_same_sheet=True）→ 不抑制
            # （非 LLM 过产，是用户显式要的多行 op，保留）。DecomposeAgent 标注。
            if getattr(it, "multi_op_same_sheet", False):
                continue
            key = (stem, sheet)
            if key in seen:
                skip_idx.append(i)
                continue
            seen.add(key)
        # 从后往前删,避免索引错位
        for i in reversed(skip_idx):
            intents.pop(i)
        return len(skip_idx)

    def _dedup_intents(self, intents: list) -> int:
        """O20b/O20e：4-step 路径同表同 sheet 同字段 hash 去重（S1 Quest 6 条重复根治）。

        区别 _suppress_over_produce：后者仅去 produces 过产（同表多 producer 一 op 契约）；
        本方法去完全重复（fields 内容一致的同表同 sheet intent）。
        BuildingInteract 不同 state 行 fields 真实差异，不会被误杀。留首条，原地修改。

        O20e 根治：fields_sig 计算把 `<...>` 占位符值归一为 <ph>，
        消解"6 候选表各产 1 条 Quest intent 但 consumes 引用不同 producer label
        → fields 占位符不同 → sig 不同不去重"的过产残留。
        占位符差异非真实业务差异（仅 LLM 对 consumes 的不同引用），
        去重应忽略；真实字段值差异（如 state=idle/collect）保留。
        """
        if not intents:
            return 0
        import hashlib
        import json as _json
        import re as _re_ph
        _PH_RE = _re_ph.compile(r"<[^>]+>")
        seen: set[str] = set()
        skip_idx: list[int] = []
        for i, it in enumerate(intents):
            stem = getattr(it, "table_hint", "") or ""
            sheet = getattr(it, "sheet_hint", "") or ""
            # fields 来自 extras.fields（NLIntent）或 _SplitIntent.fields
            fields = (getattr(it, "extras", None) or {}).get("fields")
            if fields is None:
                fields = getattr(it, "fields", None)
            # O20e：占位符值归一为 <ph>，消除跨候选 prompt 产同表 intent
            #       因 consumes 引用不同 producer label 导致的假性差异。
            #       真实字段值（state=idle/collect 等）不含 <...> 不受影响。
            try:
                norm_fields = {
                    k: (_PH_RE.sub("<ph>", str(v)) if isinstance(v, str) else v)
                    for k, v in (fields or {}).items()
                }
                fields_sig = _json.dumps(norm_fields, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                fields_sig = str(sorted(
                    (k, _PH_RE.sub("<ph>", str(v)) if isinstance(v, str) else v)
                    for k, v in (fields or {}).items()
                ))
            action = getattr(it, "action", "") or ""
            # locator 定位信号也参与（同表同 sheet 同字段 但 locator 不同 = 改不同行）
            loc_val = getattr(it, "locator_value", None) or ""
            loc_field = getattr(it, "locator_field", None) or ""
            sig = hashlib.md5(
                f"{stem.lower()}|{sheet.lower()}|{action}|{loc_val}|{loc_field}|{fields_sig}".encode("utf-8")
            ).hexdigest()
            if sig in seen:
                skip_idx.append(i)
                continue
            seen.add(sig)
        for i in reversed(skip_idx):
            intents.pop(i)
        return len(skip_idx)

    # ── 批内同 PK 互撞去重（Pack 2）────────────────────────────

    def _dedup_inter_pk_dup(self, intents: list, data_getter=None, schema_getter=None) -> int:
        """同 add 同表同 sheet 同 PK 列同值的多 intent 去重/改号。

        区别 _dedup_intents（按完全 hash 重复 fields_sig 去重）：本方法处理
        "同 PK 但 fields 不同"（含真实互补 / 字段冲突 / name 不一致）。实证
        bench 样例 reward_id=100608 出现 3 次（首通/冰封首通/冰封里程碑 不同
        name + fields），item_id=29012 3 次（冰魄碎片 / 冰魄之戒 name 冲突），
        仅 _dedup_intents 不去 → Step3 first 写入，rest 撞 pk_conflict 入
        failures 噪音，Step4 汇总成"ID 已被占用"。

        策略：组内 >1 → first canonical，余下成员 →
          - 有 _ask_callback → 走 _ask_pk_conflict 询问用户 accept 改号 /
            自定义 / skip（与现有 PK-vs-existing ask 同模式，前端契约不变）
          - 无 _ask_callback（非交互/CI）→ 自动改号：建议=max(existing_values +
            first PK)+1，snowball 递增避免连环撞
          - _next_n 算不出（无 existing_values 数字值）→ mark skipped
        PK 列名走 _pk_cols_cache 优先（防首列非 PK 的多状态行误并，如
        BuildingInteract state rows），fallback schema_getter 表头含 id 列。
        """
        if not intents:
            return 0
        try:
            _pk_cache = self._pk_cols_cache or self._load_pk_cols_cache() or {}
        except Exception:
            _pk_cache = {}
        try:
            from collections import defaultdict
            groups: dict = defaultdict(list)
            # §复合主键批内去重组：key=(stem, sheet, tuple(pk_norm), tuple(combo_vals))
            cgroups: dict = defaultdict(list)
            for i, it in enumerate(intents):
                if getattr(it, "action", "") != "add":
                    continue
                stem = (getattr(it, "table_hint", "") or "").lower()
                sheet = (getattr(it, "sheet_hint", "") or "").lower()
                if not stem or not sheet:
                    continue
                # §复合主键：本表 sheet 声明复合主键时走 cgroups 批内组合去重
                # （(5,1)/(5,2)/(5,3) 各不同 = 合法多行；批内 (5,1)x2 = 真重复 → skip 首条外）
                _pk_cols_this = self._get_pk_cols(it, schema_getter) \
                    if hasattr(self, "_get_pk_cols") else []
                _pk_cols_clean = [c for c in (_pk_cols_this or []) if c]
                if len(_pk_cols_clean) >= 2:
                    _fields = (getattr(it, "extras", None) or {}).get("fields")
                    if isinstance(_fields, dict) and _fields:
                        _pk_norm = [_norm_col(c) for c in _pk_cols_clean]
                        _combo = []
                        _all_set = True
                        for _n in _pk_norm:
                            _v = None
                            for _k, _fv in _fields.items():
                                if _norm_col(_k) == _n:
                                    _v = _fv
                                    break
                            _sv = str(_v).strip() if _v is not None else ""
                            if not _sv:
                                _all_set = False
                                break
                            _combo.append(_sv)
                        if _all_set:
                            cgroups[(stem, sheet, tuple(_pk_norm),
                                     tuple(_combo))].append((i, it))
                    continue
                # 单列主键取首个
                pk_col = _pk_cols_clean[0] if _pk_cols_clean else ""
                if not pk_col:
                    continue
                _fields = (getattr(it, "extras", None) or {}).get("fields")
                if not isinstance(_fields, dict) or not _fields:
                    continue
                _pk_l = pk_col.lower()
                _pk_val = None
                _pk_field_key = ""
                for k, v in _fields.items():
                    if k and str(k).split(":")[0].strip().lower() == _pk_l:
                        _pk_val = v
                        _pk_field_key = k
                        break
                if _pk_val is None:
                    _fi = next(iter(_fields.items()), None)
                    if _fi and _fi[1] is not None:
                        _pk_val = _fi[1]
                        _pk_field_key = _fi[0]
                if _pk_val in (None, ""):
                    continue
                groups[(stem, sheet, _pk_l, str(_pk_val).strip())].append(
                    (i, it, _pk_field_key))
            n_resolved = 0
            # §复合主键批内同组合去重：组内 >1 → 首条留，余下 mark skipped
            # （复合键无法自动改号——多列联合，无自然"next id"，skip + 提示用户改组合）
            for _ckey, cmembers in cgroups.items():
                if len(cmembers) <= 1:
                    continue
                _stem, _sheet, _pk_norm, _combo = _ckey
                _combo_desc = ",".join(f"{n}={v}" for n, v in zip(_pk_norm, _combo))
                _cbs = getattr(self, "_ask_callback", None)
                for _ci, _cit in cmembers[1:]:
                    if _cbs is not None:
                        # 复合键列名从 PK 列名归一恢复（取首元素描述即可，用户能看到组合值）
                        _reply = self._ask_pk_conflict(
                            _cit, ",".join(_pk_norm), _combo_desc, None)
                        if not (_reply.get("accept_suggest")
                                or _reply.get("custom_id")):
                            self._mark_intent_skipped(_cit)
                            n_resolved += 1
                    else:
                        self._mark_intent_skipped(_cit)
                        try:
                            self.add_thinking("校验",
                                f"复合主键组合「{_combo_desc}」在意图间重复"
                                f"（{_stem}/{_sheet}），已跳过重复条")
                        except Exception:
                            pass
                        n_resolved += 1
            if not (groups or cgroups) or \
                    (all(len(g) <= 1 for g in groups.values())
                     and all(len(g) <= 1 for g in cgroups.values())):
                return n_resolved
            for key, members in groups.items():
                if len(members) <= 1:
                    continue
                _stem, _sheet, _pk_l, _pk_s = key
                _first_it = members[0][1]
                _set_used = {_pk_s}
                try:
                    if data_getter is not None and callable(data_getter):
                        _data = data_getter(_first_it)
                        _ev = (_data or {}).get("existing_values") or {}
                        for _k, _v in _ev.items():
                            if _k and _k.lower() == _pk_l \
                                    and isinstance(_v, (set, list, tuple)):
                                _set_used |= {
                                    str(x).strip() for x in _v if x is not None}
                                break
                except Exception:
                    pass
                _next_n = None
                try:
                    _nums = sorted({int(x) for x in _set_used if str(x).isdigit()})
                    if _nums:
                        _next_n = _nums[-1] + 1
                except Exception:
                    _next_n = None
                _cb = getattr(self, "_ask_callback", None)
                for _, _it, _fk in members[1:]:
                    _pk_col_used = _fk or _pk_l
                    if _cb is not None:
                        _reply = self._ask_pk_conflict(
                            _it, _pk_col_used, _pk_s, _next_n)
                        if _reply.get("accept_suggest") and _next_n is not None:
                            self._apply_pk_to_intent(_it, _pk_col_used, _next_n)
                            _set_used.add(str(_next_n))
                            _next_n += 1
                            n_resolved += 1
                        elif _reply.get("custom_id"):
                            _cu = str(_reply["custom_id"]).strip()
                            self._apply_pk_to_intent(_it, _pk_col_used, _cu)
                            _set_used.add(_cu)
                            if _next_n is not None:
                                try:
                                    _all_n = sorted({
                                        int(x) for x in _set_used
                                        if str(x).isdigit()})
                                    if _all_n:
                                        _next_n = _all_n[-1] + 1
                                except Exception:
                                    pass
                            n_resolved += 1
                        else:
                            self._mark_intent_skipped(_it)
                            n_resolved += 1
                    else:
                        if _next_n is not None:
                            self._apply_pk_to_intent(_it, _pk_col_used, _next_n)
                            try:
                                self.add_thinking("校验",
                                    f"同 prompt 多 intent 共用 PK「{_pk_col_used}={_pk_s}」"
                                    f"（{_stem}/{_sheet}），自动改号 {_next_n}")
                            except Exception:
                                pass
                            _set_used.add(str(_next_n))
                            _next_n += 1
                        else:
                            self._mark_intent_skipped(_it)
                        n_resolved += 1
            return n_resolved
        except Exception:
            logger.warning("inter_pk_dup 失败", exc_info=True)
            return 0

    # ── produces 标签对齐 ──────────────────────────────────────

    def _align_produces_labels(self, intents: list) -> list[str]:
        """produces 标签对齐 _norm_name 归一 + 同步更新 consumes 占位符。

        统一风格:新主键 produces 用 "new_<stem>_id"。
        若 LLM 产风格漂移(如 "new_pet" / "pet_id"),归一为标准,
        并同步把 fields 里引用旧标签的 <old_label> 占位符更新为 <new_label>。
        返回 fixes 描述列表。
        """
        fixes: list[str] = []
        # 先收集所有需要改的 (old_label → new_label) 映射
        label_map: dict[str, str] = {}
        for it in intents:
            produces = getattr(it, "produces", None)
            if not produces:
                continue
            stem = getattr(it, "table_hint", "") or ""
            standard = f"new_{stem}_id" if stem else produces
            if _norm_name(produces) != _norm_name(standard) and produces != standard:
                label_map[produces] = standard
        # 应用 produces 归一
        for it in intents:
            produces = getattr(it, "produces", None)
            if produces and produces in label_map:
                old = produces
                it.produces = label_map[produces]
                fixes.append(f"produces 标签归一:{old} → {it.produces}")
        # 同步更新 consumes 占位符:<old_label> → <new_label>
        if label_map:
            for it in intents:
                fields = getattr(it, "fields", None) or (getattr(it, "extras", None) or {}).get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                for k in list(fields.keys()):
                    v = fields[k]
                    label = _label_from_consumes(v)
                    if label and label in label_map:
                        fields[k] = f"<{label_map[label]}>"
                        fixes.append(f"consumes 占位符同步:{k} <{label}> → <{label_map[label]}>")
        return fixes

    # ── consumes 匹配 produces ─────────────────────────────────

    def _validate_consumes_match(self, intents: list) -> list[str]:
        """校验 consumes 占位符能否匹配到 produces 标签。

        若有 consumes 指向不存在的 produces → 报 issue(引用断链)。
        """
        issues: list[str] = []
        # 收集所有 produces 标签
        produces_labels: set[str] = set()
        for it in intents:
            p = getattr(it, "produces", None)
            if p:
                produces_labels.add(_norm_name(p))
        # 校验每个 consumes 占位符
        for it in intents:
            fields = getattr(it, "fields", None) or (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            for k, v in fields.items():
                label = _label_from_consumes(v)
                if label is None:
                    continue
                if _norm_name(label) not in produces_labels:
                    issues.append(
                        f"引用断链:字段 {k} 消费 <{label}> 但无对应 produces")
        return issues

    # ── FK 边覆盖校验 ──────────────────────────────────────────

    def _validate_fk_coverage(self, intents: list, fk_edges: list[FKEdge]) -> list[str]:
        """校验 FK 边覆盖:每条 FK 边的 from 表应有 op,to 表应有 produces。

        仅 warning 级(不阻断,因 FK 边可能不全适用于本次指令)。
        """
        issues: list[str] = []
        op_stems = {getattr(it, "table_hint", "") or "" for it in intents}
        produces_stems = {getattr(it, "table_hint", "") or ""
                          for it in intents if getattr(it, "produces", None)}
        for e in fk_edges:
            if e.from_stem in op_stems and e.to_stem not in produces_stems:
                issues.append(
                    f"FK 边 {e.from_stem}.{e.from_column} → {e.to_stem} "
                    f"建议 {e.to_stem} 声明 produces(供 {e.from_stem} 消费)")
        return issues

    # ── LLM 前向引用裁决（opt-in:CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1）─────

    @staticmethod
    def _field_matches_fk(field_key: str, fk_col: str) -> bool:
        """字段名是否匹配 FK 列名（容忍点分键末段/类型后缀/中英文差异）。

        P12 修复：原 `fk in k or k in fk` 子串匹配过宽——fk='id' 命中
        'model_id'/'item_id' 等任意含 id 字段，导致 forward_ref LLM 对每条
        FK 边×每个含 id 字段触发假阳性 + 额外 LLM。审计「精确等值 + 后缀」
        仍让 'model_id' 命中 'id'（后缀 '_id'），故改为**精确等值 only**
        （与 produces_inference._field_matches_col 对齐）。
        """
        fk = _norm_name(fk_col)
        if not fk:
            return False
        k = _norm_name(str(field_key).split(".")[-1])
        if not k:
            return False
        return k == fk

    def _validate_forward_refs_llm(self, intents: list,
                                   locator_result: LocatorResult) -> list[str]:
        """LLM 裁决前向引用:consumer FK 字段引用未在本批 produces 的 concrete id。

        仅对 concrete id 值（非占位符）的 FK 字段触发。LLM 看上下文判:
          - 已存在（引用既存行）→ 不阻断
          - 需补建（引用未建目标）→ 报 issue 建议补 producer
        失败/不可达 → 静默返回空（不阻断,rule 路径兜底）。
        """
        if not self.parser or not locator_result or not locator_result.fk_edges:
            return []
        # P13：producer PK 列 = relation graph 声明的 to_column（非 "id" in kl
        # 启发式）。原启发式把 model_id/effect_id/grid_id 等非主键 id 字段也收
        # 进 produced 集 → 与 P12 叠加使前向引用"已产出"判定失真 → 假阴性
        # （本应触发 build 的 LLM 裁决被跳过）+ 行为不确定。改为仅收 relation
        # 图声明的 producer PK 列（to_column）的 concrete 值。
        producer_pk_cols: dict[str, set[str]] = {}  # to_stem -> {to_column}
        for e in locator_result.fk_edges:
            producer_pk_cols.setdefault(e.to_stem, set()).add(e.to_column)
        # 收集本批 produces 的 (to_stem, pk_value) —— producer fields 中匹配
        # relation 声明 PK 列（to_column）的非占位符 concrete 值
        produced: set[tuple[str, str]] = set()
        for it in intents:
            stem = getattr(it, "table_hint", "") or ""
            fields = getattr(it, "fields", None) or (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            pk_cols = producer_pk_cols.get(stem)
            if not pk_cols:
                continue  # 非 relation 声明的 producer → 不收（无 consumer 引用）
            for k, v in fields.items():
                if _label_from_consumes(v) is not None:
                    continue
                if v in (None, "", "<auto>"):
                    continue
                # P13：字段须匹配 relation 声明的 producer PK 列（to_column）
                if any(self._field_matches_fk(k, pk) for pk in pk_cols):
                    produced.add((stem, str(v)))
        issues: list[str] = []
        for it in intents:
            stem = getattr(it, "table_hint", "") or ""
            fields = getattr(it, "fields", None) or (getattr(it, "extras", None) or {}).get("fields") or {}
            if not isinstance(fields, dict):
                continue
            for e in locator_result.fk_edges:
                if e.from_stem != stem:
                    continue
                for k, v in fields.items():
                    if not self._field_matches_fk(k, e.from_column):
                        continue
                    if _label_from_consumes(v) is not None:
                        continue  # 占位符由 _validate_consumes_match 处理
                    if v in (None, "", "<auto>"):
                        continue
                    if (e.to_stem, str(v)) in produced:
                        continue  # 本批产出,OK
                    # 潜在悬空前向引用 → LLM 裁决
                    verdict = self._llm_judge_forward_ref(e.to_stem, k, v)
                    if verdict == "build":
                        issues.append(
                            f"前向引用未建:{stem}.{k}={v} 指向 {e.to_stem},"
                            f"建议补建 {e.to_stem}(id={v})")
        return issues

    def _llm_judge_forward_ref(self, to_stem: str, field: str, value) -> str:
        """LLM 判 concrete FK 引用是已存在还是需补建。

        返回 'exists' | 'build' | ''（失败/不可达）。
        复用 SubAgent._call_llm_raw（隔离 session,免 R7）。

        P14：所有 `return ""` 路径（无 sid / LLM 异常 / 空响应 / JSON 解析失败 /
        未知 verdict）均 logger.warning 留痕，使「LLM 不可达」行为可观测、
        可复现。返回 "" = 不产 issue = 非阻断（既存引用放行，交写后
        ref_integrity.validate_sheet_references 真验证，design D2 写前零 LLM
        阻断）。opt-in（CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1，默认 off）。
        """
        sid = self._ensure_own_session()
        if not sid:
            logger.warning("P14 forward_ref LLM 无 session,放行 %s.%s=%s", to_stem, field, value)
            return ""
        prompt = (
            f"配表引用校验。一个 add 操作的字段 {field}={value} 引用表 {to_stem} 的主键。\n"
            f"本批操作未在 {to_stem} 产出 id={value}。\n"
            f"判断:这个 id={value} 在 {to_stem} 表里是【已存在】"
            f"（引用既存行,无需补建）还是【需补建】（引用未建目标,应加 producer）?\n"
            f"仅输出 JSON: {{\"verdict\":\"exists\"或\"build\",\"reason\":\"简短理由\"}}"
        )
        try:
            raw = self._call_llm_raw(prompt, timeout=30)
        except Exception:
            logger.warning("P14 forward_ref LLM 异常,放行 %s.%s=%s", to_stem, field, value, exc_info=True)
            return ""
        if not raw:
            logger.warning("P14 forward_ref LLM 空响应,放行 %s.%s=%s", to_stem, field, value)
            return ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            logger.warning("P14 forward_ref LLM 无 JSON,放行 %s.%s=%s raw=%s", to_stem, field, value, raw[:120])
            return ""
        try:
            d = json.loads(m.group(0))
        except ValueError:
            logger.warning("P14 forward_ref LLM JSON 解析失败,放行 %s.%s=%s", to_stem, field, value)
            return ""
        v = str(d.get("verdict", "")).lower()
        if v == "build":
            return "build"
        if v == "exists":
            return "exists"
        logger.warning("P14 forward_ref LLM 未知 verdict=%s,放行 %s.%s=%s", v, to_stem, field, value)
        return ""

    def _run_impl(self, prompt: str, skill_docs: list, context: dict):
        """SubAgent 接口适配:从 context 取 intents + locator_result。"""
        intents = context.get("intents") or context.get("split_intents")
        locator_result = context.get("locator_result")
        if not intents:
            return None
        result = self.validate(intents, locator_result)
        return {
            "sql_or_ops": [],
            "produces": None,
            "references": [],
            "validation": result,
            "target_table": "",
            "target_sheet": "",
        }


def attach_tips_as_soft_failures(intents: list, tips: list[dict]) -> int:
    """P23：把 validate_two_layer 遗留 tips 软失败 dict 追加 intent.failures。

    O3 后 validate_two_layer 非阻断（ok=True 恒），tips 供 thinking 展示但
    不上报 → CI/非交互 continue 带病照样落盘、不上报（违 D6「失败必上报
    不静默吞」）。本函数把遗留 tips 转 #40 形状软失败 dict 追加到对应
    intent.failures（按 tip.subtask_id == id(intent) 匹配），让下游 partition
    创建时 transfer 到 res.failures → all_failures 聚合 + _phase_summarize
    上报。返回附加条数。
    """
    if not tips:
        return 0
    by_id = {id(it): it for it in intents if it is not None}
    n = 0
    for tip in tips:
        sid = tip.get("subtask_id")
        it = by_id.get(sid)
        if it is None:
            continue
        stem = getattr(it, "table_hint", "") or ""
        sheet = getattr(it, "sheet_hint", "") or ""
        issue_type = tip.get("issue_type", "")
        expected = tip.get("expected", "")
        root_cause = f"{issue_type}: {expected}".strip(": ") if expected else str(issue_type)
        soft = {
            "type": "validation_tip",
            "table": stem,
            "sheet": sheet,
            "col": tip.get("col", ""),
            "root_cause": root_cause,
            "suggestion": tip.get("suggestion", ""),
            "status": "soft",
            "snip": (getattr(it, "raw", "") or "")[:120],
        }
        # NLIntent.failures 字段（P23 新增）；dataclass 实例直接 append
        failures = getattr(it, "failures", None)
        if failures is None:
            failures = []
            try:
                it.failures = failures
            except AttributeError:
                # 非 dataclass 替身（SimpleNamespace 等无该字段时）跳过
                continue
        failures.append(soft)
        n += 1
    return n


__all__ = ["ValidatorAgent", "attach_tips_as_soft_failures"]
