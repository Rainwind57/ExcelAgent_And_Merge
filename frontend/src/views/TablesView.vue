<script setup>
import { ref, onMounted, computed, nextTick } from 'vue'

// ── 状态 ──
const tables = ref([])
const selectedTable = ref(null)
const selectedSheet = ref('')
const sheetData = ref(null)
const sidebarCollapsed = ref(false)
// R8: 列约束元数据（hover 列头 tooltip），key=列号(1-based) → FormColumn
const columnsMeta = ref({})
const loading = ref(false)
const searchQuery = ref('')
const searchResults = ref(null)
const searching = ref(false)
// T1: 搜索结果视图与表格浏览视图切换 — 跳转后保留 searchResults，可点"返回搜索结果"回到列表
const showSearchResults = ref(true)
let searchDebounceTimer = null
const currentPage = ref(1)
const pageSize = 50
const highlightedRow = ref(null)  // 跳转高亮行（页内索引）

// T11: 下拉待选 + 键盘导航
const dropdownActiveIndex = ref(-1)  // 当前键盘选中项（-1 未选）
const DROPDOWN_LIMIT = 12             // 下拉最多显示 12 条，超出走"查看全部结果"
// R17: 下拉显隐独立开关——点击跳转后关闭下拉（避免遮挡表格），新搜索时重新打开
const showDropdown = ref(true)

function highlightSnippet(text, query) {
  if (!text || !query) return String(text ?? '')
  const s = String(text)
  const q = String(query)
  const idx = s.toLowerCase().indexOf(q.toLowerCase())
  if (idx < 0) return s
  // 用 ⟦⟧ 占位，外层转 HTML 后替换为 <mark>，避免 XSS（cell 值先 escape）
  return s.slice(0, idx) + '\u27e6' + s.slice(idx, idx + q.length) + '\u27e7' + s.slice(idx + q.length)
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function renderSnippet(v, q, max = 80) {
  const s = v == null ? '' : String(v)
  const marked = highlightSnippet(s, q)
  const truncated = marked.length > max ? marked.slice(0, max) + '…' : marked
  // escape 后把占位符 ⟦⟧ 替换为 <mark> 高亮标签
  return escapeHtml(truncated)
    .replace(/\u27e6/g, '<mark class="snippet-hit">')
    .replace(/\u27e7/g, '</mark>')
}

const dropdownResults = computed(() => {
  if (!searchResults.value || !searchResults.value.results) return []
  return searchResults.value.results.slice(0, DROPDOWN_LIMIT)
})

function pickDropdown(r) {
  if (!r) return
  gotoResult(r)
  dropdownActiveIndex.value = -1
}

function onSearchKeydown(e) {
  if (!searchResults.value || searchResults.value.results.length === 0) return
  const total = dropdownResults.value.length
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    dropdownActiveIndex.value = total ? (dropdownActiveIndex.value + 1) % total : -1
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    dropdownActiveIndex.value = total ? (dropdownActiveIndex.value - 1 + total) % total : -1
  } else if (e.key === 'Enter') {
    // 有下拉项被选中 → 跳转该条；未选中 → 展开为完整卡片列表（showSearchResults=true）
    if (dropdownActiveIndex.value >= 0 && dropdownActiveIndex.value < total) {
      e.preventDefault()
      pickDropdown(dropdownResults.value[dropdownActiveIndex.value])
    } else {
      showSearchResults.value = true
    }
  } else if (e.key === 'Escape') {
    dropdownActiveIndex.value = -1
    searchResults.value = null
    showSearchResults.value = false
  }
}

function showAllResults() {
  // 展开为完整卡片列表（content-area 渲染），保留下拉状态清空
  showSearchResults.value = true
  dropdownActiveIndex.value = -1
}

// ── 加载表格列表 ──
onMounted(async () => {
  try {
    const res = await fetch('/api/tables')
    tables.value = await res.json()
  } catch (e) {
    console.error('加载表格列表失败', e)
  }
})

// ── 分组（按目录） ──
const tableGroups = computed(() => {
  const groups = {}
  for (const t of tables.value) {
    const dir = t.path.includes('/') ? t.path.split('/')[0] : 'root'
    if (!groups[dir]) groups[dir] = []
    groups[dir].push(t)
  }
  return groups
})

// ── 选择表格 ──
async function selectTable(table) {
  selectedTable.value = table
  selectedSheet.value = ''
  sheetData.value = null
  columnsMeta.value = {}
  currentPage.value = 1

  // 自动选择第一个 sheet
  if (table.sheets && table.sheets.length) {
    selectedSheet.value = table.sheets[0].name
    await loadSheetData()
  }
}

async function selectSheet(sheetName) {
  selectedSheet.value = sheetName
  currentPage.value = 1
  await loadSheetData()
}

async function loadSheetData() {
  if (!selectedTable.value || !selectedSheet.value) return
  loading.value = true
  try {
    const url = `/api/tables/${selectedTable.value.stem}/sheets/${selectedSheet.value}?page=${currentPage.value}&page_size=${pageSize}`
    // R8: 并行取列约束（hover 列头 tooltip），失败不阻断浏览
    const [dataRes, colsRes] = await Promise.all([
      fetch(url),
      fetch(`/api/tables/${selectedTable.value.stem}/sheets/${selectedSheet.value}/columns`).catch(() => null),
    ])
    sheetData.value = await dataRes.json()
    if (colsRes && colsRes.ok) {
      const cols = await colsRes.json()
      const map = {}
      ;(cols || []).forEach(c => { map[c.col] = c })
      columnsMeta.value = map
    } else {
      columnsMeta.value = {}
    }
  } catch (e) {
    console.error('加载数据失败', e)
  } finally {
    loading.value = false
  }
}

// R8: 拼接列头 tooltip — 列号+列名 + 约束描述（类型/必填/唯一/外键）
function colTooltip(i, h) {
  const base = `列 ${i + 1}：${h || '(空)'}`
  const meta = columnsMeta.value[i + 1]
  if (!meta || !meta.description) return base
  return `${base}（${meta.description}）`
}

// ── 搜索 ──
async function doSearch() {
  if (!searchQuery.value.trim()) {
    searchResults.value = null
    return
  }
  searching.value = true
  showSearchResults.value = false  // 默认走下拉态，不铺满 content-area
  showDropdown.value = true        // 新搜索打开下拉待选
  dropdownActiveIndex.value = -1
  try {
    const res = await fetch(`/api/tables/search?q=${encodeURIComponent(searchQuery.value)}`)
    searchResults.value = await res.json()
  } catch (e) {
    console.error('搜索失败', e)
  } finally {
    searching.value = false
  }
}

// T1: 输入即搜（300ms debounce），回车立即搜
function onSearchInput() {
  clearTimeout(searchDebounceTimer)
  searchDebounceTimer = setTimeout(() => doSearch(), 300)
}

// T1: 从表格浏览返回搜索结果列表
function backToResults() {
  showSearchResults.value = true
}

// ── 点击搜索结果跳转到对应表格/sheet/行 ──
async function gotoResult(r) {
  // 找到目标表格
  const t = tables.value.find(t => t.stem === r.table_stem)
  if (!t) {
    console.warn('未找到表格:', r.table_stem)
    return
  }
  // 切到表格浏览视图（保留 searchResults，供"返回搜索结果"用）
  showSearchResults.value = false
  showDropdown.value = false  // R17: 跳转后关闭下拉，避免遮挡表格主体
  selectedTable.value = t
  selectedSheet.value = r.sheet
  // 用浏览页相对行号（data_row，对齐表格 "#" 列）算页码与页内索引；
  // data_row 缺失时回退绝对行号（旧行为，可能存在偏移）
  const rowNo = r.data_row > 0 ? r.data_row : r.row
  currentPage.value = Math.floor((rowNo - 1) / pageSize) + 1
  await loadSheetData()
  // 数据加载后滚动到目标行 + 高亮
  await nextTick()
  const rowIdxInPage = (rowNo - 1) % pageSize
  highlightedRow.value = rowIdxInPage
  const el = document.getElementById(`row-${rowIdxInPage}`)
  if (el) {
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
  // 3 秒后清除高亮
  setTimeout(() => { highlightedRow.value = null }, 3000)
}

// ── 分页 ──
function prevPage() { if (currentPage.value > 1) { currentPage.value--; loadSheetData() } }
function nextPage() {
  if (sheetData.value && currentPage.value * pageSize < sheetData.value.total_rows) {
    currentPage.value++; loadSheetData()
  }
}

// ── 截断显示 ──
function trunc(v, max = 50) {
  const s = v == null ? '' : String(v)
  return s.length > max ? s.slice(0, max) + '…' : s
}

// ── R8: 原地编辑 ──
// R5: 快速编辑模式开关 — 开启后整表进入编辑态，单击即编辑（填表器体验）；关闭时仅双击编辑
const editMode = ref(false)
function toggleEditMode() {
  editMode.value = !editMode.value
  if (!editMode.value) cancelEdit()
}
const editing = ref(null)  // { ri, ci, value, saving, error }
const cellToast = ref('')  // 错误/成功提示

function startEdit(ri, ci, cell) {
  // Excel 绝对行号 = data_start + 页偏移 + 页内索引
  // （data_start 是后端返回的数据起始 Excel 行号，避免索引错位导致改错行）
  const ds = (sheetData.value && sheetData.value.data_start) || 1
  const absRow = ds + (currentPage.value - 1) * pageSize + ri
  editing.value = { ri, ci, absRow, value: cell == null ? '' : String(cell), saving: false, error: '' }
  nextTick(() => {
    const el = document.querySelector('.cell-input')
    if (el) { el.focus(); el.select() }
  })
}

function cancelEdit() { editing.value = null }

async function commitEdit() {
  const ed = editing.value
  if (!ed || ed.saving) return
  // 值未变 → 直接退出
  const original = sheetData.value.rows[ed.ri][ed.ci]
  if (String(ed.value) === String(original ?? '')) { editing.value = null; return }
  ed.saving = true
  ed.error = ''
  try {
    const res = await fetch('/api/tables/cell/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table_stem: selectedTable.value.stem,
        sheet: selectedSheet.value,
        row: ed.absRow,
        col: ed.ci + 1,  // 1-based
        value: ed.value,
      }),
    })
    const data = await res.json()
    if (data.ok) {
      // 本地更新单元格值，避免整页重载丢失滚动位置
      sheetData.value.rows[ed.ri][ed.ci] = data.new_value
      cellToast.value = data.message || '已更新'
      editing.value = null
      setTimeout(() => { cellToast.value = '' }, 2000)
    } else {
      ed.error = data.message || data.error || '更新失败'
      cellToast.value = ed.error
      setTimeout(() => { cellToast.value = '' }, 3000)
    }
  } catch (e) {
    ed.error = '网络错误：' + (e.message || e)
    cellToast.value = ed.error
    setTimeout(() => { cellToast.value = '' }, 3000)
  } finally {
    if (editing.value) editing.value.saving = false
  }
}

function onEditKey(e) {
  if (e.key === 'Enter') { e.preventDefault(); commitEdit() }
  else if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
}

// ── 是否有可显示数据（至少一个非空表头才渲染，避免只显示行号列"#"）──
const hasVisibleData = computed(() => {
  if (!sheetData.value) return false
  const hs = sheetData.value.headers || []
  return hs.some(h => h != null && String(h).trim() !== '')
})
</script>

<template>
<div class="explorer-view">
  <!-- 搜索栏 + T11 下拉待选 -->
  <div class="search-bar-wrap">
    <div class="search-bar">
      <input
        v-model="searchQuery" class="search-input"
        placeholder="搜索表格内容（支持中英文关键词，输入即搜）..."
        @input="onSearchInput"
        @keydown="onSearchKeydown"
      />
      <button class="search-btn" @click="doSearch" :disabled="searching">
        {{ searching ? '搜索中...' : '搜索' }}
      </button>
    </div>

    <!-- T11: 下拉待选列表（输入即显示，键盘上下选 + Enter 跳转） -->
    <div v-if="searchResults && !showSearchResults && showDropdown && dropdownResults.length"
         class="search-dropdown">
      <div
        v-for="(r, i) in dropdownResults" :key="r.table_stem + r.sheet + r.row + i"
        class="dropdown-item"
        :class="{ active: dropdownActiveIndex === i }"
        @click="pickDropdown(r)"
        @mouseenter="dropdownActiveIndex = i"
      >
        <span class="dd-table">{{ r.table_path }}</span>
        <span class="dd-sheet">{{ r.sheet }}</span>
        <span class="dd-pos">行{{ r.data_row || r.row }},{{ r.col_name }}</span>
        <span class="dd-match" v-html="renderSnippet(r.cell_value, searchQuery)"></span>
      </div>
      <div v-if="searchResults.total > DROPDOWN_LIMIT" class="dropdown-more"
           @click="showAllResults">
        查看全部 {{ searchResults.total }} 条结果 →
      </div>
    </div>
  </div>

  <div class="explorer-layout">
    <!-- 左侧：文件树（可收起） -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div v-if="!sidebarCollapsed">
        <h3 class="sidebar-title">表格列表</h3>
        <div v-for="(files, group) in tableGroups" :key="group" class="tree-group">
          <div class="group-name">{{ group }}/</div>
          <div
            v-for="t in files" :key="t.stem"
            class="tree-item"
            :class="{ active: selectedTable?.stem === t.stem }"
            @click="selectTable(t)"
          >
            {{ t.stem }}
            <span class="item-count">{{ t.sheets?.length || 0 }}</span>
          </div>
        </div>
      </div>
      <button class="sidebar-collapse" @click="sidebarCollapsed = !sidebarCollapsed"
              :title="sidebarCollapsed ? '展开' : '收起'">
        {{ sidebarCollapsed ? '›' : '‹' }}
      </button>
    </aside>

    <!-- 右侧：内容区 -->
    <div class="content-area">
      <!-- T1: 返回搜索结果按钮（跳转到表格浏览后可回到搜索列表） -->
      <div v-if="searchResults && !showSearchResults" class="back-to-results">
        <button class="btn-back" @click="backToResults">← 返回搜索结果（{{ searchResults.total }} 条）</button>
      </div>

      <!-- 搜索结果 -->
      <template v-if="searchResults && showSearchResults">
        <div class="search-header">
          <h3>搜索 "{{ searchQuery }}" — {{ searchResults.total }} 条结果</h3>
          <button class="btn-clear" @click="searchResults = null; showSearchResults = true">✕ 清除</button>
        </div>
        <div v-if="searchResults.results.length === 0" class="empty">无匹配结果</div>
        <div v-for="r in searchResults.results" :key="r.table_stem + r.sheet + r.row"
             class="search-result-card clickable" @click="gotoResult(r)">
          <div class="sr-header">
            <span class="sr-table">{{ r.table_path }}</span>
            <span class="sr-sheet">{{ r.sheet }}</span>
            <span class="sr-pos">行 {{ r.data_row || r.row }}, 列 {{ r.col_name }}</span>
            <span class="sr-jump-hint">→ 点击跳转</span>
          </div>
          <div class="sr-match">匹配: <b v-html="renderSnippet(r.cell_value, searchQuery, 100)"></b></div>
          <div class="sr-row" v-if="r.row_data">
            行数据: {{ r.row_data.filter(v => v != null).map(v => trunc(v, 30)).join(' | ') }}
          </div>
        </div>
      </template>

      <!-- 表格数据 -->
      <template v-else-if="selectedTable">
        <!-- Sheet 标签 -->
        <div class="sheet-tabs">
          <button
            v-for="s in selectedTable.sheets" :key="s.name"
            class="sheet-tab"
            :class="{ active: selectedSheet === s.name }"
            @click="selectSheet(s.name)"
          >{{ s.name }}
            <span class="sheet-rows">{{ s.row_count }}行</span>
          </button>
          <!-- R5: 快速编辑模式开关 -->
          <button class="edit-mode-toggle" :class="{ active: editMode }"
                  :title="editMode ? '当前为快速编辑模式，单击单元格即可编辑' : '开启后单击单元格即可编辑（关闭则需双击）'"
                  @click="toggleEditMode">{{ editMode ? '✎ 编辑中' : '✎ 快速编辑' }}</button>
        </div>

        <!-- 数据表格 -->
        <div class="data-table-wrap" v-if="sheetData && !loading && hasVisibleData">
          <table class="data-table">
            <thead>
              <tr>
                <th class="row-num">#</th>
                <th v-for="(h, i) in sheetData.headers" :key="i" :title="colTooltip(i, h)">{{ h }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, ri) in sheetData.rows" :key="ri"
                  :id="`row-${ri}`"
                  :class="{ 'row-highlight': highlightedRow === ri }">
                <td class="row-num">{{ (currentPage - 1) * pageSize + ri + 1 }}</td>
                <td v-for="(cell, ci) in row" :key="ci"
                    :class="{
                      'cell-editing': editing && editing.ri === ri && editing.ci === ci,
                      'cell-error': editing && editing.ri === ri && editing.ci === ci && editing.error,
                      'cell-editable': editMode
                    }"
                    @dblclick="startEdit(ri, ci, cell)"
                    @click="editMode && startEdit(ri, ci, cell)">
                  <input v-if="editing && editing.ri === ri && editing.ci === ci"
                         ref="editInput"
                         v-model="editing.value"
                         :disabled="editing.saving"
                         class="cell-input"
                         @keydown="onEditKey"
                         @blur="commitEdit" />
                  <span v-else>{{ trunc(cell, 60) }}</span>
                </td>
              </tr>
            </tbody>
          </table>

          <!-- R8 编辑提示 toast -->
          <div v-if="cellToast" class="cell-toast">{{ cellToast }}</div>

          <!-- 分页 -->
          <div class="pagination">
            <button :disabled="currentPage <= 1" @click="prevPage">◀ 上一页</button>
            <span>第 {{ currentPage }} 页 / 共 {{ Math.ceil(sheetData.total_rows / pageSize) }} 页</span>
            <button :disabled="currentPage * pageSize >= sheetData.total_rows" @click="nextPage">下一页 ▶</button>
          </div>
        </div>

        <div v-else-if="sheetData && !loading" class="empty">该 Sheet 无可显示数据（表头或数据行为空）</div>
        <div v-else-if="loading" class="loading">加载中...</div>
        <div v-else class="empty">请选择 Sheet</div>
      </template>

      <div v-else class="empty">请从左侧选择一个表格</div>
    </div>
  </div>
</div>
</template>

<style scoped>
.explorer-view { display: flex; flex-direction: column; height: 100%; }

.search-bar-wrap { position: relative; }

.search-bar { display: flex; gap: 8px; padding: 10px 16px; background: var(--bg-card); border-bottom: 1px solid var(--border); }
.search-input {
  flex: 1; padding: 6px 12px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-input); color: var(--text-primary); font-size: 0.85rem; outline: none;
}
.search-input:focus { border-color: var(--accent); }
.search-btn {
  padding: 6px 16px; border: none; border-radius: 6px;
  background: var(--accent); color: #fff; cursor: pointer; font-size: 0.85rem;
}
.search-btn:disabled { opacity: 0.5; }

/* T11: 下拉待选列表 */
.search-dropdown {
  position: absolute; left: 16px; right: 16px; top: 100%;
  background: var(--bg-input); border: 1px solid var(--border); border-top: none;
  border-radius: 0 0 6px 6px; max-height: 360px; overflow-y: auto;
  z-index: 20; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.dropdown-item {
  padding: 6px 12px; cursor: pointer; font-size: 0.8rem;
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  border-bottom: 1px solid var(--bg-card);
}
.dropdown-item:hover, .dropdown-item.active { background: var(--bg-active); }
.dropdown-item.active { border-left: 3px solid var(--accent); }
.dd-table { color: var(--accent); font-weight: 600; }
.dd-sheet { color: var(--success); }
.dd-pos { color: var(--text-muted); font-size: 0.75rem; }
.dd-match { color: var(--text-secondary); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dropdown-more {
  padding: 8px 12px; text-align: center; color: var(--accent);
  font-size: 0.8rem; cursor: pointer; background: var(--bg-card);
}
.dropdown-more:hover { background: var(--bg-active); }
.snippet-hit { background: var(--accent); color: #fff; padding: 0 2px; border-radius: 2px; }

.explorer-layout { display: flex; flex: 1; overflow: hidden; }

.sidebar {
  width: 240px; overflow-y: auto; padding: 8px;
  background: var(--bg-input); border-right: 1px solid var(--border); flex-shrink: 0;
  position: relative; transition: width 0.2s;
}
.sidebar.collapsed { width: 32px; overflow: visible; padding: 0; }
.sidebar-collapse {
  position: absolute; top: 8px; right: 4px;
  padding: 2px 8px; background: var(--bg-hover); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-secondary); cursor: pointer; font-size: 0.9rem; line-height: 1;
}
.sidebar-collapse:hover { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.sidebar.collapsed .sidebar-collapse { position: static; margin: 8px auto; display: block; }
.sidebar-title { font-size: 0.85rem; color: var(--text-muted); padding: 6px 8px; }
.tree-group { margin-bottom: 6px; }
.group-name { font-size: 0.75rem; color: var(--accent); padding: 4px 8px; font-weight: bold; }
.tree-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 5px 12px; cursor: pointer; font-size: 0.82rem;
  border-radius: 4px; color: var(--text-secondary);
}
.tree-item:hover { background: var(--bg-card); }
.tree-item.active { background: var(--bg-active); color: #fff; }
.item-count { font-size: 0.7rem; color: var(--text-placeholder); background: var(--bg-input); padding: 1px 6px; border-radius: 8px; }

.content-area { flex: 1; overflow: auto; }

.sheet-tabs {
  display: flex; flex-wrap: wrap; gap: 4px;
  padding: 8px 12px; border-bottom: 1px solid var(--border); background: var(--bg-card);
}
.sheet-tab {
  padding: 4px 12px; border: 1px solid var(--border); border-radius: 4px;
  background: transparent; color: var(--text-muted); cursor: pointer; font-size: 0.8rem;
}
.sheet-tab:hover { border-color: var(--accent); color: var(--text-secondary); }
.sheet-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.sheet-rows { font-size: 0.7rem; opacity: 0.7; margin-left: 4px; }

/* R5: 快速编辑模式开关 */
.edit-mode-toggle {
  margin-left: auto;
  padding: 4px 12px; border: 1px solid var(--success); border-radius: 4px;
  background: transparent; color: var(--success); cursor: pointer; font-size: 0.8rem;
}
.edit-mode-toggle:hover { background: var(--success-soft); }
.edit-mode-toggle.active { background: var(--success); color: var(--bg-input); border-color: var(--success); }

.data-table-wrap { overflow: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.data-table th {
  position: sticky; top: 0; z-index: 1;
  background: var(--bg-card); color: var(--accent); padding: 6px 8px;
  text-align: left; white-space: nowrap; border-bottom: 2px solid var(--border);
}
.data-table td {
  padding: 4px 8px; border-bottom: 1px solid var(--bg-card);
  max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  cursor: text;  /* R8: 双击编辑提示 */
}
.data-table tbody tr:hover { background: var(--bg-card); }
.row-num { color: var(--text-placeholder); font-size: 0.75rem; text-align: center; width: 40px; }

/* R5: 快速编辑模式 — 单元格可编辑态视觉提示 */
.cell-editable { cursor: cell; box-shadow: inset 2px 0 0 var(--success); }
.cell-editable:hover { background: var(--success-soft); }

/* R8: 原地编辑 */
.cell-editing { background: var(--bg-active) !important; box-shadow: inset 0 0 0 2px var(--accent); }
.cell-error { box-shadow: inset 0 0 0 2px var(--danger) !important; }
.cell-input {
  width: 100%; min-width: 80px; padding: 2px 4px;
  background: var(--bg-input); color: #fff; border: 1px solid var(--success); border-radius: 3px;
  font-size: 0.8rem; font-family: inherit; outline: none;
}
.cell-input:disabled { opacity: 0.6; }
.cell-toast {
  position: fixed; bottom: 20px; right: 20px; z-index: 100;
  padding: 8px 16px; border-radius: 6px; background: var(--bg-hover); color: var(--success);
  border: 1px solid var(--success); font-size: 0.82rem; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  max-width: 400px;
}

.pagination {
  display: flex; justify-content: center; align-items: center; gap: 12px;
  padding: 12px; font-size: 0.8rem; color: var(--text-muted);
}
.pagination button {
  padding: 4px 12px; border: 1px solid var(--border); border-radius: 4px;
  background: transparent; color: var(--text-secondary); cursor: pointer;
}
.pagination button:disabled { opacity: 0.3; cursor: not-allowed; }

.search-header { display: flex; justify-content: space-between; align-items: center; padding: 12px; }
.search-result-card {
  padding: 10px 16px; margin: 4px 8px; border-radius: 6px;
  background: var(--bg-card); border: 1px solid var(--border); font-size: 0.82rem;
}
.search-result-card.clickable {
  cursor: pointer; transition: border-color 0.15s, background 0.15s;
}
.search-result-card.clickable:hover {
  border-color: var(--accent); background: var(--bg-active);
}
.sr-jump-hint { color: var(--accent); font-size: 0.75rem; margin-left: auto; }
.data-table tbody tr.row-highlight {
  background: var(--bg-active) !important;
  animation: row-flash 3s ease-out;
}
@keyframes row-flash {
  0%, 100% { background: var(--bg-active); }
  50% { background: var(--bg-active); }
}
.sr-header { display: flex; gap: 12px; margin-bottom: 4px; align-items: center; }
.sr-table { color: var(--accent); } .sr-sheet { color: var(--success); } .sr-pos { color: var(--text-muted); }
.sr-match { color: var(--text-secondary); }
.sr-row { color: var(--text-placeholder); font-size: 0.78rem; margin-top: 4px; }

.btn-clear { padding: 4px 10px; border: 1px solid var(--text-placeholder); border-radius: 4px; background: transparent; color: var(--text-muted); cursor: pointer; }

/* T1: 返回搜索结果按钮 */
.back-to-results { padding: 8px 12px; background: var(--bg-hover); border-bottom: 1px solid var(--border); }
.btn-back {
  padding: 4px 12px; border: 1px solid var(--accent); border-radius: 4px;
  background: transparent; color: var(--accent); cursor: pointer; font-size: 0.82rem;
}
.btn-back:hover { background: var(--accent); color: #fff; }
.empty { padding: 32px; text-align: center; color: var(--text-placeholder); }
.loading { padding: 32px; text-align: center; color: var(--text-muted); }
</style>
