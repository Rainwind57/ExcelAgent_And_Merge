# O20a S4 错表修复 + classifier regex 放开

> 轮次：O20a（2026-08-19）
> 范围：需求文档 §1 P0 — S4 错表（alias 硬 bug + DecomposeAgent 幻觉表 + classifier regex 顺序）。见 `docs/bench_failure_fix_requirements.md` §1。

## 改动

| 项 | 文件 | 改动 |
|---|---|---|
| alias 修 | `server/agent/excel/alias_mapping.json:8` | `"道具": "assistant_level.xlsx"` → `"道具": "item.xlsx"`（S4 文本"道具"3 次命中错表 assistant_level，应为 item） |
| DecomposeAgent table 校验 | `server/agent/excel/subagent/decompose_agent.py:147` `_run_one` | LLM 输出 intent.table 校验是否在候选 stem 集内（防幻觉表错路由）。过滤不在候选集的 intent + thinking 上报。 |
| classifier regex 放开 | `server/agent/excel/repair/error_classifier.py:46` `_COL_NOT_FOUND_RE` | 加 alternation：`未找到列[X]`/`无法匹配目标列[X]` 正向匹配（原仅"列[X]...不存在"顺序，反序不匹配落 UNKNOWN 短路） |

## 验证

```
python -m pytest server/tests/test_decompose_agent.py \
               server/tests/test_verify_repair_loop.py -q
=> 25 passed   # 4 decompose + 21 verify-repair

python -m pytest server/tests/ -q
=> 981 passed, 1 预存红（同 O14-O19，无新增回归）
```

e2e 复跑：
- S2 幽冥宗：ok=False stages=5 正常失败（"school/解锁等级列名不存在" + "已试策略：轮1:column_not_found/column_candidate_remap"）— O19 str+list 修复 + O20a classifier 仍 OK，无回归。
- S4 万圣狂欢：stages=2 done="codemaker 解析失败"（parser 层 LLM 空响应，非 O20a 引入）— alias 错表源消除（residence_building 不再被道具 alias 误命中），但 S4 parser 崩是新问题（§4 覆盖度/LLM 能力缺口，留 O20d）。

## 残留

- S4 parser 阶段 `codemaker_parser._parse_via_llm` 返 None（S4 长 LLM 空响应）— 非 alias/DecomposeAgent/classifier 问题，属 parser LLM 能力缺口，留 O20d。
- DecomposeAgent 跨候选表 intent 重复（每 candidate prompt 返跨表链）— O20b 去重处理。
