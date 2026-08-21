# 配表操作模式

CodeMaker 配表操作模式入口指引。

## 激活

在 CodeMaker 对话框输入 **"进入配表模式"** → CodeMaker 询问确认 → 确认后进入模式。

进入后 CodeMaker 仅用 skill 知识 + 后端原子 API 操作 `resources/` Excel 配表，不调后端完整 agent，用自身 LLM 理解意图。其他项目内容无权访问，无关请求被拒绝。

退出输入 **"退出配表模式"**，恢复完整能力。

## 安装

`.codemaker/` 不提交（svn:ignore），配表模式需各成员本地生成。**`tools/deploy.bat`（第 6 步）和 `tools/update.bat`（第 6 步）已自动安装，正常跑脚本即可，无需手动操作**。

仅当单独改了 `tools/table-mode/` 源文档或 `server/agent/excel/skills/` 想立即生效时，手动重装：

```bash
uv run python tools/table-mode/install.py
```

安装后生成 `.codemaker/table-mode/`：
- `配表操作模式.md` — 模式行为指令（CodeMaker 进入模式后遵循）
- `知识库.md` — 配表知识（实体路由/列别名/行定位/原子 API）
- `skills/` — skill yaml 原文（列别名/sheet 别名/行定位/反模式等）

## 激活机制

CodeMaker 识别"进入配表模式"指令后，读 `.codemaker/table-mode/配表操作模式.md`（未安装则读 `tools/table-mode/配表操作模式.md` 源）并遵循。

## 前提

后端服务运行中：`http://127.0.0.1:8000`（`python -m server.main` 或 `tools/start.bat`）。

## verify-repair 迭代环（capability: verify-repair-loop）

写操作执行后跑轻量规则校验门控（ref_integrity / id_scope / 类型约束 / anti_pattern，零 LLM）；
失败时进入 repair→execute 迭代环，按错误分类（ErrorType）走定向修复策略，最多 `VERIFY_REPAIR_MAX_ROUNDS` 轮。
**快路径优先**：成功路径零额外 LLM 往返，延迟无显著增加；失败路径最多 +2 轮（Level 1 规则修复零 LLM，Level 2 LLM+skill tools ReAct）。

### 配置开关（环境变量，默认开）

| 开关 | 默认 | 说明 |
|------|------|------|
| `ENABLE_VERIFY_REPAIR_LOOP` | `1` | 迭代环总开关。`0` 退回原线性 pipeline + 单轮 retry |
| `ENABLE_SKILL_TOOLS_RECOVERY` | `1` | repair Level 2 是否绑定 skill tools。`0` 退回纯 LLM 诊断 |
| `VERIFY_REPAIR_MAX_ROUNDS` | `3` | repair 最大轮数（含首次执行共 +1 次尝试） |
| `SKILL_TOOL_CALL_LIMIT` | `4` | 单次 Level 2 ReAct 的 skill tool 调用上限 |

### 错误类型 → 定向修复策略

`column_not_found`→列名候选重注+模糊匹配；`row_not_found`→row_aliases 扩展；`type_mismatch`→类型强转；
`id_conflict`→新 ID 分配；`pk_misplaced`→清空走自增；`cross_ref_broken`→级联依赖图检查；`formula_error`→公式引用修复。

达上限仍失败时，结果对象的 `thinking_steps` 携带结构化 `repair_failure`（error_type / root_cause / 已尝试策略列表）供汇总呈现，不静默吞错。
分类置信度低或同类型重复时自动记录反模式候选信号到 skill_updater 学习闭环。
