"""Agent 聊天路由：自然语言 → Excel 增删改查。"""
import asyncio
import json
import logging
import os
import queue
import threading
from typing import Optional

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

from agent.codemaker_client import (
    get_effective_model, set_runtime_model, persist_model_to_env,
)
from models.agent_models import (
    AgentChatRequest, AgentChatResponse,
    TableInfo, SheetDataPage,
    SearchResponse,
    PreviewRequest, PreviewResponse,
    ValidateRequest, ValidateResponse,
    BatchRequest, BatchResponse,
    SuggestMergeRequest, SuggestMergeResponse,
    SuggestMergeBatchRequest, SuggestMergeBatchResponse,
)
from services.agent_service import get_agent_service

router = APIRouter(prefix="/api/agent", tags=["agent"])

# 会话级取消信号：session_id → threading.Event。
# /cancel 端点 set；SSE 断开（刷新/关页）watchdog 也 set → agent 循环顶检查退出。
_SESSION_CANCELS: dict = {}

# 会话级用户回复队列：session_id → queue.Queue。
# agent 遇阻断错误时经 ask_callback 推 "ask" 事件给前端并阻塞 worker 线程，
# 用户改完 POST /reply → 端点 put 回复到队列 → ask_callback 解阻塞返回回复续跑。
_SESSION_REPLIES: dict = {}


def _normalize_models(raw: list) -> list[dict]:
    """把 codemaker serve /api/model 返回归一化为前端下拉选项。

    兼容两种常见形态：
      - provider 容器：[{"id":"netease-codemaker","models":[{"id":"deepseek-v4-flash",...}]}]
      - 扁平模型：[{"providerID":"netease-codemaker","id":"deepseek-v4-flash","name":...}]
    产出 [{"value":"provider/modelID","label":"名称","provider":"providerID"}]，去重。
    """
    options: list[dict] = []
    seen: set[str] = set()

    def add(provider: str, model_id: str, name: str = "") -> None:
        if not model_id:
            return
        value = f"{provider}/{model_id}" if provider else model_id
        if value in seen:
            return
        seen.add(value)
        options.append({"value": value, "label": name or model_id, "provider": provider or ""})

    for item in raw:
        if not isinstance(item, dict):
            continue
        models = item.get("models")
        if isinstance(models, list) and models:
            # provider 容器形态：顶层 id 是 providerID，子项 id 是 modelID
            prov = item.get("id") or item.get("name") or item.get("providerID") or ""
            for m in models:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or m.get("modelID") or m.get("name") or ""
                add(prov, mid, m.get("name") or m.get("id") or "")
        else:
            # 扁平形态：providerID + id(modelID)
            prov = item.get("providerID") or item.get("provider") or ""
            mid = item.get("id") or item.get("modelID") or item.get("name") or ""
            add(prov, mid, item.get("name") or mid)
    return options


@router.post("/chat", response_model=AgentChatResponse)
async def agent_chat(req: AgentChatRequest):
    """发送自然语言指令，返回执行结果。

    支持的操作类型：
    - 查询：查询灵兽饕餮的攻击力
    - 修改：将灵兽饕餮的攻击力改为 1200
    - 新增：新增一个灵兽，名称朱雀，品质神兽
    - 删除：删除灵兽测试兽
    """
    service = get_agent_service()
    try:
        # M16: service.chat 同步调用 codemaker urlopen 会阻塞 event loop，
        # 卸到默认线程池执行，释放 loop 处理其他请求（如 /chat/stream 的 SSE）。
        return await asyncio.to_thread(
            service.chat,
            text=req.message,
            session_id=req.session_id,
            dry_run=req.dry_run,
            confirm_token=req.confirm_token,
            confirm_cascade=req.confirm_cascade,
        )
    except Exception as e:
        # 任何未捕获异常都返回结构化错误，避免 500 HTML 让前端 JSON 解析崩溃
        return AgentChatResponse(
            ok=False, session_id=req.session_id,
            message=f"服务异常：{e}", error=str(e),
        )


@router.post("/chat/stream")
async def agent_chat_stream(req: AgentChatRequest, request: Request):
    """SSE 流式 Agent 执行（思考过程实时推送）。

    event 类型：
      - thinking: {phase, detail} —— 思考逐条实时流式
      - step: {name, ok, detail} —— 执行步骤（done 时批量回放）
      - subtask_start/done: Step5 子任务级进度
      - heartbeat: 心跳（防前端空白）
      - done: 完整 AgentChatResponse —— 最终结果
      - error: {message}
      - [DONE]: 流结束标记
    """
    service = get_agent_service()
    # 会话级取消信号：/cancel 端点或 SSE 断开（刷新/关页）set → agent 循环顶检查退出
    old_ev = _SESSION_CANCELS.get(req.session_id)
    if old_ev is not None:
        old_ev.set()  # 取消同 session 前一个未完成请求，避免孤儿
    cancel_event = threading.Event()
    _SESSION_CANCELS[req.session_id] = cancel_event
    # 用户回复队列：agent 中断反问时阻塞等此队列，/reply 端点 put 回复
    reply_queue: "queue.Queue" = queue.Queue()
    _SESSION_REPLIES[req.session_id] = reply_queue

    async def _disconnect_watchdog():
        # 每 2s 检测客户端断开：刷新/关页 → set cancel_event，agent 循环顶退出
        while True:
            await asyncio.sleep(2)
            if await request.is_disconnected():
                cancel_event.set()
                return

    async def event_generator():
        wd_task = asyncio.ensure_future(_disconnect_watchdog())
        try:
            gen = service.chat_stream(
                text=req.message,
                session_id=req.session_id,
                dry_run=req.dry_run,
                confirm_token=req.confirm_token,
                confirm_cascade=req.confirm_cascade,
                cancel_event=cancel_event,
                reply_queue=reply_queue,
            )
            done_result = None
            async for etype, payload in gen:
                if etype == "thinking":
                    t_data = json.dumps({
                        "type": "thinking",
                        "phase": payload.get("phase", ""),
                        "detail": payload.get("detail", ""),
                    }, ensure_ascii=False)
                    yield f"data: {t_data}\n\n"
                elif etype == "tool":
                    # 管道模式 instrument 层 tool 调用可见性,实时推送
                    tool_data = json.dumps({
                        "type": "tool",
                        **payload,
                    }, ensure_ascii=False, default=str)
                    yield f"data: {tool_data}\n\n"
                elif etype == "step":
                    # 管道模式 7 步进度,实时推送(前端实时渲染步骤卡片)
                    step_data = json.dumps({
                        "type": "step",
                        "name": payload.get("name", ""),
                        "ok": payload.get("ok", False),
                        "detail": payload.get("detail", ""),
                    }, ensure_ascii=False, default=str)
                    yield f"data: {step_data}\n\n"
                elif etype in ("subtask_start", "subtask_done"):
                    # Step5 子任务级进度：前端增量渲染卡片骨架/填结果（消除空白转圈）
                    sub_data = json.dumps({
                        "type": etype,
                        **payload,
                    }, ensure_ascii=False, default=str)
                    yield f"data: {sub_data}\n\n"
                elif etype == "heartbeat":
                    # 心跳：防单次长 LLM 期间前端空白
                    hb_data = json.dumps({
                        "type": "heartbeat",
                        "phase": payload.get("phase", "心跳"),
                        "detail": payload.get("detail", ""),
                        "llm_calls": payload.get("llm_calls", 0),
                        "subtask_idx": payload.get("subtask_idx", 0),
                        "subtask_total": payload.get("subtask_total", 0),
                    }, ensure_ascii=False, default=str)
                    yield f"data: {hb_data}\n\n"
                elif etype in ("stage_start", "stage_end"):
                    # 阶段切分事件：前端按阶段新开/收尾 agent 气泡
                    stage_data = json.dumps({
                        "type": etype,
                        **payload,
                    }, ensure_ascii=False, default=str)
                    yield f"data: {stage_data}\n\n"
                elif etype == "llm_token":
                    # LLM token 级流式（serve /event SSE 泵出），前端实时渲染思考流
                    token_data = json.dumps({
                        "type": "llm_token",
                        "kind": payload.get("kind", "text"),
                        "delta": payload.get("delta", ""),
                    }, ensure_ascii=False, default=str)
                    yield f"data: {token_data}\n\n"
                elif etype == "ask":
                    # agent 遇阻断错误中断反问：前端弹错误原因+文本框+建议补全
                    ask_data = json.dumps({
                        "type": "ask",
                        **payload,
                    }, ensure_ascii=False, default=str)
                    yield f"data: {ask_data}\n\n"
                elif etype == "done":
                    done_result = payload  # AgentChatResponse
                elif etype == "error":
                    error_data = json.dumps({
                        "type": "error",
                        "message": payload.get("message", ""),
                    }, ensure_ascii=False)
                    yield f"data: {error_data}\n\n"
                    yield "data: [DONE]\n\n"
                    return

            # 发送完成事件（完整响应字段）。
            # 步骤已全路径实时推送（CRUD 经 on_step、管道经 _emit_step），不再批量回放。
            if done_result is not None:
                done_data = json.dumps({
                    "type": "done",
                    "ok": getattr(done_result, 'ok', False),
                    "session_id": getattr(done_result, 'session_id', ''),
                    "intent": getattr(done_result, 'intent', ''),
                    "message": getattr(done_result, 'message', ''),
                    "reply_type": getattr(done_result, 'reply_type', 'crud'),
                    "data": getattr(done_result, 'data', None),
                    "diff_preview": getattr(done_result, 'diff_preview', None).model_dump() if getattr(done_result, 'diff_preview', None) else None,
                    "result_table": getattr(done_result, 'result_table', None).model_dump() if getattr(done_result, 'result_table', None) else None,
                    "sub_tasks": [st.model_dump() for st in (getattr(done_result, 'sub_tasks', []) or [])],
                    "thinking_steps": [dict(t) for t in (getattr(done_result, 'thinking_steps', []) or [])],
                    "row_alternatives": getattr(done_result, 'row_alternatives', []) or [],
                    "multi_results": [rt.model_dump() for rt in (getattr(done_result, 'multi_results', []) or [])],
                    "needs_confirm": getattr(done_result, 'needs_confirm', False),
                    "confirm_token": getattr(done_result, 'confirm_token', None),
                    "confirm_message": getattr(done_result, 'confirm_message', None),
                    "confirm_kind": getattr(done_result, 'confirm_kind', ""),
                    "cross_table_candidates": getattr(done_result, 'cross_table_candidates', []) or [],
                    "pending_search": getattr(done_result, 'pending_search', None),
                    "checkpoint_id": getattr(done_result, 'checkpoint_id', None),
                    "error": getattr(done_result, 'error', None),
                    "needs_user_fill": getattr(done_result, 'needs_user_fill', []) or [],
                    "partial": getattr(done_result, 'partial', False),
                    # §5.4 失败项结构化 payload：AgentChatResponse.failures（#40 字段
                    # type/table/sheet/col/root_cause/attempted_strategies/suggestion/status）。
                    # 前端 AgentChatView 据此渲染失败块（与 ask-card 失败分支区分）。
                    "failures": getattr(done_result, 'failures', []) or [],
                }, ensure_ascii=False, default=str)
                yield f"data: {done_data}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            error_data = json.dumps({
                "type": "error",
                "message": str(e),
            }, ensure_ascii=False)
            yield f"data: {error_data}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            wd_task.cancel()
            # 仅清理自己注册的 event（避免清掉同 session 后续重发的新 event）
            if _SESSION_CANCELS.get(req.session_id) is cancel_event:
                _SESSION_CANCELS.pop(req.session_id, None)
            if _SESSION_REPLIES.get(req.session_id) is reply_queue:
                _SESSION_REPLIES.pop(req.session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/cancel")
async def agent_cancel(payload: dict = Body(...)):
    """取消指定会话的执行：set cancel_event，agent 循环顶检查后退出。

    前端"停止"按钮调用此端点；SSE 断开（刷新/关页）由 watchdog 自动 set。
    中断粒度：① 子任务/verify-repair/ReAct 循环边界（同步检查 _cancel_event）；
    ② 单次 LLM 调用内（cancel_event 下传至 CodemakerClient.prompt，urlopen 跑
    daemon 子线程 + 主线程轮询 0.2s，set 即返回 error_type=cancelled，阻塞的
    socket 子线程随进程退出回收）。cancel_event 仅在 chat_stream 路径注入，
    /chat /preview 等非流端点不接 cancel 机制。
    """
    session_id = payload.get("session_id", "")
    ev = _SESSION_CANCELS.get(session_id)
    if ev is not None:
        ev.set()
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False, "message": "无活跃会话"}


@router.post("/reply")
async def agent_reply(payload: dict = Body(...)):
    """agent 中断反问后，用户提交回复续跑。

    agent 遇阻断错误（verify-repair 达上限 / 占位符未替换 / 悬空 FK 等）时，
    经 ask_callback 推 "ask" SSE 事件给前端并阻塞 worker 线程等本队列。
    前端弹窗渲染错误原因+建议补全，用户改完 POST /reply：
      {session_id, mode: "field"|"nl"|"skip", fix_payload?, text?}
    - mode=field: 用 fix_payload 走 _apply_repair_fix 改 intent.extras 后续跑
    - mode=nl:    用 text 重新解析该子任务 intent 后续跑
    - mode=skip:  放弃修复，记 failure 继续后续子任务（不阻断整批）
    """
    session_id = payload.get("session_id", "")
    q = _SESSION_REPLIES.get(session_id)
    if q is None:
        return {"ok": False, "message": "无等待回复的会话"}
    q.put({
        "mode": payload.get("mode", "skip"),
        "fix_payload": payload.get("fix_payload", {}),
        "text": payload.get("text", ""),
        # PK 冲突简化交互:接受建议ID / 自定义输入ID
        "accept_suggest": payload.get("accept_suggest", False),
        "custom_id": payload.get("custom_id"),
    })
    return {"ok": True, "replied": True}


@router.post("/preview", response_model=AgentChatResponse)
async def agent_preview(req: PreviewRequest):
    """预览 NL 指令的效果（dry-run，不实际修改文件）。"""
    service = get_agent_service()
    # M16: 同 agent_chat，同步调用卸线程池避免阻塞 event loop。
    return await asyncio.to_thread(
        service.chat,
        text=req.message,
        session_id="preview",
        dry_run=True,
        table_hint=req.table_hint,
    )


@router.get("/history")
async def agent_history(session_id: str = Query(default="default")):
    """获取会话操作历史。"""
    service = get_agent_service()
    return {"session_id": session_id, "operations": service.get_history(session_id)}


@router.get("/checkpoints")
async def agent_checkpoints(session_id: str = Query(default="default")):
    """列出会话的所有 checkpoint（每次写操作完成后的快照点）。

    前端据此渲染回退菜单，用户可选回退到任意一次写操作完成后的状态。
    """
    service = get_agent_service()
    return {"session_id": session_id, "checkpoints": service.list_checkpoints(session_id)}


@router.post("/rollback")
async def agent_rollback(session_id: str = Query(default="default"),
                        checkpoint_id: Optional[str] = Query(default=None)):
    """把表格回退到指定 checkpoint（= 某次写操作完成后的状态）。

    适用于"匹配错了地方/误改了数据"场景：可回退到任意一次写操作完成后的状态，
    不必整段对话回退。一个自然语言输入可能含多个原子写操作，但共用一个 checkpoint。

    Args:
        session_id: 会话 id。
        checkpoint_id: 目标 checkpoint id；缺省时回退到最近一个 checkpoint
                       （即撤销最近一次写输入的影响）。回退后丢弃更晚的 checkpoint。
    """
    service = get_agent_service()
    return service.rollback_to_checkpoint(session_id, checkpoint_id)


@router.post("/batch", response_model=BatchResponse)
async def agent_batch(req: BatchRequest):
    """批量执行多条指令。"""
    service = get_agent_service()
    # M16: 同步批量调用卸线程池。
    return await asyncio.to_thread(
        service.batch,
        messages=req.messages,
        session_id=req.session_id,
        stop_on_error=req.stop_on_error,
    )


@router.post("/validate", response_model=ValidateResponse)
async def agent_validate(req: ValidateRequest):
    """数据一致性校验。"""
    service = get_agent_service()
    return service.validate(
        tables=req.tables,
        check_types=req.check_types,
    )


@router.post("/suggest-merge", response_model=SuggestMergeResponse)
async def agent_suggest_merge(req: SuggestMergeRequest):
    """AI 辅助冲突解决建议。

    优先调 LLM（基于 SVN 修订时间先后 + 内容）生成建议；
    LLM 不可用或无 version_meta 时回退规则。LLM 调用放线程池避免阻塞 event loop。
    """
    service = get_agent_service()
    result = await asyncio.to_thread(
        service.suggest_merge,
        table_stem=req.table_stem,
        sheet=req.sheet,
        col_name=req.col_name,
        row_key=req.row_key,
        base_value=req.base_value,
        versions=req.versions,
        version_meta=req.version_meta,
        base_file=req.base_file,
    )
    return SuggestMergeResponse(**result)


@router.post("/suggest-merge-batch", response_model=SuggestMergeBatchResponse)
async def agent_suggest_merge_batch(req: SuggestMergeBatchRequest):
    """批量并行 AI 建议请求。

    一次请求为多个冲突单元格并行调 LLM 生成建议，单次 POST 聚合返回。
    用于并排两表视图的「全部 AI 建议」按钮：避免逐格请求的串行等待。
    LLM 调用放线程池，每格独立失败回退规则。
    """
    service = get_agent_service()
    result = await asyncio.to_thread(
        service.suggest_merge_batch,
        table_stem=req.table_stem,
        sheet=req.sheet,
        version_meta=req.version_meta,
        items=[it.model_dump() for it in req.items],
        base_file=req.base_file,
    )
    return SuggestMergeBatchResponse(**result)


# ── 模型切换（前端下拉框）──
@router.get("/models")
async def agent_models():
    """列出 codemaker serve 可用模型 + 当前生效/默认模型，供前端下拉框渲染。"""
    service = get_agent_service()
    client = getattr(getattr(service, "router", None), "client", None)
    raw = client.list_models() if client is not None else []
    return {
        "models": _normalize_models(raw),
        "current": get_effective_model(),
        "default": os.environ.get("CODEMAKER_MODEL", ""),
    }


@router.get("/model")
async def agent_get_model():
    """返回当前生效模型（运行时覆盖优先，回退 .env 默认）。"""
    return {"model": get_effective_model() or os.environ.get("CODEMAKER_MODEL", "")}


@router.post("/model")
async def agent_set_model(model: str = Body("", embed=True)):
    """设置运行时模型覆盖（provider/modelID 格式）。

    选具体模型：即时生效（运行时覆盖）+ 写回 .env 持久化（重启后端仍保留）。
    传空串：仅清除运行时覆盖回退 .env 默认，不改 .env 文件。
    """
    effective = set_runtime_model(model)
    persisted = persist_model_to_env(effective) if effective else False
    return {"ok": True, "model": effective, "persisted": persisted}
