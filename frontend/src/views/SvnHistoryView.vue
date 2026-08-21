<script setup>
// ── SvnHistoryView：resources 下文件的 SVN 历史 diff 展示 ──
// 双表对照：左 = 历史版本(版本号较小)，右 = 当前版本(版本号较大)
// 由 base/curr 下拉框选择两个版本，按版本号自动判定左右
import { ref, computed, onMounted, nextTick, watch } from 'vue'

const files = ref([])
const search = ref('')
const selected = ref('')
const log = ref([])
const logLoading = ref(false)
const logError = ref('')
const baseRev = ref(null)   // base 下拉选中
const currRev = ref(null)   // curr 下拉选中
const diff = ref(null)
const diffLoading = ref(false)
const diffError = ref('')
const activeSheet = ref('')
const filterMode = ref('all')  // all | diff
const curDiffIdx = ref(-1)    // 当前聚焦的差异行索引（filteredDiffRows 内）
const leftTableRef = ref(null)
const rightTableRef = ref(null)
const sidebarCollapsed = ref(false)

// 下拉框选项：按版本号降序（最新在上）
const revOptions = computed(() => {
  return [...log.value].sort((a, b) => b.rev - a.rev)
})

// 判定左右：版本号小 = 历史（左），版本号大 = 当前（右）
// 后端 /api/svn/diff 接 rev1/rev2，返回的 rev1/rev2 透传，diff 表旧值取 rev1、新值取 rev2
// 前端按版本号大小把较小者作为 rev1（历史），较大者作为 rev2（当前）
const leftRev = computed(() => {
  if (!baseRev.value || !currRev.value) return null
  return Math.min(baseRev.value, currRev.value)
})
const rightRev = computed(() => {
  if (!baseRev.value || !currRev.value) return null
  return Math.max(baseRev.value, currRev.value)
})

const filteredFiles = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return files.value
  return files.value.filter(f => f.toLowerCase().includes(q))
})

const curSheet = computed(() => {
  if (!diff.value) return null
  return diff.value.sheets.find(s => s.name === activeSheet.value) || null
})

// 仅差异行（新增/删除/修改），用于跳转
const diffRows = computed(() => {
  if (!curSheet.value) return []
  return curSheet.value.rows.filter(r =>
    r.type === 'added' || r.type === 'removed' ||
    (r.type === 'matched' && r.cells.some(c => c.changed))
  )
})

// 显示行：all=全部，diff=仅差异
const displayRows = computed(() => {
  if (!curSheet.value) return []
  if (filterMode.value === 'all') return curSheet.value.rows
  return diffRows.value
})

// 当过滤模式或 sheet 变化时重置跳转索引
watch([filterMode, activeSheet, () => diff.value], () => {
  curDiffIdx.value = diffRows.value.length ? 0 : -1
  scrollToCur()
})

function isCurRow(row) {
  const dr = diffRows.value[curDiffIdx.value]
  return dr && dr.key === row.key && dr.type === row.type
}

function onPick(f) {
  selected.value = f
  log.value = []
  diff.value = null
  loadLog()
}

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
}

async function loadFiles() {
  try {
    const res = await fetch('/api/svn/files')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    files.value = obj.files || []
  } catch (e) { logError.value = e.message }
}

async function loadLog() {
  logLoading.value = true; logError.value = ''; log.value = []
  baseRev.value = null
  currRev.value = null
  try {
    const res = await fetch(`/api/svn/log?path=${encodeURIComponent(selected.value)}&limit=50`)
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    log.value = obj.entries || []
    // 默认：curr = 最新版本，base = 上一个版本
    if (log.value.length >= 2) {
      const sorted = [...log.value].sort((a, b) => b.rev - a.rev)
      currRev.value = sorted[0].rev
      baseRev.value = sorted[1].rev
    }
  } catch (e) { logError.value = e.message }
  finally { logLoading.value = false }
}

// 下拉框选项文本
function revLabel(e) {
  const msg = e.msg ? e.msg.slice(0, 30) : '(无说明)'
  return `r${e.rev}  ${msg}`
}

async function doDiff() {
  if (!baseRev.value || !currRev.value) { diffError.value = '请选择 base 和 curr 两个版本'; return }
  if (baseRev.value === currRev.value) { diffError.value = '请选择不同的版本'; return }
  diffLoading.value = true; diffError.value = ''; diff.value = null
  try {
    // 传给后端的 rev1=左(历史=小), rev2=右(当前=大)
    const res = await fetch(`/api/svn/diff?path=${encodeURIComponent(selected.value)}&rev1=${leftRev.value}&rev2=${rightRev.value}`)
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    diff.value = obj
    const firstDiffSheet = obj.sheets.find(s => s.rows.some(r =>
      r.type !== 'matched' || r.cells.some(c => c.changed)
    )) || obj.sheets[0]
    activeSheet.value = firstDiffSheet ? firstDiffSheet.name : ''
  } catch (e) { diffError.value = e.message }
  finally { diffLoading.value = false }
}

// 跳转：找到当前行在 displayRows 中的位置，映射到 diffRows 再 ±1
function gotoPrev() {
  if (!diffRows.value.length) return
  let i = curDiffIdx.value
  if (i <= 0) i = diffRows.value.length
  curDiffIdx.value = i - 1
  scrollToCur()
}
function gotoNext() {
  if (!diffRows.value.length) return
  let i = curDiffIdx.value
  if (i < 0 || i >= diffRows.value.length - 1) i = -1
  curDiffIdx.value = i + 1
  scrollToCur()
}

function scrollToCur() {
  nextTick(() => {
    const dr = diffRows.value[curDiffIdx.value]
    if (!dr) return
    const sel = `tr[data-key="${CSS.escape(dr.key)}"][data-type="${dr.type}"]`
    ;[leftTableRef.value, rightTableRef.value].forEach(t => {
      if (!t) return
      const row = t.querySelector(sel)
      if (row) row.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  })
}

onMounted(loadFiles)
</script>

<template>
<div class="svn-view">
  <!-- 左侧文件列表（可收起） -->
  <aside class="file-panel" :class="{ collapsed: sidebarCollapsed }">
    <div class="panel-head">
      <span v-if="!sidebarCollapsed">resources 文件</span>
      <span v-if="!sidebarCollapsed" class="count">{{ files.length }}</span>
      <button class="collapse-btn" @click="toggleSidebar" :title="sidebarCollapsed ? '展开' : '收起'">
        {{ sidebarCollapsed ? '›' : '‹' }}
      </button>
    </div>
    <template v-if="!sidebarCollapsed">
      <input class="search-box" v-model="search" placeholder="搜索文件名..." />
      <ul class="file-list">
        <li v-for="f in filteredFiles" :key="f"
            :class="{ active: selected === f }"
            @click="onPick(f)">{{ f }}</li>
      </ul>
    </template>
  </aside>

  <!-- 右侧主区 -->
  <section class="main-panel">
    <div v-if="!selected" class="empty-hint">← 选择左侧文件查看 SVN 历史</div>

    <template v-else>
      <!-- 版本选择区 -->
      <div class="log-section">
        <div class="section-head">
          <h3>{{ selected }} 的提交历史</h3>
        </div>
        <div v-if="logError" class="err">{{ logError }}</div>
        <div v-if="logLoading" class="hint">加载历史...</div>

        <!-- 双下拉框选 base/curr -->
        <div class="rev-select-bar" v-if="log.length">
          <div class="rev-field">
            <label class="rev-label">base 版本</label>
            <select class="rev-sel" v-model="baseRev">
              <option v-for="e in revOptions" :key="e.rev" :value="e.rev">{{ revLabel(e) }}</option>
            </select>
          </div>
          <div class="rev-field">
            <label class="rev-label">curr 版本</label>
            <select class="rev-sel" v-model="currRev">
              <option v-for="e in revOptions" :key="e.rev" :value="e.rev">{{ revLabel(e) }}</option>
            </select>
          </div>
          <button class="diff-btn" :disabled="!baseRev || !currRev || diffLoading" @click="doDiff">
            {{ diffLoading ? '比对中...' : '比对' }}
          </button>
        </div>
      </div>

      <!-- diff 结果区 -->
      <div v-if="diff" class="diff-section">
        <div class="section-head">
          <h3>r{{ diff.rev1 }} → r{{ diff.rev2 }} 差异</h3>
          <div class="stats">
            <span class="stat add">+{{ diff.total.added }}</span>
            <span class="stat rm">-{{ diff.total.removed }}</span>
            <span class="stat ch">~{{ diff.total.changed }}</span>
          </div>
        </div>
        <div v-if="diffError" class="err">{{ diffError }}</div>

        <!-- sheet tabs -->
        <div class="sheet-tabs">
          <button v-for="s in diff.sheets" :key="s.name"
                  :class="{ active: activeSheet === s.name }"
                  @click="activeSheet = s.name">
            {{ s.name }}
            <span class="dot" v-if="s.stats.added || s.stats.removed || s.stats.changed">●</span>
          </button>
        </div>

        <div v-if="curSheet" class="sheet-body">
          <div class="sheet-toolbar">
            <div class="left-tools">
              <label>筛选：</label>
              <button :class="{ on: filterMode === 'all' }" @click="filterMode = 'all'">全部</button>
              <button :class="{ on: filterMode === 'diff' }" @click="filterMode = 'diff'">仅差异</button>
              <span class="muted">（{{ displayRows.length }} 行）</span>
            </div>
            <div class="right-tools">
              <span class="nav-info" v-if="diffRows.length">
                {{ curDiffIdx < 0 ? 0 : curDiffIdx + 1 }} / {{ diffRows.length }}
              </span>
              <span class="nav-info" v-else>无差异</span>
              <button class="nav-btn" :disabled="!diffRows.length" @click="gotoPrev">↑ 上一差异</button>
              <button class="nav-btn" :disabled="!diffRows.length" @click="gotoNext">↓ 下一差异</button>
            </div>
          </div>

          <!-- 双表对照 -->
          <div class="dual-table">
            <div class="table-col">
              <div class="col-head rev1-head">历史版本 r{{ diff.rev1 }}</div>
              <div class="table-wrap" ref="leftTableRef">
                <table class="diff-table">
                  <thead>
                    <tr>
                      <th class="pk">主键</th>
                      <th class="type">类型</th>
                      <th v-for="(h, i) in curSheet.headers" :key="i">{{ h }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in displayRows" :key="'L'+row.key"
                        :data-key="row.key" :data-type="row.type"
                        :class="[row.type, { 'cur-row': isCurRow(row) }]">
                      <td class="pk">{{ row.key }}</td>
                      <td class="type">
                        <span :class="['tag', row.type]">
                          {{ row.type === 'added' ? '+' : row.type === 'removed' ? '-' : '~' }}
                        </span>
                      </td>
                      <td v-for="c in row.cells" :key="c.col"
                          :class="{ changed: c.changed }">
                        <!-- 左表显示旧值；added 行显示空 -->
                        <span v-if="row.type === 'added'" class="empty-cell">—</span>
                        <span v-else-if="c.changed" class="cell-old">{{ c.old ?? '' }}</span>
                        <span v-else>{{ c.old ?? '' }}</span>
                      </td>
                    </tr>
                    <tr v-if="!displayRows.length">
                      <td :colspan="curSheet.headers.length + 2" class="empty">无数据</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div class="table-col">
              <div class="col-head rev2-head">当前版本 r{{ diff.rev2 }}</div>
              <div class="table-wrap" ref="rightTableRef">
                <table class="diff-table">
                  <thead>
                    <tr>
                      <th class="pk">主键</th>
                      <th class="type">类型</th>
                      <th v-for="(h, i) in curSheet.headers" :key="i">{{ h }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in displayRows" :key="'R'+row.key"
                        :data-key="row.key" :data-type="row.type"
                        :class="[row.type, { 'cur-row': isCurRow(row) }]">
                      <td class="pk">{{ row.key }}</td>
                      <td class="type">
                        <span :class="['tag', row.type]">
                          {{ row.type === 'added' ? '+' : row.type === 'removed' ? '-' : '~' }}
                        </span>
                      </td>
                      <td v-for="c in row.cells" :key="c.col"
                          :class="{ changed: c.changed }">
                        <!-- 右表显示新值；removed 行显示空 -->
                        <span v-if="row.type === 'removed'" class="empty-cell">—</span>
                        <span v-else-if="c.changed" class="cell-new">{{ c.new ?? '' }}</span>
                        <span v-else>{{ c.new ?? '' }}</span>
                      </td>
                    </tr>
                    <tr v-if="!displayRows.length">
                      <td :colspan="curSheet.headers.length + 2" class="empty">无数据</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>
</div>
</template>

<style scoped>
.svn-view { display: flex; height: 100%; overflow: hidden; }

.file-panel {
  width: 260px; flex-shrink: 0;
  background: var(--bg-card); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
  transition: width 0.2s;
}
.file-panel.collapsed { width: 32px; }
.panel-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 10px; font-size: 0.85rem; color: var(--text-secondary); border-bottom: 1px solid var(--border);
}
.file-panel.collapsed .panel-head { padding: 8px 4px; justify-content: center; }
.count { color: var(--accent); font-weight: 600; }
.collapse-btn {
  padding: 2px 8px; background: var(--bg-hover); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-secondary); cursor: pointer; font-size: 0.9rem; line-height: 1;
}
.collapse-btn:hover { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.search-box { margin: 8px 10px; padding: 6px 10px;
  background: var(--bg-input); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-primary); font-size: 0.85rem;
}
.search-box:focus { outline: none; border-color: var(--accent); }
.file-list { flex: 1; overflow-y: auto; list-style: none; padding: 4px 0; }
.file-list li {
  padding: 6px 14px; font-size: 0.82rem; color: var(--text-secondary);
  cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.file-list li:hover { background: var(--bg-hover); color: var(--text-primary); }
.file-list li.active { background: var(--bg-hover); color: var(--accent); border-left: 3px solid var(--accent); }

.main-panel { flex: 1; overflow-y: auto; padding: 16px 20px; }
.empty-hint { color: var(--text-placeholder); text-align: center; padding: 60px 0; font-size: 0.95rem; }
.hint { color: var(--text-muted); font-size: 0.85rem; }
.err { color: var(--danger); padding: 10px 12px; background: var(--danger-soft); border-radius: 6px; font-size: 0.85rem; }

.section-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; flex-wrap: wrap; gap: 8px;
}
.section-head h3 { font-size: 0.95rem; color: var(--text-primary); font-weight: 500; }

/* 双下拉框选版本 */
.rev-select-bar {
  display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap;
  padding: 10px 12px; background: var(--bg-stripe); border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 12px;
}
.rev-field { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 220px; }
.rev-label { font-size: 0.78rem; color: var(--text-muted); font-weight: 600; }
.rev-sel {
  padding: 5px 10px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-input); color: var(--text-primary); font-size: 0.85rem; cursor: pointer;
}
.rev-sel:focus { outline: none; border-color: var(--accent); }
.diff-btn {
  padding: 6px 18px; background: var(--accent); color: #fff; border: none;
  border-radius: 6px; cursor: pointer; font-size: 0.85rem; font-weight: 600;
}
.diff-btn:disabled { background: var(--border-strong); color: var(--text-muted); cursor: not-allowed; }
.diff-btn:hover:not(:disabled) { background: var(--accent-hover); }

.diff-section { margin-top: 20px; }
.stats { display: flex; gap: 12px; font-size: 0.85rem; }
.stat.add { color: var(--diff-add); }
.stat.rm { color: var(--diff-del); }
.stat.ch { color: var(--diff-chg); }

.sheet-tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
.sheet-tabs button {
  padding: 4px 12px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text-secondary); cursor: pointer; font-size: 0.8rem;
}
.sheet-tabs button:hover { background: var(--bg-hover); color: var(--text-primary); }
.sheet-tabs button.active { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.dot { color: var(--accent); margin-left: 2px; }

.sheet-toolbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 0.8rem; color: var(--text-secondary); flex-wrap: wrap; gap: 8px;
}
.sheet-toolbar .left-tools, .sheet-toolbar .right-tools { display: flex; align-items: center; gap: 8px; }
.sheet-toolbar button {
  padding: 3px 10px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-secondary); cursor: pointer; font-size: 0.78rem;
}
.sheet-toolbar button.on { background: var(--bg-hover); color: var(--accent); }
.sheet-toolbar button:disabled { color: var(--text-placeholder); cursor: not-allowed; }
.muted { color: var(--text-muted); }
.nav-info { color: var(--text-secondary); font-size: 0.78rem; min-width: 60px; text-align: right; }
.nav-btn {
  padding: 3px 10px; background: var(--bg-hover); border: 1px solid var(--border-strong); border-radius: 4px;
  color: var(--text-primary); cursor: pointer; font-size: 0.78rem;
}
.nav-btn:hover:not(:disabled) { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.nav-btn:disabled { color: var(--text-placeholder); cursor: not-allowed; background: var(--bg-card); }

/* 双表对照 */
.dual-table { display: flex; gap: 10px; }
.table-col { flex: 1; min-width: 0; display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.col-head {
  padding: 6px 12px; font-size: 0.82rem; font-weight: 600; border-bottom: 1px solid var(--border);
}
.col-head.rev1-head { background: var(--info-soft); color: var(--info); }
.col-head.rev2-head { background: var(--diff-chg-bg); color: var(--diff-chg); }
.table-wrap { overflow: auto; max-height: 70vh; }

.diff-table { border-collapse: collapse; font-size: 0.78rem; width: 100%; }
.diff-table th {
  background: var(--bg-card); padding: 6px 10px; text-align: left; color: var(--text-secondary);
  font-weight: 500; border-bottom: 1px solid var(--border); white-space: nowrap;
  position: sticky; top: 0; z-index: 1;
}
.diff-table td {
  padding: 4px 10px; border-bottom: 1px solid var(--border-soft); color: var(--text-primary);
  vertical-align: top; word-break: break-all; max-width: 320px;
}
.diff-table tr.added { background: var(--diff-add-bg); }
.diff-table tr.removed { background: var(--diff-del-bg); }
.diff-table tr.matched td.changed { background: var(--diff-chg-bg); }
.diff-table tr.cur-row { box-shadow: inset 0 0 0 2px var(--accent); }
.pk { color: var(--info); font-weight: 600; white-space: nowrap; }
.type { white-space: nowrap; text-align: center; }
.tag { display: inline-block; min-width: 18px; text-align: center; font-weight: 700; border-radius: 3px; padding: 0 3px; }
.tag.added { color: var(--diff-add); }
.tag.removed { color: var(--diff-del); }
.tag.matched { color: var(--text-muted); }
.cell-old { color: var(--diff-del); }
.cell-new { color: var(--diff-add); }
.empty-cell { color: var(--text-placeholder); }
.empty { text-align: center; color: var(--text-muted); padding: 24px; }
</style>
