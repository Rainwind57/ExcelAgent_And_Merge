# AI 配表助手

AI 辅助 Excel 游戏配表工具，支持**自然语言操作配表**、**多版本差异比对合并**、**表格浏览搜索**、**数据一致性验证**。

## 功能

- **AI Chat** — 自然语言 → Excel CRUD（增删改查），复合语句/代词消解/多字段赋值
- **智能问答** — 询问配表结构/用途，LLM 基于 table_index 回答
- **版本比对** — 上传多版本 Excel，按主键对齐逐格 diff，冲突可视化解决，导出合并
- **表格浏览** — 60+ 配表的文件树浏览、分页查看、全文搜索
- **数据验证** — 引用完整性、唯一性、数值范围自动检查
- **批量操作** — 支持多条指令一次性执行

## 快速开始

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- [CodeMaker CLI](https://codemaker.netease.com/package/codemaker-cli/)
- Node.js（仅前端开发需要）

### 登录 CodeMaker 账户（每个用户必做一次）

LLM 由 `netease-codemaker` provider 提供，需登录**你自己**的网易 CodeMaker 账户（走你本人的额度）。**用 `tools\deploy.bat` 一键部署时会自动检测 CLI 并引导登录**，无需手动操作；如需手动：

```bash
codemaker providers login -p netease-codemaker
```

凭据存于本地 `~/.local/share/codemaker/auth.json`（per-user，不随项目分发）。deploy.bat / start.bat 会自动检测，未登录会阻断并提示。

> `.env` 里的 `OPENCODE_SERVER_USERNAME/PASSWORD` 只是本地 serve HTTP 接口的门禁，**与网易账户无关**，团队可共用。

### 环境搭建

```bash
uv sync
```

### 启动

**推荐方式：一键脚本**

```bash
# 双击或命令行运行（自动加载 .env + 启动 codemaker serve + FastAPI）
tools\start.bat
```

`start.bat` 会从 `.env` 读取环境变量并启动两个独立窗口常驻的服务。首次运行前若没有 `.env`，先跑 `tools\deploy.bat`（会从 `.env.example` 复制生成）。

**手动方式：需开两个终端**

> 注意：手动启动必须确保 `.env` 里的环境变量已注入到当前 shell，否则后端会回退默认模型导致调用失败。

终端 1 — CodeMaker Serve（LLM 底座）：
```powershell
# PowerShell
$env:OPENCODE_SERVER_USERNAME="codemaker"
$env:OPENCODE_SERVER_PASSWORD="CMniubi2026"
codemaker serve --port 8666 --hostname 0.0.0.0
```

终端 2 — FastAPI 业务后端：
```powershell
# PowerShell：先把 .env 的变量注入当前会话，否则会回退默认模型导致调用失败
Get-Content .env | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object { $kv=$_.Split('=',2); Set-Item -Path ("env:"+$kv[0].Trim()) -Value $kv[1].Trim() }

uv run python server/main.py
```

> 若用 cmd 而非 PowerShell，加载 .env 改为：`for /f "usebackq eol=# tokens=1,* delims==" %a in (.env) do @set "%a=%b"`

> codemaker serve 不可用时后端会报「AI 服务未启动」；模型额度耗尽/不支持会报「底层模型调用失败」——按提示处理即可。

**访问：**
- `http://127.0.0.1:8000` — 生产模式（后端直出前端页面）
- `http://localhost:5173` — 开发模式（`cd frontend && npm install && npm run dev`）
- `http://127.0.0.1:8000/docs` — Swagger API 文档

## 架构

主智能体（OrchestratorAgent）负责聊天对话 + 意图分类 + 调度，分发到 QA/CRUD 子智能体，统一走 codemaker serve LLM：

```
FastAPI :8000 ──HTTP──▶ CodeMaker Serve :8666
    │
    ├─ qa 分支  ──▶ QAHandler（LLM 基于 table_index 回答）
    └─ crud 分支 ─▶ TableAgent（LLM 解析 → ColumnMatcher 列名匹配 → openpyxl 读写 Excel）
```

## 项目结构

```
project/
├── server/                    # 后端
│   ├── main.py                # FastAPI 入口
│   ├── config.py              # 路径配置
│   ├── routers/               # API 路由（diff / agent / tables / workflow）
│   ├── engine/                # 差异比对 & 合并引擎
│   ├── agent/                 # AI 引擎（主子智能体架构，含 LLM 解析/列匹配/表定位/技能配置）
│   ├── models/                # Pydantic 模型
│   ├── services/              # 业务服务层
│   └── static/                # 前端打包产物
├── frontend/                  # Vue 3 + Vite 前端
│   └── src/views/
│       ├── AgentChatView.vue
│       ├── DiffMergeView.vue
│       └── TablesView.vue
├── resources/                 # 游戏 Excel 配表（60+ 文件）
└── merge/                     # 合并测试样例
```

## 使用示例

### AI Chat

**CRUD 操作：**
- "查询灵兽饕餮的所有属性"
- "将灵兽饕餮的物攻资质改为 1500"
- "新增一个灵兽，名称朱雀，品质神兽，成长率1.5"
- "删除灵兽名称为测试兽的行"
- "增加建筑名称为瞭望塔，赋值它的id是99999"（复合语句 + 代词消解）

**智能问答：**
- "灵兽相关的表有哪些" / "building表有哪些列" / "邮件相关的表格有哪些"

### Diff/Merge

选择基准文件 + 多个衍生版本 → 开始比对 → 逐个解决冲突 → 导出合并 Excel。

## 配表操作模式（第二形态）

除上述 Web 端 AI 配表助手（第一形态）外，项目还提供**配表操作模式**（第二形态）：在 CodeMaker 对话框激活后，CodeMaker 仅用 skill 知识 + 后端原子 API 操作 `resources/` 配表，轻量、受限、与其他项目内容隔离。

**启动第二形态：**

1. 跑过 `tools/deploy.bat`（首次）或 `tools/update.bat`（更新）——脚本第 6 步已自动把配表模式文档装到 `.codemaker/table-mode/`，无需手动安装。
2. 后端服务运行中（`http://127.0.0.1:8000`）。
3. 在 CodeMaker 对话框输入 **"进入配表模式"** → 确认后进入。
4. 退出输入 **"退出配表模式"** 恢复完整能力。

模式内可查表/搜索/改单元格值/新增行/删行/插行/列增删/跨表 ID 校验，删插行带公式引用机械位移 + 缓存自动保护；不支持公式语义重写（L2 原语未暴露 HTTP，需退出用 Web 端 agent）。详见 `TABLE_MODE.md`。

## 技术栈

FastAPI + openpyxl + Vue 3 + Vite + codemaker serve LLM，uv 管理 Python 依赖。
