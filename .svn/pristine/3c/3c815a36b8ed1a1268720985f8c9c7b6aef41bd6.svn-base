# tools/ — 运维脚本

一键部署、更新、启停服务的 Windows 批处理脚本。所有脚本均从项目根目录运行（脚本内部已 `cd` 到项目根）。

## 脚本一览

| 脚本 | 作用 |
|------|------|
| `deploy.bat` | 首次部署：检查工具（**缺 codemaker 自动安装**）→ **登录网易账户** → 生成 `.env` → 装 Python 依赖 → **搭建 SVN demo fixture** → 构建前端 → 建索引 → 装配表模式 → 启动服务（**自动打开浏览器**） |
| `update.bat` | 更新：停服务 → `svn update` → 同步依赖 → 重建前端 → 重建索引 → 重装配表模式 → 重启服务 |
| `start.bat` | 启动服务（CodeMaker Serve + FastAPI），各开独立窗口常驻 |
| `stop.bat` | 按端口 8666 / 8000 停止服务；`stop.bat --no-pause` 供其他脚本调用 |

## 典型用法

**首次安装（新用户，真·一键）：**
```
双击 deploy.bat
```
deploy.bat 会自动：① 检测/安装 CodeMaker CLI（缺失时跑官方 install.ps1）→ ② 引导登录你自己的网易账户（`codemaker providers login`，浏览器跳转验证）→ ③ 装依赖/**建 SVN demo fixture**/建前端/建索引 → ④ 启动服务并自动打开浏览器。

> **SVN demo fixture**（`merge/svn/demo_svn/`，合并引导数据源）随 deploy 自动从项目内置种子数据 `merge/_seed_data/` 搭建，幂等（repo 已存在则跳过）。需本机有 `svn` + `svnadmin` 命令行工具（TortoiseSVN 安装时勾选 *command line client tools*）。

> 若自动安装 CLI 失败（如网络受限），手动装后重跑 deploy.bat：
> ```
> powershell -Command "irm https://codemaker.netease.com/package/codemaker-cli/install.ps1 | iex"
> ```

**拉取最新代码后：**
```
双击 update.bat
```

**单独启停服务：**
```
start.bat   # 启动（会先校验已登录 netease-codemaker）
stop.bat    # 停止
```

## 账户与鉴权（重要，多人协作必读）

本项目有**两套完全独立**的凭据，别混淆：

| 凭据 | 作用 | 是否 per-user | 存放位置 |
|------|------|--------------|----------|
| `codemaker providers login` 登录的**网易 CodeMaker 账户** | 真正调用 LLM（`netease-codemaker` provider）的账户，走用户各自的额度/计费 | **是，每个用户各自登录** | 本地 `~\.local\share\codemaker\auth.json`（不随项目分发） |
| `.env` 的 `OPENCODE_SERVER_USERNAME/PASSWORD` | 仅本地 `codemaker serve` HTTP 接口的 Basic Auth 门禁（防止本机 8666 被随意访问） | 否，团队可共用 | 项目根 `.env` |

**结论**：换一个用户用本项目，deploy/start 能正常启动，但**必须先 `codemaker providers login -p netease-codemaker` 登录他自己的账户**——LLM 调用用的是他本人的账户额度，不是你的。`.env` 里的 `OPENCODE_SERVER_*` 只保护本地 serve 端口，与网易账户无关，无需每人修改。

> deploy.bat 第 1 步、start.bat 启动前都会检测 `auth.json` 是否含 `netease-codemaker` 凭据；未登录会阻断并给出登录命令。

## 前置依赖

`deploy.bat` 启动时会检查以下命令是否可用，缺一不可：

- `uv` — Python 包管理器（[安装](https://docs.astral.sh/uv/)）
- `node` / `npm` — 前端构建（首次部署需要，后续启动不依赖）
- `svn` — 版本控制
- `svnadmin` — SVN 仓库管理（搭建合并引导 demo fixture 用，TortoiseSVN 安装时勾选 *command line client tools*）
- `codemaker` — CodeMaker CLI（LLM 底座，[安装](https://codemaker.netease.com/package/codemaker-cli/)）。首次使用需登录自己的账户：`codemaker providers login -p netease-codemaker`；启动后由 deploy/start 自动执行 `codemaker serve` 提供 LLM API

## 配置文件

服务启动所需的环境变量从项目根 `.env` 读取。`deploy.bat` 首次运行会从 `.env.example` 复制生成 `.env`，请编辑填入真实凭据：

| 变量 | 用途 |
|------|------|
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | 本地 CodeMaker Serve HTTP 接口的 Basic Auth 门禁（**非**网易账户，团队可共用） |
| `CODEMAKER_SERVER_URL` | Serve 地址（默认 `http://127.0.0.1:8666`） |
| `CODEMAKER_USERNAME` / `CODEMAKER_PASSWORD` | 后端访问 Serve 的凭据 |
| `CODEMAKER_MODEL` | 模型，格式 `providerID/modelID`。**必须用 serve 实际存在的 provider/model** |
| `CODEMAKER_API_TIMEOUT` | 请求超时秒数（默认 `120`） |

### CODEMAKER_MODEL 配置要点

codemaker serve 1.17 已内置 provider `netease-codemaker`，`.env` 配成 `netease-codemaker/<模型名>`：

- ✅ 可用：`netease-codemaker/deepseek-v4-flash`（最便宜，默认）/ `deepseek-v4-pro` / `qwen3.7-plus` / `glm-5.2` / `claude-sonnet-5` 等
- 查看所有可用模型：`curl -u codemaker:CMniubi2026 http://127.0.0.1:8666/api/model`
- 后端走**同步端点** `POST /session/{id}/message` 拿回复（model 用 `{providerID, modelID}`），可稳定对话

> **重要**：`.env` 的变量必须注入到后端进程环境。`start.bat` 已通过 `for /f` 自动注入；若手动 `uv run python server/main.py` 直接启动，环境变量不会加载，会回退默认模型（公共额度易耗尽，报 403 余额不足）。

> `.env` 已加入忽略列表，不会提交到版本库。

## 端口约定

| 端口 | 服务 |
|------|------|
| 8666 | CodeMaker Serve（LLM 底座） |
| 8000 | FastAPI 后端（直出前端页面 + API） |
| 5173 | 前端开发服务器（`cd frontend && npm run dev`，仅开发用） |

## 配表操作模式（第二形态）

项目有两种使用形态：

- **第一形态** — Web 端 AI 配表助手（浏览器访问 `http://127.0.0.1:8000`，自然语言 CRUD / 问答 / 比对合并）
- **第二形态** — 配表操作模式（在 CodeMaker 对话框激活，CodeMaker 仅用 skill 知识 + 后端原子 API 操作配表，轻量、受限、隔离）

### 自动安装

`deploy.bat` 的第 6 步、`update.bat` 的第 6 步会自动运行 `tools/table-mode/install.py`，把配表模式指令文档 + skill 知识库复制到 `.codemaker/table-mode/`（该目录不提交，各成员本地生成）。**正常使用 deploy/update 即可，无需手动安装**。

安装内容：
- `配表操作模式.md` — 模式行为指令（CodeMaker 进入模式后遵循）
- `知识库.md` — 配表知识（实体路由/列别名/行定位/原子 API）
- `skills/` — skill yaml 原文（列别名/sheet 别名/行定位/反模式等）

### 单独安装/重装

不跑完整 deploy 流程，仅重装配表模式（如改了 `tools/table-mode/` 源文档或 `server/agent/excel/skills/` 后想立即生效）：

```
uv run python tools/table-mode/install.py
```

### 如何启动第二形态

1. 后端服务运行中（`http://127.0.0.1:8000`，跑过 `deploy.bat` 或 `start.bat`）
2. 配表模式已安装（`.codemaker/table-mode/` 存在，deploy 已自动装）
3. 在 CodeMaker 对话框输入 **"进入配表模式"** → CodeMaker 询问确认 → 确认后进入
4. 退出输入 **"退出配表模式"** 恢复完整能力

详见 `TABLE_MODE.md`。

## 单独重建索引

不跑完整流程，仅重建配表索引：

```
uv run python -m server.agent.excel.index_builder          # 生成 _table_index.json
uv run python -m server.agent.excel.index_builder --verify  # 校验索引与 resources 一致性
uv run python -m server.agent.excel.index_builder --print   # 打印索引预览
```
