 <script setup>
import { ref, nextTick, watch, onMounted, computed } from 'vue'

// ── 状态 ──
const messages = ref([])
const inputText = ref('')
const sending = ref(false)
const sessionId = ref('session_' + Date.now())
const currentTable = ref('')
const currentSheet = ref('')
const rollingBack = ref(false)
// 本会话是否执行过写动作（用于回退按钮可用性判断）
const hasWrites = ref(false)
// checkpoint 下拉：{checkpoint_id, timestamp, label, text}
const checkpoints = ref([])
const showCkptMenu = ref(false)

const chatEl = ref(null)
// 用户是否停留在底部附近：true 时自动追底，false（用户上滚查看历史）时停止自动滚动
const isAtBottom = ref(true)

// ── 发送态可中断性：已耗时 / 心跳 / 90s 卡住检测 / 停止 ──
const elapsedSec = ref(0)
const lastHeartbeat = ref({ time: 0, detail: '' })
const stuckWarning = ref(false)
let abortCtrl = null
let elapsedTimer = null
let stuckTimer = null
let lastEventTime = 0

// ── 示例指令 ──
const examples = [
  '查看灵兽饕餮一阶的所有属性',
  '将法宝名称测试法宝3的法宝描述修改为测试描述修改',
  '新增一个活动，名称春节活动，类型节日',
  '删除活动名称为春节活动的行',
]

// ── 模型切换 ──
const models = ref([])
const currentModel = ref('')
const modelsLoading = ref(false)

// 按 provider 分组：netease-codemaker（付费）置顶，opencode（免费）在后
const neteaseModels = computed(() => models.value.filter(m => m.provider === 'netease-codemaker'))
const opencodeModels = computed(() => models.value.filter(m => m.provider !== 'netease-codemaker'))

async function loadModels() {
  modelsLoading.value = true
  try {
    const res = await fetch('/api/agent/models')
    if (res.ok) {
      const data = await res.json()
      models.value = data.models || []
      currentModel.value = data.current || data.default || ''
    }
  } catch (e) {
    // 拉取失败静默处理，下拉框仍可用默认项
  } finally {
    modelsLoading.value = false
  }
}

async function switchModel() {
  try {
    await fetch('/api/agent/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: currentModel.value }),
    })
  } catch (e) {
    // 切换失败静默，下次加载会纠正
  }
}

onMounted(() => { loadModels() })

// ── 发送消息 ──
async function sendMessage(text, confirmToken = null, confirmCascade = true) {
  const msg = text || inputText.value.trim()
  if (!msg || sending.value) return

  if (!confirmToken) inputText.value = ''
  const userMsg = { role: 'user', content: msg, id: Date.now() }
  messages.value.push(userMsg)

  // 多端对话：本回合不预建气泡。后端按阶段推送 stage_start，
  // 前端每阶段新开一条 agent 气泡（thinking 实时流式，思考完统一回复该阶段内容）
  let cur = null
  const newAgentMsg = (stageTitle, stageId, stageTotal) => {
    cur = {
      role: 'agent', id: Date.now() + Math.random(),
      stage_title: stageTitle || '', stage_id: stageId || '',
      stage_total: stageTotal || 6,
      thinking_steps: [], tool_calls: [], steps: [],
      reply: '', live_text: '', live_kind: 'text',
      ok: null, sending: true, thinking_live: true, show_thinking: true,
    }
    messages.value.push(cur)
    return cur
  }
  const curMsg = () => cur || newAgentMsg('')

  // 用户主动发消息 → 视为要看最新，恢复追底
  isAtBottom.value = true
  sending.value = true
  // 重置发送态：计时器 / 心跳 / 卡住检测 / 中断控制器
  elapsedSec.value = 0
  lastHeartbeat.value = { time: 0, detail: '' }
  stuckWarning.value = false
  lastEventTime = Date.now()
  abortCtrl = new AbortController()
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { elapsedSec.value += 1 }, 1000)
  clearInterval(stuckTimer)
  stuckTimer = setInterval(() => {
    if (Date.now() - lastEventTime > 90000) stuckWarning.value = true
  }, 1000)
  await nextTick()
  scrollToBottom()

  try {
    const body = { message: msg, session_id: sessionId.value }
    if (confirmToken) {
      body.confirm_token = confirmToken
      body.confirm_cascade = confirmCascade
    }
    const res = await fetch('/api/agent/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: abortCtrl ? abortCtrl.signal : undefined,
    })
    if (!res.ok) {
      const fm = newAgentMsg('')
      fm.ok = false
      fm.message = `请求失败（HTTP ${res.status}）`
      fm.reply = `❌ 请求失败（HTTP ${res.status}）`
      fm.error = `HTTP ${res.status}`
      fm.sending = false
      fm.thinking_live = false
      return
    }
    // SSE 流式解析：stage_start 新开气泡 → thinking/tool/step 实时挂载 →
    // stage_end 统一回复该阶段内容 → done 挂最终结构化结果
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let donePayload = null
    let lastTokenScroll = 0
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const line = chunk.startsWith('data: ') ? chunk.slice(6) : chunk
        if (line === '[DONE]') continue
        let evt
        try { evt = JSON.parse(line) } catch { continue }
        // 任何事件都刷新"最后活动时间"→ 重置 90s 卡住检测
        lastEventTime = Date.now()
        stuckWarning.value = false
        if (evt.type === 'subtask_start') {
          // 子任务开始：增量渲染骨架卡（占位+转圈），消除空白转圈
          const tm = curMsg()
          if (!tm.sub_tasks_live) tm.sub_tasks_live = []
          tm.sub_tasks_live.push({
            idx: evt.idx, total: evt.total,
            intent_action: evt.action, table_stem: evt.table,
            loading: true, ok: null, message: '', llm_calls: evt.llm_calls,
          })
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'subtask_done') {
          // 子任务完成：找到骨架卡填结果
          const tm = curMsg()
          const arr = tm.sub_tasks_live || (tm.sub_tasks_live = [])
          let st = arr.find(s => s.idx === evt.idx)
          if (!st) { st = { idx: evt.idx, total: evt.total }; arr.push(st) }
          st.loading = false
          st.ok = evt.ok
          st.skipped = evt.skipped
          st.message = evt.message || ''
          st.dur_ms = evt.dur_ms
          st.llm_calls = evt.llm_calls
          if (evt.table) st.table_stem = evt.table
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'heartbeat') {
          // 心跳：更新状态条 + 挂到当前气泡 thinking 流
          // 要求 C：heartbeat 刷新 lastEventTime，防 ask 阻塞期误触发 stuckWarning
          lastEventTime = Date.now()
          stuckWarning.value = false
          lastHeartbeat.value = { time: Date.now(), detail: evt.detail || '' }
          curMsg().thinking_steps.push({ phase: evt.phase || '心跳', detail: evt.detail || '' })
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'stage_start') {
          newAgentMsg(evt.title, evt.stage_id, evt.total)
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'stage_resume') {
          // 阶段重新激活（多指令聚合模式）：切回已有阶段气泡，不新建
          cur = messages.value.find(m => m.stage_id === evt.stage_id && m.sending) || cur
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'stage_end') {
          if (cur) {
            cur.reply = evt.content || ''
            cur.sending = false
            cur.thinking_live = false
            cur.live_text = ''
          }
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'llm_token') {
          // token 级流式：实时拼进当前气泡的思考流（Vue 按 tick 批量渲染）
          const tm = curMsg()
          tm.live_kind = evt.kind || 'text'
          tm.live_text = (tm.live_text || '') + (evt.delta || '')
          const now = Date.now()
          if (now - lastTokenScroll > 300) {
            lastTokenScroll = now
            scrollToBottom()
          }
        } else if (evt.type === 'thinking') {
          curMsg().thinking_steps.push({ phase: evt.phase, detail: evt.detail })
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'tool') {
          curMsg().tool_calls.push({ name: evt.name || '', desc: evt.desc || '', ok: evt.ok !== false, cmd: evt.cmd || '', result: evt.result || '', show: false })
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'step') {
          curMsg().steps.push({ name: evt.name, ok: evt.ok, detail: evt.detail })
          await nextTick(); scrollToBottom()
        } else if (evt.type === 'done') {
          donePayload = evt
        } else if (evt.type === 'error') {
          const em = curMsg()
          em.ok = false
          em.error = evt.message
          em.reply = (em.reply ? em.reply + '\n' : '') + '❌ ' + (evt.message || '执行出错')
        } else if (evt.type === 'ask') {
          // agent 遇阻断错误中断反问：挂 ask 卡片，用户改/补完 POST /reply 续跑
          const tm = curMsg()
          tm.ask = evt
          tm.askReplyText = ''
          tm.askCustomId = ''
          tm.askResolved = false
          tm.askMode = null
          tm.askCollapsed = false  // 要求 C：提交后折叠
          tm.askUserReply = ''    // 要求 C：记录用户提交了什么
          await nextTick(); scrollToBottom()
        }
      }
    }
    const agentMsg = curMsg()
    agentMsg.thinking_live = false
    agentMsg.sending = false
    agentMsg.live_text = ''
    const data = donePayload || {}
    if (!data || typeof data !== 'object') {}
    agentMsg.ok = data.ok
    agentMsg.message = data.message || ''
    agentMsg.reply_type = data.reply_type || 'crud'
    // steps：保留本气泡实时累积的阶段进度，仅空时回退 done 批量数据
    if (!agentMsg.steps.length && Array.isArray(data.steps) && data.steps.length) {
      agentMsg.steps = data.steps
    }
    agentMsg.intent = data.intent
    agentMsg.diff_preview = data.diff_preview
    agentMsg.result_table = data.result_table
    agentMsg.sub_tasks = Array.isArray(data.sub_tasks) ? data.sub_tasks : []
    // done 完整结果合并到 live 骨架：按 index 复用已有对象，避免整卡重绘闪烁
    if (agentMsg.sub_tasks_live && agentMsg.sub_tasks_live.length && agentMsg.sub_tasks.length) {
      const liveMap = new Map()
      agentMsg.sub_tasks_live.forEach(st => liveMap.set(st.idx ?? st.index, st))
      agentMsg.sub_tasks.forEach(st => {
        const key = st.index ?? st.idx
        const live = liveMap.get(key)
        if (live) {
          Object.assign(live, st, { loading: false })
        } else {
          agentMsg.sub_tasks_live.push({ ...st, loading: false })
        }
      })
    }
    agentMsg.error = data.error
    agentMsg.checkpoint_id = data.checkpoint_id || null
    agentMsg.sending = false

    agentMsg.row_alternatives = Array.isArray(data.row_alternatives) ? data.row_alternatives : []
    agentMsg.multi_results = Array.isArray(data.multi_results) ? data.multi_results : []
    agentMsg.failures = Array.isArray(data.failures) ? data.failures : []
    agentMsg.selected_multi_row = 0
    if (!agentMsg.thinking_steps.length && Array.isArray(data.thinking_steps) && data.thinking_steps.length) {
      agentMsg.thinking_steps = data.thinking_steps
    }
    agentMsg.show_thinking = false

    if (data.needs_confirm && data.confirm_token) {
      agentMsg.needs_confirm = true
      agentMsg.confirm_token = data.confirm_token
      agentMsg.confirm_kind = data.confirm_kind || 'cascade'
      agentMsg.confirm_message = data.confirm_message || data.message || ''
      agentMsg.userText = msg
      agentMsg.confirmResolved = false
      agentMsg.pending_search = data.pending_search || null
      agentMsg.cross_table_candidates = Array.isArray(data.cross_table_candidates) ? data.cross_table_candidates : []
      startConfirmCountdown(agentMsg)
    }

    // CRUD 写动作成功 → 标记本会话有写操作，回退按钮可用，刷新 checkpoint 列表
    if (data.reply_type !== 'qa' && data.ok && !data.needs_confirm) {
      hasWrites.value = true
      fetchCheckpoints()
    }

    // 表单式新增：后端拦截纯类别新增 → 渲染可填写表单
    if (data.reply_type === 'form' && data.data && data.data.form) {
      const d = data.data
      agentMsg.form = {
        stem: d.table_stem,
        sheet: d.sheet,
        columns: d.columns || [],
        values: { ...(d.empty_row || {}) },
        errors: {},
        warnings: {},
        validating: false,
        submitting: false,
        validatedOk: false,
        committed: false,
        result: null,
        lastMessage: '',
      }
    }

    const rt = data.result_table
    if (rt && rt.file) {
      currentTable.value = rt.file
      currentSheet.value = rt.sheet || ''
    } else if (data.diff_preview && data.diff_preview.file) {
      currentTable.value = data.diff_preview.file
      currentSheet.value = data.diff_preview.sheet || ''
    }
  } catch (e) {
    if (e && e.name === 'AbortError') {
      // 用户主动中断：不报错，标记已停止
      const em = curMsg()
      em.sending = false
      em.thinking_live = false
      em.live_text = ''
      if (!em.reply) em.reply = '⏹ 已中断'
    } else {
      const em = curMsg()
      em.ok = false
      em.message = '网络错误：' + e.message
      em.error = e.message
      em.sending = false
      em.thinking_live = false
      if (!em.reply) em.reply = '❌ 网络错误：' + e.message
    }
  } finally {
    clearInterval(elapsedTimer); elapsedTimer = null
    clearInterval(stuckTimer); stuckTimer = null
    abortCtrl = null
  }

  sending.value = false
  await nextTick()
  scrollToBottom()
}

// 用户主动中断：先通知后端 set cancel_event（agent 循环顶退出，真正可停），再断开 SSE
function stopSending() {
  try {
    fetch('/api/agent/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId.value }),
    })
  } catch (e) {}
  if (abortCtrl) { try { abortCtrl.abort() } catch (e) {} }
  clearInterval(elapsedTimer); elapsedTimer = null
  clearInterval(stuckTimer); stuckTimer = null
  sending.value = false
}

function onChatScroll() {
  const el = chatEl.value
  if (!el) return
  // 距底部 < 80px 视为"在底部"，允许自动追底；否则用户在查看历史，停止自动滚动
  isAtBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function scrollToBottom() {
  if (chatEl.value && isAtBottom.value) {
    chatEl.value.scrollTop = chatEl.value.scrollHeight
  }
}

// ── 危险操作二次确认 ──
const confirmTimers = {}

function startConfirmCountdown(agentMsg) {
  agentMsg.confirmSeconds = 300  // 与后端 _CONFIRM_TTL_SECONDS 一致
  clearInterval(confirmTimers[agentMsg.id])
  confirmTimers[agentMsg.id] = setInterval(() => {
    agentMsg.confirmSeconds -= 1
    if (agentMsg.confirmSeconds <= 0) {
      clearInterval(confirmTimers[agentMsg.id])
      if (!agentMsg.confirmResolved) {
        agentMsg.confirmResolved = true
        agentMsg.confirmExpired = true
      }
    }
  }, 1000)
}

function confirmDangerous(agentMsg) {
  // 确认：cascade 类型→级联删除；confidence 类型→确认删除
  if (agentMsg.confirmResolved) return
  agentMsg.confirmResolved = true
  clearInterval(confirmTimers[agentMsg.id])
  sendMessage(agentMsg.userText, agentMsg.confirm_token, true)
}

function deleteCurrentOnly(agentMsg) {
  // 仅删当前行（不级联）：cascade 类型专用
  if (agentMsg.confirmResolved) return
  agentMsg.confirmResolved = true
  clearInterval(confirmTimers[agentMsg.id])
  sendMessage(agentMsg.userText, agentMsg.confirm_token, false)
}

function cancelDangerous(agentMsg) {
  // 取消：confidence 类型→完全不删；cascade 类型不使用此项
  if (agentMsg.confirmResolved) return
  agentMsg.confirmResolved = true
  agentMsg.confirmCancelled = true
  clearInterval(confirmTimers[agentMsg.id])
}

// agent 中断反问后用户回复续跑：独立 POST /reply（不开新 SSE 流），
// 后端 ask_callback 解阻塞 → 原 SSE 流继续推后续事件，reader 循环自动接上。
// 要求 C：提交后立即折叠成一行 INFO 摘要（记录用户提交了什么），不阻塞页面。
async function replyAsk(agentMsg, mode) {
  if (agentMsg.askResolved) return
  agentMsg.askResolved = true
  agentMsg.askMode = mode
  // 要求 C：折叠 + 记录用户提交内容（供事后追溯）
  if (mode === 'nl') {
    agentMsg.askUserReply = agentMsg.askReplyText
      ? `补充：${agentMsg.askReplyText.slice(0, 60)}` : '补充了文字'
  } else if (mode === 'skip') {
    agentMsg.askUserReply = '已跳过此项'
  }
  agentMsg.askCollapsed = true
  const body = { session_id: sessionId.value, mode }
  if (mode === 'nl') body.text = agentMsg.askReplyText || ''
  try {
    await fetch('/api/agent/reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (e) {
    // 提交失败静默；SSE 流会因后端超时自行结束或继续
  }
}

// PK 冲突简化交互:接受建议 ID 或自定义输入 ID
async function replyAskPk(agentMsg) {
  if (agentMsg.askResolved) return
  agentMsg.askResolved = true
  agentMsg.askMode = 'field'
  const customId = (agentMsg.askCustomId || '').toString().trim()
  const body = { session_id: sessionId.value, mode: 'field' }
  let _replySummary = ''
  if (customId && /^\d+$/.test(customId)) {
    body.custom_id = parseInt(customId, 10)
    _replySummary = `改为 ${customId}`
  } else {
    body.accept_suggest = true
    const _sid = agentMsg.ask?.suggested_id
    _replySummary = _sid ? `接受建议 ${_sid}` : '接受建议'
  }
  // 要求 C：折叠 + INFO 摘要
  agentMsg.askUserReply = `✓ 已采纳：${_replySummary}，继续配置中...`
  agentMsg.askCollapsed = true
  try {
    await fetch('/api/agent/reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (e) {
    // 静默
  }
}

function fmtCountdown(sec) {
  const s = Math.max(0, sec | 0)
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}



// ── 表单式新增 ──
const formTimers = {}

function onFormInput(form, col) {
  // 输入时去抖校验（600ms），实时标红
  clearTimeout(formTimers[col])
  formTimers[col] = setTimeout(() => validateForm(form, true), 600)
}

async function validateForm(form, silent = false) {
  form.validating = true
  form.lastMessage = ''
  try {
    const res = await fetch('/api/tables/add-form/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table_stem: form.stem, sheet: form.sheet, values: form.values,
      }),
    })
    const data = await res.json()
    form.errors = {}
    form.warnings = {}
    ;(data.errors || []).forEach(e => { form.errors[String(e.col)] = e.message })
    ;(data.warnings || []).forEach(w => { form.warnings[String(w.col)] = w.message })
    form.validatedOk = !!data.ok
    if (!silent) form.lastMessage = data.message || (data.ok ? '校验通过' : '存在错误')
  } catch (e) {
    if (!silent) form.lastMessage = '校验请求失败：' + e.message
  }
  form.validating = false
}

async function commitForm(form) {
  await validateForm(form)
  if (Object.keys(form.errors || {}).length) {
    form.lastMessage = '存在标红错误，请先修正后再提交'
    return
  }
  form.submitting = true
  form.lastMessage = ''
  try {
    const res = await fetch('/api/tables/add-form/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table_stem: form.stem, sheet: form.sheet,
        values: form.values, confirm: false,
      }),
    })
    const data = await res.json()
    form.result = data
    form.committed = !!data.ok
    form.lastMessage = data.message || (data.ok ? '新增成功' : '新增失败')
    if (data.ok) {
      currentTable.value = form.stem
      currentSheet.value = form.sheet
      hasWrites.value = true
      fetchCheckpoints()
    } else if (data.errors && data.errors.length) {
      ;(data.errors).forEach(e => { form.errors[String(e.col)] = e.message })
    }
  } catch (e) {
    form.result = { ok: false, message: '提交失败：' + e.message }
    form.lastMessage = '提交失败：' + e.message
  }
  form.submitting = false
}

function fillExample(text) {
  inputText.value = text
}

// T2/R9: 用户点击候选行 → 优先从 multi_results 缓存切换，无缓存时发"用行N"
function selectRowAlt(row, agentMsg) {
  // R9: 有多行缓存 → 本地切换，不发请求
  if (agentMsg.multi_results && agentMsg.multi_results.length) {
    const idx = agentMsg.multi_results.findIndex(mr => mr.row === row)
    if (idx >= 0) {
      agentMsg.selected_multi_row = idx
      agentMsg.result_table = agentMsg.multi_results[idx]
      // 更新 row_alternatives 中的 current 标记
      if (agentMsg.row_alternatives) {
        agentMsg.row_alternatives.forEach(ra => { ra.current = (ra.row === row) })
      }
      return
    }
  }
  // 降级：无缓存 → 发"用行N"
  sendMessage('用行' + row)
}

// ── 回退 checkpoint ──
async function fetchCheckpoints() {
  try {
    const res = await fetch(`/api/agent/checkpoints?session_id=${encodeURIComponent(sessionId.value)}`)
    const data = await res.json()
    checkpoints.value = Array.isArray(data.checkpoints) ? data.checkpoints : []
  } catch {
    checkpoints.value = []
  }
}

async function rollback(checkpointId = null) {
  if (rollingBack.value) return
  // 无显式选择时，回退到最近一个 checkpoint（撤销最近一次写输入的影响）
  const target = checkpointId
  const targetCk = checkpoints.value.find(c => c.checkpoint_id === target)
  const targetLabel = targetCk ? targetCk.label : '最近一次操作之前'
  if (!confirm(`确定回退到「${targetLabel}」吗？该 checkpoint 之后的所有改动都会被还原。`)) return

  rollingBack.value = true
  showCkptMenu.value = false
  try {
    const url = `/api/agent/rollback?session_id=${encodeURIComponent(sessionId.value)}`
      + (target ? `&checkpoint_id=${encodeURIComponent(target)}` : '')
    const res = await fetch(url, { method: 'POST' })
    const data = await res.json()
      if (data.ok) {
        messages.value.push({
          role: 'agent', id: Date.now(), ok: true,
          message: `⏪ ${data.message}`,
          reply: `⏪ ${data.message}`,
          reply_type: 'qa', steps: [], thinking_steps: [], tool_calls: [], isRollback: true,
        })
      // 回退后刷新 checkpoint 列表（时间线已截断）
      await fetchCheckpoints()
      // 若已无 checkpoint，说明回退到对话最初，写状态清零
      if (!checkpoints.value.length) {
        hasWrites.value = false
        currentTable.value = ''
        currentSheet.value = ''
      }
    } else {
      messages.value.push({
        role: 'agent', id: Date.now(), ok: false,
        message: data.message || '回退失败',
        reply: `❌ ${data.message || '回退失败'}`,
        reply_type: 'qa', steps: [], thinking_steps: [], tool_calls: [], isRollback: true,
      })
    }
  } catch (e) {
    messages.value.push({
      role: 'agent', id: Date.now(), ok: false,
      message: '回退请求失败：' + e.message,
      reply: '❌ 回退请求失败：' + e.message,
      reply_type: 'qa', steps: [], thinking_steps: [], tool_calls: [], isRollback: true,
    })
  }
  rollingBack.value = false
  await nextTick()
  scrollToBottom()
}

function toggleCkptMenu() {
  showCkptMenu.value = !showCkptMenu.value
}

onMounted(() => {
  // 欢迎消息
  messages.value.push({
    role: 'agent', id: 1,
    ok: true,
    message: '你好！我是 AI 配表助手 \n\n你可以用自然语言告诉我需要对 Excel 配表做什么操作，比如：',
    steps: [],
    isWelcome: true,
  })
})
</script>

<template>
<div class="agent-view">
  <!-- 上下文栏 -->
  <div class="context-bar" v-if="currentTable || hasWrites">
    <span class="ctx-tag" v-if="currentTable">📁 {{ currentTable }}</span>
    <span class="ctx-tag" v-if="currentSheet">📄 {{ currentSheet }}</span>
    <div class="ckpt-wrap">
      <button
        class="rollback-btn"
        @click="toggleCkptMenu"
        :disabled="!hasWrites || rollingBack || !checkpoints.length"
        :title="hasWrites && checkpoints.length ? '选择回退到某次写操作完成后的状态' : '本会话尚无可用 checkpoint'"
      >
        {{ rollingBack ? '⏳ 回退中…' : '⏪ 回退' }}
        <span class="ckpt-caret" v-if="!rollingBack">▾</span>
      </button>
      <div class="ckpt-menu" v-if="showCkptMenu && checkpoints.length">
        <div class="ckpt-menu-item ckpt-quick" @click="rollback(null)">
          ⏪ 回退到最近一次操作之前
        </div>
        <div class="ckpt-menu-sep">或选择具体 checkpoint：</div>
        <div
          v-for="c in [...checkpoints].reverse()"
          :key="c.checkpoint_id"
          class="ckpt-menu-item"
          @click="rollback(c.checkpoint_id)"
        >
          <span class="ckpt-label">{{ c.label }}</span>
          <span class="ckpt-time">{{ c.timestamp }}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 聊天区域 -->
  <div class="chat-area" ref="chatEl" @scroll="onChatScroll">
    <div v-for="msg in messages" :key="msg.id" class="msg-row" :class="{ 'row-user': msg.role === 'user' }">
      <div class="avatar" :class="msg.role === 'user' ? 'av-u' : 'av-c'">{{ msg.role === 'user' ? 'U' : 'C' }}</div>
      <div class="msg-main">
      <!-- 用户消息 -->
      <div v-if="msg.role === 'user'" class="msg-bubble user-bubble">{{ msg.content }}</div>

      <!-- Agent 欢迎消息 -->
      <div v-else-if="msg.isWelcome" class="msg-bubble agent-bubble">
        <p>{{ msg.message }}</p>
        <div class="example-chips">
          <button v-for="ex in examples" :key="ex" class="example-chip" @click="fillExample(ex)">{{ ex }}</button>
        </div>
      </div>

      <!-- Agent 阶段消息：thinking 实时流式 → 思考完统一回复该阶段内容 -->
      <div v-else class="msg-bubble agent-bubble" :class="{ 'msg-error': msg.ok === false, 'msg-qa': msg.reply_type === 'qa' }">
          <div v-if="msg.stage_title" class="stage-badge">
            <b v-if="msg.stage_id !== 'summary'" class="stage-step">Step {{ stageNo(msg.stage_id) }}</b>
            <b v-else class="stage-step">✓</b>
            <span class="stage-name">· {{ msg.stage_title }}</span>
            <span v-if="msg.stage_id !== 'summary'" class="stage-progress">{{ stageNo(msg.stage_id) }}/{{ stageTotal(msg) }}</span>
          </div>

          <!-- Thinking 折叠：思考实时流式 + step/tool 进度，完成后自动折叠 -->
          <div v-if="msg.thinking_steps.length || msg.steps.length || msg.tool_calls.length || msg.live_text" class="think-block">
            <div class="think-toggle" @click="msg.show_thinking = !msg.show_thinking">
              <span class="think-arrow">{{ msg.show_thinking ? '▼' : '▶' }}</span>
              <span class="think-label">Thinking</span>
              <span v-if="msg.thinking_live" class="think-live">进行中…</span>
            </div>
            <div v-if="msg.show_thinking" class="think-list">
              <div v-for="(ts, i) in msg.thinking_steps" :key="'t' + i" class="think-line">
                <span class="think-phase">{{ stepLabel(ts.phase) }}</span>
                <span class="think-desc">{{ sanitizeDetail(ts.detail) }}</span>
              </div>
              <div v-for="(s, i) in msg.steps" :key="'s' + i" class="think-line">
                <span class="think-phase">{{ s.ok ? '✅' : '❌' }}</span>
                <span class="think-desc"><b>{{ stepLabel(s.name) }}</b> {{ sanitizeDetail(s.detail) }}</span>
              </div>
              <div v-for="(t, i) in msg.tool_calls" :key="'c' + i" class="tool-card" :class="{ 'tool-ok': t.ok, 'tool-fail': !t.ok }" @click="t.show = !t.show">
                <div class="tool-card-head">
                  <span class="tool-badge">{{ toolBadge(t.name) }}</span>
                  <span class="tool-text">{{ t.desc || t.name }}</span>
                  <span class="tool-chev">{{ t.show ? '▼' : '▶' }}</span>
                </div>
                <div v-if="t.show" class="tool-expand">
                  <pre v-if="t.cmd">{{ t.cmd }}</pre>
                  <pre v-if="t.result">{{ t.result }}</pre>
                </div>
              </div>
              <div v-if="msg.thinking_live" class="think-pulse"><span class="dot-pulse"></span></div>
            </div>
            <!-- LLM token 流：思考实时打出 -->
            <pre v-if="msg.live_text" class="think-stream" :class="{ 'ts-reasoning': msg.live_kind === 'reasoning' }">{{ msg.live_text }}▍</pre>
          </div>

          <!-- 阶段统一回复（markdown 渲染） -->
          <div v-if="msg.reply" class="reply-md" v-html="renderMarkdown(msg.reply)"></div>

          <!-- 加载中 -->
          <div v-if="msg.sending" class="sending-indicator">
            <span class="dot-pulse"></span> 正在执行...
          </div>

          <!-- R15/R9: 多行候选卡片，点击本地切换(有缓存)或发"用行N"(无缓存) -->
          <div v-if="!msg.sending && msg.row_alternatives && msg.row_alternatives.length" class="row-alt-block">
            <div class="row-alt-hint">
              命中 {{ msg.row_alternatives.length }} 条记录，点击切换：
              <span v-if="msg.multi_results && msg.multi_results.length" class="ra-cached-badge">📦 已缓存</span>
            </div>
            <div class="row-alt-cards" :class="{ 'is-grid': msg.row_alternatives.length >= 3 }">
              <div
                v-for="(ra, ri) in msg.row_alternatives" :key="ri"
                class="row-alt-card"
                :class="{ 'is-current': ra.current }"
                @click="selectRowAlt(ra.row, msg)"
              >
                <div class="ra-head">
                  <span class="ra-row-badge">行{{ ra.row }}</span>
                  <span class="ra-primary">{{ ra.value }}</span>
                  <span v-if="ra.current" class="ra-cur-tag">当前展示</span>
                </div>
                <div v-if="ra.summary && Object.keys(ra.summary).length" class="ra-summary">
                  <div v-for="(val, key) in ra.summary" :key="key" class="ra-field">
                    <span class="ra-field-key">{{ key }}</span>
                    <span class="ra-field-val">{{ val }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 表单式新增：表头 + 约束 + 可填写空行 + 实时标红 -->
          <div v-if="msg.reply_type === 'form' && msg.form" class="form-card">
            <div class="form-header">
              <span class="form-title">📝 新增表单</span>
              <span class="form-loc">📁 {{ msg.form.stem }} · 📄 {{ msg.form.sheet }}</span>
            </div>
            <div class="form-scroll">
              <table class="form-table">
                <thead>
                  <tr><th>列名</th><th>约束</th><th>值</th></tr>
                </thead>
                <tbody>
                  <tr v-for="c in msg.form.columns" :key="c.col">
                    <td class="ft-col">
                      {{ c.col_name || ('col_' + c.col) }}<span v-if="c.required" class="ft-req">*</span>
                    </td>
                    <td class="ft-constraint">{{ c.description || c.col_type || '—' }}</td>
                    <td class="ft-val-cell">
                      <input
                        v-model="msg.form.values[String(c.col)]"
                        class="ft-input"
                        :class="{ 'ft-error': msg.form.errors[String(c.col)], 'ft-warn-input': msg.form.warnings[String(c.col)] }"
                        :placeholder="c.col_type || ''"
                        :disabled="msg.form.committed"
                        @input="onFormInput(msg.form, String(c.col))"
                      />
                      <div v-if="msg.form.errors[String(c.col)]" class="ft-err">⚠ {{ msg.form.errors[String(c.col)] }}</div>
                      <div v-else-if="msg.form.warnings[String(c.col)]" class="ft-warn">⚠ {{ msg.form.warnings[String(c.col)] }}</div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="form-actions" v-if="!msg.form.committed">
              <button class="form-btn validate-btn" @click="validateForm(msg.form)" :disabled="msg.form.validating">
                {{ msg.form.validating ? '校验中…' : '🔍 校验' }}
              </button>
              <button class="form-btn commit-btn" @click="commitForm(msg.form)" :disabled="msg.form.submitting">
                {{ msg.form.submitting ? '提交中…' : '✅ 确认新增' }}
              </button>
            </div>
            <div v-if="msg.form.lastMessage" class="form-msg" :class="{ 'fm-ok': msg.form.committed, 'fm-err': !msg.form.committed && Object.keys(msg.form.errors||{}).length }">
              {{ msg.form.lastMessage }}
            </div>
          </div>

          <!-- 复合操作：按子任务分段渲染（定位→操作→结果→表体） -->
          <div v-if="msg.reply_type !== 'qa' && ((msg.sub_tasks_live && msg.sub_tasks_live.length) || (msg.sub_tasks && msg.sub_tasks.length))" class="subtasks-block">
            <div class="subtasks-progress" v-if="msg.sub_tasks && msg.sub_tasks.length">
              <span class="progress-text">{{ subtaskProgress(msg.sub_tasks) }}</span>
              <div class="progress-bar"><div class="progress-fill" :style="{ width: subtaskProgressPct(msg.sub_tasks) + '%' }"></div></div>
            </div>
            <div v-for="(st, si) in (msg.sub_tasks_live && msg.sub_tasks_live.length ? msg.sub_tasks_live : msg.sub_tasks)" :key="si" class="subtask-card" :class="{ 'subtask-ok': st.ok === true, 'subtask-fail': st.ok === false, 'subtask-loading': st.loading }">
              <div class="subtask-head">
                <span class="subtask-idx">#{{ st.index || st.idx }}</span>
                <span class="subtask-action">{{ subtaskTitle(st) }}</span>
                <span class="subtask-status" v-if="st.loading">⏳</span>
                <span class="subtask-status" v-else>{{ st.ok === false ? '❌' : (st.skipped ? '⏭' : '✅') }}</span>
              </div>
              <div v-if="st.loading" class="subtask-skeleton"><span class="skel-line"></span><span class="skel-line"></span></div>
              <template v-else>
              <!-- 子任务步骤 -->
              <div v-if="st.steps && st.steps.length" class="steps-card">
                <div v-for="(s, i) in st.steps" :key="i" class="step-row" :class="{ 'step-ok': s.ok, 'step-fail': !s.ok }">
                  <span class="step-icon">{{ s.ok ? '✅' : '❌' }}</span>
                  <span class="step-name">{{ stepLabel(s.name) }}</span>
                  <span class="step-detail">{{ s.detail }}</span>
                </div>
              </div>
              <!-- 子任务结果消息 -->
              <div v-if="st.message" class="subtask-msg" :class="{ 'result-ok': st.ok, 'result-err': !st.ok }">
                {{ st.message }}
              </div>
              <!-- 子任务表体 -->
              <div v-if="st.result_table && st.result_table.columns && st.result_table.columns.length" class="result-table-card">
                <div class="rt-header">
                  <span class="rt-title">{{ rtTitle(st.result_table.kind) }}</span>
                  <span class="rt-loc" v-if="st.result_table.file">📁 {{ st.result_table.file }}<span v-if="st.result_table.sheet"> · 📄 {{ st.result_table.sheet }}</span><span v-if="st.result_table.row"> · 行 {{ st.result_table.row }}</span></span>
                </div>
                <table class="rt-table">
                  <thead v-if="st.result_table.kind === 'set'">
                    <tr><th>列名</th><th>旧值</th><th>→</th><th>新值</th></tr>
                  </thead>
                  <thead v-else>
                    <tr><th>列名</th><th>{{ rtValueHead(st.result_table.kind) }}</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(c, ci) in st.result_table.columns" :key="ci">
                      <td class="rt-col">{{ c.col_name || ('col_' + c.col) }}</td>
                      <template v-if="st.result_table.kind === 'set'">
                        <td class="rt-old">{{ fmtVal(c.old_value) }}</td>
                        <td class="rt-arrow">→</td>
                        <td class="rt-new">{{ fmtVal(c.new_value) }}</td>
                      </template>
                      <template v-else>
                        <td class="rt-val" :class="rtValClass(st.result_table.kind)">{{ fmtVal(rtCellVal(st.result_table.kind, c)) }}</td>
                      </template>
                    </tr>
                  </tbody>
                </table>
              </div>
              </template>
            </div>
          </div>

          <!-- 危险操作二次确认卡片 -->
          <div v-if="msg.needs_confirm" class="confirm-card">
            <!-- 跨表搜索：展示当前表 top5 候选行 + 确认搜索其他表 -->
            <template v-if="msg.confirm_kind === 'cross_table_search'">
              <div class="confirm-icon">🔍 在 {{ msg.pending_search?.table_stem }}/{{ msg.pending_search?.sheet }} 未找到「{{ msg.pending_search?.value }}」，当前表最可能候选：</div>
              <div v-if="msg.pending_search?.top5?.length" class="pending-rows-card">
                <table class="rt-table">
                  <thead><tr><th>行</th><th>{{ msg.pending_search?.col_name || '名称' }}</th><th>其他列（摘要）</th></tr></thead>
                  <tbody>
                    <tr v-for="(r, i) in msg.pending_search.top5" :key="i">
                      <td class="rt-col">{{ r.row }}</td>
                      <td class="rt-val">{{ r.value }}</td>
                      <td class="rt-col">
                        <span v-for="(val, key) in (r.summary || {})" :key="key" class="ra-field">
                          <span class="ra-field-key">{{ key }}</span>=<span class="ra-field-val">{{ val }}</span>&nbsp;
                        </span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <template v-if="!msg.confirmResolved">
                <div class="confirm-actions">
                  <button class="confirm-btn confirm-yes" @click="confirmDangerous(msg)">🔍 搜索其他表格</button>
                  <button class="confirm-btn confirm-no" @click="cancelDangerous(msg)">取消</button>
                  <span class="confirm-countdown">剩余 {{ fmtCountdown(msg.confirmSeconds) }}</span>
                </div>
              </template>
              <template v-else>
                <div class="confirm-resolved">
                  <span v-if="msg.confirmCancelled">已取消。</span>
                  <span v-else-if="msg.confirmExpired">确认已超时失效，请重新发起操作。</span>
                  <span v-else>正在搜索其他表格…</span>
                </div>
              </template>
            </template>
            <!-- 级联/置信度删除（原逻辑） -->
            <template v-else>
              <div class="confirm-icon">⚠️ {{ msg.confirm_kind === 'cascade' ? '存在关联数据，请选择删除方式' : '定位置信度较低，请确认删除' }}</div>
              <template v-if="!msg.confirmResolved">
                <!-- 级联：确认级联删除 / 仅删当前行 -->
                <div v-if="msg.confirm_kind === 'cascade'" class="confirm-actions">
                  <button class="confirm-btn confirm-yes" @click="confirmDangerous(msg)">确认（级联删除关联）</button>
                  <button class="confirm-btn confirm-only" @click="deleteCurrentOnly(msg)">仅删当前行</button>
                  <span class="confirm-countdown">剩余 {{ fmtCountdown(msg.confirmSeconds) }}</span>
                </div>
                <!-- 低置信度：确认删除 / 取消 -->
                <div v-else class="confirm-actions">
                  <button class="confirm-btn confirm-yes" @click="confirmDangerous(msg)">确认删除</button>
                  <button class="confirm-btn confirm-no" @click="cancelDangerous(msg)">取消</button>
                  <span class="confirm-countdown">剩余 {{ fmtCountdown(msg.confirmSeconds) }}</span>
                </div>
              </template>
              <template v-else>
                <div class="confirm-resolved">
                  <span v-if="msg.confirmCancelled">已取消，未执行删除。</span>
                  <span v-else-if="msg.confirmExpired">确认已超时失效，请重新发起操作。</span>
                  <span v-else>已提交，正在执行…</span>
                </div>
              </template>
            </template>
          </div>

          <!-- 错误中断反问卡片：agent 遇阻断错误，用户改/补后续跑 -->
          <!-- 要求 B/C：user_friendly 大白话优先渲染；提交后折叠成 INFO 摘要 -->
          <div v-if="msg.ask" class="ask-card" :class="{ 'ask-card--collapsed': msg.askCollapsed }">
            <!-- 要求 C：折叠态：一行 INFO 摘要（记录用户提交了什么） -->
            <div v-if="msg.askCollapsed" class="ask-collapsed-info">
              <span class="ask-collapsed-icon">✓</span>
              <span class="ask-collapsed-text">{{ msg.askUserReply || '已提交，续跑中...' }}</span>
            </div>
            <!-- 展开态：完整 ask 卡片 -->
            <template v-else>
            <div class="ask-icon">❌ {{ msg.ask.reason }}</div>
            <div class="ask-detail">
              <span class="ask-loc">📍 {{ msg.ask.table }}/{{ msg.ask.sheet }}<span v-if="msg.ask.failed_col"> 列[{{ msg.ask.failed_col }}]</span></span>
              <!-- 要求 B：优先渲染 user_friendly 大白话，fallback root_cause -->
              <span v-if="msg.ask.user_friendly" class="ask-cause ask-cause--friendly">{{ msg.ask.user_friendly.reason }}</span>
              <span v-else class="ask-cause">原因：{{ msg.ask.root_cause }}</span>
              <span v-if="msg.ask.attempted_strategies" class="ask-strats">已试：{{ msg.ask.attempted_strategies }}</span>
              <span v-if="msg.ask.snip" class="ask-snip">原指令：「{{ msg.ask.snip }}」</span>
            </div>
            <!-- 要求 B：user_friendly.action 优先渲染 -->
            <div class="ask-hint">{{ (msg.ask.user_friendly && msg.ask.user_friendly.action) || msg.ask.suggestion }}</div>
            <div v-if="msg.ask.example" class="ask-example">参考写法：{{ msg.ask.example }}</div>
            <template v-if="!msg.askResolved">
              <!-- PK 冲突简化交互:建议ID + 接受/自定义输入 -->
              <template v-if="msg.ask.mode_hint === 'pk_conflict' && msg.ask.suggested_id != null">
                <div class="ask-pk-suggest">
                  建议 ID：<b>{{ msg.ask.suggested_id }}</b>
                  <input v-model="msg.askCustomId" type="number" class="ask-input ask-input--pk" :placeholder="`或输入其他 ID（默认 ${msg.ask.suggested_id}）`">
                </div>
                <div class="ask-actions">
                  <button class="confirm-btn confirm-yes" @click="replyAskPk(msg)">接受并续跑</button>
                  <button class="confirm-btn confirm-no" @click="replyAsk(msg, 'skip')">跳过</button>
                </div>
              </template>
              <!-- 其他失败:保留原文本填值 -->
              <template v-else>
                <textarea v-model="msg.askReplyText" class="ask-input" placeholder="例：建筑类型填 5；数字/枚举列请填数字或编号，写完点提交续跑"></textarea>
                <div class="ask-actions">
                  <button class="confirm-btn confirm-yes" @click="replyAsk(msg, 'nl')">提交续跑</button>
                  <button class="confirm-btn confirm-no" @click="replyAsk(msg, 'skip')">跳过</button>
                </div>
              </template>
            </template>
            <template v-else>
              <div class="confirm-resolved">
                <span v-if="msg.askMode === 'skip'">已跳过，记失败继续后续子任务。</span>
                <span v-else>已提交，续跑中…</span>
              </div>
            </template>
            </template>
          </div>

          <!-- 结构化失败清单（done 阶段，非交互失败/用户跳过时展示） -->
          <div v-if="msg.failures && msg.failures.length" class="failures-card">
            <div class="failures-icon">⚠️ {{ msg.failures.length }} 项失败未解决</div>
            <div v-for="(f, i) in msg.failures" :key="'fail'+i" class="failure-item">
              <span class="failure-loc">📍 {{ f.table }}/{{ f.sheet }}<span v-if="f.col"> 列[{{ f.col }}]</span></span>
              <span class="failure-type">{{ f.type }}</span>
              <span class="failure-cause">原因：{{ f.root_cause }}</span>
              <span v-if="f.attempted_strategies" class="failure-strats">已试：{{ f.attempted_strategies }}</span>
              <span v-if="f.snip" class="failure-snip">原指令：「{{ f.snip }}」</span>
              <span v-if="f.suggestion" class="failure-hint">建议：{{ f.suggestion }}</span>
              <span v-if="f.user_reply" class="failure-reply">用户回复：{{ f.user_reply }}</span>
            </div>
          </div>

          <!-- 单指令：表体结构（优先于 diff_preview） -->
          <div v-if="msg.reply_type !== 'qa' && !(msg.sub_tasks && msg.sub_tasks.length) && msg.result_table && msg.result_table.columns && msg.result_table.columns.length" class="result-table-card">
            <div class="rt-header">
              <span class="rt-title">{{ rtTitle(msg.result_table.kind) }}</span>
              <span class="rt-loc" v-if="msg.result_table.file">📁 {{ msg.result_table.file }}<span v-if="msg.result_table.sheet"> · 📄 {{ msg.result_table.sheet }}</span><span v-if="msg.result_table.row"> · 行 {{ msg.result_table.row }}</span></span>
            </div>
            <table class="rt-table">
              <!-- 修改/清空：列 / 旧值 / 新值 -->
              <thead v-if="msg.result_table.kind === 'set'">
                <tr><th>列名</th><th>旧值</th><th>→</th><th>新值</th></tr>
              </thead>
              <!-- 增/删/查：列名 / 值 -->
              <thead v-else>
                <tr><th>列名</th><th>{{ rtValueHead(msg.result_table.kind) }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="(c, ci) in msg.result_table.columns" :key="ci">
                  <td class="rt-col">{{ c.col_name || ('col_' + c.col) }}</td>
                  <template v-if="msg.result_table.kind === 'set'">
                    <td class="rt-old">{{ fmtVal(c.old_value) }}</td>
                    <td class="rt-arrow">→</td>
                    <td class="rt-new">{{ fmtVal(c.new_value) }}</td>
                  </template>
                  <template v-else>
                    <td class="rt-val" :class="rtValClass(msg.result_table.kind)">{{ fmtVal(rtCellVal(msg.result_table.kind, c)) }}</td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 差异预览（result_table 缺失时的回退） -->
          <div v-else-if="msg.reply_type !== 'qa' && !(msg.sub_tasks && msg.sub_tasks.length) && msg.diff_preview && msg.diff_preview.changes && msg.diff_preview.changes.length" class="diff-inline">
            <div class="diff-title">📝 变更预览</div>
            <table class="diff-table">
              <thead><tr><th>列</th><th>旧值</th><th>→</th><th>新值</th></tr></thead>
              <tbody>
                <tr v-for="(c, ci) in msg.diff_preview.changes" :key="ci">
                  <td>{{ c.col_name || 'col_' + c.col }}</td>
                  <td class="diff-old">{{ c.old_value ?? '(空)' }}</td>
                  <td>→</td>
                  <td class="diff-new">{{ c.new_value ?? '(空)' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 输入区 -->
  <div class="input-area">
    <div class="model-bar">
      <span class="model-label">模型</span>
      <select v-model="currentModel" @change="switchModel" class="model-sel" :disabled="modelsLoading" :title="currentModel || '默认（.env CODEMAKER_MODEL）'">
        <option value="">默认（.env）</option>
        <optgroup v-if="neteaseModels.length" :label="`netease-codemaker（付费，${neteaseModels.length}个）`">
          <option v-for="m in neteaseModels" :key="m.value" :value="m.value">{{ m.label }}</option>
        </optgroup>
        <optgroup v-if="opencodeModels.length" :label="`opencode（免费，${opencodeModels.length}个）`">
          <option v-for="m in opencodeModels" :key="m.value" :value="m.value">{{ m.label }}</option>
        </optgroup>
      </select>
    </div>
    <div v-if="sending" class="send-status-bar">
      <span class="ss-time">⏱ {{ elapsedSec }}s</span>
      <span v-if="lastHeartbeat.detail" class="ss-hb">💓 {{ lastHeartbeat.detail }}</span>
      <span v-if="stuckWarning" class="ss-stuck">⚠ 疑似卡住（90s 无新事件），可点停止中断</span>
    </div>
    <div class="input-row">
      <textarea
        v-model="inputText"
        class="chat-input"
        placeholder="输入操作指令，如：将灵兽饕餮的物攻资质改为1500"
        rows="2"
        @keydown.enter.exact.prevent="sendMessage()"
        :disabled="sending"
      ></textarea>
      <button v-if="!sending" class="send-btn" @click="sendMessage()" :disabled="!inputText.trim()">
        🚀
      </button>
      <button v-else class="send-btn stop-btn" @click="stopSending" title="中断当前执行">
        ⏹ 停止
      </button>
    </div>
  </div>
</div>
</template>

<script>
export default {
  methods: {
    stepLabel(name) {
      const map = {
        resolve_table: '定位表格', resolve_sheet: '定位Sheet',
        match_locator: '匹配定位列', match_target: '匹配目标列',
        locate_row: '定位行', write: '写入', read_cell: '读取',
        add_values: '提取新增值', append_row: '追加行',
        delete_cell: '清空单元格', delete_row: '删除行',
        // 6 步流程
        'Step1解析': 'Step1 解析', 'Step2分区': 'Step2 分区',
        'Step3计划': 'Step3 计划', 'Step4校验': 'Step4 校验',
        'Step5应用': 'Step5 应用', 'Step6汇总': 'Step6 汇总',
        // V2 4-Step step_id
        'step1_parse': '解析', 'step2_validate': '校验',
        'step3_execute': '执行', 'step4_conclude': '汇总',
        'Step1 解析': '解析', 'Step2 校验': '校验',
        'Step3 执行': '执行', 'Step4 汇总': '汇总',
      }
      return map[name] || name
    },
    subtaskTitle(st) {
      const map = { add: '新增', delete: '删除', set: '修改', get: '查询' }
      const action = map[st.intent_action] || st.intent_action || '操作'
      const loc = []
      if (st.table_stem) loc.push(st.table_stem)
      if (st.table_sheet) loc.push(st.table_sheet)
      return action + (loc.length ? ' · ' + loc.join(' / ') : '')
    },
    subtaskProgress(subs) {
      if (!subs || !subs.length) return ''
      const done = subs.filter(s => s.ok === true || s.ok === false || s.skipped).length
      const ok = subs.filter(s => s.ok === true).length
      const fail = subs.filter(s => s.ok === false).length
      const skip = subs.filter(s => s.skipped).length
      const parts = [`已完成 ${done}/${subs.length}`]
      if (ok) parts.push(`成功 ${ok}`)
      if (fail) parts.push(`失败 ${fail}`)
      if (skip) parts.push(`跳过 ${skip}`)
      return parts.join(' · ')
    },
    subtaskProgressPct(subs) {
      if (!subs || !subs.length) return 0
      const done = subs.filter(s => s.ok === true || s.ok === false || s.skipped).length
      return Math.round((done / subs.length) * 100)
    },
    stageNo(id) {
      if (id === 'summary') return ''
      // V2 4-Step step_id（step1_parse 等）
      const order4v2 = ['step1_parse', 'step2_validate', 'step3_execute', 'step4_conclude']
      const i4v2 = order4v2.indexOf(id)
      if (i4v2 >= 0) return i4v2 + 1
      // 旧 4-Step Loop（CODEMAKER_4STEP_LOOP=1）
      const order4 = ['s1_parse', 's2_validate', 's3_execute', 's4_summary']
      const order6 = ['s1_parse', 's2_partition', 's3_plan',
                     's4_verify', 's5_apply', 's6_summary']
      if (['s2_validate', 's3_execute', 's4_summary'].includes(id)) {
        const i = order4.indexOf(id)
        return i >= 0 ? i + 1 : '-'
      }
      const i6 = order6.indexOf(id)
      return i6 >= 0 ? i6 + 1 : '-'
    },
    stageTotal(msg) {
      // 优先读后端 stage_start total 字段（4-Step=4,6 步=6,解决 s1_parse 错位）
      if (msg && msg.stage_total) return msg.stage_total
      // fallback：基于 stage_id（V2/4-Step 专属 → 4,否则 6）
      const id = msg && msg.stage_id ? msg.stage_id : ''
      const order4v2 = ['step1_parse', 'step2_validate', 'step3_execute', 'step4_conclude']
      if (order4v2.includes(id) || ['s2_validate', 's3_execute', 's4_summary'].includes(id)) return 4
      return 6
    },
    // 黑话脱敏：把技术字段名转成用户友好文案
    sanitizeDetail(detail) {
      if (!detail) return detail
      let s = String(detail)
      // source=splitter_baseline 等隐藏
      s = s.replace(/source=splitter_baseline/g, '（模板兜底）')
      s = s.replace(/source=llm_(?:decompose|chain)/g, '（智能解析）')
      s = s.replace(/source=\S+/g, '')
      // locator_value → 定位值；produces/consumes 占位符转大白话
      s = s.replace(/locator_value/g, '定位值')
      s = s.replace(/produces/g, '（新建ID）')
      s = s.replace(/consumes/g, '（引用ID）')
      s = s.replace(/<produces:([^>]+)>/g, '（新建ID: $1）')
      s = s.replace(/<consume:([^>]+)>/g, '（引用ID: $1）')
      // 内部 Step5/Step6 编号统一成 4-Step 展示
      s = s.replace(/Step5[^:]*[:：]/g, '执行：')
      s = s.replace(/Step6[^:]*[:：]/g, '汇总：')
      s = s.replace(/Step2[^:]*[:：](?!.*校验)/g, '校验：')
      s = s.replace(/Step3[^:]*[:：](?!.*执行)/g, '执行：')
      s = s.replace(/Step4[^:]*[:：](?!.*汇总)/g, '汇总：')
      return s
    },
    toolBadge(name) {
      const n = (name || '').toLowerCase()
      if (n.startsWith('read') || n.startsWith('list') || n.startsWith('get')) return 'Read'
      if (n.startsWith('write') || n.startsWith('append') || n.startsWith('delete') || n.startsWith('add') || n.startsWith('auto')) return 'Write'
      if (n.startsWith('resolve') || n.startsWith('match') || n.startsWith('locate')) return 'Locate'
      return 'Tool'
    },
    renderMarkdown(md) {
      if (!md) return ''
      const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      const inline = s => s
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      const out = []
      let code = null, list = null, table = null
      const closeList = () => { if (list) { out.push('</' + list + '>'); list = null } }
      const closeTable = () => {
        if (!table) return
        const rows = table.filter(r => r !== null)
        if (rows.length) {
          let h = '<table class="md-table"><thead><tr>' + rows[0].map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>'
          for (let i = 1; i < rows.length; i++) {
            h += '<tr>' + rows[i].map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>'
          }
          out.push(h + '</tbody></table>')
        }
        table = null
      }
      for (const raw of String(md).split('\n')) {
        const line = esc(raw)
        const t = line.trim()
        if (t.startsWith('```')) {
          if (code === null) { closeList(); closeTable(); code = [] }
          else { out.push('<pre class="md-code">' + code.join('\n') + '</pre>'); code = null }
          continue
        }
        if (code !== null) { code.push(line); continue }
        if (t.startsWith('|')) {
          closeList()
          if (!table) table = []
          if (/^[\s|:-]+$/.test(t)) { table.push(null); continue }
          table.push(t.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim()))
          continue
        }
        closeTable()
        const hm = t.match(/^(#{1,4})\s+(.*)$/)
        if (hm) { closeList(); out.push('<h4 class="md-h">' + inline(hm[2]) + '</h4>'); continue }
        if (/^[-*]\s+/.test(t)) {
          closeTable()
          if (list !== 'ul') { closeList(); out.push('<ul class="md-list">'); list = 'ul' }
          out.push('<li>' + inline(t.replace(/^[-*]\s+/, '')) + '</li>')
          continue
        }
        if (/^\d+[.)]\s+/.test(t)) {
          if (list !== 'ol') { closeList(); out.push('<ol class="md-list">'); list = 'ol' }
          out.push('<li>' + inline(t.replace(/^\d+[.)]\s+/, '')) + '</li>')
          continue
        }
        closeList()
        if (!t) { continue }
        out.push('<p class="md-p">' + inline(t) + '</p>')
      }
      if (code !== null) out.push('<pre class="md-code">' + code.join('\n') + '</pre>')
      closeList(); closeTable()
      return out.join('')
    },
    rtTitle(kind) {
      const map = { set: '✏️ 修改对比', add: '✨ 新增行内容', delete: '🗑️ 删除行内容', get: '🔍 查询结果' }
      return map[kind] || '📝 操作结果'
    },
    rtValueHead(kind) {
      const map = { add: '新增值', delete: '被删值', get: '查询值' }
      return map[kind] || '值'
    },
    rtValClass(kind) {
      const map = { add: 'rt-new', delete: 'rt-old', get: 'rt-get' }
      return map[kind] || ''
    },
    rtCellVal(kind, c) {
      if (kind === 'delete') return c.old_value
      return c.new_value
    },
    fmtVal(v) {
      if (v === null || v === undefined || v === '') return '(空)'
      return String(v)
    },
  },
}
</script>

<style scoped>
.agent-view {
  display: flex; flex-direction: column; height: 100%;
}

.context-bar {
  display: flex; gap: 8px; padding: 6px 16px;
  background: var(--bg-active); border-bottom: 1px solid var(--border);
}
.ctx-tag {
  padding: 2px 10px; border-radius: 4px;
  background: var(--bg-active); color: var(--accent); font-size: 0.8rem;
}

.chat-area {
  flex: 1; overflow-y: auto; padding: 16px;
}

.msg-wrapper { margin-bottom: 12px; }
.msg-user { display: flex; justify-content: flex-end; }
.msg-agent { display: flex; justify-content: flex-start; }

.msg-bubble {
  max-width: 85%; padding: 10px 14px; border-radius: 12px;
  font-size: 0.9rem; line-height: 1.5;
}
.user-bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
.agent-bubble { background: var(--bg-card); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
.msg-error { border-color: var(--danger); }
.msg-sending { opacity: 0.7; }
.msg-qa { border-color: var(--success); background: var(--success-soft); }

.steps-card {
  background: var(--bg-input); border-radius: 8px; padding: 8px 12px;
  margin-bottom: 8px; font-size: 0.82rem;
}
.step-row { display: flex; gap: 6px; padding: 3px 0; align-items: baseline; }
.step-icon { width: 18px; text-align: center; flex-shrink: 0; }
.step-name { color: var(--text-muted); min-width: 70px; flex-shrink: 0; }
.step-detail { color: var(--text-secondary); word-break: break-all; }
.step-fail .step-detail { color: var(--accent); }

.result-text { padding: 4px 0; font-weight: 500; }
.result-ok { color: var(--success); }
.result-err { color: var(--accent); }

.confirm-card { margin-top: 8px; padding: 10px 12px; border: 1px solid var(--warning); border-radius: 8px; background: var(--warning-soft); }
.confirm-icon { color: var(--warning); font-weight: 600; margin-bottom: 8px; }
.confirm-actions { display: flex; align-items: center; gap: 10px; }
.confirm-btn { padding: 5px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
.confirm-yes { background: var(--accent); color: #fff; }
.confirm-yes:hover { background: var(--accent-hover); }
.confirm-only { background: var(--warning); color: var(--bg-card); }
.confirm-only:hover { background: var(--warning); }
.confirm-no { background: var(--bg-hover); color: var(--text-secondary); }
.confirm-no:hover { background: var(--bg-hover); }
.confirm-countdown { color: var(--text-muted); font-size: 0.85rem; margin-left: auto; }

.ask-card { margin-top: 8px; padding: 10px 12px; border: 1px solid var(--accent); border-radius: 8px; background: rgba(220, 53, 69, 0.08); }
/* 要求 C：折叠态 — 一行 INFO 摘要，不占空间 */
.ask-card--collapsed { padding: 6px 12px; background: rgba(25, 135, 84, 0.08); border-color: var(--success, #198f54); }
.ask-collapsed-info { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; color: var(--success, #198f54); }
.ask-collapsed-icon { font-weight: 600; }
.ask-collapsed-text { color: var(--text-secondary); }
/* 要求 B：user_friendly 大白话样式 */
.ask-cause--friendly { color: var(--text-primary); font-weight: 500; }
.ask-icon { color: var(--accent); font-weight: 600; margin-bottom: 6px; }
.ask-detail { display: flex; flex-direction: column; gap: 2px; font-size: 0.88rem; color: var(--text-secondary); margin-bottom: 6px; }
.ask-loc { color: var(--accent); font-weight: 500; }
.ask-cause { color: var(--text-primary); }
.ask-strats { color: var(--text-muted); font-size: 0.82rem; }
.ask-snip { color: var(--text-muted); font-size: 0.82rem; }
.ask-hint { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 4px; }
.ask-example { font-size: 0.82rem; color: var(--text-muted); font-style: italic; margin-bottom: 8px; }
.ask-input { width: 100%; min-height: 56px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-input); color: var(--text-primary); font-size: 0.9rem; resize: vertical; box-sizing: border-box; }
.ask-actions { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.ask-pk-suggest { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 6px 0; font-size: 0.9rem; }
.ask-pk-suggest b { color: var(--primary, #2563eb); font-size: 1rem; }
.ask-input--pk { flex: 1 1 160px; min-height: 0; padding: 6px 8px; }

.failures-card { margin-top: 8px; padding: 10px 12px; border: 1px solid var(--accent); border-radius: 8px; background: rgba(220, 53, 69, 0.06); }
.failures-icon { color: var(--accent); font-weight: 600; margin-bottom: 6px; }
.failure-item { display: flex; flex-direction: column; gap: 2px; padding: 6px 0; border-top: 1px dashed var(--border); font-size: 0.88rem; }
.failure-item:first-of-type { border-top: none; }
.failure-loc { color: var(--accent); font-weight: 500; }
.failure-type { color: var(--text-muted); font-size: 0.8rem; }
.failure-cause { color: var(--text-primary); }
.failure-strats, .failure-snip, .failure-reply { color: var(--text-muted); font-size: 0.82rem; }
.failure-hint { color: var(--text-secondary); font-size: 0.82rem; }
.confirm-resolved { color: var(--text-muted); font-size: 0.9rem; }

.qa-answer { display: flex; gap: 8px; align-items: flex-start; }
.qa-icon { font-size: 1.1rem; flex-shrink: 0; margin-top: 2px; }
.qa-text { line-height: 1.7; word-break: break-word; }
.qa-text :deep(code) { background: var(--bg-hover); padding: 1px 4px; border-radius: 3px; font-size: 0.85em; }
.qa-text :deep(strong) { color: var(--success); }

.diff-inline { margin-top: 8px; }
.diff-title { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px; }
.diff-table { width: 100%; font-size: 0.82rem; border-collapse: collapse; }
.diff-table th, .diff-table td {
  padding: 3px 8px; text-align: left; border-bottom: 1px solid var(--border);
}
.diff-old { color: var(--accent); }
.diff-new { color: var(--success); }

/* 表体结构卡片 */
.result-table-card { margin-top: 8px; background: var(--bg-input); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.rt-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.rt-title { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.rt-loc { font-size: 0.72rem; color: var(--text-muted); }
.rt-table { width: 100%; font-size: 0.82rem; border-collapse: collapse; }
.rt-table th { color: var(--text-muted); font-weight: 500; padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border); }
.rt-table td { padding: 4px 8px; border-bottom: 1px solid var(--bg-hover); vertical-align: top; word-break: break-all; }
.rt-col { color: var(--text-secondary); min-width: 90px; }
.rt-old { color: var(--accent); }
.rt-new { color: var(--success); }
.rt-get { color: var(--info); }
.rt-arrow { color: var(--text-placeholder); text-align: center; width: 20px; }

.example-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.example-chip {
  padding: 4px 12px; border: 1px solid var(--accent); border-radius: 14px;
  background: transparent; color: var(--accent); cursor: pointer;
  font-size: 0.8rem; transition: all 0.2s;
}
.example-chip:hover { background: var(--accent); color: #fff; }

.sending-indicator { display: flex; align-items: center; gap: 8px; color: var(--text-muted); }
.dot-pulse {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); animation: pulse 1s infinite;
}
@keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

/* R15: 行定位歧义候选卡片 */
/* R9: 思考过程折叠 */
.flow-block { margin: 6px 0; }
.flow-toggle { display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; font-size: 0.82rem; color: var(--text-muted); user-select: none; }
.flow-toggle:hover { background: var(--bg-input); }
.flow-arrow { font-size: 0.7rem; width: 14px; }
.flow-label { font-weight: 600; color: var(--info); }
.flow-live { color: var(--accent); font-weight: 400; }
.flow-list { margin-top: 4px; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; display: flex; flex-direction: column; gap: 3px; }
.flow-thinking { display: flex; gap: 6px; padding: 2px 0; font-size: 0.76rem; color: var(--text-muted); align-items: baseline; }
.flow-thinking .ft-phase { color: var(--accent); font-weight: 600; min-width: 48px; }
.flow-thinking .ft-detail { color: var(--text-muted); }
.flow-tool { background: var(--bg-card, #fff); border: 1px solid var(--border, #e2e4ea); border-radius: 6px; margin: 2px 0; overflow: hidden; font-size: 0.78rem; }
.flow-tool.tool-ok { border-left: 3px solid var(--success, #2ecc71); }
.flow-tool.tool-fail { border-left: 3px solid var(--danger, #e74c3c); }
.flow-tool-head { display: flex; gap: 6px; padding: 4px 8px; cursor: pointer; align-items: center; user-select: none; }
.flow-tool-head .ft-icon { font-weight: 700; min-width: 14px; }
.flow-tool.tool-ok .ft-icon { color: var(--success, #2ecc71); }
.flow-tool.tool-fail .ft-icon { color: var(--danger, #e74c3c); }
.flow-tool-head .ft-name { color: var(--accent); font-weight: 600; min-width: 50px; }
.flow-tool-head .ft-desc { color: var(--text-muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.flow-tool-head .ft-chevron { font-size: 0.7rem; color: var(--text-muted); }
.flow-tool-body { border-top: 1px solid var(--border, #e2e4ea); padding: 6px 8px; }
.ft-cmd pre, .ft-result pre { margin: 0; padding: 6px 8px; background: var(--bg, #1e1e2e); color: var(--text, #cdd6f4); border-radius: 4px; font-size: 0.72rem; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; }
.ft-result pre { margin-top: 4px; }
.flow-step { display: flex; gap: 6px; padding: 3px 8px; font-size: 0.78rem; border-radius: 4px; background: var(--bg-card, #fff); align-items: center; }
.flow-step.step-ok { border-left: 3px solid var(--success, #2ecc71); }
.flow-step.step-fail { border-left: 3px solid var(--danger, #e74c3c); }
.flow-step .ft-icon { min-width: 16px; }
.flow-step .ft-name { color: var(--accent); font-weight: 600; min-width: 60px; }
.flow-step .ft-detail { color: var(--text-muted); }
.flow-pulse { padding: 4px 0; }

.steps-block { margin: 4px 0; }
.steps-toggle { display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; font-size: 0.82rem; color: var(--text-muted); user-select: none; }
.steps-toggle:hover { background: var(--bg-input); }
.steps-arrow { font-size: 0.7rem; width: 14px; }
.steps-label { font-weight: 600; color: var(--info); }
.tool-block { margin: 4px 0; }
.tool-toggle { display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; font-size: 0.82rem; color: var(--text-muted); user-select: none; }
.tool-toggle:hover { background: var(--bg-input); }
.tool-arrow { font-size: 0.7rem; width: 14px; }
.tool-label { font-weight: 600; color: var(--info); }
.tool-detail { margin-top: 4px; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; }
.tool-row { display: flex; gap: 8px; padding: 3px 0; font-size: 0.78rem; border-bottom: 1px solid var(--bg-input); align-items: center; }
.tool-row:last-child { border-bottom: none; }
.tool-icon { font-weight: 700; min-width: 16px; }
.tool-ok .tool-icon { color: var(--success, #2ecc71); }
.tool-fail .tool-icon { color: var(--danger, #e74c3c); }
.tool-name { color: var(--accent); font-weight: 600; min-width: 60px; }
.tool-desc { color: var(--text-muted); }

.thinking-block { margin: 4px 0; }
.thinking-toggle { display: flex; align-items: center; gap: 6px; cursor: pointer; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; font-size: 0.82rem; color: var(--text-muted); user-select: none; }
.thinking-toggle:hover { background: var(--bg-input); }
.thinking-arrow { font-size: 0.7rem; width: 14px; }
.thinking-label { font-weight: 600; color: var(--info); }
.thinking-summary { color: var(--text-muted); font-size: 0.78rem; margin-left: auto; }
.thinking-detail { margin-top: 4px; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; }
.thinking-step { display: flex; gap: 8px; padding: 3px 0; font-size: 0.78rem; border-bottom: 1px solid var(--bg-input); }
.thinking-step:last-child { border-bottom: none; }
.thinking-phase { color: var(--accent); font-weight: 600; min-width: 32px; }
.thinking-desc { color: var(--text-muted); }

.row-alt-block { margin: 8px 0; padding: 10px 12px; background: var(--bg-input); border: 1px solid var(--accent); border-radius: 8px; }
.row-alt-hint { font-size: 0.82rem; color: var(--accent); margin-bottom: 8px; }
.row-alt-cards { display: flex; flex-direction: column; gap: 8px; }
.row-alt-cards.is-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.row-alt-card {
  display: flex; flex-direction: column; gap: 6px;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--info-soft); cursor: pointer; transition: all 0.15s;
}
.row-alt-card:hover { background: var(--info-soft); border-color: var(--accent); }
.row-alt-card.is-current { background: var(--bg-active); border-color: var(--success); }
.ra-head { display: flex; align-items: center; gap: 8px; }
.ra-row-badge {
  flex-shrink: 0; padding: 2px 8px; border-radius: 10px;
  background: var(--accent); color: #fff; font-size: 0.74rem; font-weight: 600;
}
.row-alt-card.is-current .ra-row-badge { background: var(--success); color: var(--info-soft); }
.ra-primary { color: var(--text-primary); font-size: 0.84rem; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ra-cur-tag { font-size: 0.7rem; color: var(--success); opacity: 0.9; }
.ra-cached-badge { font-size: 0.7rem; color: var(--info); margin-left: 8px; }
.ra-summary { display: flex; flex-direction: column; gap: 3px; padding-left: 2px; border-top: 1px solid var(--border); padding-top: 6px; }
.ra-field { display: flex; gap: 6px; font-size: 0.76rem; line-height: 1.3; }
.ra-field-key { color: var(--info); min-width: 56px; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ra-field-val { color: var(--text-secondary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.input-area { padding: 12px 16px; border-top: 1px solid var(--border); background: var(--bg-card); }
.model-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.model-label { font-size: 0.78rem; color: var(--text-muted); flex-shrink: 0; }
.model-sel {
  max-width: 320px; padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-input); color: var(--text-primary); font-size: 0.82rem; outline: none;
  cursor: pointer; font-family: inherit;
}
.model-sel:hover { border-color: var(--accent); }
.model-sel:focus { border-color: var(--accent); }
.model-sel:disabled { opacity: 0.5; cursor: not-allowed; }
.quick-row { display: flex; gap: 6px; margin-bottom: 8px; }
.quick-btn {
  padding: 4px 12px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-input); color: var(--text-muted); cursor: pointer; font-size: 0.8rem;
}
.quick-btn:hover { border-color: var(--accent); color: var(--accent); }
.input-row { display: flex; gap: 8px; }
.chat-input {
  flex: 1; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-input); color: var(--text-primary); font-size: 0.9rem;
  resize: none; outline: none; font-family: inherit;
}
.chat-input:focus { border-color: var(--accent); }
.send-btn {
  padding: 8px 16px; border: none; border-radius: 8px;
  background: var(--accent); color: #fff; cursor: pointer; font-size: 1.1rem;
}
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 表单式新增 */
.form-card {
  margin-top: 8px; background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px;
}
.form-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.form-title { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.form-loc { font-size: 0.72rem; color: var(--text-muted); }
.form-scroll { max-height: 320px; overflow-y: auto; }
.form-table { width: 100%; font-size: 0.82rem; border-collapse: collapse; }
.form-table th { color: var(--text-muted); font-weight: 500; padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--bg-input); }
.form-table td { padding: 4px 8px; border-bottom: 1px solid var(--bg-hover); vertical-align: top; }
.ft-col { color: var(--text-secondary); min-width: 90px; white-space: nowrap; }
.ft-req { color: var(--accent); margin-left: 2px; }
.ft-constraint { color: var(--text-muted); font-size: 0.76rem; min-width: 100px; max-width: 180px; }
.ft-val-cell { min-width: 140px; }
.ft-input {
  width: 100%; padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px;
  background: var(--bg-card); color: var(--text-primary); font-size: 0.82rem; outline: none;
  font-family: inherit; box-sizing: border-box;
}
.ft-input:focus { border-color: var(--accent); }
.ft-input:disabled { opacity: 0.6; cursor: not-allowed; }
.ft-error { border-color: var(--accent); background: var(--danger-soft); }
.ft-warn-input { border-color: var(--warning); }
.ft-err { color: var(--accent); font-size: 0.74rem; margin-top: 2px; }
.ft-warn { color: var(--warning); font-size: 0.74rem; margin-top: 2px; }
.form-actions { display: flex; gap: 8px; margin-top: 8px; }
.form-btn {
  padding: 5px 14px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-input); color: var(--text-secondary); cursor: pointer; font-size: 0.82rem;
  transition: all 0.2s;
}
.validate-btn:hover { border-color: var(--info); color: var(--info); }
.commit-btn { border-color: var(--success); color: var(--success); }
.commit-btn:hover:not(:disabled) { background: var(--success); color: var(--bg-input); }
.form-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.form-msg { margin-top: 6px; font-size: 0.8rem; color: var(--text-secondary); }
.fm-ok { color: var(--success); }
.fm-err { color: var(--accent); }

/* 复合操作子任务分段卡片 */
.subtasks-block { display: flex; flex-direction: column; gap: 10px; margin-top: 4px; }
.subtasks-progress { display: flex; align-items: center; gap: 10px; padding: 6px 8px; background: var(--bg-elevated, #f5f7fa); border-radius: 6px; font-size: 13px; }
.subtasks-progress .progress-text { color: var(--text-secondary, #666); white-space: nowrap; }
.subtasks-progress .progress-bar { flex: 1; height: 6px; background: var(--bg-disabled, #e0e4e8); border-radius: 3px; overflow: hidden; }
.subtasks-progress .progress-fill { height: 100%; background: var(--accent, #4f46e5); transition: width 0.3s; }
.subtask-card {
  background: var(--bg-input); border: 1px solid var(--border); border-left: 3px solid var(--success);
  border-radius: 6px; padding: 8px 10px;
}
.subtask-card.subtask-fail { border-left-color: var(--accent); }
.subtask-card.subtask-loading { border-left-color: var(--info); }
.subtask-skeleton { display: flex; flex-direction: column; gap: 6px; padding: 4px 0; }
.skel-line { height: 10px; border-radius: 4px; background: linear-gradient(90deg, var(--bg-hover) 25%, var(--border) 50%, var(--bg-hover) 75%); background-size: 200% 100%; animation: skel-shimmer 1.4s infinite; }
@keyframes skel-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
.subtask-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px dashed var(--border); }
.subtask-idx { color: var(--info); font-weight: 600; font-size: 0.82rem; }
.subtask-action { color: var(--text-primary); font-size: 0.82rem; flex: 1; }
.subtask-status { font-size: 0.9rem; }
.subtask-msg { margin-top: 6px; font-size: 0.8rem; color: var(--text-secondary); }

/* 回退按钮 + checkpoint 下拉 */
.ckpt-wrap { position: relative; margin-left: auto; }
.rollback-btn {
  padding: 3px 12px; border: 1px solid var(--warning); border-radius: 4px;
  background: transparent; color: var(--warning); cursor: pointer;
  font-size: 0.78rem; transition: all 0.2s; display: flex; align-items: center; gap: 4px;
}
.rollback-btn:hover:not(:disabled) { background: var(--warning); color: var(--bg-card); }
.rollback-btn:disabled { opacity: 0.35; cursor: not-allowed; border-color: var(--text-placeholder); color: var(--text-placeholder); }
.ckpt-caret { font-size: 0.7rem; opacity: 0.8; }
.ckpt-menu {
  position: absolute; top: calc(100% + 4px); right: 0; min-width: 260px; max-width: 380px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5); z-index: 50; padding: 4px 0; max-height: 320px; overflow-y: auto;
}
.ckpt-menu-item {
  padding: 6px 12px; cursor: pointer; font-size: 0.8rem; color: var(--text-secondary);
  display: flex; justify-content: space-between; gap: 8px; align-items: baseline;
}
.ckpt-menu-item:hover { background: var(--bg-hover); color: #fff; }
.ckpt-quick { color: var(--warning); border-bottom: 1px solid var(--border); }
.ckpt-menu-sep { padding: 4px 12px; font-size: 0.72rem; color: var(--text-placeholder); border-bottom: 1px solid var(--border); }
.ckpt-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ckpt-time { font-size: 0.66rem; color: var(--text-placeholder); flex-shrink: 0; }

/* ── 多端对话布局（头像 + 阶段气泡） ── */
.msg-row { display: flex; gap: 10px; margin-bottom: 14px; align-items: flex-start; }
.avatar {
  flex-shrink: 0; width: 28px; height: 28px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.78rem; font-weight: 700; margin-top: 2px;
}
.av-u { background: var(--success-soft); color: var(--success); border: 1px solid var(--success); }
.av-c { background: var(--info-soft); color: var(--info); border: 1px solid var(--info); }
.msg-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.msg-row .msg-bubble { max-width: 100%; }
/* 用户消息右对齐 */
.msg-row.row-user { flex-direction: row-reverse; }
.msg-row.row-user .msg-main { align-items: flex-end; }
/* 阶段徽章：Step 编号加黑加粗，进度可见 */
.stage-badge { display: flex; align-items: baseline; gap: 6px; font-size: 0.78rem; margin-bottom: 6px; letter-spacing: 0.5px; }
.stage-badge .stage-step { font-size: 0.88rem; font-weight: 800; color: var(--text-primary); }
.stage-badge .stage-name { color: var(--info); font-weight: 600; }
.stage-badge .stage-progress { color: var(--text-muted); font-size: 0.7rem; }

/* Thinking 折叠 */
.think-block { margin-bottom: 8px; }
.think-toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; padding: 2px 0; }
.think-arrow { font-size: 0.68rem; color: var(--text-muted); }
.think-label { font-style: italic; color: var(--text-muted); font-size: 0.82rem; }
.think-live { color: var(--accent); font-size: 0.74rem; }
.think-list { margin-top: 6px; padding: 8px 10px; background: var(--bg-input); border-radius: 8px; display: flex; flex-direction: column; gap: 4px; }
.think-line { display: flex; gap: 8px; font-size: 0.78rem; align-items: baseline; }
.think-line .think-phase { color: var(--accent); font-weight: 600; min-width: 40px; flex-shrink: 0; }
.think-line .think-desc { color: var(--text-muted); word-break: break-all; }
.think-line .think-desc b { color: var(--text-secondary); }
.tool-card { border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); overflow: hidden; font-size: 0.78rem; }
.tool-card.tool-ok { border-left: 3px solid var(--success); }
.tool-card.tool-fail { border-left: 3px solid var(--danger); }
.tool-card-head { display: flex; align-items: center; gap: 8px; padding: 5px 8px; cursor: pointer; user-select: none; }
.tool-badge {
  flex-shrink: 0; padding: 1px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600;
  background: var(--info-soft); color: var(--info);
}
.tool-text { flex: 1; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-chev { font-size: 0.66rem; color: var(--text-muted); }
.tool-expand { border-top: 1px solid var(--border); padding: 6px 8px; }
.tool-expand pre { margin: 0 0 4px; padding: 6px 8px; background: var(--bg-hover); border-radius: 4px; font-size: 0.72rem; white-space: pre-wrap; word-break: break-all; max-height: 180px; overflow-y: auto; }
.think-pulse { padding: 2px 0; }
.think-stream {
  margin: 6px 0 0; padding: 8px 10px; background: var(--bg-input); border-radius: 8px;
  font-size: 0.78rem; line-height: 1.6; color: var(--text-secondary);
  white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow-y: auto;
  font-family: inherit;
}
.think-stream.ts-reasoning { color: var(--text-muted); font-style: italic; }

/* Markdown 回复体 */
.reply-md { font-size: 0.88rem; line-height: 1.7; color: var(--text-primary); word-break: break-word; }
.reply-md .md-p { margin: 3px 0; }
.reply-md .md-h { margin: 8px 0 4px; font-size: 0.92rem; color: var(--text-primary); }
.reply-md .md-list { margin: 4px 0; padding-left: 20px; }
.reply-md .md-list li { margin: 2px 0; }
.reply-md code { background: var(--bg-hover); padding: 1px 5px; border-radius: 3px; font-size: 0.8em; }
.reply-md strong { color: var(--success); }
.reply-md .md-code { margin: 6px 0; padding: 8px 10px; background: var(--bg-hover); border-radius: 6px; font-size: 0.76rem; white-space: pre-wrap; word-break: break-all; }
.reply-md .md-table { width: auto; margin: 6px 0; border-collapse: collapse; font-size: 0.8rem; }
.reply-md .md-table th, .reply-md .md-table td { border: 1px solid var(--border); padding: 4px 10px; text-align: left; }
.reply-md .md-table th { background: var(--bg-input); color: var(--text-muted); font-weight: 600; }

/* 发送态状态条 + 停止按钮 */
.send-status-bar { display: flex; align-items: center; gap: 12px; padding: 4px 10px; font-size: 0.78rem; color: var(--text-secondary); background: var(--bg-hover); border-radius: 6px; margin-bottom: 6px; }
.ss-time { color: var(--info); font-weight: 600; }
.ss-hb { color: var(--text-secondary); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ss-stuck { color: var(--accent); font-weight: 600; }
.send-btn.stop-btn { background: var(--accent); }
.send-btn.stop-btn:hover { filter: brightness(1.1); }
</style>
