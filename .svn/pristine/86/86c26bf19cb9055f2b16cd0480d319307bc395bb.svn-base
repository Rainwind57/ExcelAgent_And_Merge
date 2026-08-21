"""CodeMaker Serve API 集成说明。

## 架构概览

```
┌──────────────────────────────────────────────────────┐
│                    前端 (Vue)                         │
│                http://127.0.0.1:8000                  │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP REST
                       ▼
┌──────────────────────────────────────────────────────┐
│              FastAPI (业务后端 :8000)                   │
│  ┌──────────────────┐  ┌────────────────────────────┐ │
│  │  AgentService     │  │  Diff/Merge Engine         │ │
│  │  ┌──────────────┐ │  │  (openpyxl 本地操作)       │ │
│  │  │ CodemakerNLP  │ │  └────────────────────────────┘ │
│  │  │ Parser        │─┼──── HTTP ────────────────────┐ │
│  │  └──────────────┘ │                                │ │
│  └──────────────────┘                                │ │
└──────────────────────────────────────────────────────┘ │
                                                         │
┌──────────────────────────────────────────────────────┐ │
│           CodeMaker Serve (LLM 后端 :8666)             │◄┘
│  ┌──────────────────────────────────────────────────┐ │
│  │  /api/session → 创建会话                           │ │
│  │  /session/{id}/message → 同步发消息，返回完整回复   │ │
│  │  /api/model → 列出可用模型                          │ │
│  └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

## 启动方式

**推荐：一键脚本（自动加载 .env）**

```bash
tools\start.bat
```

**手动方式：**

```powershell
# 终端1 (PowerShell):
$env:OPENCODE_SERVER_USERNAME="codemaker"
$env:OPENCODE_SERVER_PASSWORD="CMniubi2026"
codemaker serve --port 8666 --hostname 0.0.0.0

# 终端2（必须先注入 .env，否则 CODEMAKER_MODEL 不生效会回退默认模型）：
Get-Content .env | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object { $kv=$_.Split('=',2); Set-Item -Path ("env:"+$kv[0].Trim()) -Value $kv[1].Trim() }
cd server && python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

> 若用 cmd 而非 PowerShell，加载 .env 改为：`for /f "usebackq eol=# tokens=1,* delims==" %a in (.env) do @set "%a=%b"`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CODEMAKER_SERVER_URL` | `http://127.0.0.1:8666` | codemaker serve 地址 |
| `CODEMAKER_USERNAME` | (空) | Basic auth 用户名 |
| `CODEMAKER_PASSWORD` | (空) | Basic auth 密码 |
| `CODEMAKER_MODEL` | (空) | 模型，格式 `providerID/modelID`。**必须用 serve 实际存在的 provider/model**（见 `/api/model`）。codemaker serve 1.17 内置 provider `netease-codemaker`，可用模型如 `netease-codemaker/deepseek-v4-flash`（最便宜，默认）/ `deepseek-v4-pro` / `qwen3.7-plus` / `glm-5.2`。空值则 serve 用其默认模型 |
| `CODEMAKER_API_TIMEOUT` | 120 | API 超时秒数 |

## 核心文件

```
server/agent/
├── orchestrator.py       # 主智能体：聊天对话 + 调度 QA/CRUD 子智能体
├── codemaker_client.py   # codemaker serve HTTP API 客户端
├── codemaker_parser.py   # 基于 serve API 的 NL 解析器
├── qa_handler.py         # QA 子智能体（LLM 基于 table_index 回答）
├── agent.py              # TableAgent：CRUD 子智能体（执行层）
├── nl_parser.py          # NLIntent 数据类
└── real_cli.py           # 基于 openpyxl 的 Excel 操作

tools/start.bat             # 一键启动脚本（加载 .env + codemaker serve + FastAPI）
```

## 核心流程

1. 用户在前端输入自然语言（如"把 ability 表里饕餮的攻击力改为 200"）
2. FastAPI `/api/agent/chat` 收到请求
3. `AgentService` → `OrchestratorAgent.chat()` → LLM 分类 qa/crud
4. crud 分支 → `CodemakerNLParser.parse()` → HTTP `codemaker serve /session/{id}/message`
5. LLM 返回 JSON：`{"action":"set","table_hint":"ability",...}`
6. `TableAgent` 定位表格/列/行，调用 openpyxl 执行 CRUD
7. 结果返回前端
"""
