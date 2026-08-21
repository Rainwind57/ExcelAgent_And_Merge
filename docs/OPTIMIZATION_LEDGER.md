# Excel-Agent 优化总账（OPTIMIZATION_LEDGER）

> **单一事实源**。整合自 12 份历史 TODO/优化/诊断文档（已归档至 `docs/archive/`，见 §7）。
> **基线日期**：2026-08-18（O5 更新：2026-08-18）。
> **架构方向**：excel-agent = 4-Step 多 Agent Loop（主线）；merge 已稳定（降权附录 §5）；事前预防八方法横切。
> **伴随文档**（不归档，本总账引用）：
> - `docs/validator_audit.md` — validator P9–P27 证据明细（file:line）
> - `docs/excel-agent-4step-loop-design.md` — 4-Step 架构设计（§一~§七）
> - `openspec/changes/*/` — 各变更 proposal/design/tasks（已完成态）
> - `.codemaker/优化工作流.md` — 每轮优化流程规范

---

## 0. 架构方向

**excel-agent 主线 = 4-Step 多 Agent Loop**（`CODEMAKER_4STEP_LOOP=1`，`agent.py:3848`）：
1. **ParseAgent**（`parse_agent.py`）= LocatorAgent（FK 路由+扩表，零 LLM）+ DecomposeAgent（schema 驱动 LLM 拆分）+ `infer_produces_consumes`（关系图 produces 推断）→ 产 NLIntent[]（schema-grounded + produces 标注）
2. **ValidateAgent**（`validator_agent.py`）= `validate_two_layer`：字段层（列/类型/必填/唯一/枚举/范围/id_scope）+ FK 拓扑层（in-batch produces/consumes 闭环）。**O3 后纯展示非阻断**（ok=True 恒，tips 供 thinking 展示，不 ask/skip）
3. **ExecuteAgent**（`agent.py:_phase_execute`）= placeholder gate（必填/跨表引用占位未解→ask/failure）+ `_run_verify_repair_loop`（写后 3 轮，error_classifier + skill tools + repair_playbook）
4. **Summarize**（`_phase_summarize`）= failures 汇总 → "❌ 失败清单"

**设计契约**（`openspec/changes/agent-verify-repair-loop/design.md`）：D1 verify-repair 在 TableAgent 内 / **D2 写前门控零 LLM、写后 verify 纯规则内存校验** / D6 失败必上报不静默吞。

**merge** = 已稳定（R3/R6/R9 落地），剩余优化全链降权（§5）。

**事前预防八方法** = 横切守门层：A 公式 / / C PATCH_CONFIG / D 批注（已落地首版）；E 血缘 / F 编号账本 / G RAG / H 对抗网（待建）。

---

## 1. 已完成里程碑

| 波次 | 内容 | 证据 |
|---|---|---|
| **O1–O4**（2026-08-18）| O1 per-subtask skip → O3 纯展示非阻断；O2 删写前 `_validate_forward_refs_llm`（`validate_two_layer` 不调、`validate()` 默认 off）；O3 `validate_two_layer` 降级纯展示（不 ask/skip/阻断，修复交 C）；O4 字段层补 id_scope + `IssueType.ID_OUT_OF_SCOPE` | `validator_agent.py` / `nl_parser.py` + 单测 247 passed |
| **O5 validator P0 第一批**（2026-08-18）| P12 `_field_matches_col`/`_field_matches_fk` 子串匹配 → **精确等值 only**（消 `id` 命中 `model_id`，审计「+后缀」公式仍命中故升级为精确 only）；P10 `add_keys` 覆盖 → `setdefault`（首 producer 候选保留）；P11 produces 标签 `new_{stem}_id` → **sheet-aware** `new_{stem}_{sheet}_id`（消同 stem 多 producer 撞标签）；P17 `_should_consume` `<auto>` → **False**（留空不转占位，消 placeholder_unresolved 二次 ask） | `produces_inference.py` + `validator_agent.py:_field_matches_fk` + 新增 `tests/test_produces_inference_p0.py` 21 passed；回归 137 passed / 9 预存红不变（DecomposeAgent mock 路径，与本批无关） |
| **O6 validator P13**（2026-08-18）| P13 `_validate_forward_refs_llm` produced 收集从 `"id" in kl` 启发式 → **relation `to_column` 声明 PK 列**（`producer_pk_cols` map + `_field_matches_fk` 匹配）。消 model_id/effect_id 等非主键 id 字段污染 produced 集 → 与 P12 叠加使前向引用"已产出"判定失真（假阴性：本应触发 build 的 LLM 裁决被跳过）。注：opt-in 路径（`CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1`，4-step 主线不调），低频但补齐 P12 语义 | `validator_agent.py:_validate_forward_refs_llm` + 新增 `tests/test_validator_forward_refs_p13.py` 9 passed；回归 128 passed / 9 预存红不变 |
| **O7 test harness：DecomposeAgent 9 预存红修复**（2026-08-18）| `test_decompose_agent.py` `make_cli()` 用相对 `Path("resources")` → 从 server/ cwd 跑解析为 `server/resources`（不存在）→ `list_tables()` 空 → `_build_schema_block` 空 → jobs 空 → `decompose` 返 0。改为 `Path(__file__).resolve().parents[2] / "resources"`（cwd 无关）。解封 DecomposeAgent→ValidatorAgent e2e 路径，O5/O6 produces_inference 改动经此路径验证无回归 | `tests/test_decompose_agent.py:103`；9 预存红全绿，全相关回归 189 passed / 0 failed |
| **O8 validator 第二批（部分）**（2026-08-18）| **P22/P23** tips→failures 通道统一：NLIntent 加 `failures` 字段 + `attach_tips_as_soft_failures()` 把 validate_two_layer 遗留 tips 转 #40 形状软失败 dict 追加 intent.failures（4-step + 6-step 两路径接入）+ partition 创建时 transfer 到 res.failures → all_failures 聚合 + _phase_summarize 上报（保 D6「失败必上报不静默吞」，消 CI/非交互 continue 带病照样落盘）；**P19** `execute_no_llm` × `enable_verify_repair_loop` 互斥校验（`_check_p19_mutex_conflict` 方法，__init__ 调，同开 warning 提示）。P24/P25（skip partial 态）/P21（两入口校验集合统一）留下轮 | `nl_parser.py:NLIntent.failures` + `validator_agent.py:attach_tips_as_soft_failures` + `agent.py:_check_p19_mutex_conflict` + 两路径 attach + partition transfer + 新增 `tests/test_validator_tips_to_failures_p23.py` 10 + `tests/test_agent_p19_mutex.py` 4；全相关回归 223 passed / 0 failed |
| **O9 validator 第二批收尾**（2026-08-18）| **P21** `validate()` 加可选 `schema_getter`/`data_getter` 参数，提供时跑 `validate_field_layer`+`validate_fk_layer` 合并 issues（Issue→str，FORWARD_REF_BROKEN 带「断链」关键字供 hard_issues 判定），消除「同输入不同路径结论不同」（两入口共享同一字段/FK 校验集合）；缺省 None→保留旧行为，现有调用方不传→不变。**P24/P25** 两过滤点（4-step 3928/6-step 4227）增强 thinking 列出 skipped stem/sheet（非仅计数，「不静默丢」供汇总单列「已跳过清单」）+ 注释 O3-dormant（validate_two_layer 非阻断不标 skipped → 此分支 dormant，保留供未来 partial 态复用）+ _phase_execute:5512 skip 分支注释 dormant | `validator_agent.py:validate` + `agent.py` 两过滤点 + skip 分支注释 + 新增 `tests/test_validator_unified_entry_p21.py` 8；全相关回归 204 passed / 0 failed |
| **O10 validator 第三批 P26**（2026-08-18）| **P26** 批级事务/部分回滚：`_compute_rollback_targets` staticmethod + `CODEMAKER_BATCH_TRANSACTIONAL=1` opt-in strict 模式（任一硬失败回滚整批前序已 commit op，不限 G8 直接依赖，批级原子，消 district 成功+combat 失败留半成品→重跑 UNIQUE_VIOLATION）。默认 off（保留 G8 链回滚：仅回滚失败步直接依赖 producer，避免牵连无关独立 op）。**P27**（4-step NL 路径 checkpoint：parse/validate 后拍中间态 NLIntent 序列化，stall 可续跑免 Step1 重 LLM decompose）留 follow-up（涉及 NLIntent 序列化格式 + 续跑协议，改动面更广） | `agent.py:_compute_rollback_targets` + batch_transactional 属性 + run() 回滚目标集改用该方法 + 新增 `tests/test_agent_p26_batch_txn.py` 9；全相关回归 178 passed / 0 failed |
| **O11 validator P 项清零 P9/P14/P27**（2026-08-18）| **P9** NLIntent 加 `multi_op_same_sheet` 字段 + `_suppress_over_produce` 跳过标记 op（保用户显式多 producer 同 sheet，非 LLM 过产）；**P14** `_llm_judge_forward_ref` 5 个静默 `return ""` 路径加 `logger.warning` 留痕（无 session/异常/空响应/无 JSON/解析失败/未知 verdict），使「LLM 不可达」行为可观测可复现，返回 "" 非阻断语义不变（交写后 ref_integrity 真验证）；**P27** NLIntent 加 `to_checkpoint_dict`/`from_checkpoint_dict`（嵌套 ValidationResult/ExecutionResult (de)serialization）+ agent_service `_save_nl_checkpoint`/`_load_nl_checkpoint`（opt-in `CODEMAKER_4STEP_CHECKPOINT=1`，拍 parse/validate 后中间态，stall 可续跑免 Step1 重 LLM decompose）。4-step 路径接线（save 调用 + stall 检测 + resume skip已成功）留 follow-up | `nl_parser.py:NLIntent.multi_op_same_sheet + to/from_checkpoint_dict + ValidationResult/ExecutionResult to/from_dict` + `validator_agent.py:_suppress + _llm_judge_forward_ref` + `agent_service.py:_save/_load_nl_checkpoint` + 新增 `tests/test_validator_p9_p14_p27.py` 21；全相关回归 234 passed / 0 failed |
| **O12 validator follow-up P27 接线**（2026-08-18）| **P27 接线**：TableAgent 加 `_nl_checkpoints` 属性 + `_save_nl_checkpoint`/`_load_nl_checkpoint`/`_resume_from_checkpoint` 方法；4-step 路径 post_parse（agent.py:3913）+ post_validate（agent.py:4042）save 调用接线（opt-in `CODEMAKER_4STEP_CHECKPOINT=1`）；`_resume_from_checkpoint` 优先 post_validate 回退 post_parse，session_id 隔离。**resume 自动跳过 parse + skip已成功 Step5 留 follow-up**（需 stall 检测 + per-op 成功跟踪 + e2e）。**P24/P25 真 partial 态**：需反转 O3 非阻断设计（O8 选 soft-failure 通道 P23 而非 partial-skip），design 决策非接线，文档化为 design follow-up | `agent.py:_nl_checkpoints + _save/_load/_resume_from_checkpoint + 4-step 两 save 接线` + 新增 `tests/test_agent_p27_checkpoint.py` 13；全相关回归 247 passed / 0 failed |
| **O14 P27 resume 全闭环**（2026-08-18）| **P27 resume follow-up 收尾**：① stall 检测=显式 env `CODEMAKER_4STEP_RESUME=<session_id>` 触发（run() 入口调 `_resume_from_checkpoint`，skip Step1 parse + post_parse/post_validate save，免重 LLM decompose）；② per-op 成功跟踪=checkpoint dict 加 `completed_op_keys` 字段（orig_idx 集），`_save_nl_checkpoint` 加可选参数；③ Step5 loop 成功 op 后增量回写（新 `_save_nl_progress` 方法，覆盖 post_validate/回退 post_parse stage）；④ resume 跳过已成功 op（filter `ordered_idx` 去 `completed_op_keys`）+ 从 checkpoint execution 重算 produced（调 `_capture_produced` 重放 result_rows，供后续 op 占位符替换，免查库）；⑤ `_resume_from_checkpoint` 返三元组 `(intents, stage, completed)`。opt-in `CODEMAKER_4STEP_CHECKPOINT=1` + `CODEMAKER_4STEP_RESUME=<session_id>` 双 env gate，默认 off。e2e 阻 R7，证据用确定性单测。**残留 follow-up**：heartbeat auto-trigger（后台线程+竞态，留 serve 修复后）、P24/P25 真 partial 态（design 决策非接线） | `agent.py:_save_nl_checkpoint(+completed_op_keys) + _resume_from_checkpoint(三元组) + _save_nl_progress + run() 入口 resume 检测 + Step5 增量 save + produced 重算` + 新增 `tests/test_agent_p27_checkpoint.py` TestO14CompletedOpKeys 7（共 20）；全相关回归 113 passed / 0 failed；全量 942 passed / 1 预存红（`test_column_matcher_semantic` 列匹配器优先级，与 O14 无关） |
| **O15 方法 A/D 升级接 pre_commit_hold 通道**（2026-08-18）| **AD1/AD2/A2/D4 闭环**：① AD1 `CODEMAKER_FORMULA_GATE=hold` + needs_manual_fix=True → CLI `_save_with_cache_check` 构造 `PreCommitHoldEvent(kind=formula_loss)` + `record_hold_audit` 留痕；② AD2 批注二次回写后仍丢（still_lost>0）→ `kind=comment_loss` hold 事件 + D4 `comment_replay_partial` audit（二次做差仍丢留痕）；③ `CLICallResult` 加 `hold_events` 字段，9 写库点（write_cell/append_row/sort/shift×6）透传；④ A2 agent 层 `_handle_cli_hold_events` 消费 hold_events → #40 软失败追加 `res.failures`（保 D6 上报）+ 经 `_agent_subtask_sink` 推 `pre_commit_hold` SSE 事件；⑤ `_write_cell_and_verify` 始终透出 `cli_result`（原仅失败携带）；⑥ `formula_ref_shifter.merge_into` 保留 `comment_replay`+`hold_events`（原丢）。CLI 层无 SSE task 上下文 → 仅 audit + 附返回 dict，agent 层触发 SSE。**残留 follow-up**：前端 MergeGuideView.vue 红 card 接 pre_commit_hold 事件（minified 需重建）、override 弹窗 | `cli_interface.py:_save_with_cache_check(AD1/AD2 hold 事件构造+D4 audit) + CLICallResult(+hold_events) + 9 写库点透传` + `formula_ref_shifter.py:merge_into(保留 hold_events)` + `agent.py:_handle_cli_hold_events(新) + _write_cell_and_verify(始终透出 cli_result) + _run_set/_run_add(A2 消费)` + 新增 `tests/test_ad_upgrade_hold_o15.py` 9；全相关回归（formula_gate+comment_guard+merge_preflight+patch_validator）38 passed / 0 failed；全量 951 passed / 1 预存红（同 O14） |
| **O16 方法 F 编号账本跨分支**（2026-08-18）| **F1-F5 闭环**：① F1 新增 `IdScopeValidator.validate_multi_branch(branch_roots) → CrossBranchReport`（扫多分支根目录 ID 列，location 加 branch 维度，冲突判定从"文件"改"分支"，同分支同 stem 跨 sheet 白名单）；② F2 `get_id_scope_validator()` 单例向后兼容（多根用独立 `validate_multi_branch` 调用不复用单例活实例）；③ F3 `routers/validate.py` `GET /id-scope` 加 `?mode=multibranch`（扫 RESOURCES_DIR + 子目录各分支根）+ 新增 `GET /id-claim?id=xxx&mode=multibranch`；④ F4 `_validate_id_scope` 加 id-claim 跨分支查重（段校验通过后）+ 命中冲突 → `PreCommitHoldEvent(kind=id_conflict)` + SSE 推送 + 建议下一空闲号（已用最大+1，跳过占用）+ `CODEMAKER_ID_SCOPE_BRANCHES=path1;path2` env 多分支模式；⑤ 新增 `CrossBranchConflict`/`CrossBranchReport` dataclass + `claim_id` 方法（单/多分支查重 + suggested_next）。**残留 follow-up**：真 SVN 分支目录对接（archive TODO §4 生产迁移）、前端 id-claim 预检弹窗 | `engine/id_scope.py:CrossBranchReport + CrossBranchConflict + validate_multi_branch + claim_id + _build_multi_branch_index + find_cross_branch_conflicts` + `routers/validate.py:id-scope(?mode) + id-claim(新)` + `agent.py:_validate_id_scope(F4 id-claim+hold+SSE+next_id)` + 新增 `tests/test_id_scope_multi_branch_o16.py` 10（F1×4 + F4-claim×3 + F4-agent×3，autouse 重置单例）；全量 961 passed / 1 预存红（同 O14/O15） |
| **O17 方法 E Schema 血缘联动**（2026-08-18）| **E1-E3 闭环**（E4 agent 注入留 follow-up）：① E1 新建 `engine/column_lineage.py` — `compute_column_lineage(branch_roots) → ColumnLineageGraph`（扫多分支根目录所有 .xlsx 数据 sheet 列定义，row1=header/row2=type/row3=constraints，`present_in_branches` 记录列出现在哪些分支；`_capped` 后缀统一 table_key：pet_capped 与 pet 同 table）；`sync_preview(table, sheet)` 返回 trunk 有列其他分支缺的清单；`ColumnLineageEntry`/`ColumnLineageGraph` dataclass；② E2 `routers/structural.py` 新增 `compute_column_changes(src_cols, tgt_cols)` → `kind=column_added`（src 有 tgt 无的列，changed 需 type 比对留 follow-up）；③ E3 `routers/validate.py` 新增 `GET /structural/sync-preview?table=xxx&sheet=yyy`（扫 RESOURCES_DIR + 子目录建图 + sync_preview）。**残留 follow-up**：E4 agent `_capped` 写入前预检（agent.py 不写 _capped，注入语义需 testtest 有 capped 目录才能 e2e）、column_changed 需 type 数据（现仅 set 比对）、前端 sync-preview 拦截卡 | `engine/column_lineage.py(新:ColumnLineageGraph+compute_column_lineage+sync_preview+_scan_sheet_columns)` + `routers/structural.py:compute_column_changes(新,kind=column_added)` + `routers/validate.py:GET /structural/sync-preview(新)` + 新增 `tests/test_column_lineage_o17.py` 12（E1×4 + sync-preview×3 + E2×3 + helper×2）；全量 973 passed / 1 预存红（同 O14-O16） |
| **O18 方法 G 阶段一 BM25 RAG**（2026-08-18）| **G1/G2/G4 闭环**（G3 few-shot + G6 A/B 评估留 follow-up）：① G1 新建 `agent/excel/rag_searcher.py` — `bm25_search(query, index_path, top_k) → list[RAGHit]`（Okapi BM25，复用 `_table_index.json` 的 `search_blob` + `segmenter.segment` jieba 分词；search_blob 按 `\n` 分值独立分词保值边界；`_BM25Index` 单例缓存按 path 复用；`RAGHit` dataclass 含 path/stem/sheet/score/matched_terms）；② G2 `table_locator.py:locate_all` 末尾加 `_bm25_recall` 召回层（BM25 top-5 归一 confidence 0.3-0.5，低于规则 0.7-1.0 不压过，level="bm25"，与规则候选合并去重）；③ G4 env `CODEMAKER_RAG_MODE=bm25|off`（默认 bm25，off 关闭召回向后兼容）。**残留 follow-up**：G3 dialog_failures few-shot 注入 DecomposeAgent/Validator prompt（需 prompt 接线）、G6 skill_ab_test/table_case_eval 加 BM25 对照维 A/B 评估（locate_rate 不退化断言）、阶段二 vector（sentence-transformers/faiss/chromadb 装依赖） | `agent/excel/rag_searcher.py(新:_BM25Index+bm25_search+RAGHit+_tokenize)` + `agent/excel/locator/table_locator.py:_bm25_recall(新)+locate_all(注入,import os)` + 新增 `tests/test_rag_bm25_o18.py` 8（G1×4 + 缓存×2 + G2×2）；全量 981 passed / 1 预存红（同 O14-O17） |
| **O19 bench e2e 验证 + str+list 崩溃修复**（2026-08-19）| **R7 解封后首次真 LLM e2e 跑通 6 样例**（archive R7 记录过时）：① 起 codemaker serve (:8666) + backend uvicorn (:8000)，跑 `bench_4step.py` 全 6 样例；② **发现硬崩 bug**：S2/S5 `TypeError: can only concatenate str (not "list") to str` @ `agent.py:6754` `_run_verify_repair_loop` 的 `"已尝试：" + rctx.summarized_strategies()`（summarized_strategies 返 list[str] 非 str）；③ **修复**：改 `" | ".join(list)` 拼 str + 单次调用缓存（原调两次）+ 移除 diag 钩子（agent_service.py L1878/L2701 临时 traceback 捕获还原）；④ **e2e 基线指标**：S1 封印魔龙 ok=True 237s 5stages（部分完成，quest 2/8 表，Quest 18-23 行 6 条重复写入）；S2 幽冥宗 ok=False 45s 4stages→修复后 278s 5stages（正常失败上报：school/解锁等级列不存在）；S3 九尾天狐 ok=True 118s 5stages（pet 1/4 表）；S4 万圣狂欢 ok=False 256s 2stages（DecomposeAgent 错表：活动指令定位到 residence_building）；S5 聚灵塔 ok=False 248s→修复后 302s 5stages（部分完成 3/6 + 11 次失败）；S6 复合修改 ok=False 213s 5stages（2/7 成功）。**残留 follow-up**：S4 DecomposeAgent 错表（LLM 拆分把活动指令错配建筑表）、S1 Quest 重复写入（拓扑重跑或 intent 重复）、llm_calls=0（heartbeat 计数未对接 bench SSE）、覆盖度不足（S1/S3 expect 多表实际只写 1-2 表） | `agent.py:_run_verify_repair_loop(L6754 str+list 修复 + _strategies_str 缓存)` + bench 6 样例真 e2e 基线（`bench_s{1-6}.json` + `bench_fengyin_result.json`）；单测 981 passed / 1 预存红（同 O14-O18，无新增回归） |
| **O20a-c bench 失败修复**（2026-08-19）| 需求文档 `docs/bench_failure_fix_requirements.md` §1-3 代码层修复：① **O20a S4 错表**：`alias_mapping.json:8` `"道具"→item.xlsx`（原 assistant_level 错表）+ `decompose_agent.py:_run_one` LLM 输出 intent.table 校验在候选集内（防幻觉表）+ `error_classifier.py:46` `_COL_NOT_FOUND_RE` 加 `未找到列`/`无法匹配目标列` 反向匹配（原仅"列[X]...不存在"顺序反序落 UNKNOWN 短路）；② **O20b S1 重复写入**：`validator_agent.py` 新 `_dedup_intents`（按 stem/sheet/action/locator/fields_hash 去重，区别 `_suppress_over_produce` 仅去 produces 过产）+ `validate_two_layer` 4-step 路径接去重；③ **O20c S6 modify 失败信号**：`agent.py:_run_set` 多字段/单字段失败入 `res.failures` 带 `{failed_col, failed_val, kind}` + `error_classifier.classify` 优先读 res.failures 结构化 failed_col（kind=column_not_found 直接定类免 regex 漏）。e2e：S2 回归确认 classifier 生效（O19 str+list 修复 + O20a-c 无副作用）。**残留**：S1 实跑仍 6 条重复（fields 含不同占位符 sig 不同不去重，需 DecomposeAgent prompt 约束 follow-up）、S4 parser 崩（`_parse_via_llm` 空 LLM 响应非 O20 引入）、S1/S3/S4 覆盖度（O20d 候选多跳+全链兜底，含 LLM 能力缺口） | `alias_mapping.json` + `decompose_agent.py:_run_one(候选校验)` + `error_classifier.py:_COL_NOT_FOUND_RE(regex 放开)+classify(读 res.failures)` + `validator_agent.py:_dedup_intents+validate_two_layer 接线` + `agent.py:_run_set(failures 入 res.failures)` + 新增 `tests/test_dedup_intents_o20b.py` 8 + `tests/test_run_set_failures_o20c.py` 5；全量 994 passed / 1 预存红（同 O14-O19） |
| **O20d 覆盖度（S1/S3/S4 候选策略 + 全链兜底）**（2026-08-19）| 需求文档 `docs/bench_failure_fix_requirements.md` §4 代码层修复：① **LocatorAgent `_expand_by_fk` 多跳传递闭包**：单跳 → BFS 多跳（2 跳上限，env `CODEMAKER_LOCATOR_FK_HOPS` 默认 2），置信度按跳衰减（hop1=0.50/hop2=0.40），双向邻接表（任一端命中候选即扩对端），缓解 S1/S3 候选不全（如 quest→combat→reward 2 跳扩到 reward）；② **DecomposeAgent `_full_chain_fallback` 全链 LLM 兜底**：单表并发产 <2 intent 且 jobs≥2 时，补一次全候选 schema 合置单 prompt（识别"任务→战斗→奖励"业务链，LLM 看完整跨表 schema 而非每表孤立），兜底产出 > 单表则覆盖，走同一 `_to_split_intents` + 候选校验（幻觉表过滤），独立 cancel event（不复用 F3 fail-fast 的 `_local_ce`，免旧 fail-fast 取消兜底新调用），缓解 S1/S3 单表漏拆；③ **占位符未解 → skip 写库 + failure 上报**：`agent.py:5946-5960` 占位符残留时已 append `placeholder_unresolved` failure，但 5961 仍进 `_dispatch` 写库污染数据，改为 append failure 后 `res.ok=False` + `return res` 跳写库（保 D6 不静默吞 + 不留半成品，res.failures 已聚合→`_phase_summarize` 上报）。e2e 阻 R7（serve 未在线），证据用确定性单测。**残留**：LLM 能力缺口（单表漏拆子任务，G3 few-shot RAG 注入 DecomposeAgent prompt 长期解）、S1 6 条重复根治（O20b follow-up）、S4 parser 崩（独立项） | `locator_agent.py:_expand_by_fk(BFS 多跳+置信度衰减+CODEMAKER_LOCATOR_FK_HOPS env)` + `decompose_agent.py:_full_chain_fallback(新:全候选 schema 合置单 prompt+幻觉过滤+独立 cancel event)+decompose(触发条件<2&jobs≥2)` + `agent.py:5946-5960(占位符残留→res.ok=False+return res 跳写库)` + 新增 `tests/test_coverage_o20d.py` 12（locator 多跳×7 + full_chain_fallback×4 + placeholder skip×1）；全量 1006 passed / 1 预存红（同 O14-O20c，零回归） |
| **O20e S1 重复写入根治（O20b follow-up）**（2026-08-19）| O20b 残留 follow-up 收尾：S1 实跑仍 6 条重复——6 候选表各产 1 条 Quest intent 但 consumes 引用不同 producer label → fields 占位符值不同（`<new_combat_id>` vs `<new_reward_id>` 等）→ `_dedup_intents` fields_sig 不同不去重。**双保险修复**：① `validator_agent.py:_dedup_intents` fields_sig 计算把 `<...>` 占位符值归一为 `<ph>`（正则 `<[^>]+>` sub），消除跨候选 prompt 产同表 intent 因 consumes 引用不同 producer label 的假性差异，占位符差异非真实业务差异（仅 LLM 对 consumes 的不同引用），去重应忽略；真实字段值差异（如 state=idle/collect 不含 `<...>`）不受影响保留；② `decompose_agent.py:_build_prompt` 加 prompt 强约束"同一表同一 sheet 只产一条主配置，不要因 consumes 占位符不同而重复产同配置"。e2e 阻 R7，证据用确定性单测。**残留**：LLM 能力缺口（单表漏拆，G3 长期）、S4 parser 崩（独立项） | `validator_agent.py:_dedup_intents(占位符归一 <ph>+正则 _PH_RE)` + `decompose_agent.py:_build_prompt(同表同 sheet 只产一条主配置约束)` + 新增 `tests/test_dedup_intents_o20b.py` TestDedupPlaceholderNormalizationO20e 5（占位符仅差异去重×1 + 真实字段差异保留×1 + 混合×1 + 多占位符字段×1 + 非字符串字段不受影响×1）；全量 1011 passed / 1 预存红（同 O14-O20d，零回归） |
| **O20f S4 parser 崩修复（_dry_run_chat parse 降级 parse_multi）**（2026-08-19）| 需求文档 §0 S4 "parser 崩"根治：S4 v2（O20a 后）done_message "codemaker 解析失败"——bench dry_run 预览路径走 `agent_service.py:_dry_run_chat` 2586 `self.agent.parser.parse(text)` 单意图解析，S4 万圣狂欢 6 表 add+modify 混合指令单 parse 易超时/空响应返 None → raise RuntimeError → 2587 catch 返回错误响应（崩在 s1_parse 阶段，stages_seq 仅 [s1_parse, summary]）。**修复**：`_dry_run_chat` 2586 `parse` 失败时降级 `parse_multi`（复杂跨表指令多意图解析更健壮，含规则快速路径 + LLM 多意图），取首条 intent 作为定位 intent；`parse_multi` 失败返空 list 不 raise（5.7/6.3 已处理），仍无 intent 才返回错误响应（真正 LLM 不可用）。e2e 阻 R7，证据用确定性单测。**残留**：LLM 能力缺口（单表漏拆，G3 长期）、S4 实跑验证（serve 起后跑 S4 确认降级链生效） | `agent_service.py:_dry_run_chat(2586 parse 失败→降级 parse_multi 取首条+仍空返回错误)` + 新增 `tests/test_dry_run_parse_fallback_o20f.py` 4（parse 失败降级 parse_multi 成功×1 + parse_multi 也空返回错误×1 + 无 parse_multi 属性返回错误×1 + parse_multi raise 不二次崩×1）；全量 1015 passed / 1 预存红（同 O14-O20e，零回归） |
| **O20g set/delete locator 兜底提取（"删除名称为X的行"崩溃修复）**（2026-08-19）| 用户实测「删除活动名称为春节活动的行」崩：Step3 `match_locator` 成功（列活动名称 3, mode=contains）但 `locator_value=None` → `locate_row: 缺少行定位值` → 通用 error_feedback retry → 未知错误中断。**根因**：DecomposeAgent `_to_split_intents` 只提取 table/sheet/action/fields/produces/consumes，**漏产 locator_field/locator_value**（prompt 模板无这俩字段），"删除名称为X的行"类 set/delete 指令无行定位信号。**双保险修复**：① `decompose_agent.py:_to_split_intents` 提取 LLM JSON 的 `locator_field`/`locator_value` + `_build_prompt` 加这俩输出字段说明（"set/delete 用 locator_field+locator_value 标注定位行"）；② `agent.py:_run_set`/`_run_delete` locate_row 前 locator_value 为空时调新 helper `_fill_locator_from_fields` 从 fields 字典按 `loc_match.column`（已解析定位列名）兜底提取值填 `intent.locator_value`（占位符 `<...>` 不提取，delete 操作定位列从 fields 移除避免误写，set 保留），`loc_match.column` 含后缀（如 `类型:int`）取 `:` 前段匹配。e2e 阻 R7，证据用确定性单测。**残留**：LLM 能力缺口（单表漏拆，G3 长期）、实跑验证（serve 起后跑"删除X的行"确认不再崩） | `decompose_agent.py:_to_split_intents(提取 locator_field/locator_value)+_build_prompt(加 locator 输出字段说明)` + `agent.py:_fill_locator_from_fields(新:fields 兜底提取+占位符跳过+delete 移除定位列+set 保留+后缀去:)+_run_set/_run_delete(locate_row 前接兜底)` + 新增 `tests/test_locator_fallback_o20g.py` 11（_to_split_intents 提取×3 + _fill_locator_from_fields×8）；全量 1026 passed / 1 预存红（同 O14-O20f，零回归） |
| **O20h llm_calls=0 可观测性修复（dry_run counter 共享）**（2026-08-19）| 需求文档 §5 P2 修复：bench_4step.py 6 样例 `llm_calls=0`。**根因**：bench `dry_run=True` 走 `agent_service.py:_dry_run_chat` 2624 构造 `tmp_agent = TableAgent(...)`，TableAgent.__init__ 611 新建独立 `_llm_counter`；而 heartbeat loop 2232 `c = getattr(self.agent, "_llm_counter", None)` 读**主 agent** counter（永 0，主 agent run 未走 dry_run 路径）→ bench SSE heartbeat `llm_calls=0`。bench_4step.py:131-132 已正确解析 heartbeat event 的 `llm_calls` 字段，agent_service.py:2232-2233 heartbeat 已正确推 `peek_total()`，字段对齐无 bug，仅 counter 实例隔离。**修复**：`_dry_run_chat` 2638 共享属性列表加 `"_llm_counter"`，tmp_agent 构造后 `setattr(tmp_agent, "_llm_counter", main_counter)` 共享主 agent 实例，tmp_agent run 内 LLM 计数（4096 下传 parser + 4434/6299/6325 inc）实时累计到主 counter → heartbeat `peek_total()` 非 0 → bench `llm_calls` 非 0。run() 开头 4084 reset 共享 counter 是期望行为（dry_run 预览计数应独立于主 agent 历史，bench 单样例独立 session 无影响）。e2e 阻 R7，证据用确定性单测。**残留**：实跑验证（serve 起后跑 bench 6 样例确认 llm_calls 非 0） | `agent_service.py:_dry_run_chat(2638 共享属性列表加 _llm_counter)` + 新增 `tests/test_dry_run_parse_fallback_o20f.py` TestDryRunChatCounterSharedO20h 2（源码断言 _llm_counter 在共享列表×1 + tmp_agent 共享主 counter×1）；全量 1028 passed / 1 预存红（同 O14-O20g，零回归） |
| **O20i #30 required_fields.yaml 自动生成（§6 跨模块）**（2026-08-19）| §6 跨模块 TODO #30 收尾：required_fields.yaml 原"README 宣称但缺"→ O20i 前部分落地（独立文件空跑），自动生成（由 index 非空列派生）留 TODO。**修复**：① `table_index.py:SheetMeta` 加 `col_non_empty: list[int]` 字段（per-col 非空计数，向后兼容默认空）；`_scan_sheet` 行遍历内统计 per-col 非空（非 None 且 strip 非空）；`load_index` 反序列化 `s.get("col_non_empty", [])` 兼容旧索引；② 新建 `skills/derive_required_fields.py` 派生脚本：从 index 统计每表每 sheet 每列非空率 `col_non_empty[c] / row_count`，≥ 阈值（默认 0.9）→ 必填列；row_count<2 跳过（统计无意义）；col_non_empty 空/长度不匹配跳过（旧索引兼容）；手工条目优先（同 stem+sheet 不被派生覆盖）；输出 yaml 顶层 `required_fields` key 包裹（与 `_load_required_fields` 读取对齐）；③ 重建 `_table_index.json`（83 tables 含 col_non_empty）+ 跑派生 → 必填配置写入 `required_fields.yaml`。e2e 阻 R7，证据用确定性单测。**残留**：#22 produces 阈放宽（暂缓需 eval）、#37 run_one_case --no-raise（已实现，TODO 标记滞后）、变体样例基线（阻 R7 需 serve） | `table_index.py:SheetMeta(col_non_empty 字段)+_scan_sheet(per-col 非空统计)+load_index(兼容旧索引)` + 新建 `skills/derive_required_fields.py(派生脚本:非空率阈值+手工优先+required_fields key 包裹)` + 重建 `_table_index.json` + 派生 `required_fields.yaml` + 新增 `tests/test_derive_required_fields_o20i.py` 9（SheetMeta col_non_empty×2 + derive 派生×6 + _load_required_fields 集成×1）；全量 1037 passed / 1 预存红（同 O14-O20h，零回归） |
| **O21 健壮性 + 真错误 + 表格交互 + 影响结果才阻塞**（2026-08-19）| 用户反馈四原则：agent 不应因一个错误全部失效；报错必须真错误非假错误；错误需明确清晰；交互应给表格填而非让用户输入句子；只有影响结果的错误才阻塞。**四改动**：① **健壮性**（`agent.py:4780` Step5 主循环）：`_phase_execute` 调用包 try/except 兜异常 → `dispatch_exception` failure 上报（保 D6）+ `broken_producers` 标记 + `failed_tables` 追加 + `continue` 下一 op（独立任务不受影响）。原 `_phase_execute:6046` 内部 try/except 仅 rollback 后 `raise` 重抛 → 单 op raise 中断后续所有 op；② **表格交互**（`agent.py` 3 处 ask 点）：占位符 ask（5955）suggestion 改"在下方表格按列填入具体值"（原"补一句自然语言"与 example 字段填法矛盾）；verify_repair 达上限 ask（6818）加 `example` 字段 + suggestion 改"在下方表格按失败列填入正确字段值"（原"重写这一条指令"纯句子）；悬空 FK ask（4928）加 `example` + suggestion 改"在下方表格填入需补建的目标行的主键值"（原"补一条指令"纯句子）。mode=field 路径已支持（5967/6835 读 fix_payload.fields），统一引导用户走 field 模式填表格；③ **真错误判定**（`error_classifier.py:225`）：headers 比对去 `:` 后缀（`"类型:int"` 取 `"类型"`），避免 LLM 产的带后缀列名 `not in headers` 误判为列不存在（实为列名+类型标注，列存在）→ 假 COLUMN_NOT_FOUND；④ **影响结果才阻塞**（`agent.py:4953`）：`CODEMAKER_CONNECTIVITY_DEEP_CHECK` 默认值 `"0"`→`"1"`，判定 `== "1"`→`!= "off"`，悬空 FK（指向不存在的行，影响结果）默认 on 阻塞 ask 用户补建，env=off 显式关闭（向后兼容降级路径）。原 opt-in 默认 off 致影响结果的悬空 FK 不检测不阻塞。**O20i 残留修复**：`_table_index.json` 重建落盘 col_non_empty（O20i 记"重建"实际 json 未落盘，O21 跑全量暴露 `test_load_index_old_format_compat` 红 → 重建 83 tables json 含 col_non_empty → 测试绿）。e2e 阻 R7，证据用确定性单测。**残留**：bench e2e 实跑验证（serve 起后跑 6 样例确认 Step5 异常兜底 + ask field 模式 + 悬空 FK 阻塞生效）、前端 field 模式渲染确认（minified 需重建）、`_table_index.json` 全量跑被刷回旧版（某测试触发 build_index 覆盖，O20i 测试隔离问题留 follow-up） | `agent.py:Step5 主循环(4780 try/except 兜 dispatch_exception failure+continue) + 3 处 ask suggestion(5955/6818/4928 改 field 模式引导+example 字段) + CODEMAKER_CONNECTIVITY_DEEP_CHECK(4953 默认 on!=off)` + `error_classifier.py:classify(headers 比对去:后缀避假 COLUMN_NOT_FOUND)` + 重建 `_table_index.json`(83 tables 含 col_non_empty) + 新增 `tests/test_robustness_o21.py` 13（classify headers 后缀×4 + Step5 try/except 源码断言×3 + ask field 模式源码断言×4 + deep check 默认 on×2）；全量 1050 passed / 1 预存红（同 O14-O20i，零回归） |
| **O22 §9.1 replan-on-failure（Plan-Execute 显式化）**（2026-08-19）| §9.1 P0-1 深度路线首项。设计文档 §11.2：ExecuteAgent 失败即入 failures 上报无重规划 → 增Agent（≤1 LLM/轮）结合 failures.root_cause + remaining 子任务重新拓扑产修订 SubTask[] 回 Step5 重跑。与 §D4 不冲突（D4 否决"执行阶段现场 LLM 推理"，replan 是"失败后离线重规划"，增量 LLM 触发点默认关）。**实现**：① 新建 `subagent/replan_agent.py` — `ReplanAgent` 类：`replan(failures, remaining_intents, produced, user_text, cli) → list[NLIntent]`，LLM prompt 给 failures 清单 + remaining op schema + produced ID + 重规划原则（补建/改字段/跳过），产修订 JSON → `_to_nl_intents` 转 NLIntent[]（source='replan' 标记）；`_call_llm` 复用 DecomposeAgent 隔离 session 模式（`_isolated_empty_dir`）；`_parse_json_array` fenced ```json```/裸数组兼容；门控 `replan_enabled()=CODEMAKER_REPLAN_ON_FAILURE=0` 默认关 + `replan_max_rounds()=2` 上限防死循环；② `agent.py:__init__` 初始化 `_replan_agent`（与三 agent 同 try 块）；③ `agent.py:_run_replan_phase`（新方法，Step5+backfill 后、Step6 前扫前接入）：聚合失败 partition failures + remaining NLIntent → ReplanAgent.replan → 重跑修订 op（`_resolve_table`+`_resolve_sheet` 解析表/sheet + `_resolve_placeholders` 占位符替换 + `_phase_execute` 单 op 执行 + `_capture_produced` 产出 ID）→ 成功 op 的 result_rows/steps 聚合到顶层 all_result_rows/all_steps；上限 max_rounds=2 轮（每轮后重评估仍失败才下一轮）；失败/空响应/异常降级走原 Step6 上报。e2e 阻 R7，证据用确定性单测（Mock LLM）。**残留**：e2e 实跑验证（serve 起后跑"中途失败"用例对比直接上报 vs replan success_count 提升）、A/B 评估（§9.1.4）、openspec 提案（§9.1.5 `openspec/changes/replan-on-failure/`）、replan LLM prompt 优化（failures/remaining 截断策略 + produces 注入） | 新建 `subagent/replan_agent.py`(ReplanAgent 类 + replan_enabled/max_rounds 门控) + `agent.py:__init__(_replan_agent 初始化) + _run_replan_phase(新方法:批级失败聚合→replan→重跑 Step5+上限 2 轮)` + 新增 `tests/test_replan_agent_o22.py` 20（正常路径×3 + 降级×5 + JSON 解析×3 + 字段映射×4 + 门控/上限×3 + source 标记×1 + 异常兜底×1）；全量 1069 passed / 1 预存红 + 1 O20i 残留 json 刷（与 O22 无关，零回归） |
| **4-Step §0.1**（20 项）| ParseAgent 类 + schema 驱动 LLM 拆分 + produces 推断 + splitter_baseline 兜底 + NLIntent 扩展 + Step1 入口 + execute_no_llm + 占位符断言/拓扑派发 + validate_field_layer(6 项)+validate_fk_layer+IssueType/assemble_tips+ask_user+_phase_summarize failures | `parse_agent.py`/`validator_agent.py`/`agent.py` + 单测 |
| **O13 R7 serve 审计 + 诊断探针**（2026-08-18）| R7 根治**审计**（非根治，根因在 codemaker serve 侧非本仓库可修）。审计确认仓库侧缓解全部在位（session 隔离 `_isolated_empty_dir` + `CODEMAKER_SUBAGENT_ISOLATE_CONTEXT=1`/`CODEMAKER_AI_ENHANCER_ISOLATE_CONTEXT=1` 默认 on + scoped-decompose per-candidate + 超时可配 + 熔断 + 规则兜底）。新增 `tests/r7_serve_probe.py` 诊断探针（serve 可达时表征 R7 + 修复后验证，opt-in `CODEMAKER_R7_PROBE=1`）。文档化 serve 侧根治需求（关 auto-context / 纯文本补全端点 / serve 日志排查） | `tests/r7_serve_probe.py` + `tests/reports/archive/r7_serve_audit_O13_20260818.md`；探针机械验证通过（serve 不可达→exit 3，符合预期） |
| **R1–R8c** | R1 别名污染（reward 支线）/ R2 quest 不可达 / R3 5 级列名匹配 / R4 歧义消解+FK 扩表 / R5 DecomposeAgent FK 朝向扩表 / R6 splitter 盲区跳模板 / R7 option_go+branch_conv / R8 type_aliases 嵌套字段 / R8b 关系图 produces 推断层 / R8c verify-repair fix_fields+显式 PK 字面代换 | 历史（见归档文档）|
| **R9-D/A1/B1/C1** | 批注守门（D1-D8，D4 除外）/ 公式守门 audit（A1/A3-A6，A2 除外）/ pre_commit_hold 漏行预检（B1/B2/B4/B6-B8）/ PATCH_CONFIG 5 坑守门（C1/C3/C5-C7）| `cli_interface` / `precommit_hold.py` / `engine/patch_validator.py` |
| **openspec/agent-verify-repair-loop** | verify→repair 迭代环（写后规则校验门控+失败修复，3 轮）| tasks 全 `[x]` |
| **openspec/perf-accuracy-merge-stall-overhaul** | merge 性能正确性大修（Batch 1-4）| tasks 全 `[x]`（Batch3.5/3.6 待办见 §5）|
| **openspec/skill-ai-anti-pattern-induction** | skill AI 反模式归纳自学习 | tasks 全 `[x]` |
| **TODO_OPTIMIZATION 已完成项** | #3 双命名空间 / #9 alias 自动生成 / #16 locator import 修复 / #19 ValidatorAgent 接真 LLM（opt-in 默认 off）/ #26 StepAIEnhancer session 隔离 / #29 repair_playbook LLM 细分 / #38 Step6 failures / #39 交互纠正 / #28/#12/#22/#25 验证集 等 | 见归档 `TODO_OPTIMIZATION.md` §执行进度表 |

---

## 2. 开放待办 — excel-agent 4-Step 主线

### P0 阻断（外部依赖）
- [ ] **R7 serve 根治**：codemaker serve agentic LLM 慢/贵 + serve 侧 auto-context 读文件。**非 excel-agent code 可修**。4-Step e2e 与跨链指标上不去的根因；§2.1/§3 e2e、跨链样例 5-7 全阻塞于此。→ scoped-decompose 临时绕。来源：`diagnosis` R7 / `4step-loop-task` §0.4 / `总结` 进阶。
  - **O13 审计结论**（2026-08-18）：仓库侧缓解全部在位（session 隔离 + scoped-decompose + 超时可配 + 熔断 + 规则兜底，详见 `tests/reports/archive/r7_serve_audit_O13_20260818.md`），无更多仓库侧优化空间。根治需 serve 侧改：① 关 auto-context-grounding ② 提供纯文本补全端点 ③ serve 日志排查 xlsx 读取返空。探针 `tests/r7_serve_probe.py`（opt-in `CODEMAKER_R7_PROBE=1`）供 serve 修复后验证（healthy→exit 0 → 跑 LLM e2e）。

### P1 4-Step 收尾（阻塞于 R7）
- [ ] e2e/A/B + 全量回归基线（`§1.4/§2.12/§3.8/§4.9/§5.5/§6.1-6.4` + TODO #36 全量用例基线表）— 阻 R7
- [ ] verify-repair 完整版抽文件（464 行 +:6109-6372` → 独立模块）— **O21 审计暂缓**：8 方法分散 6194-7098 跨 900 行，深耦合 self 数十属性/方法（cli/auditor/skill_executor/_ai_enhancer/repair_playbook/enable_*/_run_add/_run_set 等），抽文件改动面大易回归，纯重构零功能增益。待 verify-repair 稳定后或借其他轮次触该区域时附带抽。
- [x] **6 步路径补传 `validate_two_layer`（默认关 6 项）**（O22 核实 2026-08-19）：`agent.py:4529` 6 步路径（`CODEMAKER_4STEP_LOOP=0`）已调 `validate_two_layer`（传 schema_getter + data_getter），含 `validate_field_layer`（字段层 6 项）+ `validate_fk_layer`。4 步路径（主线，4219）同已接。ledger 原"默认关 6 项"实为 `validate` 旧入口缺 schema_getter 时不跑字段层（validator_agent:93），`validate_two_layer` 总跑字段层。**已完成**。
- [x] **O20d 占位符未解 skip 写库**（2026-08-19）：`agent.py:5946-5960` 占位符残留 → append `placeholder_unresolved` failure 后 `res.ok=False` + `return res` 跳 `_dispatch` 写库（原仍写库污染数据），保 D6 不静默吞 + 不留半成品。
- [x] **ExecuteAgent 跳 skipped（`_phase_execute` 加 `validation.skipped` 检查）**（O22 核实 2026-08-19）：`agent.py:5964-5971` 已实现 skipped 早返检查。O3 后 validate_two_layer 非阻断（ok=True 恒）不标 skipped → 此分支 dormant（死代码），注释明"保留供 O7 引 partial 态复用"。**代码在位，dormant 合理**。
- [ ] HTTP schema_bundle 化（独立部署时 `build_data_getter` 走 HTTP）+ `_suggest_cache` 复用 — 部署阶段优化非链路常态，留待独立部署时做

### P1 深度路线 §9（新模块，O22 首项已落地）
- [x] **§9.1 replan-on-failure**（O22，2026-08-19）：`ReplanAgent` 新建（`subagent/replan_agent.py`）+ `agent.py:_run_replan_phase` 接入（Step5+backfill 后、Step6 前）+ 门控 `CODEMAKER_REPLAN_ON_FAILURE=0` 默认关 + 上限 2 轮。残留：e2e 实跑验证 + A/B 评估 + openspec 提案 + prompt 优化
- [ ] §9.2 多 Agent 动态编排 + Reviewer（P0-2）
- [ ] §9.3 三层记忆（P1-1）
- [ ] §9.4 ToolRegistry + 风险分级审批（P1-2）
- [ ] §9.5 预算驱动模型分层（P1-3）
- [ ] §9.6 安全沙箱 + 聚合监控（P2）

### P2 双骨架统一（方案 A/B/C/D，`excel-agent与merge优化总结`）
- [ ] **方案 A**：CoreEngine + 规模分层，CRUD 链复杂意图走 dispatcher 多 Agent 并行（直接解 R7 慢/贵）
- [ ] **方案 B**：CRUD 断点续跑 + 增量 patch（`CODEMAKER_CRUD_CHECKPOINT=1` opt-in）
- [ ] **方案 C**：Pre-commit hold 下沉 CRUD 链（`_phase_execute` 前 preflight_op，`CODEMAKER_CRUD_PREFLIGHT=warn`）
- [ ] **方案 D**：Evidence×RAG×AntiPattern 三层自学习闭环（协同 §3 方法 G）

### P2 4-Step 框架后续
- [ ] splitter `sheet_hint` 路由保护 / LLM 路径 schema 注入校验 / pet_evolve 模板字段对齐（R8c.5）
- [ ] produces 推断增强：consume-eligible 加显式 id match（R8b.5）
- [ ] TODO #6 阶段2/3：11 模板灰度替换 DecomposeAgent（需 #36 基线）
- [ ] TODO #24 阶段2：删旧 table_resolver，`_resolve_table` 不再 fallback
- [ ] TODO #10 表索引分片/懒加载（76MB 启动慢） / #14 FK 图提示注入 prompt / #11 LocatorAgent 解耦召回 / #25 column_aliases 拆 per-table

### P3 R9.4 用户 5 问后端大改（规划中，`优化全过程` 未登记）
- [ ] Step1 并行 / parse 超时自适应 / LLM token 流式 / 扩模板命中跳 parse_multi / ProcessPool 隔离

---

## 3. 开放待办 — 事前预防横切（八方法）

> 来源：`docs/事前预防优化TODO.md` + `docs/优化全过程.md` R9 系列。D/A/B/C 首版已落地（见 §1），下列为升级 + E/F/G/H。

- [x] **AD1-AD3**（P0，O15 完成）：方法 A/D 升级接 `pre_commit_hold`（kind=formula_loss/comment_loss）+ `CODEMAKER_FORMULA_GATE=hold` 触发 hold 事件 + 二次做差
- [x] **A2**（P0，O15 完成）：agent 层 `_run_set`/`_run_add` 消费 `needs_manual_fix`/`hold_events`（经 `_handle_cli_hold_events` → res.failures 软失败 + SSE）
- [x] **方法 F**（P0，O16 完成）：编号账本跨分支 `validate_multi_branch` + `claim_id` + `routers/validate` ?mode=multibranch + `id-claim` + `agent.py _validate_id_scope` id-claim 注入
- [x] **方法 E**（P1，O17 完成 E1-E3）：Schema 血缘联动 `engine/column_lineage.py` 新建 + `structural.py` 加 column_added + `routers/validate` sync-preview 路由（E4 agent 注入留 follow-up：agent 不写 _capped）
- [x] **方法 G**（P0/P1，O18 完成 G1/G2/G4）：领域 RAG 阶段一 BM25（`excel/rag_searcher.py` 新建，jieba + _table_index search_blob，G3 few-shot + G6 A/B 评估留 follow-up）
- [ ] **方法 H**（P0）：多 Agent 对抗网 Red/Blue/Auditor（`subagent/red_team_agent` 等）
- [x] **D4**（中，O15 完成）：`comment_replay_partial` audit 留痕（批注回写二次做差仍丢）
- [ ] **#34**：`anti_patterns.yaml`（85KB）接入 ValidatorAgent 实际使用

---

## 4. 开放待办 — validator 线（详见 `docs/validator_audit.md`）

> O1–O4 已消解 P1/P3/P4/P5/P15/P16/P18/P20。O5 已消解 P10/P11/P12/P17。O6 已消解 P13。O8 已消解 P22/P23/P19。O9 已消解 P21 + P24/P25 增强。O10 已消解 P26。O11 已消解 P9/P14 + P27 序列化层。O12 已接线 P27 save + resume 方法。**O14 已闭环 P27 resume（显式 env stall 触发 + completed_op_keys per-op 跟踪 + Step5 增量 save + produced 重算）**。下列为残留（P27 heartbeat auto-trigger follow-up + §2 P1/R7/§3 八方法/§5 merge/§6 跨模块）。

**第一批（结构性误杀 + 匹配假阳性，必先动）**
- [x] **P12** ✅(O5) 收紧 `_field_matches_col`/`_field_matches_fk`：子串 `fk in k or k in fk` → **精确等值 only**（`produces_inference.py:57` + `validator_agent.py:692`）。审计「精确等值+后缀」公式 `k.endswith("_"+fk)` 仍让 `model_id` 命中 `id`（model_id 以 `_id` 结尾），升级为精确 only 彻底消假阳性。
- [x] **P13** ✅(O6) `_validate_forward_refs_llm` produced 收集改用 producer 显式 PK 列（relation `to_column`），非 `"id" in kl` 启发式（`validator_agent.py:703`）。`producer_pk_cols` map + `_field_matches_fk` 匹配，仅收 relation 声明 PK 列 concrete 值。opt-in 路径，4-step 主线不调，补齐 P12 语义。
- [x] **P11** ✅(O5) produces 标签去冲突：`new_{stem}_id` → **sheet-aware** `new_{stem}_{sheet}_id`（`produces_inference.py:138`，sheet 缺省回退 stem 级）。注：`validate_two_layer`（4-step 主线）不调 `_align_produces_labels`，故 sheet-aware 标签不被 align 折叠；fallback `validate()` 路径仍会折叠（非主线，保留旧行为）。
- [x] **P10** ✅(O5) `add_keys` 同 key 覆盖 → `setdefault`（`produces_inference.py:121`），保留首 producer 候选，与 `_suppress_over_produce`「一表一 op 契约」语义一致。多 producer 列表扩展（同 stem 多 sheet 完整支持）留 follow-up。
- [x] **P9** ✅(O11) `_suppress_over_produce` 加 `multi_op_same_sheet` 标记（NLIntent 加字段，`_suppress` 跳过标记 op 保用户显式多 producer 同 sheet，仅抑制未标记 LLM 过产）。4-step `validate_two_layer` 不调 `_suppress` → 此修复仅 fallback `validate()` 路径生效，降级。
- [x] **P17** ✅(O5) `_should_consume`：`<auto>` 不当 consume（`produces_inference.py:73` 改 False，`<auto>` 留空不转占位，消 `_phase_execute` placeholder_unresolved 二次 ask）

**第二批（失败语义 + 状态机，归 O7）**
- [x] **P23/P22** ✅(O8) tips → failures 通道统一：NLIntent 加 `failures` 字段 + `attach_tips_as_soft_failures()` 把 validate_two_layer 遗留 tips 转 #40 形状软失败 dict 追加 intent.failures（4-step + 6-step 两路径接入）+ partition 创建时 transfer 到 res.failures → all_failures 聚合 + _phase_summarize 上报（保 D6，消 CI/非交互 continue 带病照样落盘）
- [x] **P24/P25** ✅(O9 增强 + O12 design 评估) skip 过滤点增强：4-step/6-step 两过滤点 thinking 列出 skipped stem/sheet（非仅计数，「不静默丢」供汇总单列「已跳过清单」）+ 注释 O3-dormant。注：O3 后路径无 intent 标 skipped → 过滤 + _phase_execute:5512 skip 分支均 dormant（no-op），真 partial 态需重激活 `_mark_validation_skipped`。**O12 评估**：O8 选 soft-failure 通道（P23）而非 partial-skip → 重激活 partial-skip 需反转 O3 非阻断设计，属 design 决策非接线，留 design follow-up。
- [x] **P19** ✅(O8) `execute_no_llm` × `enable_verify_repair_loop` 互斥校验（`agent.py:_check_p19_mutex_conflict` 方法，__init__ 调，同开 warning 提示，不强制改保用户意图）
- [x] **P21** ✅(O9) `validate()` 加可选 `schema_getter`/`data_getter` 参数，提供时跑 `validate_field_layer`+`validate_fk_layer` 合并 issues（Issue→str，FORWARD_REF_BROKEN 带「断链」关键字），消除「同输入不同路径结论不同」（两入口共享同一字段/FK 校验集合）；缺省 None→保留旧行为，现有调用方不传→不变

**第三批（事务 + 成本）**
- [x] **P26** ✅(O10) 批级事务/部分回滚：`_compute_rollback_targets` staticmethod + `CODEMAKER_BATCH_TRANSACTIONAL=1` opt-in strict 模式（任一硬失败回滚整批前序已 commit op，不限 G8 直接依赖，批级原子）。默认 off（保留 G8 链回滚：仅回滚失败步直接依赖 producer，避免牵连无关独立 op）。strict 场景：district 成功+combat 失败且非直接依赖时，默认留半成品→重跑 UNIQUE_VIOLATION；strict 回滚 district 整批原子。
- [x] **P27** ✅(O11 序列化层 + O12 接线 + O14 resume 闭环) 4-step NL 路径 checkpoint：NLIntent `to/from_checkpoint_dict` + TableAgent `_nl_checkpoints` + `_save/_load/_resume_from_checkpoint` + 4-step post_parse/post_validate save 接线（opt-in `CODEMAKER_4STEP_CHECKPOINT=1`）。**O14 resume 全闭环**：显式 env `CODEMAKER_4STEP_RESUME=<session_id>` 触发 stall 续跑（run() 入口调 `_resume_from_checkpoint`，skip Step1 parse）+ `completed_op_keys` per-op 成功跟踪（checkpoint dict 字段）+ Step5 增量回写（`_save_nl_progress`）+ resume 跳过已成功 op（filter `ordered_idx`）+ 从 checkpoint execution 重算 produced（`_capture_produced` 重放，免查库）。`_resume_from_checkpoint` 返三元组 `(intents, stage, completed)`。**残留 follow-up**：heartbeat auto-trigger（后台线程+竞态，留 serve 修复后）、e2e（阻 R7）。

---

## 5. 开放待办 — merge（降权附录 `[M]`）

> 用户确认 merge 问题目前较少，全链降权。R3/R6/R9 已落地（见 §1），下列为低优先残留。

- [ ] **P0-1/P0-2/P0-3**（高→降权）：无变更跳过提前到 `read_group_files` 前 / `_collect_table_keys` 按 (dir,mtime) 缓存 / base export 按 base_rev 磁盘复用
- [ ] **方案⑦ P1-1**（中）：解析结果磁盘缓存（(file_path,svn_rev) key）
- [ ] **方案⑧ P1-3**（中高）：公式/批注 sheet 向量化 + sparse 补差
- [ ] **方案⑥**（低）：apply 接入 `precommit_hold`（防漏行静默丢）+ B5 override 单独 audit + B3 返回值加 holds
- [ ] **C2/C4**：apply 接 `validate_capped_workbook` / 脱敏样例 `resources/sample_capped.xlsx`
- [ ] `parallel_map_tables` hang 防护（process future timeout 回退 ThreadPool）— #32 已修卡死，补 timeout
- [ ] **方案⑤**（极低）：`progress_cb` → SSE 接前端 progressBar
- [ ] **P2-1/P2-2**（高，先 PoC）：pysvn 替代 subprocess / 持久化 + 流式响应（大合并，改前后端协议）

---

## 6. 其它 / 跨模块

- [ ] 变体样例基线 2-4（quest_npc_variants）+ 跨链 5-7（cross_chain，引用一致 0.00 未达 0.24 平均）评估 — 暴露模板/路由/LLM 字段缺口
- [x] **#30 required_fields.yaml 自动生成**（O20i，2026-08-19）：SheetMeta 加 col_non_empty + derive_required_fields.py 派生脚本（非空率 ≥ 0.9 → 必填，手工优先）。78 表/171 sheet 写入。
- [ ] TODO #22 produces 阈放宽（暂缓，需 eval） / #18 RAISE 调试开关 / #37 run_one_case `--no-raise` CI smoke（已实现，TODO 标记滞后）

---

## 7. 已归档文档（移至 `docs/archive/`）

| 原文档 | 去向 |
|--- `TODO_OPTIMIZATION.md` | 37 项并入 §1 + §2 |
| `优化全过程.md`（root 旧版）+ `docs/优化全过程.md` | R1-R9 并入 §1 |
| `excel-agent-diagnosis.md` + `docs/excel-agent问题与优化方向.md` | R1-R9 根因并入 §2 |
| `docs/excel-agent与merge优化总结.md` | 路线图/方案 A-D 并入 §2 |
| `docs/excel-agent-4step-loop-task.md` | §0.1 并入 §1，§0.3-0.6 并入 §2 |
| `docs/excel-agent-4step-deep-optimize.md` | 历史记录并入 §1 |
| `docs/事前预防优化TODO.md` | 八方法并入 §3 |
| `docs/合并引导性能优化诊断.md` | merge 方案并入 §5 |
| `bench_v1.md` + `doc/优化指标对照表.md` | 数据快照，归档 |

**保留不归档**：`docs/validator_audit.md`（validator P9-P27 证据明细，本总账 §4 引用）/ `docs/excel-agent-4step-loop-design.md`（架构设计）/ `openspec/changes/*/`（spec）/ `README.md` / `.codemaker/优化工作流.md` / `doc/ca-overview.md` / merge SETUP/README（使用说明）。

---

## 8. 来源映射

| 本总账节 | 主要来源文档 |
|---|---|
| §0 架构方向 | `4step-loop-design` + `validator_audit` + openspec D1/D2 |
| §1 已完成 | `优化全过程`(docs) R1-R9 + `4step-loop-task` §0.1 + openspec tasks + validator O1-O4 |
| §2.1 P0 R7 | `diagnosis` R7 / `4step-loop-task` §0.4 / `总结` 进阶 |
| §2.2 P1 收尾 | `4step-loop-task` §0.3/§0.6 尾部 |
| §2.3 P1 §9 深度路线 | `4step-loop-task` §9 |
| §2.4 P2 方案 A-D | `excel-agent与merge优化总结` |
| §2.5 P2 框架后续 | `优化全过程` R8c.5/R8b.5 + `TODO_OPTIMIZATION` #6/#10/#11/#14/#24/#25 |
| §2.6 P3 R9.4 | `TODO_OPTIMIZATION` R9 行（规划） |
| §3 事前预防 | `事前预防优化TODO` + `优化全过程` R9-AD/E/F/G/H |
| §4 validator | `docs/validator_audit.md` |
| §5 merge | `合并引导性能优化诊断` + `excel-agent与merge优化总结` merge 段 |
| §6 跨模块 | `优化全过程` 变体样例 + `TODO_OPTIMIZATION` #22/#30/#37 |
