"""AI 辅助 Excel 配表工具 — 统一后端入口。

整合三大子系统：
  - Diff/Merge   (/api/compare, /api/merge, ...)
  - Agent Chat   (/api/agent/chat, /api/agent/validate, ...)
  - Table Browser (/api/tables, ...)
  - Workflow     (/api/workflow/snapshot, ...)

启动：python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
（默认监听 0.0.0.0，支持本机 localhost 与局域网 IP 访问；
  仅本机访问可设环境变量 BACKEND_HOST=127.0.0.1）
"""

import logging
import os
import sys
import time
from pathlib import Path

# 确保 server/ 目录在 sys.path 中（使得 agent/ 和 engine/ 可导入）
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 在导入任何读 os.environ 的模块（如 agent.codemaker_client）前加载 .env，
# 避免 start.bat 未预加载环境变量时凭据为空（下拉框只显示「默认（.env）」根因）。
try:
    from dotenv import load_dotenv
    _ENV_PATH = SERVER_DIR.parent / ".env"
    load_dotenv(_ENV_PATH)
except Exception:
    pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from config import STATIC_DIR, RESOURCES_DIR
from routers.diff import router as diff_router
from routers.merge_stages import router as merge_stages_router
from routers.agent import router as agent_router
from routers.tables import router as tables_router
from routers.workflow import router as workflow_router
from routers.validate import router as validate_router
from routers.skills import router as skills_router
from routers.svn_history import router as svn_router
from routers.merge_branch import router as merge_branch_router
from routers.merge_subdir import router as merge_subdir_router
from routers.merge_commits import router as merge_commits_router

# ── 日志：统一 INFO 级别，接口调用与 codemaker 传输均打到终端 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aitable")

# 默认 0.0.0.0 监听所有网卡：支持本机 localhost + 局域网 IP 访问；
# 仅本机访问可设 BACKEND_HOST=127.0.0.1
HOST = os.environ.get("BACKEND_HOST", "0.0.0.0")
PORT = int(os.environ.get("BACKEND_PORT", "8000"))
SERVE_URL = os.environ.get("CODEMAKER_SERVER_URL", "http://127.0.0.1:8666")


def _lan_ips() -> list[str]:
    """枚举本机局域网 IPv4 地址（供打印 IP 访问入口）。"""
    import socket
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return sorted(ips)


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    app = FastAPI(
        title="AI 配表助手",
        description="AI 辅助 Excel 配表工具：自然语言操作 + 版本比对合并 + 数据验证",
        version="2.0.0",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "X-Export-Path", "X-Export-Name"],
    )

    # 接口调用日志：记录方法/路径/状态/耗时（INFO 打到终端）
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # 静态资源/首页不刷屏，仅记录 /api 接口
        is_api = request.url.path.startswith("/api")
        if is_api:
            logger.info("▶ %s %s", request.method, request.url.path)
        start = time.time()
        response = await call_next(request)
        if is_api:
            ms = (time.time() - start) * 1000
            logger.info("◀ %s %s %s  %.0fms",
                        response.status_code, request.method, request.url.path, ms)
        return response

    # 注册路由
    app.include_router(diff_router)
    app.include_router(merge_stages_router)
    app.include_router(agent_router)
    app.include_router(tables_router)
    app.include_router(workflow_router)
    app.include_router(validate_router)
    app.include_router(skills_router)
    app.include_router(svn_router)
    app.include_router(merge_branch_router)
    app.include_router(merge_subdir_router)
    app.include_router(merge_commits_router)

    # 静态文件（仅在目录存在时挂载）
    os.makedirs(STATIC_DIR, exist_ok=True)
    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # 前端入口
    @app.get("/")
    async def index():
        """返回前端单页面。"""
        html_path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.exists(html_path):
            return HTMLResponse("""
            <html><head><title>AI 配表助手</title><style>
              body { font-family: sans-serif; display:flex; align-items:center; justify-content:center;
                     height:100vh; margin:0; background:#1a1a2e; color:#eee; }
              .box { text-align:center; padding:40px; background:#16213e; border-radius:12px; }
              h1 { color:#e94560; } code { background:#0f3460; padding:4px 8px; border-radius:4px; }
            </style></head><body>
              <div class="box">
                <h1>🤖 AI 配表助手</h1>
                <p>后端服务运行中</p>
                <p>API 文档：<a href="/docs" style="color:#e94560">/docs</a></p>
                <p>启动前端：<code>cd frontend && npm run dev</code></p>
              </div>
            </body></html>
            """)
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # 前端图标等静态资源
    @app.get("/favicon.svg")
    async def favicon():
        path = os.path.join(STATIC_DIR, "favicon.svg")
        if os.path.exists(path):
            return FileResponse(path)

    return app


# 应用实例
app = create_app()


# ── 启动事件 ──

@app.on_event("startup")
async def startup():
    """启动时初始化 AgentService 并构建索引。"""
    import threading
    import webbrowser
    from services.agent_service import init_agent_service
    init_agent_service(RESOURCES_DIR)
    logger.info("资源目录: %s", RESOURCES_DIR)

    # skills 重生成放后台 daemon 线程，不阻塞 READY：schema_infer 内有 mtime 戳，
    # 资源未变则秒过；变更则后台重算，期间 skill_loader 用上一轮盘上 L1_derived 文件。
    # （用户反馈"每次重生成 skills 耗时太长"，改异步后启动即就绪，skills 在后台刷新。）
    def _bg_regenerate_skills():
        try:
            from agent.excel.schema_infer import regenerate_skills
            logger.info("后台生成/更新 skills 中...")
            regenerate_skills(RESOURCES_DIR)
        except Exception as e:
            logger.warning("L1_derived 预生成跳过（非致命）：%s", e)
    threading.Thread(target=_bg_regenerate_skills, name="skills-regen", daemon=True).start()

    # 预热合并引导目录缓存：浏览器一打开前端即调 /api/merge/branch/dirs +
    # /api/merge/subdir/dirs，冷首次 subdir 全树 rglob 慢（曾实测 11.8s）。**同步**先填满
    # 30s TTL 缓存再打开浏览器（用户要求"第一次打开浏览器前就加载好"），页面挂载即命中，
    # 切换到合并引导页可直接选择。已跳过 _seed_data/branches/demo + 单遍 rglob，秒级不阻塞。
    try:
        from routers.merge_branch import list_branch_dirs
        from routers.merge_subdir import list_subdir_dirs
        list_branch_dirs()
        list_subdir_dirs()
        logger.info("合并引导目录缓存预热完成（branch + subdir /dirs），即将打开浏览器")
    except Exception as e:
        logger.debug("合并目录预热跳过（非致命）：%s", e)

    ui_url = f"http://127.0.0.1:{PORT}"
    docs_url = f"{ui_url}/docs"
    logger.info("=" * 56)
    logger.info("  AI 配表助手已就绪，可以开始使用")
    logger.info("  本机访问 : %s", ui_url)
    for ip in _lan_ips():
        logger.info("  IP 访问 : http://%s:%d", ip, PORT)
    logger.info("  API 文档 : %s", docs_url)
    logger.info("  LLM 后端 : %s  (codemaker serve)", SERVE_URL)
    logger.info("  配表模式 : 在 CodeMaker 输入 “进入配表模式” 即可使用")
    logger.info("=" * 56)

    # 自动打开浏览器（延迟 1.5s 确保服务已开始监听；可用 AITABLE_NO_BROWSER=1 关闭）。
    # 绑定 0.0.0.0 时浏览器打开本机地址（127.0.0.1），IP 访问由局域网用户自行输入。
    browser_url = ui_url if HOST not in ("0.0.0.0", "127.0.0.1") else f"http://127.0.0.1:{PORT}"
    if os.environ.get("AITABLE_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(browser_url)).start()
        logger.info("已尝试自动打开浏览器：%s", browser_url)


if __name__ == "__main__":
    import uvicorn
    # 直接传 app 实例而非字符串 "main:app"，避免 uvicorn 重新 import 本模块
    # 导致 create_app() 与 startup 二次执行（双浏览器根因）。
    uvicorn.run(app, host=HOST, port=PORT, reload=False)
