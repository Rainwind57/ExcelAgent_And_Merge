
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


def _is_id_col(col_name: str) -> bool:
    """ID/编号 列启发式（与 TableAgent._is_id_column 一致,供 O4 字段层段校验触发）。"""
    if not col_name:
        return False
    name = (col_name or "").split(":")[0].strip().lower()
    return bool(re.search(r"(^|_)(id)$|id$|^编号|编号", name))


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
        # §P1-9 PK 列缓存：从 table_relations.json 预加载 {stem: {to_column}} 真实 PK 列集合。
        # 替代 _is_id_col 启发式（漏"key"/常量字段名，误判 space 非主键 FK 列含 id）。
        # 懒加载，首次 _is_pk_like_col 调用时填充。
        self._pk_cols_cache: dict[str, set[str]] = None

    def _load_pk_cols_cache(self) -> dict[str, set[str]]:
        """从 table_relations.json 加载 {stem: {to_column}} 真实 PK 列集合。

        to_column 是被引用表的真实主键（如 灵兽id/reward_id/quest_id/编号），
        比启发式更准。含配置表主键（key/常量字段名 不含 id，但若无 relation 声明
        仍靠首列兜底）。失败返回空 dict。
        """
        if self._pk_cols_cache is not None:
            return self._pk_cols_cache
        cache: dict[str, set[str]] = {}
        try:
            from ..core.table_relations import RelationGraph
            rg = RelationGraph.load()
            for r in rg.relations:
                # to_path 形如 'pet/pet.xlsx'，stem = 去路径去后缀
                to_path = str(r.to_path).replace("\\", "/").rstrip("/")
                if to_path.endswith(".xlsx"):
                    to_path = to_path[:-5]
                to_stem = to_path.rsplit("/", 1)[-1]
                col = (r.to_column or "").split(":")[0].strip()
                if to_stem and col:
                    cache.setdefault(to_stem, set()).add(col.lower())
        except Exception:
            logger.debug("table_relations PK 列缓存加载失败", exc_info=True)
        self._pk_cols_cache = cache
        return cache

    def _is_pk_like_col(self, col_clean: str, stem: str = "",
                        headers: list = None) -> bool:
        """PK 列判定（P1-9 读真实元数据）。

        优先级：① table_relations 声明的 to_column（真实 PK）
                ② _is_id_col 启发式（含 id/编号 子串）
                ③ 表第一列（惯例主键）
        """
        if not col_clean:
            return False
        col_lower = col_clean.lower()
        # ① 真实 PK 列（table_relations 声明）
        pk_cache = self._load_pk_cols_cache()
        if stem and stem in pk_cache and col_lower in pk_cache[stem]:
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
            headers_norm = {(h or "").split(":")[0].strip().lower()
                            for h in headers if h}
            # row2 规范名集合（点分键末段精确命中需，如 aptitude_base.StrPotCon → StrPotCon）
            # 末段为 row2 规范名末段（非中文），headers_norm 是 row1 中文，需额外对齐
            # _type_aliases 给 row2→row1 映射，但此处 schema_getter 不一定传，
            # 故对含 . 的点分键：取末段，若末段命中 headers_norm 或 headers 含点分全名则豁免
            for col, val in fields.items():
                col_clean = (col or "").split(":")[0].strip()
                col_lower = col_clean.lower()
                # ① 列存在性
                if col_lower not in headers_norm:
                    # 点分键豁免：aptitude_base.StrPotCon 等。表头格式多为
                    # "中文显示名（row2.规范名:int）"，row2 点分键在括号内。
                    # 取末段 StrPotCon 查表头 norm；再查表头是否含点分全名子串。
                    if "." in col_lower:
                        seg_last = col_lower.rsplit(".", 1)[-1]
                        if seg_last and seg_last in headers_norm:
                            continue  # 末段命中 row1 中文表头
                        # 表头含点分全名子串（"体力资质（aptitude_base.strpotcon:int）"）
                        # split(":")[0] 后是 "体力资质（aptitude_base.strpotcon"
                        col_in_header = any(
                            col_lower in h or col_lower.rsplit(".", 1)[-1] in h
                            for h in headers_norm if h)
                        if col_in_header:
                            continue
                    issues.append(Issue(
                        col=col, issue_type=IssueType.COL_NOT_FOUND.value,
                        expected=f"列存在于 {stem}/{sheet} 表头",
                        suggestion=f"列「{col}」不存在,检查 LLM 幻觉或别名映射",
                        value=val,
                    ))
                    continue
                # 占位符/空软跳过 ④⑤（待拓扑序前序产出替换或可选留空）
                _val_str = str(val).strip() if val is not None else ""
                _is_placeholder = (val is None or _val_str == ""
                                   or _val_str == "<auto>"
                                   or (_val_str.startswith("<") and _val_str.endswith(">")))
                # ② 类型 coerce
                col_type = self._lookup_col_type(col, headers, type_row)
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
                # §P1-9 改用 _is_pk_like_col 读 table_relations 真实 PK（to_column），
                # 替代原 _is_id_col+首列启发（漏 key/常量字段名，误判 space 非主键 FK 列含 id）。
                _is_pk_like = self._is_pk_like_col(col_clean, stem=stem, headers=headers)
                if (_is_pk_like and not _is_placeholder
                        and col_lower in existing_values
                        and isinstance(existing_values[col_lower], (set, list, tuple))):
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
            # ③ 必填性（required_fields.yaml,当前空,配置填充后生效,#30）
            required_fields = self._load_required_fields()
            if required_fields:
                t_cfg = required_fields.get(stem) or {}
                required_cols = (t_cfg.get(sheet) if isinstance(t_cfg, dict)
                                 else None) or (t_cfg.get("") if isinstance(t_cfg, dict)
                                                else None) or []
                fields_lower = {(c or "").split(":")[0].strip().lower()
                               for c in fields.keys()}
                for req_col in (required_cols or []):
                    if str(req_col or "").strip().lower() not in fields_lower:
                        issues.append(Issue(
                            col=req_col, issue_type=IssueType.MISSING_REQUIRED.value,
                            expected=f"必填列「{req_col}」",
                            suggestion=f"补充 {req_col} 字段值",
                        ))
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
            issues_map[sid] = issues
        return issues_map

    def _load_required_fields(self) -> dict:
        """懒加载 required_fields.yaml（§4.1 ③ 必填性,#30）。

        路径：skills/L1_derived/required_fields.yaml
        结构：{table_stem: {sheet: [field_aliases]}}
        当前空 {}（TODO §十一#30 待填充），加载返空 dict，校验跳过。
        配置填充后自动生效。
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
                    self._required_fields = data
        except Exception:
            logger.debug("required_fields.yaml 加载失败", exc_info=True)
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
        col_clean = (col or "").split(":")[0].strip().lower()
        for h, t in zip(headers, type_row):
            if h and (h or "").split(":")[0].strip().lower() == col_clean:
                return str(t or "")
        return ""

    def _coerce_field_simple(self, col_type, val) -> tuple:
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
            from ..engine_core.operation_orchestrator import OperationOrchestrator
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
            # consumes 占位符前向引用校验
            for col, val in fields.items():
                label = _label_from_consumes(val)
                if label is not None and label not in produced:
                    issues.append(Issue(
                        col=col, issue_type=IssueType.FORWARD_REF_BROKEN.value,
                        expected=f"前序产出 produces label「{label}」",
                        suggestion=f"检查 producer 是否漏建或 produces 标签拼写",
                        value=val,
                    ))
            # produces_label 写 produced（供下游 consumer 校验）
            prod_label = (getattr(it, "produces_label", None)
                          or (getattr(it, "extras", None) or {}).get("produces"))
            if prod_label:
                stem = getattr(it, "table_hint", "") or ""
                sheet = getattr(it, "sheet_hint", "") or ""
                produced[prod_label] = (stem, sheet, None)
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
        """两段式校验 + 纯展示（O3：非阻断，§4.1+4.2）。

        字段层（validate_field_layer）+ FK 拓扑层（validate_fk_layer）→ 收集 tips
        供上层 thinking 展示（agent.py:3891 据 _vr['tips'] 推送）。不 ask、不 skip、
        不阻断：全 intent 标 validation.ok=True 继续写盘，真正修复全交写后
        verify_repair_loop（门控 C，design D1/D2：成功路径零 LLM、失败路径 repair）。
        写前不调 _validate_forward_refs_llm（O2：串行 LLM 卡死 + 与写后
        ref_integrity 重叠）；既存 FK 字面值引用交写后 ref_integrity 真验证。

        Args:
            intents: list[NLIntent]（ParseAgent 产出）
            schema_getter: validate_field_layer 用
            locator_result: validate_fk_layer 用

        Returns:
            {ok, issues, fixes, intents, tips, user_reply}。
            ok 恒 True（非阻断）；issues/tips 为收集到的展示项（空=无 issue）。
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
        _ph_auto_re = re.compile(r"<\s*auto\s*>")
        _ph_re = re.compile(r"<([^>]+)>")
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
                _ph_cols.append(_k)
            if _ph_cols:
                # 占位符悬空 = 跨表前序产出未对上 → FORWARD_REF_BROKEN
                for _c in _ph_cols:
                    merged.setdefault(_sid, []).append(Issue(
                        col=_c,
                        issue_type=IssueType.FORWARD_REF_BROKEN.value,
                        expected="已解析的具体值（非占位符）",
                        suggestion=(
                            f"「{_c}」需前序操作产出或手动填值，"
                            f"当前仍为占位符 <...>。先建依赖项或手动填编号。"),
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
        # 检测与 ask 解耦：data_getter 可读 → 无条件跑检测；
        # 仅 ask 阶段门控 _cb。无 cb / 用户 skip → 标 validation.skipped=True
        # 让 _phase_execute 跳写盘（不落 Step3 半成品 + 误判成功路径）。
        _pk_skipped: set = set()  # 未解决(无 cb / 用户 skip)的 PK 冲突 sid
        # §dry_run 跳 PK 检查：dry_run 预览不写盘，PK 冲突检测无意义（不真冲突）。
        # 省几十次 data_getter 全表扫描，Step2 提速。PK 真冲突交真执行 ref_integrity 验。
        if data_getter is not None and not dry_run:
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
                _pk_col_name = ""
                if _hdrs:
                    for h in _hdrs:
                        if h and "id" in str(h).lower():
                            _pk_col_name = str(h).split(":")[0].strip()
                            break
                    if not _pk_col_name and _hdrs:
                        _pk_col_name = str(_hdrs[0] or "").split(":")[0].strip()
                # 从 intent fields 找 PK 列的值(精确键 + 含 id 键双路径)
                _pk_val = None
                _pk_field_key = ""
                if _pk_col_name:
                    # 精确键匹配
                    for k, v in _fields.items():
                        if k and str(k).split(":")[0].strip().lower() == _pk_col_name.lower():
                            _pk_val = v
                            _pk_field_key = k
                            break
                if _pk_val is None:
                    # 回退:fields 键含 id 子串
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
        #   UNIQUE_VIOLATION（PK 冲突，未 accept/skip 已在上方处理）
        #   FORWARD_REF_BROKEN（占位符悬空 / 跨表前向引用未在本批 produces）
        #   MISSING_REQUIRED / COL_NOT_FOUND / SCHEMA_MISSING（致命字段问题）
        #   TYPE_MISMATCH（类型无法 coerce，如 int 列填中文标签"节日"无枚举映射）
        # §Step2 类型不匹配拦截：原 TYPE_MISMATCH 不阻断 → 漏到 Step3 coerce 失败
        # 写入半成品 + 回滚（案例三 activity/活动类型="节日"）。现纳入硬 issue，
        # 走 _ask_hard_issue → ask 用户改值/跳过，Step3 不写未解决字段（零半成品）。
        _hard_issue_types = {
            IssueType.UNIQUE_VIOLATION.value,
            IssueType.FORWARD_REF_BROKEN.value,
            IssueType.MISSING_REQUIRED.value,
            IssueType.COL_NOT_FOUND.value,
            IssueType.SCHEMA_MISSING.value,
            IssueType.TYPE_MISMATCH.value,
        }
        _has_hard = any(
            (tip.get("issue_type") if isinstance(tip, dict)
             else getattr(tip, "issue_type", "")) in _hard_issue_types
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
            for tip in (tips or []):
                _itype = (tip.get("issue_type") if isinstance(tip, dict)
                          else getattr(tip, "issue_type", ""))
                if _itype not in _hard_issue_types:
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
                _reply = self._ask_hard_issue(it, tip)
                if _reply.get("mode") == "field" and \
                        self._apply_issue_fix_to_intent(it, tip, _reply):
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
                elif _reply.get("accept_suggest"):
                    # 默认接受建议但 suggestion 为空（如 schema_missing 读不到表头
                    # 无法给 suggestion）：放行不 skip，交写后 verify_repair_loop
                    # 真验（防误拦致链路断，用户要求默认接受）。
                    _resolved_sids.add(sid)
                else:
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
        if cb is None:
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

    def _ask_hard_issue(self, intent, tip) -> dict:
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
        # 按 issue_type 派生文案
        if _itype == IssueType.UNIQUE_VIOLATION.value:
            _reason = f"值唯一冲突：列「{_col}」值「{_val}」已被占用"
            _action = (f"请输入新的「{_col}」值（原值「{_val}」重复），"
                       f"或点「跳过」放弃此项。建议：{_sug}")
            _mode = "field"
        elif _itype == IssueType.COL_NOT_FOUND.value:
            _reason = f"列不存在：列「{_col}」不在「{_tbl}/{_sht}」表头"
            _action = (f"LLM 可能用了别名/幻觉列名。请输入真实列名"
                       f"（或填「删除此列」丢弃该字段），建议：{_sug}")
            _mode = "field"
        elif _itype == IssueType.TYPE_MISMATCH.value:
            # §枚举列增强：int 列填中文标签(如"节日")是常见场景，文案要告诉用户
            # 该列是数字枚举列，中文标签需转数字码，并提示如何填（填数字或换列）。
            _is_int_col = "int" in str(_exp).lower()
            if _is_int_col and _val and any(ord(c) > 127 for c in str(_val)):
                _reason = (f"类型不符：列「{_col}」是数字列（{_exp}），"
                           f"但你填的「{_val}」是中文文字")
                _action = (f"该列只接受数字。如果它是枚举码列（存数字代表分类），"
                           f"请把「{_val}」改成对应的数字码"
                           f"（如不确定编码，可填 0 或点「跳过」先不配这列）。"
                           f"提示：{_sug}")
            else:
                _reason = f"类型不符：列「{_col}」值「{_val}」类型错"
                _action = (f"期望类型「{_exp}」，请输入符合类型的值，建议：{_sug}")
            _mode = "field"
        elif _itype == IssueType.SCHEMA_MISSING.value:
            _reason = f"读不到表头：「{_tbl}/{_sht}」schema 缺失"
            _action = "表/sheet 可能不存在，请确认表名/sheet 名，或跳过"
            _mode = "field"
        else:
            _reason = f"校验问题：{_itype} 列「{_col}」"
            _action = _sug or "请处理或跳过"
            _mode = "field"
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
            "user_friendly": {"reason": _reason, "action": _action},
        }
        try:
            return cb(question) or {"mode": "skip"}
        except Exception:
            logger.warning("_ask_hard_issue 失败,降级 skip", exc_info=True)
            return {"mode": "skip"}

    def _apply_issue_fix_to_intent(self, intent, tip, reply) -> bool:
        """按用户回复改写 intent fields。返回是否改了。

        - mode=field + custom_id/value → 改 fields[col]
        - mode=field + text=删除此列 → 从 fields 删该列
        - 其余（skip/无）→ False
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
