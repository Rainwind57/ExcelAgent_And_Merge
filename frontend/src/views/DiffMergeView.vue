<script setup>
// ── DiffMergeView：Excel 多版本比对 ──
import { ref, computed, nextTick, watch, onActivated, onDeactivated } from 'vue'

const files = ref([])
const loading = ref(false)
const error = ref('')
const data = ref(null)
const activeGroup = ref('')
const activeSheet = ref('')
const conflictIdx = ref(-1)
const modalCell = ref(null)          // { cell, key, header, ri, ci }
// M13: AI 冲突建议（冲突 modal 内"💡 AI 建议"按钮调 /api/agent/suggest-merge）
const aiSuggestion = ref(null)       // { suggested_version, suggestion, reasoning, confidence }
const aiSuggestBusy = ref(false)
const aiSuggestError = ref('')
const allDone = ref(false)
const showMoreMenu = ref(false)      // 工具栏"更多"下拉菜单（合回历史/比对历史/批量合回）
const selectedRows = ref(new Set())
const baseMap = ref({})

const filePrefixes = computed(() => {
  const set = new Set()
  files.value.forEach(f => {
    const name = f.name.replace(/\.xlsx$/i, '')
    const prefix = name.replace(/_\d+$/, '')
    set.add(prefix)
  })
  return [...set].sort()
})

const prefixFiles = computed(() => {
  const map = {}
  filePrefixes.value.forEach(p => {
    map[p] = files.value.filter(f => {
      const name = f.name.replace(/\.xlsx$/i, '')
      const prefix = name.replace(/_\d+$/, '')
      return prefix === p
    })
  })
  return map
})

watch(filePrefixes, (prefixes) => {
  const next = { ...baseMap.value }
  prefixes.forEach(p => {
    const candidates = prefixFiles.value[p]
    if (!candidates) return
    if (next[p] && candidates.some(f => f.name === next[p])) return
    const base = candidates.find(f => !/_\d+/.test(f.name.replace(/\.xlsx$/i, '')))
    next[p] = base ? base.name : candidates[0].name
  })
  for (const k of Object.keys(next)) {
    if (!prefixes.includes(k)) delete next[k]
  }
  baseMap.value = next
})

const history = ref([])
const historyIdx = ref(-1)

// 解决单元格冲突：写入新值并清除冲突标记，记录 resolvedBy 来源（用于审计/撤销）
// 若该行有公式列，改输入值后异步调后端重算公式列预览值（冲突选版本 → 下游公式实时更新）
// 公式冲突选版本：选中值是公式文本(以 = 开头)时同步 formula_text 并保留 diff_type='formula'，
// 触发 recomputeRowFormula 重算出计算值展示；formula_resolved 标记供 apply 写回选定的公式文本
function resolveCell(cell, newVal, source = '') {
  if (newVal !== undefined) cell.value = newVal
  if (typeof newVal === 'string' && newVal.startsWith('=')) {
    cell.formula_text = newVal
    cell.diff_type = 'formula'
    cell.formula_resolved = true
  } else {
    cell.diff_type = ''
    cell.formula_resolved = false
  }
  cell.conflict = false; cell.changed = false; cell.resolved = true; cell.resolvedBy = source
}

// 调后端重算指定行所有公式列的结果（用户改输入列值后实时预览下游公式）
async function recomputeRowFormula(row) {
  if (!row.cells.some(c => c.diff_type === 'formula')) return
  try {
    const payload = { cells: row.cells.map(c => ({ col: c.col, value: c.value, formula_text: c.formula_text || '', diff_type: c.diff_type })) }
    const res = await fetch('/api/preview-row-formula', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (data.cells) for (const r of data.cells) {
      const fc = row.cells.find(c => c.col === r.col)
      if (fc) fc.value = r.value
    }
  } catch (e) { /* 重算失败不阻断，导出时由 libreoffice 兜底 */ }
}

// 判断某 sheet 是否仍有未解决冲突（仅冲突需人工处理，新增/删除/单向变更只高亮展示）
function sheetHasIssue(sheet) {
  return sheet.rows.some(row =>
    row.cells.some(c => c.conflict)
  )
}

// 统计某 sheet 当前实时的各类差异（冲突需处理，新增/删除/变更为纯展示）
function sheetLiveCounts(sheet) {
  let conflicts = 0, changed = 0, inserted = 0, deleted = 0, missing = 0
  sheet.rows.forEach(row => {
    if (row.row_type === 'inserted') inserted++
    else if (row.row_type === 'deleted') deleted++
    else if (row.row_type === 'missing_row') missing++
    // “修改”仅指 matched 行上的值→值变更，不包含新增/删除行
    const isMatched = row.row_type === 'matched'
    row.cells.forEach(c => {
      if (c.conflict) conflicts++
      else if (isMatched && c.changed) changed++
    })
  })
  return { conflicts, changed, inserted, deleted, missing }
}

// 快照：默认轻量（仅单元格可变状态）；deep=true 时深拷贝整行结构，
// 用于 smartMerge 这类会改变行结构（拆分/重映射）的操作，保证 undo 能完整还原。
function saveSnapshot(deep = false) {
  const snap = {}
  for (const [gk, group] of Object.entries(data.value.groups)) {
    snap[gk] = {}
    for (const [sk, sheet] of Object.entries(group.sheets)) {
      if (deep) {
        snap[gk][sk] = {
          fullRows: sheet.rows.map(row => ({
            key: row.key,
            row_type: row.row_type,
            acknowledged: row.acknowledged || false,
            id_remapped: row.id_remapped || false,
            original_pk: row.original_pk || '',
            cells: row.cells.map(c => ({
              col: c.col, col_letter: c.col_letter, value: c.value,
              versions: c.versions ? { ...c.versions } : {},
              conflict: c.conflict, changed: c.changed, diff_type: c.diff_type,
              formula_changed: c.formula_changed || false, formula_source: c.formula_source || '',
              formula_text: c.formula_text || '', formula_resolved: c.formula_resolved || false,
              resolved: c.resolved || false, resolvedBy: c.resolvedBy || '',
            })),
          })),
        }
      } else {
        snap[gk][sk] = {
          rows: sheet.rows.map(row => ({
            acknowledged: row.acknowledged || false,
            cells: row.cells.map(c => ({ value: c.value, conflict: c.conflict, changed: c.changed, diff_type: c.diff_type, formula_text: c.formula_text || '', formula_resolved: c.formula_resolved || false, resolved: c.resolved || false, resolvedBy: c.resolvedBy || '' })),
          })) 
        }
      }
    }
  }
  return snap
}

function restoreSnapshot(snap) {
  for (const [gk, group] of Object.entries(snap)) {
    for (const [sk, sheetSnap] of Object.entries(group)) {
      const sheet = data.value.groups[gk]?.sheets[sk]
      if (!sheet) continue
      if (sheetSnap.fullRows) {
        // 深快照：整行结构还原（含拆分/重映射后的行）
        sheet.rows = sheetSnap.fullRows.map(savedRow => ({
          key: savedRow.key,
          row_type: savedRow.row_type,
          acknowledged: savedRow.acknowledged,
          id_remapped: savedRow.id_remapped,
          original_pk: savedRow.original_pk,
          cells: savedRow.cells.map(c => ({
            col: c.col, col_letter: c.col_letter, value: c.value,
            versions: c.versions ? { ...c.versions } : {},
            conflict: c.conflict, changed: c.changed, diff_type: c.diff_type,
            resolved: c.resolved, resolvedBy: c.resolvedBy,
          })),
        }))
        continue
      }
      sheetSnap.rows.forEach((savedRow, ri) => {
        const row = sheet.rows[ri]
        if (!row) return
        row.acknowledged = savedRow.acknowledged
        savedRow.cells.forEach((c, ci) => {
          if (ci < row.cells.length) {
            row.cells[ci].value = c.value
            row.cells[ci].conflict = c.conflict
            row.cells[ci].changed = c.changed
            row.cells[ci].diff_type = c.diff_type
            row.cells[ci].resolved = c.resolved
            row.cells[ci].resolvedBy = c.resolvedBy || ''
          }
        })
      })
    }
  }
}

function pushSnapshot(deep = false) {
  history.value = history.value.slice(0, historyIdx.value + 1)
  history.value.push(saveSnapshot(deep))
  historyIdx.value = history.value.length - 1
}

function undo() {
  if (historyIdx.value <= 0) return
  historyIdx.value--
  restoreSnapshot(history.value[historyIdx.value])
}

function redo() {
  if (historyIdx.value >= history.value.length - 1) return
  historyIdx.value++
  restoreSnapshot(history.value[historyIdx.value])
}

function onKeyDown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) { e.preventDefault(); undo(); return }
  if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) { e.preventDefault(); redo(); return }
  const tag = e.target?.tagName
  const inInput = tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA'
  if (modalCell.value && !inInput) {
    if (e.key === 'Escape') { e.preventDefault(); closeModal() }
    else if (modalCell.value.cell.conflict) {
      if (e.key === ' ') { e.preventDefault(); advanceModal() }
      else if (/^[1-9]$/.test(e.key)) {
        const fns = Object.keys(modalCell.value.cell.versions)
        const idx = parseInt(e.key) - 1
        if (idx < fns.length) { e.preventDefault(); chooseVersionForCell(modalCell.value.ci, fns[idx]) }
      }
    }
  }
}

// keep-alive 下用 onActivated/onDeactivated 管理全局键盘监听，
// 避免切到其它标签页时仍劫持按键。
// 每次启动项目或刷新页面进入时保持空白会话，不恢复上次历史记录；
// 切换标签页再切回仍保留内存中的状态（keep-alive 原行为）。
onActivated(() => { window.addEventListener('keydown', onKeyDown) })
onDeactivated(() => { window.removeEventListener('keydown', onKeyDown) })

function onFileChange(e) { files.value = Array.from(e.target.files); error.value = '' }

async function doCompare() {
  if (files.value.length < 2) { error.value = '请至少选择2个文件'; return }
  loading.value = true; error.value = ''; exportMsg.value = ''
  stageMode.value = ''; stage1Done.value = false; stage2Done.value = false; stageMsg.value = ''
  // 每次比对从空白开始，不保留上次历史记录
  data.value = null; history.value = []; historyIdx.value = -1
  mergeResult.value = null; selectedRows.value = new Set(); conflictIdx.value = -1
  const fd = new FormData()
  files.value.forEach(f => fd.append('files', f))
  fd.append('base_files', JSON.stringify(baseMap.value))
  try {
    const res = await fetch('/api/compare', { method: 'POST', body: fd })
    if (!res.ok) { const t = await res.text(); throw new Error(t) }
    data.value = await res.json()
    adoptChangedValues()
    allDone.value = false; selectedRows.value = new Set()
    history.value = []; historyIdx.value = -1
    mergeResult.value = null
    const gk = Object.keys(data.value.groups)
    if (gk.length) {
      activeGroup.value = gk[0]
      const sk = Object.keys(data.value.groups[gk[0]].sheets)
      if (sk.length) activeSheet.value = sk[0]
    }
    conflictIdx.value = -1
    sessionId.value = data.value.session_id || ''
    saveSession()
  } catch (e) { error.value = e.message || '比对失败' } finally { loading.value = false }
}

// 从项目根 merge 文件夹加载并比对（输入与导出均在该文件夹下）
// 支持手动勾选要参与比对的文件，并为每个分组确认基准文件
const showFolderPicker = ref(false)
const folderFiles = ref([])
const folderSelected = ref(new Set())
const folderLoading = ref(false)
const folderError = ref('')
const folderBaseMap = ref({})  // {prefix: 基准文件名}
const trunkMode = ref(false)   // M3: 漏行检测模式，自动注入 trunk 基准 + missing_row P0 告警

// 将文件名按前缀分组（同名前缀的多版本归为一组）
const folderGroups = computed(() => {
  const map = {}
  for (const name of folderFiles.value) {
    const stem = name.replace(/\.xlsx$/i, '')
    const prefix = stem.replace(/_\d+$/, '')
    if (!map[prefix]) map[prefix] = []
    map[prefix].push(name)
  }
  return map
})
// 分组默认基准：无 _数字 后缀的那个；否则取第一个
function defaultBaseName(files) {
  if (!files || !files.length) return ''
  const base = files.find(f => !/_\d+/.test(f.replace(/\.xlsx$/i, '')))
  return base || files[0]
}

async function openFolderPicker() {
  showFolderPicker.value = true
  folderLoading.value = true; folderError.value = ''
  folderSelected.value = new Set()
  folderBaseMap.value = {}
  try {
    const res = await fetch('/api/merge-folder-files')
    if (!res.ok) { const t = await res.text(); throw new Error(t) }
    const obj = await res.json()
    folderFiles.value = obj.files || []
    folderFiles.value.forEach(f => folderSelected.value.add(f))
    const bm = {}
    for (const [p, fs] of Object.entries(folderGroups.value)) bm[p] = defaultBaseName(fs)
    folderBaseMap.value = bm
  } catch (e) { folderError.value = e.message || '加载文件列表失败' }
  finally { folderLoading.value = false }
}
function toggleFolderFile(name) {
  const next = new Set(folderSelected.value)
  if (next.has(name)) next.delete(name); else next.add(name)
  folderSelected.value = next
}
function toggleAllFolderFiles() {
  if (folderSelected.value.size === folderFiles.value.length) folderSelected.value = new Set()
  else folderSelected.value = new Set(folderFiles.value)
}
function closeFolderPicker() { showFolderPicker.value = false }

async function compareFromFolder() {
  // trunk 模式下 trunk 基准自动注入，只需选 1 个衍生文件；普通模式需 ≥2
  const minFiles = trunkMode.value ? 1 : 2
  if (folderSelected.value.size < minFiles) {
    folderError.value = trunkMode.value ? '漏行检测模式：请至少选择 1 个衍生文件' : '请至少选择2个文件'
    return
  }
  showFolderPicker.value = false
  loading.value = true; error.value = ''; exportMsg.value = ''
  stageMode.value = ''; stage1Done.value = false; stage2Done.value = false; stageMsg.value = ''
  // 每次比对从空白开始，不保留上次历史记录
  data.value = null; history.value = []; historyIdx.value = -1
  mergeResult.value = null; selectedRows.value = new Set(); conflictIdx.value = -1
  try {
    const url = trunkMode.value ? '/api/compare-folder?base=trunk' : '/api/compare-folder'
    const res = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ selected: [...folderSelected.value], base_files: { ...folderBaseMap.value } }),
    })
    if (!res.ok) { const t = await res.text(); throw new Error(t) }
    data.value = await res.json()
    adoptChangedValues()
    allDone.value = false; selectedRows.value = new Set()
    history.value = []; historyIdx.value = -1
    mergeResult.value = null
    const gk = Object.keys(data.value.groups)
    if (gk.length) {
      activeGroup.value = gk[0]
      const sk = Object.keys(data.value.groups[gk[0]].sheets)
      if (sk.length) activeSheet.value = sk[0]
    }
    conflictIdx.value = -1
    sessionId.value = data.value.session_id || ''
    saveSession()
  } catch (e) { error.value = e.message || '加载失败' } finally { loading.value = false }
}

const curGroup = computed(() => data.value?.groups[activeGroup.value])
const curSheet = computed(() => curGroup.value?.sheets[activeSheet.value])

// 当前分组下每个 sheet 的实时统计（用于 tab 红点、筛选，随单元格解决状态变化）
const sheetsLiveCounts = computed(() => {
  const map = {}
  if (curGroup.value) {
    for (const [sk, s] of Object.entries(curGroup.value.sheets)) map[sk] = sheetLiveCounts(s)
  }
  return map
})

// 找到当前分组下下一个仍有冲突的 Sheet（用于完成后跳转）
const nextConflictSheet = computed(() => {
  if (!curGroup.value) return null
  const entries = Object.entries(curGroup.value.sheets)
  let foundCurrent = false
  for (const [sk, s] of entries) {
    if (sk === activeSheet.value) { foundCurrent = true; continue }
    if (foundCurrent && sheetHasIssue(s)) return sk
  }
  // 从头循环
  for (const [sk, s] of entries) {
    if (sk === activeSheet.value) break
    if (sheetHasIssue(s)) return sk
  }
  return null
})

// sheet 是否存在增删改（修改/新增/删除，不含冲突）——用于下拉框过滤
function sheetHasDiff(sheet) {
  const c = sheetLiveCounts(sheet)
  return c.changed > 0 || c.inserted > 0 || c.deleted > 0
}

// sheet 下拉框过滤：固定规则——只列有未解决冲突（sheetHasIssue）或有增删改（sheetHasDiff）的 sheet。
// 行级 filterType（全部/冲突/修改/新增/删除）只筛当前 sheet 的行，不影响 sheet 下拉框。
// 空态兜底：当前分组所有 sheet 均无冲突/增删改时，回退全量 sheets 供用户选择查看。
const filteredSheets = computed(() => {
  if (!curGroup.value) return {}
  const sheets = curGroup.value.sheets
  const r = {}
  for (const [k, s] of Object.entries(sheets)) {
    if (sheetHasIssue(s) || sheetHasDiff(s)) r[k] = s
  }
  return Object.keys(r).length ? r : sheets
})

// 下拉框是否处于空态兜底（所有 sheet 均无冲突/增删改 → 回退全量，无前缀）
const sheetSelectEmpty = computed(() => {
  if (!curGroup.value) return true
  for (const s of Object.values(curGroup.value.sheets)) {
    if (sheetHasIssue(s) || sheetHasDiff(s)) return false
  }
  return true
})

// 下拉框选项：[冲突] sheetHasIssue 为真；[增删改] 否则若 sheetHasDiff 为真；空态兜底无前缀。
// 始终保证当前 activeSheet 在选项中（解决完冲突后该 sheet 可能被过滤掉，避免下拉框无选中项）。
const sheetSelectOptions = computed(() => {
  const opts = Object.entries(filteredSheets.value).map(([k, s]) => {
    const prefix = sheetSelectEmpty.value ? '' : (sheetHasIssue(s) ? '[冲突] ' : '[增删改] ')
    return { key: k, label: prefix + k }
  })
  if (activeSheet.value && !opts.some(o => o.key === activeSheet.value)) {
    opts.unshift({ key: activeSheet.value, label: '· ' + activeSheet.value })
  }
  return opts
})

// 冲突列表：仅真冲突需人工选择（新增/删除/单向变更不进列表，只高亮展示）
const diffList = computed(() => {
  if (!curSheet.value) return []
  const list = []
  const headers = curSheet.value.headers
  curSheet.value.rows.forEach((row, ri) => {
    row.cells.forEach((cell, ci) => {
      if (cell.conflict) list.push({ ri, ci, cell, key: row.key, header: headers[ci] || '' })
    })
  })
  return list
})

// 变更列表（非冲突的单向修改，可点击弹窗查看但无需选择版本）
const diffListOfChanged = computed(() => {
  if (!curSheet.value) return []
  const list = []
  const headers = curSheet.value.headers
  curSheet.value.rows.forEach((row, ri) => {
    row.cells.forEach((cell, ci) => {
      if (cell.changed && !cell.conflict) list.push({ ri, ci, cell, key: row.key, header: headers[ci] || '' })
    })
  })
  return list
})

// 当前 sheet 待确认的新增行（仅高亮展示，无需手动确认）
const pendingInsertRows = computed(() => {
  if (!curSheet.value) return []
  return curSheet.value.rows.filter(r => r.row_type === 'inserted')
})

// 已解决记录计数（接受版本后无颜色标识，但保留 resolved 记录用于撤销）
const resolvedCount = computed(() => {
  if (!curSheet.value) return 0
  let n = 0
  curSheet.value.rows.forEach(row => row.cells.forEach(c => { if (c.resolved) n++ }))
  return n
})

watch(diffList, (list) => {
  if (!data.value) return
  // 只有冲突需要处理；新增/删除/单向变更默认采用，不阻塞
  allDone.value = (list.length === 0)
  if (list.length === 0) modalCell.value = null
  saveSession()
})

function toggleRow(ri) { const next = new Set(selectedRows.value); if (next.has(ri)) next.delete(ri); else next.add(ri); selectedRows.value = next }
function toggleAllRows() {
  if (!curSheet.value) return
  if (selectedRows.value.size === curSheet.value.rows.length) { selectedRows.value = new Set() }
  else { selectedRows.value = new Set(curSheet.value.rows.map((_, i) => i)) }
}
function clearSelection() { selectedRows.value = new Set() }

function switchSheet(sk) {
  activeSheet.value = sk; conflictIdx.value = -1; selectedRows.value = new Set()
  modalCell.value = null; previewVersion.value = null; previewData.value = null; rowPreviewRis.value = []
  renderLimit.value = RENDER_BATCH  // 切换 sheet 重置渐进加载上限
  nextTick(() => {
    const s = curSheet.value; if (!s) return
    // 定位第一个冲突行（仅冲突需处理，新增/删除/变更只高亮不跳转）
    let firstRi = -1
    s.rows.forEach((row, ri) => {
      if (firstRi === -1 && row.cells.some(c => c.conflict)) firstRi = ri
    })
    if (firstRi >= 0) {
      conflictIdx.value = 0
      const el = document.getElementById('diff-row-' + firstRi)
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  })
}

// sheet 下拉框切换：复用 switchSheet 的全部副作用（重置冲突定位/选择/弹窗/滚动）
function onSheetSelectChange(e) {
  const sk = e.target.value
  if (sk) switchSheet(sk)
}

function switchGroup(gk) {
  activeGroup.value = gk; allDone.value = false
  selectedRows.value = new Set()
  // 落点优先选第一个有冲突/增删改的 sheet（filteredSheets 已含空态兜底）
  const sk = Object.keys(filteredSheets.value)
  if (sk.length) switchSheet(sk[0])
}

function goConflict(delta) {
  const list = diffList.value; if (!list.length) return
  let idx = conflictIdx.value + delta
  if (idx < 0) idx = list.length - 1
  if (idx >= list.length) idx = 0
  conflictIdx.value = idx; modalCell.value = list[idx]
  nextTick(() => {
    const el = document.getElementById('diff-row-' + list[idx].ri)
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  })
}

function cellClick(cell, ri, ci) {
  // 只有冲突需人工选择版本；新增/修改/删除默认采纳，仅颜色标识，不弹窗
  if (!cell.conflict) return
  aiSuggestion.value = null; aiSuggestError.value = ''
  const row = curSheet.value.rows[ri]
  modalCell.value = { cell, key: row.key, header: curSheet.value.headers[ci] || '', ri, ci }
  const idx = diffList.value.findIndex(d => d.ri === ri && d.ci === ci)
  if (idx >= 0) conflictIdx.value = idx
}

function reopenConflict(ri, ci) {
  if (!curSheet.value) return
  const cell = curSheet.value.rows[ri].cells[ci]
  if (!cell.resolved) return
  pushSnapshot()
  aiSuggestion.value = null; aiSuggestError.value = ''
  cell.resolved = false
  cell.conflict = true
  cell.changed = false
  cell.resolvedBy = ''
  // 公式冲突误触撤销：恢复为待选择的 formula_conflict 态（versions 仍存各版本公式文本，
  // 重新点开弹窗可再次对比选择）。恢复 formula_text/value 为基准公式，清 formula_resolved
  // 避免 apply 误写上次选定公式。
  if (cell.formula_resolved) {
    cell.diff_type = 'formula_conflict'
    cell.formula_resolved = false
    const baseFile = curGroup.value?.base_file || ''
    const baseFormula = cell.versions?.[baseFile]
    if (typeof baseFormula === 'string' && baseFormula.startsWith('=')) {
      cell.formula_text = baseFormula
      cell.value = baseFormula
    }
  } else {
    cell.diff_type = ''
  }
  const row = curSheet.value.rows[ri]
  modalCell.value = { cell, key: row.key, header: curSheet.value.headers[ci] || '', ri, ci }
  const idx = diffList.value.findIndex(d => d.ri === ri && d.ci === ci)
  if (idx >= 0) conflictIdx.value = idx
}

// 单元格弹窗：点击某版本列的单元格值 → 仅当前单元格采用该版本
function chooseVersionForCell(ci, fname) {
  if (!modalCell.value || !modalRow.value) return
  const cell = modalRow.value.cells[ci]
  if (!cell) return
  pushSnapshot()
  resolveCell(cell, cell.versions[fname] ?? cell.value, fname)
  recomputeRowFormula(modalRow.value)
  advanceModal()
}
// 单元格弹窗：点击列首「整行用此版本」→ 当前行所有冲突列都采用该版本
function chooseVersionForRow(fname) {
  if (!modalRow.value) return
  pushSnapshot()
  modalRow.value.cells.forEach(cell => {
    if (cell.conflict) resolveCell(cell, cell.versions[fname] ?? cell.value, fname)
  })
  recomputeRowFormula(modalRow.value)
  advanceModal()
}
function closeModal() { modalCell.value = null; aiSuggestion.value = null; aiSuggestError.value = '' }

// M13: 请求 AI 冲突建议（基于列类型/值特征的规则建议）
async function requestAiSuggest() {
  if (!modalCell.value || !curSheet.value || !curGroup.value) return
  const mc = modalCell.value
  const cell = mc.cell
  const baseFile = curGroup.value.base_file || ''
  aiSuggestBusy.value = true
  aiSuggestError.value = ''
  aiSuggestion.value = null
  try {
    const res = await fetch('/api/agent/suggest-merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        table_stem: activeGroup.value,
        sheet: activeSheet.value,
        col_name: mc.header,
        row_key: mc.key,
        base_value: cell.versions?.[baseFile] ?? cell.value,
        versions: cell.versions || {},
      })
    })
    if (!res.ok) { aiSuggestError.value = '建议请求失败：' + await res.text(); return }
    aiSuggestion.value = await res.json()
  } catch (e) {
    aiSuggestError.value = '建议请求失败：' + (e.message || e)
  } finally {
    aiSuggestBusy.value = false
  }
}
// M13: 一键采纳 AI 建议 → 用 suggested_version 的值解决当前单元格
function adoptAiSuggestion() {
  if (!aiSuggestion.value || !aiSuggestion.value.suggested_version) return
  if (!modalCell.value) return
  const ver = aiSuggestion.value.suggested_version
  chooseVersionForCell(modalCell.value.ci, ver)
  aiSuggestion.value = null
}
function advanceModal() {
  // 切换到下一个冲突，清空上一条的 AI 建议状态
  aiSuggestion.value = null; aiSuggestError.value = ''
  // 仅冲突需人工处理，解决后跳到下一个冲突（修改/新增/删除默认采纳，不进流程）
  const list = diffList.value
  if (!list.length) { modalCell.value = null; return }
  const curIdx = list.findIndex(d => d.ri === modalCell.value.ri && d.ci === modalCell.value.ci)
  const nextIdx = curIdx === -1 ? 0 : (curIdx + 1) % list.length
  conflictIdx.value = nextIdx; modalCell.value = list[nextIdx]
  // 滚动到对应行
  nextTick(() => {
    const el = document.getElementById('diff-row-' + list[nextIdx].ri)
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  })
}

// ── 全局导出：仅冲突需处理完毕才可导出（新增/删除/变更默认采用） ──
const globalUnresolved = computed(() => {
  if (!data.value) return { conflicts: 0, changed: 0, inserted: 0, deleted: 0, missing: 0 }
  let conflicts = 0, changed = 0, inserted = 0, deleted = 0, missing = 0
  for (const group of Object.values(data.value.groups)) {
    for (const sheet of Object.values(group.sheets)) {
      sheet.rows.forEach(row => {
        if (row.row_type === 'inserted') inserted++
        else if (row.row_type === 'deleted') deleted++
        else if (row.row_type === 'missing_row') missing++
        const isMatched = row.row_type === 'matched'
        row.cells.forEach(c => { if (c.conflict) conflicts++; else if (isMatched && c.changed) changed++ })
      })
    }
  }
  return { conflicts, changed, inserted, deleted, missing }
})
// 冲突清零 且 无未补回漏行 才允许导出（漏行阻断见 M3 spec）
const allResolved = computed(() => globalUnresolved.value.conflicts === 0 && globalUnresolved.value.missing === 0)

const exportMsg = ref('')  // 导出成功后的落盘路径反馈

async function exportAllGroups() {
  if (!data.value || !allResolved.value) return
  error.value = ''; exportMsg.value = ''
  const exported = []
  for (const [gk, group] of Object.entries(data.value.groups)) {
    try {
      const sheet_list = Object.values(group.sheets).map(s => ({ name: s.name, headers: s.headers, rows: s.rows }))
      const res = await fetch('/api/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_name: gk, sheets: sheet_list, session_id: sessionId.value, base_file: group.base_file || '' }),
      })
      if (!res.ok) throw new Error(await res.text())
      // 从响应头取实际导出文件名（带编号）与落盘路径
      const disp = res.headers.get('Content-Disposition') || ''
      let fname = `merged_${gk}.xlsx`
      const m = disp.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)
      if (m) fname = decodeURIComponent(m[1])
      const exportPath = res.headers.get('X-Export-Path') || ''
      const exportName = res.headers.get('X-Export-Name') || fname
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = fname; a.click()
      URL.revokeObjectURL(url)
      exported.push({ group: gk, name: exportName, path: exportPath })
      await new Promise(r => setTimeout(r, 300))
    } catch (e) {
      error.value = `分组 ${gk} 导出失败：${e.message || e}`
    }
  }
  if (exported.length) {
    exportMsg.value = exported.map(e => e.path ? `✓ ${e.group}：${e.path}` : `✓ ${e.group}：${e.name}`).join('\n')
  }
}

// ── M3: 从 trunk 补回漏行 ──
// 调 restore-row 接口取 trunk 基准该行数据，本地把 row_type 从 missing_row 改为 matched。
// 补回后导出按 matched 处理（基准克隆已含此行，cells 已带 trunk 值，无需额外写盘）。
const restoringPk = ref('')
async function restoreRow(row) {
  if (!row || row.row_type !== 'missing_row') return
  const group = curGroup.value
  const sheetName = activeSheet.value
  if (!group || !sheetName) return
  restoringPk.value = row.key
  try {
    const res = await fetch(`/api/diff/merge-session/${sessionId.value}/restore-row`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        group_name: group.group_name,
        base_file: group.base_file || '',
        sheet: sheetName,
        pk: row.key,
      }),
    })
    if (!res.ok) {
      const txt = await res.text()
      error.value = `补回失败（pk=${row.key}）：${txt}`
      return
    }
    const body = await res.json()
    // 用 trunk 值刷新该行 cells 的 versions + value，确保与基准一致
    if (Array.isArray(body.cells)) {
      row.cells.forEach(c => {
        const fresh = body.cells.find(fc => fc.col === c.col)
        if (fresh) {
          c.value = fresh.value
          c.versions = fresh.versions || { [group.base_file]: fresh.value }
        }
      })
    }
    row.row_type = 'matched'
    row.restored = true
  } catch (e) {
    error.value = `补回失败（pk=${row.key}）：${e.message || e}`
  } finally {
    restoringPk.value = ''
  }
}

// ── 整表改用版本 X（带预览，类 Git 操作，作用范围：当前 Sheet 全部冲突/变更） ──
const previewVersion = ref(null)   // 待预览/接纳的版本名
const previewData = ref(null)      // 预览数据：该版本在所有冲突/变更单元格的值
const previewFilter = ref('all')   // all / conflict / changed

function showAcceptAllPreview(fname) {
  if (!curSheet.value) return
  const base = curGroup.value?.base_file
  const headers = curSheet.value.headers
  const items = []
  curSheet.value.rows.forEach((row, ri) => {
    row.cells.forEach((cell, ci) => {
      if (cell.conflict) {
        items.push({
          ri, ci, key: row.key,
          header: headers[ci] || '', col_letter: cell.col_letter,
          base: base ? cell.versions[base] : null,
          target: cell.versions[fname],
          conflict: cell.conflict,
          changed: cell.changed,
        })
      }
    })
  })
  if (!items.length) { error.value = '当前 Sheet 没有冲突可接纳'; return }
  previewVersion.value = fname
  previewData.value = items
  previewFilter.value = 'all'
  error.value = ''
}

const previewFiltered = computed(() => {
  if (!previewData.value) return []
  if (previewFilter.value === 'conflict') return previewData.value.filter(i => i.conflict)
  if (previewFilter.value === 'changed') return previewData.value.filter(i => i.changed)
  return previewData.value
})

const previewStats = computed(() => {
  if (!previewData.value) return { all: 0, conflict: 0, changed: 0 }
  const c = previewData.value.filter(i => i.conflict).length
  return { all: previewData.value.length, conflict: c, changed: previewData.value.length - c }
})

function confirmAcceptAll() {
  if (!previewVersion.value || !curSheet.value) return
  pushSnapshot()
  const fname = previewVersion.value
  const touchedRows = []
  curSheet.value.rows.forEach(row => {
    let touched = false
    row.cells.forEach(cell => {
      if (cell.conflict) { resolveCell(cell, cell.versions[fname] ?? cell.value, fname); touched = true }
    })
    if (touched) touchedRows.push(row)
  })
  // 批量改输入值后重算每行公式列预览值
  touchedRows.forEach(row => recomputeRowFormula(row))
  previewVersion.value = null
  previewData.value = null
}

function cancelAcceptAll() {
  previewVersion.value = null
  previewData.value = null
}

// ── 行版本预览：选中1或多行，先看各版本在差异列的具体内容，再选版本整行应用 ──
const rowPreviewRis = ref([])   // 正在预览的行号数组，空 = 关闭

function openRowPreview(ris) {
  if (!curSheet.value || !ris.length) return
  rowPreviewRis.value = ris.slice()
}
function closeRowPreview() { rowPreviewRis.value = [] }

// 返回某行中存在冲突的单元格列表 [{c, ci}]（仅冲突需选择，单向变更默认采用）
function diffCellsOf(row) {
  if (!row) return []
  return row.cells.map((c, ci) => ({ c, ci })).filter(x => x.c.conflict)
}

function applyRowPreviewVersion(fname) {
  if (!curSheet.value || !rowPreviewRis.value.length) return
  pushSnapshot()
  for (const ri of rowPreviewRis.value) {
    const row = curSheet.value.rows[ri]
    if (!row) continue
    row.cells.forEach(cell => resolveCell(cell, cell.versions[fname] ?? cell.value, fname))
    recomputeRowFormula(row)
  }
  closeRowPreview()
  clearSelection()
}

// 行预览弹窗：点击某行某版本单元格值 → 仅该行该单元格采用该版本（不关闭弹窗，可继续选）
function chooseRowVersionForCell(ri, ci, fname) {
  if (!curSheet.value) return
  const row = curSheet.value.rows[ri]
  const cell = row?.cells[ci]
  if (!cell) return
  pushSnapshot()
  resolveCell(cell, cell.versions[fname] ?? cell.value, fname)
  recomputeRowFormula(row)
}
// 行预览弹窗：点击某行列首「整行用此版本」→ 该行所有冲突列采用该版本
function chooseRowVersionForRow(ri, fname) {
  if (!curSheet.value) return
  const row = curSheet.value.rows[ri]
  if (!row) return
  pushSnapshot()
  row.cells.forEach(cell => {
    if (cell.conflict) resolveCell(cell, cell.versions[fname] ?? cell.value, fname)
  })
  recomputeRowFormula(row)
}

const mergeResult = ref(null)  // {auto, manual}（保留占位，供各处重置引用）

function colLetter(n) { let s = ''; n += 1; while (n > 0) { n--; s = String.fromCharCode(65 + (n % 26)) + s; n = Math.floor(n / 26) } return s }
function truncated(val, max) { const s = val == null ? '' : String(val); return s.length > max ? s.slice(0, max) + '…' : s }

// R16: 搜索词在单元格内片段高亮（先截断 → 用 ⟦⟧ 占位标记命中位置 → escapeHtml 转义 → 替换为 <mark>，防 XSS）
function highlightSearchHit(val, max, key) {
  let s = val == null ? '' : String(val)
  if (s.length > max) s = s.slice(0, max) + '…'
  const kw = (key || '').trim()
  if (!kw) return escapeHtml(s)
  const k = kw.toLowerCase()
  const lower = s.toLowerCase()
  let marked = '', i = 0
  while (i < s.length) {
    const idx = lower.indexOf(k, i)
    if (idx === -1) { marked += s.slice(i); break }
    marked += s.slice(i, idx) + '⟦' + s.slice(idx, idx + kw.length) + '⟧'
    i = idx + kw.length
  }
  return escapeHtml(marked).replace(/⟦/g, '<mark class="search-hit">').replace(/⟧/g, '</mark>')
}

// ── 版本标签：从文件名提取短标签（ability_1.xlsx → v1，基准 → 基准名）──
function versionLabel(fname) {
  if (!fname) return ''
  const meta = curGroup.value?.version_meta?.[fname]
  if (meta && meta.rev) return 'r' + meta.rev
  const stem = fname.replace(/\.xlsx$/i, '')
  const m = stem.match(/_(\d+)$/)
  return m ? 'v' + m[1] : stem
}
function versionMeta(fname) {
  return curGroup.value?.version_meta?.[fname] || null
}
function fmtRevDate(s) {
  if (!s) return ''
  return s.replace('T', ' ').replace(/\.\d+Z$/, '').replace('Z', '')
}
// 已解决冲突的来源标签（取版本标签）
function resolvedLabel(cell) {
  const s = cell.resolvedBy || ''
  if (!s) return '已解决'
  return versionLabel(s)
}
function str(v) { return v == null ? '' : String(v) }
// diff 明细：判空 + 改动类型中文徽章
function hasVal(v) { return v !== '' && v !== null && v !== undefined }
function kindText(k) { return { insert: '新增', delete: '删除', change: '修改', resolved: '解冲突' }[k] || '变更' }

// 改动型单元格（仅单个衍生版本改动，自动采纳）：把 cell.value 置为该衍生版本的值，
// 基准旧值仍可由 versions[base_file] 取得，用于“旧→新”对比与导出采纳。
function adoptChangedValues() {
  if (!data.value) return
  for (const group of Object.values(data.value.groups)) {
    const base = group.base_file
    for (const sheet of Object.values(group.sheets)) {
      sheet.rows.forEach(row => {
        row.cells.forEach(cell => {
          if (cell.changed && !cell.conflict && !cell.resolved) {
            const baseStr = str(cell.versions?.[base])
            let adopted = cell.versions?.[base]
            for (const [fn, v] of Object.entries(cell.versions || {})) {
              if (fn === base) continue
              if (str(v) !== baseStr) { adopted = v; break }
            }
            cell.value = adopted
          }
        })
      })
    }
  }
}

// 改动型单元格的来源版本名（与基准不同的那个衍生版本）
function changedVersion(cell) {
  const base = curGroup.value?.base_file
  if (!base || !cell.versions) return ''
  const baseStr = str(cell.versions[base])
  for (const [fn, v] of Object.entries(cell.versions)) {
    if (fn === base) continue
    if (str(v) !== baseStr) return fn
  }
  return ''
}
function baseValOf(cell) {
  const base = curGroup.value?.base_file
  return cell.versions?.[base]
}

// 通用字符级 diff 渲染（val 相对 baseVal），供单元格弹窗横向表格与行预览复用
function renderDiffFor(val, baseVal) {
  const valStr = val == null ? '' : String(val)
  const baseStr = baseVal == null ? '' : String(baseVal)
  if (valStr === baseStr) return escapeHtml(valStr)
  if (valStr.length > 200 || baseStr.length > 200) {
    return `<span class="diff-del">${escapeHtml(baseStr)}</span> → <span class="diff-add">${escapeHtml(valStr)}</span>`
  }
  return lcsDiff(baseStr, valStr).map(p => {
    if (p.type === 'eq') return escapeHtml(p.text)
    if (p.type === 'del') return `<span class="diff-del">${escapeHtml(p.text)}</span>`
    return `<span class="diff-add">${escapeHtml(p.text)}</span>`
  }).join('')
}

// ── 单元格冲突弹窗：横向表格（各版本为列，行为该行各列），用于观察并选择版本 ──
const modalRow = computed(() => modalCell.value && curSheet.value ? curSheet.value.rows[modalCell.value.ri] : null)
// 仅展示该行中存在冲突的列（当前处理的单元格一定在其中），减少无关列干扰
const modalConflictCells = computed(() => {
  if (!modalRow.value) return []
  return modalRow.value.cells.map((c, ci) => ({ c, ci })).filter(x => x.c.conflict)
})
function vmVal(ci, fn) { return modalRow.value?.cells?.[ci]?.versions?.[fn] }
function vmIsDiff(ci, fn) {
  const base = curGroup.value?.base_file
  return str(vmVal(ci, fn)) !== str(vmVal(ci, base))
}
function vmCellHtml(ci, fn) {
  const base = curGroup.value?.base_file
  if (fn === base) return escapeHtml(vmVal(ci, fn))
  return renderDiffFor(vmVal(ci, fn), vmVal(ci, base))
}

// ── 单元格 diff 可视化：基准值 vs 该版本值，文本列字符级 diff，数值列显示变化量 ──
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
function lcsDiff(a, b) {
  const n = a.length, m = b.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const parts = []
  let i = 0, j = 0
  const push = (type, ch) => { const last = parts[parts.length - 1]; if (last && last.type === type) last.text += ch; else parts.push({ type, text: ch }) }
  while (i < n && j < m) {
    if (a[i] === b[j]) { push('eq', a[i]); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push('del', a[i]); i++ }
    else { push('add', b[j]); j++ }
  }
  while (i < n) { push('del', a[i]); i++ }
  while (j < m) { push('add', b[j]); j++ }
  return parts
}

// ── 冲突列表面板 / 审计面板 ──
const showConflictPanel = ref(false)
const showAuditPanel = ref(false)
function scrollToRow(ri) {
  const el = document.getElementById('diff-row-' + ri)
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
}
const auditStats = computed(() => {
  if (!curSheet.value) return []
  const counts = {}
  curSheet.value.rows.forEach(row => row.cells.forEach(c => {
    if (c.resolved && c.resolvedBy) counts[c.resolvedBy] = (counts[c.resolvedBy] || 0) + 1
  }))
  return Object.entries(counts).map(([source, count]) => ({ source, count })).sort((a, b) => b.count - a.count)
})

// 已解决冲突明细列表（供“跳转回该位置 + 撤销”使用）
const resolvedList = computed(() => {
  if (!curSheet.value) return []
  const list = []
  const headers = curSheet.value.headers
  curSheet.value.rows.forEach((row, ri) => {
    row.cells.forEach((c, ci) => {
      if (c.resolved) list.push({ ri, ci, key: row.key, header: headers[ci] || colLetter(ci), source: c.resolvedBy || '' })
    })
  })
  return list
})
function jumpToResolved(ri, ci) {
  scrollToRow(ri)
  nextTick(() => reopenConflict(ri, ci))
}

// ── 会话持久化（localStorage 防刷新丢失） + 后端导出 session_id ──
const STORAGE_KEY = 'diffmerge_session'
const sessionId = ref('')
let saveTimer = null
function saveSession() {
  if (!data.value) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        data: data.value, activeGroup: activeGroup.value, activeSheet: activeSheet.value, sessionId: sessionId.value,
      }))
    } catch (e) { /* 超限静默失败 */ }
  }, 800)
}
function loadSession() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return
    const obj = JSON.parse(raw)
    if (obj.data && obj.data.groups) {
      data.value = obj.data
      adoptChangedValues()
      activeGroup.value = obj.activeGroup || Object.keys(obj.data.groups)[0] || ''
      const sheets = Object.keys(obj.data.groups[activeGroup.value]?.sheets || {})
      activeSheet.value = sheets.includes(obj.activeSheet) ? obj.activeSheet : (sheets[0] || '')
      sessionId.value = obj.sessionId || ''
      history.value = []; historyIdx.value = -1
    }
  } catch (e) {}
}
function clearSession() {
  localStorage.removeItem(STORAGE_KEY)
  data.value = null; sessionId.value = ''; history.value = []; historyIdx.value = -1
  files.value = []; error.value = ''
}

// ── 大表性能：紧凑模式只渲染有差异/新增/删除的行 ──
const compactMode = ref(false)
// 大表渐进加载：避免 10w 行一次性渲染 DOM 卡死（big_data 切换卡顿根因）。
// 首屏只渲染 RENDER_BATCH 条，滚动到底自动加载下一批；切换 sheet/筛选/搜索时重置。
const RENDER_BATCH = 200
const renderLimit = ref(RENDER_BATCH)
const filterType = ref('all')  // 'all' | 'conflict' | 'changed' | 'inserted' | 'deleted' — 仅筛当前 sheet 的行，不影响 sheet 标签
// T9: 行内容搜索关键词（本地过滤当前 sheet 所有行）
const rowSearchKey = ref('')
function rowHasDiff(row) {
  if (row.row_type === 'inserted' || row.row_type === 'deleted' || row.row_type === 'missing_row') return true
  return row.cells.some(c => c.conflict || c.changed)
}
// T9: 行是否匹配搜索关键词（子串匹配 key 或任意 cell value，大小写不敏感）
function rowMatchesSearch(row, kw) {
  if (!kw) return true
  const k = kw.toLowerCase()
  if (row.key && String(row.key).toLowerCase().includes(k)) return true
  return row.cells.some(c => c.value != null && String(c.value).toLowerCase().includes(k))
}
const visibleRows = computed(() => {
  if (!curSheet.value) return []
  const rows = curSheet.value.rows
  let all = rows.map((r, i) => ({ row: r, ri: i }))
  // 单选筛选
  if (filterType.value === 'conflict') {
    all = all.filter(({ row }) => row.cells.some(c => c.conflict))
  } else if (filterType.value === 'changed') {
    all = all.filter(({ row }) => row.row_type === 'matched' && row.cells.some(c => c.changed && !c.conflict))
  } else if (filterType.value === 'inserted') {
    all = all.filter(({ row }) => row.row_type === 'inserted')
  } else if (filterType.value === 'deleted') {
    all = all.filter(({ row }) => row.row_type === 'deleted')
  } else if (filterType.value === 'missing') {
    all = all.filter(({ row }) => row.row_type === 'missing_row')
  }
  // T9: 行内容搜索叠加（与 filterType 同时生效）
  const kw = rowSearchKey.value.trim()
  if (kw) {
    all = all.filter(({ row }) => rowMatchesSearch(row, kw))
  }
  if (!compactMode.value || rows.length <= 500) {
    // 大表渐进加载：超过上限只渲染前 renderLimit 条，滚动到底自动加载下一批
    return all.length > renderLimit.value ? all.slice(0, renderLimit.value) : all
  }
  if (kw) return all  // 搜索时不压缩，避免搜索结果被紧凑模式过滤掉
  const compacted = all.filter(({ row }) => rowHasDiff(row))
  return compacted.length > renderLimit.value ? compacted.slice(0, renderLimit.value) : compacted
})
// 渐进加载：筛选/搜索变化时重置渲染上限
watch([filterType, rowSearchKey], () => { renderLimit.value = RENDER_BATCH })
// 滚动到底加载下一批
function onTableScroll(e) {
  const el = e.target
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) {
    renderLimit.value += RENDER_BATCH
  }
}
// 渐进加载提示：未截断前的总行数（visibleRows 已被 renderLimit 截断，需独立计算总数）
const filteredRowCount = computed(() => {
  if (!curSheet.value) return 0
  const rows = curSheet.value.rows
  let all = rows.map((r, i) => ({ row: r, ri: i }))
  if (filterType.value === 'conflict') {
    all = all.filter(({ row }) => row.cells.some(c => c.conflict))
  } else if (filterType.value === 'changed') {
    all = all.filter(({ row }) => row.row_type === 'matched' && row.cells.some(c => c.changed && !c.conflict))
  } else if (filterType.value === 'inserted') {
    all = all.filter(({ row }) => row.row_type === 'inserted')
  } else if (filterType.value === 'deleted') {
    all = all.filter(({ row }) => row.row_type === 'deleted')
  } else if (filterType.value === 'missing') {
    all = all.filter(({ row }) => row.row_type === 'missing_row')
  }
  const kw = rowSearchKey.value.trim()
  if (kw) all = all.filter(({ row }) => rowMatchesSearch(row, kw))
  if (!compactMode.value || rows.length <= 500) return all.length
  if (kw) return all.length
  return all.filter(({ row }) => rowHasDiff(row)).length
})
// T9: 搜索命中行数提示
const rowSearchStats = computed(() => {
  if (!curSheet.value) return { hits: 0, total: 0 }
  const total = curSheet.value.rows.length
  const kw = rowSearchKey.value.trim()
  if (!kw) return { hits: total, total }
  const hits = curSheet.value.rows.filter(r => rowMatchesSearch(r, kw)).length
  return { hits, total }
})

// ── 三阶段合并：阶段1 合并多次提交 / 阶段2 跨生产者综合 / 阶段3 合回 trunk ──
const stageMode = ref('')            // '' | 'stage1' | 'stage2' | 'stage3'
const showStagePicker = ref(false)
const stageBranches = ref([])        // [{branch, groups:[]}]
const stageBranch = ref('')
const stageGroup = ref('')
const stageMsg = ref('')
const stageBusy = ref(false)
const stage1Done = ref(false)        // 当前 branch+group 阶段1 是否已产出中间版本
const stage2Done = ref(false)        // 当前 group 阶段2 是否已产出综合版本
const showApplyConfirm = ref(false)  // 合回 trunk 前的 --stat 摘要确认框
const stageStatus = ref(null)        // /stage1/status 返回：提交/中间版本/新提交
const stageIncremental = ref(false)  // 阶段1 增量模式（base=已产出中间版本，仅合新提交）
const stageBaseFile = ref('')        // 阶段1 全量模式的基准提交文件名
const stageDerived = ref(new Set())  // 阶段1 全量模式勾选参与合并的衍生文件
const showBatchPanel = ref(false)    // 批量合回面板
const batchItems = ref([])           // /stage3/pending 返回的待合回清单
const batchSel = ref(new Set())      // 勾选批量合回的分组 key（group_name）
const batchBusy = ref(false)
const batchResult = ref('')          // 批量合回结果提示

function batchKey(it) { return it.group_name }
const batchReady = computed(() => batchItems.value.filter(i => i.ready && i.trunk_exists))

// ── 三阶段引导向导：选表分组 → 阶段1 各生产者合并 → 阶段2 跨生产者综合 → 阶段3 合回 trunk → 汇总 ──
const wizActive = ref(false)          // 向导是否激活
const wizStep = ref('branch')         // 'branch' | 'stage1' | 'stage2' | 'stage3' | 'done'
const wizBranches = ref([])           // [{branch, groups:[]}]
const wizBranch = ref('')             // 选中的生产者分支（阶段1 维度）
const wizGroups = ref([])             // 该分支各分组的向导项（阶段1 维度）
const wizCur = ref('')                // 当前正在操作的分组名
const wizBusy = ref(false)
const wizMsg = ref('')
const wizShowSummary = ref(false)     // 最终汇总确认弹窗
const wizApplyResults = ref([])       // 合回结果 [{name,ok,output,reason}]
const wizSummaryDiffCap = 300

// 阶段2 综合分组清单（跨生产者维度，按 group_name 去重）
const wizConsGroups = ref([])         // [{name, producers:[], stage2_ok, sheetsSnapshot, stat, diffRows, diffTotal, stage3_done}]
// 当前正在综合的分组名（阶段2 用，独立于 wizCur）
const wizConsCur = ref('')

// 参与合并（非跳过）的分组
const wizMergeGroups = computed(() => wizGroups.value.filter(g => g.decision === 'merge'))
// 阶段1 全部完成：所有 merge 分组都产出中间版本
const wizStage1AllDone = computed(() => wizMergeGroups.value.length > 0 && wizMergeGroups.value.every(g => g.stage1_ok))
// 阶段2 全部完成：所有综合分组都产出综合版本
const wizStage2AllDone = computed(() => wizConsGroups.value.length > 0 && wizConsGroups.value.every(g => g.stage2_ok))
// 阶段3 全部完成：所有综合分组都已加入合回队列
const wizStage3AllDone = computed(() => wizConsGroups.value.length > 0 && wizConsGroups.value.every(g => g.stage3_done))
const wizCurGroup = computed(() => wizGroups.value.find(g => g.name === wizCur.value) || null)
const wizConsCurGroup = computed(() => wizConsGroups.value.find(g => g.name === wizConsCur.value) || null)

// 汇总所有综合分组（阶段3 将合回）的改动统计
const wizSummaryStat = computed(() => {
  const st = { changed: 0, inserted: 0, deleted: 0, resolved: 0 }
  wizConsGroups.value.forEach(g => {
    if (g.stat) { st.changed += g.stat.changed; st.inserted += g.stat.inserted; st.deleted += g.stat.deleted; st.resolved += g.stat.resolved }
  })
  return st
})
// 汇总所有综合分组的改动明细（capped）
const wizSummaryDiff = computed(() => {
  const rows = []
  let total = 0
  wizConsGroups.value.forEach(g => { total += (g.diffTotal || 0); (g.diffRows || []).forEach(r => rows.push(r)) })
  return { rows: rows.slice(0, wizSummaryDiffCap), total, truncated: total > wizSummaryDiffCap }
})

// 启动向导：加载生产者列表，进入选生产者步
async function openMergeWizard() {
  wizActive.value = true; wizStep.value = 'branch'; wizMsg.value = ''
  wizBranch.value = ''; wizGroups.value = []; wizCur.value = ''; wizConsCur.value = ''
  wizConsGroups.value = []
  wizApplyResults.value = []; wizShowSummary.value = false
  // 清掉可能残留的单分组阶段状态
  stageMode.value = ''; stageMsg.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage1/branches')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    wizBranches.value = obj.branches || []
  } catch (e) { wizMsg.value = '加载生产者列表失败：' + (e.message || e) }
}

// 选定生产者：对其每个分组拉状态，构建向导分组清单，进入阶段1 步
async function wizSelectBranch(branch) {
  wizBranch.value = branch; wizBusy.value = true; wizMsg.value = ''; wizGroups.value = []
  const bo = wizBranches.value.find(b => b.branch === branch)
  const groups = bo ? bo.groups : []
  try {
    for (const name of groups) {
      let st = { commits: [], intermediate_exists: false, new_commits: [] }
      try {
        const url = `/api/merge/stage1/status?branch=${encodeURIComponent(branch)}&group_name=${encodeURIComponent(name)}`
        const r = await fetch(url)
        if (r.ok) st = await r.json()
      } catch (e) { /* 单组状态失败不阻断 */ }
      const commits = st.commits || []
      const base = commits[0] || ''
      const derived = new Set(commits.filter(c => c !== base))
      const newCommits = st.new_commits || []
      // 已产出中间版本且有新提交 → 默认走增量
      const incremental = !!(st.intermediate_exists && newCommits.length)
      wizGroups.value.push({
        name, commits,
        intermediate_exists: !!st.intermediate_exists,
        new_commits: newCommits,
        decision: 'merge',              // 'merge' | 'skip'，默认合并
        base, derived, incremental,
        // 已产出中间版本且没有新提交 → 视为阶段1 已完成
        stage1_ok: !!st.intermediate_exists && !newCommits.length,
        sheetsSnapshot: null, stat: null, diffRows: [], diffTotal: 0,
      })
    }
    wizStep.value = 'stage1'
  } catch (e) { wizMsg.value = '构建分组清单失败：' + (e.message || e) } finally { wizBusy.value = false }
}

function wizToggleDecision(g) { g.decision = g.decision === 'merge' ? 'skip' : 'merge' }
function wizSetBase(g, val) { g.base = val; g.derived = new Set(g.commits.filter(c => c !== val)) }
function wizToggleDerived(g, name) {
  const s = new Set(g.derived)
  if (s.has(name)) s.delete(name); else s.add(name)
  g.derived = s
}

// 阶段1：对某分组载入比对表格（向导自建请求，避免触发 stage* 的 watcher 覆盖配置）
async function wizStage1Compare(g) {
  wizBusy.value = true; wizMsg.value = ''; error.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage1/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        branch: wizBranch.value, group_name: g.name,
        incremental: g.incremental,
        base_file: g.incremental ? null : (g.base || null),
        derived_files: g.incremental ? null : Array.from(g.derived),
      }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage1'; wizCur.value = g.name
  } catch (e) { wizMsg.value = '阶段1 比对失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 阶段1：产出当前分组的中间版本，成功后回到分组清单
async function wizConsolidate() {
  const g = wizCurGroup.value
  if (!g || !allResolved.value) return
  wizBusy.value = true; wizMsg.value = ''; error.value = ''
  try {
    const res = await fetch('/api/merge/stage1/consolidate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        branch: wizBranch.value, group_name: g.name, sheets: stageSheetsPayload(),
        incremental: g.incremental,
        base_file: g.incremental ? '' : (g.base || ''),
        derived_files: g.incremental ? null : Array.from(g.derived),
      }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    g.stage1_ok = true
    wizMsg.value = `✓ 「${g.name}」阶段1 中间版本已产出：${body.intermediate}（已折入 ${body.merged_commits ? body.merged_commits.length : 0} 次提交${body.cache_message ? '；' + body.cache_message : ''}）`
    stageMode.value = ''; data.value = null; wizCur.value = ''   // 回到分组清单
  } catch (e) { wizMsg.value = '阶段1 产出失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 进入阶段2 步：加载跨生产者综合分组清单
async function wizGoStage2() {
  if (!wizStage1AllDone.value) return
  wizBusy.value = true; wizMsg.value = ''
  wizStep.value = 'stage2'; stageMode.value = ''; data.value = null; wizCur.value = ''
  wizConsCur.value = ''
  // 收集本分支 stage1 完成的全部 group（跨生产者维度按 group 去重）
  const names = [...new Set(wizMergeGroups.value.map(g => g.name))]
  try {
    wizConsGroups.value = names.map(name => ({
      name,
      producers: [wizBranch.value],
      stage2_ok: false,
      stage3_done: false,
      sheetsSnapshot: null, stat: null, diffRows: [], diffTotal: 0,
    }))
    wizMsg.value = `✓ 阶段1 完成。下面按表分组逐个跨生产者综合（${names.length} 个分组）。`
  } catch (e) { wizMsg.value = '构建综合清单失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 阶段2：对某分组载入跨生产者综合比对表格（base=fork 快照，多方=各生产者中间版本）
async function wizStage2Compare(g) {
  wizBusy.value = true; wizMsg.value = ''; error.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage2/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: g.name }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage2'; wizConsCur.value = g.name
  } catch (e) { wizMsg.value = '阶段2 比对失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 阶段2：产出综合版本 {group}_consolidated.xlsx，成功后回到综合清单
async function wizStage2Consolidate() {
  const g = wizConsCurGroup.value
  if (!g || !allResolved.value) return
  wizBusy.value = true; wizMsg.value = ''; error.value = ''
  try {
    const res = await fetch('/api/merge/stage2/consolidate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: g.name, sheets: stageSheetsPayload(), branch: '' }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    g.stage2_ok = true
    wizMsg.value = `✓ 「${g.name}」阶段2 综合版本已产出：${body.consolidated}${body.cache_message ? '（' + body.cache_message + '）' : ''}`
    stageMode.value = ''; data.value = null; wizConsCur.value = ''   // 回到综合清单
  } catch (e) { wizMsg.value = '阶段2 产出失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 进入阶段3 步
function wizGoStage3() {
  if (!wizStage2AllDone.value) return
  wizStep.value = 'stage3'; wizMsg.value = ''
  stageMode.value = ''; data.value = null; wizConsCur.value = ''
}

// 阶段3：对某分组载入合回比对表格（merge-base + trunk head + 综合版本）
async function wizStage3Compare(g) {
  wizBusy.value = true; wizMsg.value = ''; error.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage3/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: g.name }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage3'; wizConsCur.value = g.name
  } catch (e) { wizMsg.value = '阶段3 比对失败：' + (e.message || e) } finally { wizBusy.value = false }
}

// 阶段3：把当前分组已解决的结果快照进合回队列，回到综合清单
function wizStage3Confirm() {
  const g = wizConsCurGroup.value
  if (!g || !allResolved.value) return
  g.sheetsSnapshot = stageSheetsPayload()
  g.stat = { ...stageApplyStat.value }
  const d = stageApplyDiff.value
  g.diffRows = d.rows.map(r => ({ ...r, group: g.name }))
  g.diffTotal = d.total
  g.stage3_done = true
  wizMsg.value = `✓ 「${g.name}」已加入合回队列（变更 ${g.stat.changed} / 新增 ${g.stat.inserted} / 删除 ${g.stat.deleted} / 已解冲突 ${g.stat.resolved}）`
  stageMode.value = ''; data.value = null; wizConsCur.value = ''
}

function wizOpenSummary() { if (wizStage3AllDone.value) wizShowSummary.value = true }

// 依次对每个综合分组调 /stage3/apply，收集结果
async function wizApplyAll() {
  wizBusy.value = true; wizApplyResults.value = []; wizMsg.value = ''
  const results = []
  for (const g of wizConsGroups.value) {
    if (!g.sheetsSnapshot) continue
    try {
      const res = await fetch('/api/merge/stage3/apply', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_name: g.name, sheets: g.sheetsSnapshot, branch: '' }),
      })
      if (!res.ok) throw new Error(await res.text())
      const body = await res.json()
      results.push({ name: g.name, ok: true, output: body.output, cache: body.cache_message || '' })
    } catch (e) {
      results.push({ name: g.name, ok: false, reason: e.message || String(e) })
    }
  }
  wizApplyResults.value = results
  wizBusy.value = false
  const okN = results.filter(r => r.ok).length
  wizMsg.value = `✓ 合回完成：成功 ${okN}/${results.length}`
  wizShowSummary.value = false
  wizStep.value = 'done'
}

// 退出向导，回到起始页
function wizExit() {
  wizActive.value = false; wizStep.value = 'branch'
  stageMode.value = ''; data.value = null
  wizGroups.value = []; wizBranch.value = ''; wizCur.value = ''; wizMsg.value = ''
  wizConsGroups.value = []; wizConsCur.value = ''
  history.value = []; historyIdx.value = -1
  selectedRows.value = new Set(); conflictIdx.value = -1; mergeResult.value = null
}

// ── 合回审计历史 ──
const showMergeHistory = ref(false)
const auditEntries = ref([])
const auditBusy = ref(false)
const auditOpenIdx = ref(-1)        // 展开查看 changes 的记录索引

async function openAuditPanel() {
  showMergeHistory.value = true; auditBusy.value = true; auditEntries.value = []; auditOpenIdx.value = -1
  try {
    const res = await fetch('/api/merge/stage3/audit?limit=50')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    auditEntries.value = obj.entries || []
  } catch (e) { /* 静默 */ } finally { auditBusy.value = false }
}
function closeAuditPanel() { showMergeHistory.value = false }
function toggleAuditRow(i) { auditOpenIdx.value = auditOpenIdx.value === i ? -1 : i }

// ── R13: 比对历史（近 24h 会话记录，只读回填）──
const showCompareHistory = ref(false)
const compareHistoryBusy = ref(false)
const compareHistoryEntries = ref([])

async function openCompareHistoryPanel() {
  showCompareHistory.value = true; compareHistoryBusy.value = true
  compareHistoryEntries.value = []
  try {
    const res = await fetch('/api/merge/history?since=24')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    compareHistoryEntries.value = obj.items || []
  } catch (e) { /* 静默 */ } finally { compareHistoryBusy.value = false }
}
function closeCompareHistoryPanel() { showCompareHistory.value = false }

async function restoreCompareSession(sid) {
  if (!sid) return
  compareHistoryBusy.value = true
  try {
    const res = await fetch(`/api/merge/history/${sid}/restore`, { method: 'POST' })
    if (!res.ok) { error.value = await res.text(); return }
    const resp = await res.json()
    data.value = resp
    sessionId.value = resp.session_id || sid
    const gk = Object.keys(resp.groups || {})[0]
    if (gk) {
      activeGroup.value = gk
      const sk = Object.keys(resp.groups[gk].sheets || {})[0]
      activeSheet.value = sk || ''
    }
    error.value = ''
    closeCompareHistoryPanel()
  } catch (e) { error.value = '回填失败：' + (e.message || e) }
  finally { compareHistoryBusy.value = false }
}

async function openBatchPanel() {
  showBatchPanel.value = true; batchResult.value = ''; batchBusy.value = true
  batchItems.value = []; batchSel.value = new Set()
  try {
    const res = await fetch('/api/merge/stage3/pending')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    batchItems.value = obj.items || []
    // 默认勾选全部可一键合回（无冲突/漏行）的分组
    batchItems.value.forEach(i => { if (i.ready && i.trunk_exists) batchSel.value.add(batchKey(i)) })
  } catch (e) { batchResult.value = '加载待合回列表失败：' + (e.message || e) } finally { batchBusy.value = false }
}
function closeBatchPanel() { showBatchPanel.value = false }
function toggleBatchSel(it) {
  const k = batchKey(it); const s = new Set(batchSel.value)
  if (s.has(k)) s.delete(k); else s.add(k)
  batchSel.value = s
}

async function runBatchApply() {
  const items = batchItems.value.filter(i => batchSel.value.has(batchKey(i)))
  if (!items.length) return
  batchBusy.value = true; batchResult.value = ''
  try {
    const res = await fetch('/api/merge/stage3/apply-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items.map(i => ({ group_name: i.group_name })) }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    const okList = body.results.filter(r => r.ok).map(r => `${r.group_name}→${r.output}`)
    const skipList = body.results.filter(r => !r.ok).map(r => `${r.group_name}(${r.reason || '失败'})`)
    batchResult.value = `✓ 合回 ${body.applied}/${body.total}：${okList.join('、') || '无'}` + (skipList.length ? `；跳过：${skipList.join('、')}` : '')
    await openBatchPanel()  // 刷新列表（已合回的可能状态变化）
  } catch (e) { batchResult.value = '批量合回失败：' + (e.message || e) } finally { batchBusy.value = false }
}

const stageGroupOptions = computed(() => {
  const b = stageBranches.value.find(x => x.branch === stageBranch.value)
  return b ? b.groups : []
})
const stageOrigin = computed(() => data.value?.conflict_origin || '')
const stageStaleness = computed(() => data.value?.staleness_warning || '')

// 动作栏阶段徽章文案（三阶段）
const stageBadgeText = computed(() => {
  if (stageMode.value === 'stage1') return '阶段1 · 合并多次提交'
  if (stageMode.value === 'stage2') return '阶段2 · 跨生产者综合'
  if (stageMode.value === 'stage3') return '阶段3 · 合回 trunk'
  return ''
})
// 向导动作栏徽章文案（三阶段，带当前分组名）
const wizActbadgeText = computed(() => {
  const cur = wizConsCur.value || wizCur.value
  if (stageMode.value === 'stage1') return '阶段1 · 合并「' + cur + '」多次提交'
  if (stageMode.value === 'stage2') return '阶段2 · 跨生产者综合「' + cur + '」'
  if (stageMode.value === 'stage3') return '阶段3 · 合回「' + cur + '」到 trunk'
  return ''
})

// 阶段错误 → 可执行的任务指引（把后端报错翻译成"下一步该做什么" + 跳转动作）
const stageErrorGuide = computed(() => {
  const msg = stageMsg.value || ''
  if (msg.includes('未通过阶段1') || msg.includes('请先完成阶段1')) {
    return { text: '该分组还没完成「阶段1 合并多次提交」。需先把该生产者的多次提交合并、解决提交间冲突并产出中间版本，才能进入阶段2 跨生产者综合。', action: 'toStage1', label: '← 去完成阶段1 合并' }
  }
  if (msg.includes('缺少跨生产者综合版本') || msg.includes('请先完成阶段2')) {
    return { text: '该分组还没完成「阶段2 跨生产者综合」。需先把各生产者的中间版本综合成单一综合版本并解决跨生产者冲突，才能合回 trunk。', action: 'toStage2', label: '← 去完成阶段2 综合' }
  }
  if (msg.includes('trunk 基准不存在')) {
    return { text: 'merge/trunk 下缺少该分组的基准表，无法合回。请确认对应的 {表名}.xlsx 已放入 trunk。', action: '', label: '' }
  }
  if (msg.includes('没有未折入') || msg.includes('无需增量')) {
    return { text: '该分组的所有提交都已折入中间版本，没有新提交需要合并。可直接进入阶段2 跨生产者综合。', action: 'toStage2', label: '进入阶段2 跨生产者综合 →' }
  }
  return null
})
function runStageGuide(action) {
  if (action === 'toStage1') { stageMsg.value = ''; stageIncremental.value = false; runStage1Compare() }
  else if (action === 'toStage2') { stageMsg.value = ''; runStage2ConsolidateCompare() }
}

// 合回 trunk 前的变更摘要（--stat 式）：当前分组即将写入 trunk 的改动统计
const stageApplyStat = computed(() => {
  const st = { changed: 0, inserted: 0, deleted: 0, resolved: 0 }
  const g = data.value?.groups?.[activeGroup.value]
  if (!g) return st
  for (const sheet of Object.values(g.sheets)) {
    sheet.rows.forEach(row => {
      if (row.row_type === 'inserted') { st.inserted++; return }
      if (row.row_type === 'deleted') { st.deleted++; return }
      let rowChanged = false
      row.cells.forEach(c => {
        if (c.resolved) st.resolved++
        if (row.row_type === 'matched' && (c.changed || c.resolved)) rowChanged = true
      })
      if (rowChanged) st.changed++
    })
  }
  return st
})

// 合回 trunk 前的 diff 明细：逐行/逐格列出将写入的具体改动（供确认框展示）
const stageApplyDiffCap = 300
const stageApplyDiff = computed(() => {
  const list = []
  const g = data.value?.groups?.[activeGroup.value]
  if (!g) return { rows: [], total: 0, truncated: false }
  const baseFile = g.base_file || ''
  for (const sheet of Object.values(g.sheets)) {
    for (const row of sheet.rows) {
      if (row.row_type === 'inserted') {
        list.push({ sheet: sheet.name, key: row.key, kind: 'insert', col: '', from: '', to: '整行新增' })
      } else if (row.row_type === 'deleted') {
        list.push({ sheet: sheet.name, key: row.key, kind: 'delete', col: '', from: '整行删除', to: '' })
      } else if (row.row_type === 'matched') {
        row.cells.forEach((c, ci) => {
          if (c.col === 0) return
          if (!(c.changed || c.resolved)) return
          const fromVal = c.versions ? c.versions[baseFile] : undefined
          if (String(c.value) === String(fromVal)) return
          list.push({
            sheet: sheet.name, key: row.key,
            kind: c.resolved ? 'resolved' : 'change',
            col: sheet.headers[ci] || ('列' + (ci + 1)),
            from: fromVal === undefined || fromVal === null ? '' : fromVal,
            to: c.value === undefined || c.value === null ? '' : c.value,
          })
        })
      }
    }
  }
  const total = list.length
  return { rows: list.slice(0, stageApplyDiffCap), total, truncated: total > stageApplyDiffCap }
})

async function openStagePicker() {
  showStagePicker.value = true
  stageMsg.value = ''
  try {
    const res = await fetch('/api/merge/stage1/branches')
    if (!res.ok) throw new Error(await res.text())
    const obj = await res.json()
    stageBranches.value = obj.branches || []
    if (stageBranches.value.length) {
      stageBranch.value = stageBranches.value[0].branch
      const g = stageGroupOptions.value
      stageGroup.value = g.length ? g[0] : ''
    }
    await loadStageStatus()
  } catch (e) { stageMsg.value = '加载生产者列表失败：' + (e.message || e) }
}

// 拉取当前 branch+group 的合并状态，决定基准选项与是否可增量
async function loadStageStatus() {
  stageStatus.value = null; stageIncremental.value = false; stageBaseFile.value = ''; stageDerived.value = new Set()
  if (!stageBranch.value || !stageGroup.value) return
  try {
    const url = `/api/merge/stage1/status?branch=${encodeURIComponent(stageBranch.value)}&group_name=${encodeURIComponent(stageGroup.value)}`
    const res = await fetch(url)
    if (!res.ok) return
    const st = await res.json()
    stageStatus.value = st
    stageBaseFile.value = (st.commits && st.commits[0]) || ''
    syncDerivedDefault()
    // 已产出中间版本且有新提交 → 默认推荐增量
    if (st.intermediate_exists && st.new_commits && st.new_commits.length) stageIncremental.value = true
  } catch (e) { /* 状态获取失败不阻断，走全量默认 */ }
}
// 默认勾选除 base 外的全部提交为衍生
function syncDerivedDefault() {
  const commits = stageStatus.value?.commits || []
  stageDerived.value = new Set(commits.filter(c => c !== stageBaseFile.value))
}
function toggleDerived(name) {
  const s = new Set(stageDerived.value)
  if (s.has(name)) s.delete(name); else s.add(name)
  stageDerived.value = s
}
watch(stageBaseFile, syncDerivedDefault)
watch(stageBranch, async () => {
  const g = stageGroupOptions.value
  if (!g.includes(stageGroup.value)) stageGroup.value = g[0] || ''
  await loadStageStatus()
})
watch(stageGroup, loadStageStatus)
function closeStagePicker() { showStagePicker.value = false }
function exitStageMode() {
  stageMode.value = ''; stage1Done.value = false; stage2Done.value = false; stageMsg.value = ''
  // 退出三阶段 → 回到空白着陆页，不停留在刚才的表格
  data.value = null; history.value = []; historyIdx.value = -1
  selectedRows.value = new Set(); conflictIdx.value = -1; mergeResult.value = null
}

// 把阶段比对响应载入到通用比对视图（复用现有渲染/解决流程）
function loadStageData(resp) {
  data.value = resp
  adoptChangedValues()
  allDone.value = false; selectedRows.value = new Set()
  history.value = []; historyIdx.value = -1
  mergeResult.value = null
  const gk = Object.keys(resp.groups)
  if (gk.length) {
    activeGroup.value = gk[0]
    const sk = Object.keys(resp.groups[gk[0]].sheets)
    if (sk.length) activeSheet.value = sk[0]
  }
  conflictIdx.value = -1
  sessionId.value = resp.session_id || ''
}

function stageSheetsPayload() {
  const g = data.value?.groups[activeGroup.value]
  if (!g) return []
  return Object.values(g.sheets).map(s => ({ name: s.name, headers: s.headers, rows: s.rows }))
}

async function runStage1Compare() {
  if (!stageBranch.value || !stageGroup.value) return
  stageBusy.value = true; error.value = ''; stageMsg.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage1/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        branch: stageBranch.value, group_name: stageGroup.value,
        incremental: stageIncremental.value,
        base_file: stageIncremental.value ? null : (stageBaseFile.value || null),
        derived_files: stageIncremental.value ? null : Array.from(stageDerived.value),
      }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage1'; stage1Done.value = false
    showStagePicker.value = false
  } catch (e) { stageMsg.value = '阶段1 比对失败：' + (e.message || e) } finally { stageBusy.value = false }
}

async function runStage1Consolidate() {
  if (!allResolved.value) return
  stageBusy.value = true; error.value = ''; stageMsg.value = ''
  try {
    const res = await fetch('/api/merge/stage1/consolidate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        branch: stageBranch.value, group_name: stageGroup.value, sheets: stageSheetsPayload(),
        incremental: stageIncremental.value,
        base_file: stageIncremental.value ? '' : (stageBaseFile.value || ''),
        derived_files: stageIncremental.value ? null : Array.from(stageDerived.value),
      }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    stage1Done.value = true
    stageMsg.value = `✓ 阶段1 中间版本已产出：${body.intermediate}（已折入 ${body.merged_commits ? body.merged_commits.length : 0} 次提交${body.cache_message ? '；' + body.cache_message : ''}）`
  } catch (e) { stageMsg.value = '阶段1 产出失败：' + (e.message || e) } finally { stageBusy.value = false }
}

// 阶段2（跨生产者综合）：对某分组载入综合比对表格（base=fork 快照，多方=各生产者中间版本）
async function runStage2ConsolidateCompare() {
  if (!stageGroup.value) return
  stageBusy.value = true; error.value = ''; stageMsg.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage2/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: stageGroup.value }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage2'
  } catch (e) { stageMsg.value = '阶段2 比对失败：' + (e.message || e) } finally { stageBusy.value = false }
}

// 阶段2：产出综合版本 {group}_consolidated.xlsx
async function runStage2Consolidate() {
  if (!allResolved.value) return
  stageBusy.value = true; error.value = ''; stageMsg.value = ''
  try {
    const res = await fetch('/api/merge/stage2/consolidate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: stageGroup.value, sheets: stageSheetsPayload(), branch: '' }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    stage2Done.value = true
    stageMsg.value = `✓ 阶段2 综合版本已产出：${body.consolidated}${body.cache_message ? '（' + body.cache_message + '）' : ''}`
  } catch (e) { stageMsg.value = '阶段2 产出失败：' + (e.message || e) } finally { stageBusy.value = false }
}

// 阶段3（合回 trunk）：对某分组载入合回比对表格（merge-base + trunk head + 综合版本）
async function runStage3Compare() {
  stageBusy.value = true; error.value = ''; stageMsg.value = ''; data.value = null
  try {
    const res = await fetch('/api/merge/stage3/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: stageGroup.value }),
    })
    if (!res.ok) throw new Error(await res.text())
    loadStageData(await res.json())
    stageMode.value = 'stage3'
  } catch (e) { stageMsg.value = '阶段3 比对失败：' + (e.message || e) } finally { stageBusy.value = false }
}

function askStage3Apply() {
  if (!allResolved.value) return
  showApplyConfirm.value = true
}

async function runStage3Apply() {
  if (!allResolved.value) return
  showApplyConfirm.value = false
  const stat = stageApplyStat.value
  stageBusy.value = true; error.value = ''; stageMsg.value = ''
  try {
    const res = await fetch('/api/merge/stage3/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_name: stageGroup.value, sheets: stageSheetsPayload(), branch: '' }),
    })
    if (!res.ok) throw new Error(await res.text())
    const body = await res.json()
    stageMsg.value = `✓ 阶段3 已合回 trunk：${body.output}（变更 ${stat.changed} 行 / 新增 ${stat.inserted} / 删除 ${stat.deleted} / 已解冲突 ${stat.resolved}${body.cache_message ? '；' + body.cache_message : ''}）`
  } catch (e) { stageMsg.value = '阶段3 合回失败：' + (e.message || e) } finally { stageBusy.value = false }
}
</script>

<template>
<div class="diff-view">
  <!-- 工具栏 -->
  <div class="toolbar">
    <label v-if="!data" class="btn-up">选择文件 <input type="file" multiple accept=".xlsx,.xls,.xlsm" @change="onFileChange" /></label>
    <span class="file-info" v-if="files.length && !data">{{ files.map(f=>f.name).join(', ') }}</span>
    <button v-if="files.length && !data" class="btn-del" @click="files=[];error=''" title="清空已选文件列表">✕ 清空</button>
    <button v-if="files.length && !data" class="btn-go" :disabled="files.length<2||loading" @click="doCompare" title="比对选中的多版本 Excel 文件差异（需 ≥2 个同名前缀文件）">{{ loading ? '比对中...' : '比对' }}</button>
    <button v-if="data && !stageMode" class="btn-del" @click="clearSession" title="清除当前会话，返回起始页">🗑 清除</button>
    <!-- 低频操作归并进"更多"下拉：合回历史 / 比对历史 / 批量合回 -->
    <div v-if="data" class="more-wrap">
      <button class="btn-more" :class="{ active: showMoreMenu }" @click.stop="showMoreMenu = !showMoreMenu" title="更多操作：合回历史 / 比对历史 / 批量合回">⋯ 更多</button>
      <div v-if="showMoreMenu" class="more-menu" @click.stop>
        <button class="more-item" :disabled="loading" @click="showMoreMenu = false; openAuditPanel()" title="查看每次合回 trunk 的时间/分组/产出/改动明细">📜 合回历史</button>
        <button class="more-item" @click="showMoreMenu = false; openCompareHistoryPanel()" title="近 24 小时的比对会话记录，点击可回填只读查看">🕐 比对历史</button>
        <button class="more-item" @click="showMoreMenu = false; openBatchPanel()" title="批量勾选多个已就绪分组一次性合回 trunk">📦 批量合回</button>
      </div>
    </div>
    <div v-if="showMoreMenu" class="more-backdrop" @click="showMoreMenu = false"></div>
    <span v-if="error" class="error-msg">{{ error }}</span>

    <div v-if="!data && filePrefixes.length >= 1" class="base-config">
      <span class="base-label">基准：</span>
      <div v-for="prefix in filePrefixes" :key="prefix" class="base-row">
        <span>{{ prefix }}</span>
        <select v-model="baseMap[prefix]" class="base-sel">
          <option v-for="f in prefixFiles[prefix]" :key="f.name" :value="f.name">{{ f.name }}</option>
        </select>
      </div>
    </div>
  </div>

  <div v-if="!data" class="onboard">
    <!-- 三阶段引导向导（无表格加载时的编排层） -->
    <div v-if="wizActive" class="wiz-wrap">
      <div class="wiz-head">
        <div class="wiz-steps">
          <span class="wiz-step" :class="{ 'wiz-active': wizStep === 'branch', 'wiz-passed': wizStep !== 'branch' }">① 选生产者</span>
          <span class="wiz-arr">→</span>
          <span class="wiz-step" :class="{ 'wiz-active': wizStep === 'stage1', 'wiz-passed': wizStep === 'stage2' || wizStep === 'stage3' || wizStep === 'done' }">② 阶段1 合并</span>
          <span class="wiz-arr">→</span>
          <span class="wiz-step" :class="{ 'wiz-active': wizStep === 'stage2', 'wiz-passed': wizStep === 'stage3' || wizStep === 'done' }">③ 阶段2 综合</span>
          <span class="wiz-arr">→</span>
          <span class="wiz-step" :class="{ 'wiz-active': wizStep === 'stage3', 'wiz-passed': wizStep === 'done' }">④ 阶段3 合回</span>
          <span class="wiz-arr">→</span>
          <span class="wiz-step" :class="{ 'wiz-active': wizStep === 'done' }">⑤ 完成</span>
        </div>
        <span style="flex:1"></span>
        <span v-if="wizBranch" class="wiz-ctx">生产者：{{ wizBranch }}</span>
        <button class="btn-stage-exit" @click="wizExit" title="退出向导">退出向导</button>
      </div>
      <div v-if="wizMsg" class="wiz-msg">{{ wizMsg }}</div>

      <!-- 步骤1：选生产者分支 -->
      <div v-if="wizStep === 'branch'" class="wiz-body">
        <h3 class="wiz-title">选择一个生产者分支</h3>
        <p class="wiz-desc">向导会带你把该生产者的全部表分组整批走完：阶段1 逐组合并多次提交，阶段2 跨生产者综合，阶段3 合回 trunk。</p>
        <div v-if="!wizBranches.length" class="wiz-empty">暂无可合并的生产者分支。</div>
        <div v-else class="wiz-branch-list">
          <button v-for="b in wizBranches" :key="b.branch" class="wiz-branch-card" :disabled="wizBusy" @click="wizSelectBranch(b.branch)"
                  :title="'选择生产者 ' + b.branch">
            <span class="wb-name">{{ b.branch }}</span>
            <span class="wb-groups">{{ (b.groups || []).length }} 个分组</span>
          </button>
        </div>
      </div>

      <!-- 步骤2：阶段1 逐分组合并清单 -->
      <div v-else-if="wizStep === 'stage1'" class="wiz-body">
        <h3 class="wiz-title">阶段1：逐分组合并多次提交</h3>
        <p class="wiz-desc">对每个分组可选择「合并」或「跳过」。合并的分组可配置基准提交、参与合并的衍生文件与增量模式，逐个点「合并此分组」解决提交间冲突并产出中间版本。</p>
        <div v-if="wizBusy && !wizGroups.length" class="wiz-empty">加载分组状态中...</div>
        <div v-for="g in wizGroups" :key="g.name" class="wiz-group" :class="{ 'wg-skip': g.decision === 'skip', 'wg-done': g.stage1_ok }">
          <div class="wg-head">
            <span class="wg-name">{{ g.name }}</span>
            <span v-if="g.stage1_ok" class="wg-tag wg-ok">✓ 已产出中间版本</span>
            <span v-else-if="g.decision === 'skip'" class="wg-tag wg-muted">已跳过</span>
            <span v-else class="wg-tag wg-pending">待合并</span>
            <span style="flex:1"></span>
            <label class="wg-dec"><input type="checkbox" :checked="g.decision === 'merge'" @change="wizToggleDecision(g)" /> 参与合并</label>
          </div>
          <div v-if="g.decision === 'merge' && !g.stage1_ok" class="wg-cfg">
            <template v-if="g.intermediate_exists && g.new_commits.length">
              <label class="wg-mode"><input type="radio" :value="true" v-model="g.incremental" /> 增量合入新提交（推荐，{{ g.new_commits.length }} 个：{{ g.new_commits.join('、') }}）</label>
              <label class="wg-mode"><input type="radio" :value="false" v-model="g.incremental" /> 全量重合（{{ g.commits.length }} 个提交）</label>
            </template>
            <template v-if="!g.incremental">
              <label class="wg-base">基准提交：
                <select :value="g.base" @change="wizSetBase(g, $event.target.value)">
                  <option v-for="c in g.commits" :key="c" :value="c">{{ c }}</option>
                </select>
              </label>
              <div class="wg-derived">
                <span class="wg-dlabel">衍生文件：</span>
                <label v-for="c in g.commits.filter(x => x !== g.base)" :key="c" class="wg-ditem">
                  <input type="checkbox" :checked="g.derived.has(c)" @change="wizToggleDerived(g, c)" /> {{ c }}
                </label>
              </div>
            </template>
            <button class="btn-confirm-accept wg-go" :disabled="wizBusy || (!g.incremental && g.derived.size < 1)" @click="wizStage1Compare(g)"
                    :title="'加载「' + g.name + '」的多次提交并开始阶段1 比对'">{{ wizBusy ? '处理中...' : '合并此分组' }}</button>
          </div>
        </div>
        <div class="wiz-foot">
          <button class="btn-close-modal" @click="wizStep = 'branch'" title="返回重新选择生产者">← 换生产者</button>
          <template v-if="wizStage1AllDone && wizBranches.filter(x => x.branch !== wizBranch).length">
            <span class="wiz-switch-sep">｜</span>
            <span class="wiz-switch-label">切换生产者继续阶段1：</span>
            <button v-for="b in wizBranches.filter(x => x.branch !== wizBranch)" :key="b.branch"
                    class="btn-wiz-switch" :disabled="wizBusy" @click="wizSelectBranch(b.branch)"
                    :title="'切换到生产者 ' + b.branch + ' 继续阶段1 合并（当前生产者已全部完成）'">{{ b.branch }}</button>
          </template>
          <span style="flex:1"></span>
          <button class="btn-confirm-accept" :disabled="!wizStage1AllDone" @click="wizGoStage2"
                  :title="wizStage1AllDone ? '进入阶段2 跨生产者综合' : '还有参与合并的分组未产出中间版本'">进入阶段2 →</button>
        </div>
      </div>

      <!-- 步骤3：阶段2 跨生产者综合清单 -->
      <div v-else-if="wizStep === 'stage2'" class="wiz-body">
        <h3 class="wiz-title">阶段2：跨生产者综合</h3>
        <p class="wiz-desc">对每个表分组点「综合比对」解决跨生产者冲突，比对表格中 <b>基准 = fork 快照 ｜ 其余列 = 各生产者中间版本（_merged_）</b>。产出综合版本（{group}_consolidated.xlsx）后才允许进入阶段3 合回。</p>
        <div v-if="!wizConsGroups.length" class="wiz-empty">暂无可综合的分组（需先完成阶段1 产出中间版本）。</div>
        <div v-for="g in wizConsGroups" :key="g.name" class="wiz-group" :class="{ 'wg-done': g.stage2_ok }">
          <div class="wg-head">
            <span class="wg-name">{{ g.name }}</span>
            <span v-if="g.stage2_ok" class="wg-tag wg-ok">✓ 已产出综合版本</span>
            <span v-else class="wg-tag wg-pending">待综合</span>
            <span style="flex:1"></span>
            <span class="wg-producers">生产者：{{ (g.producers || []).join('、') || '—' }}</span>
            <button class="btn-confirm-accept wg-go" :disabled="wizBusy" @click="wizStage2Compare(g)"
                    :title="'加载「' + g.name + '」的跨生产者综合比对（fork + 各生产者中间版本）'">{{ g.stage2_ok ? '重新比对' : '综合比对' }}</button>
          </div>
        </div>
        <div class="wiz-foot">
          <button class="btn-close-modal" @click="wizStep = 'stage1'" title="返回阶段1">← 返回阶段1</button>
          <span style="flex:1"></span>
          <button class="btn-confirm-accept" :disabled="!wizStage2AllDone" @click="wizGoStage3"
                  :title="wizStage2AllDone ? '进入阶段3 合回 trunk' : '还有分组未产出综合版本'">进入阶段3 →</button>
        </div>
      </div>

      <!-- 步骤4：阶段3 逐分组合回清单 -->
      <div v-else-if="wizStep === 'stage3'" class="wiz-body">
        <h3 class="wiz-title">阶段3：逐分组合回 trunk</h3>
        <p class="wiz-desc">对每个分组点「合回比对」解决合回冲突，比对表格为 <b>merge-base + trunk 基准 + 综合版本</b> 三方。逐个确认加入合回队列，全部完成后统一确认写回。</p>
        <div v-for="g in wizConsGroups" :key="g.name" class="wiz-group" :class="{ 'wg-done': g.stage3_done }">
          <div class="wg-head">
            <span class="wg-name">{{ g.name }}</span>
            <span v-if="g.stage3_done" class="wg-tag wg-ok">✓ 已加入合回队列（变更 {{ g.stat.changed }} / 新增 {{ g.stat.inserted }} / 删除 {{ g.stat.deleted }}）</span>
            <span v-else class="wg-tag wg-pending">待合回</span>
            <span style="flex:1"></span>
            <button class="btn-confirm-accept wg-go" :disabled="wizBusy" @click="wizStage3Compare(g)"
                    :title="'加载「' + g.name + '」的合回比对（merge-base + trunk + 综合版本）'">{{ g.stage3_done ? '重新比对' : '合回比对' }}</button>
          </div>
        </div>
        <div class="wiz-foot">
          <button class="btn-close-modal" @click="wizStep = 'stage2'" title="返回阶段2">← 返回阶段2</button>
          <span style="flex:1"></span>
          <button class="btn-confirm-accept" :disabled="!wizStage3AllDone" @click="wizOpenSummary"
                  :title="wizStage3AllDone ? '汇总所有改动并确认合回' : '还有分组未加入合回队列'">合回全部 →</button>
        </div>
      </div>

      <!-- 步骤5：完成汇总 -->
      <div v-else-if="wizStep === 'done'" class="wiz-body">
        <h3 class="wiz-title">合回完成</h3>
        <table class="batch-table">
          <thead><tr><th>分组</th><th>结果</th><th>产出 / 原因</th></tr></thead>
          <tbody>
            <tr v-for="r in wizApplyResults" :key="r.name">
              <td class="bt-name">{{ r.name }}</td>
              <td><span class="bt-tag" :class="r.ok ? 'bt-ok' : 'bt-err'">{{ r.ok ? '成功' : '失败' }}</span></td>
              <td>{{ r.ok ? (r.output + (r.cache ? '（' + r.cache + '）' : '')) : r.reason }}</td>
            </tr>
          </tbody>
        </table>
        <div class="wiz-foot">
          <span style="flex:1"></span>
          <button class="btn-confirm-accept" @click="wizExit" title="结束向导，返回起始页">完成，返回起始页</button>
        </div>
      </div>
    </div>

    <div v-else-if="!files.length" class="onboard-hero">
      <h2 class="ob-title">配表合并</h2>
      <p class="ob-sub">选一种方式开始。不熟悉流程就走「三阶段引导合并」，按步骤走即可。</p>
      <div class="ob-cards">
        <div class="ob-card ob-primary" @click="openMergeWizard" title="分步引导：选生产者分支 → 阶段1 逐分组合并 → 阶段2 跨生产者综合 → 阶段3 合回 trunk">
          <div class="ob-ico">🔀</div>
          <div class="ob-h">三阶段引导合并 <span class="ob-badge">推荐</span></div>
          <div class="ob-d">生产者的多次提交先合并成中间版本（阶段1），再跨生产者综合成单一版本（阶段2），最后合回 trunk（阶段3）。分步引导，冲突逐个解决。</div>
          <div class="ob-flow">① 合并提交 <span class="ob-arrow">→</span> ② 跨生产者综合 <span class="ob-arrow">→</span> ③ 合回 trunk</div>
        </div>
        <div class="ob-card" @click="openFolderPicker" title="扫描 merge 文件夹多版本文件，快速三方比对">
          <div class="ob-ico">📁</div>
          <div class="ob-h">从 merge 文件夹加载</div>
          <div class="ob-d">扫描 merge 目录的多版本文件，快速三方比对（不分阶段的扁平流程）。</div>
        </div>
        <label class="ob-card" title="手动选择 ≥2 个同名前缀 Excel 文件比对">
          <div class="ob-ico">📄</div>
          <div class="ob-h">选择本地文件比对</div>
          <div class="ob-d">手动选 ≥2 个同名前缀的 Excel 文件直接比对。</div>
          <input type="file" multiple accept=".xlsx,.xls,.xlsm" @change="onFileChange" style="display:none" />
        </label>
      </div>
      <div class="ob-links">
        <button class="ob-link" @click="openBatchPanel">📦 批量合回 trunk</button>
        <button class="ob-link" @click="openAuditPanel">📜 合回历史</button>
      </div>
    </div>
    <div v-else class="empty-hint">已选 {{ files.length }} 个文件，点击「开始比对」。</div>
  </div>

  <template v-else>
    <!-- 三阶段合并动作栏：区分阶段、显示冲突来源、门禁下一阶段 -->
    <div v-if="stageMode" class="stage-bar" :class="'stage-bar-' + stageMode.replace('stage','')">
      <span class="stage-badge">{{ stageBadgeText }}</span>
      <span class="stage-steps">
        <span class="stage-step" :class="{ 'step-active': stageMode === 'stage1', 'step-done': stageMode === 'stage2' || stageMode === 'stage3' }">① 合并提交</span>
        <span class="step-arrow">→</span>
        <span class="stage-step" :class="{ 'step-active': stageMode === 'stage2', 'step-done': stageMode === 'stage3' }">② 跨生产者综合</span>
        <span class="step-arrow">→</span>
        <span class="stage-step" :class="{ 'step-active': stageMode === 'stage3' }">③ 合回 trunk</span>
      </span>
      <span class="stage-origin origin-inter" v-if="stageOrigin === 'inter_commit'">● 提交间冲突</span>
      <span class="stage-origin origin-cross" v-else-if="stageOrigin === 'cross_producer'">● 跨生产者冲突</span>
      <span class="stage-origin origin-back" v-else-if="stageOrigin === 'merge_back'">● 合回冲突</span>
      <span class="stage-ctx">{{ stageBranch || '（跨生产者）' }} / {{ stageGroup }}</span>
      <span style="flex:1"></span>
      <button v-if="stageMode === 'stage1' && !wizActive" class="btn-stage-act" :disabled="!allResolved || stageBusy" @click="runStage1Consolidate"
              :title="allResolved ? '解决全部提交间冲突后，产出中间版本到 devbranch 缓冲区' : '存在未解决的提交间冲突，禁止产出中间版本'">
        {{ stageBusy ? '处理中...' : '产出中间版本' }}
      </button>
      <button v-if="stageMode === 'stage1' && stage1Done && !wizActive" class="btn-stage-act stage-next" :disabled="stageBusy" @click="runStage2ConsolidateCompare" title="进入阶段2：跨生产者综合（base=fork，多方=各生产者中间版本）">进入阶段2 跨生产者综合 →</button>
      <button v-if="stageMode === 'stage2' && !wizActive" class="btn-stage-act" :disabled="!allResolved || stageBusy" @click="runStage2Consolidate"
              :title="allResolved ? '解决全部跨生产者冲突后，产出综合版本 {group}_consolidated.xlsx' : '存在未解决的跨生产者冲突，禁止产出综合版本'">
        {{ stageBusy ? '处理中...' : '产出综合版本' }}
      </button>
      <button v-if="stageMode === 'stage2' && stage2Done && !wizActive" class="btn-stage-act stage-next" :disabled="stageBusy" @click="runStage3Compare" title="进入阶段3：以 trunk 为基准合回综合版本">进入阶段3 合回 trunk →</button>
      <button v-if="stageMode === 'stage3' && !wizActive" class="btn-stage-act" :disabled="!allResolved || stageBusy" @click="askStage3Apply"
              :title="allResolved ? '解决全部合回冲突后，版本化产出到 trunk' : '存在未解决的合回冲突，禁止写回 trunk'">
        {{ stageBusy ? '处理中...' : '合回 trunk（版本化产出）' }}
      </button>
      <button class="btn-stage-exit" @click="exitStageMode" title="退出三阶段模式">退出</button>
    </div>
    <!-- 向导内的阶段动作栏：产出中间版本 / 产出综合版本 / 确认加入合回队列 / 返回清单 -->
    <div v-if="wizActive && stageMode" class="wiz-actbar" :class="'wiz-actbar-' + stageMode.replace('stage','')">
      <span class="wiz-actbadge">{{ wizActbadgeText }}</span>
      <span v-if="stageMode === 'stage2'" class="wiz-sidehint">基准 = fork 快照 ｜ 其余列 = 各生产者中间版本（_merged_）</span>
      <span v-else-if="stageMode === 'stage3'" class="wiz-sidehint">merge-base + trunk 基准 + 综合版本（三方）</span>
      <span style="flex:1"></span>
      <button v-if="stageMode === 'stage1'" class="btn-stage-act" :disabled="!allResolved || wizBusy" @click="wizConsolidate"
              :title="allResolved ? '产出该分组的中间版本' : '存在未解决的提交间冲突，禁止产出中间版本'">
        {{ wizBusy ? '处理中...' : '产出中间版本' }}
      </button>
      <button v-if="stageMode === 'stage2'" class="btn-stage-act" :disabled="!allResolved || wizBusy" @click="wizStage2Consolidate"
              :title="allResolved ? '产出该分组的综合版本' : '存在未解决的跨生产者冲突，禁止产出综合版本'">
        {{ wizBusy ? '处理中...' : '产出综合版本' }}
      </button>
      <button v-if="stageMode === 'stage3'" class="btn-stage-act" :disabled="!allResolved || wizBusy" @click="wizStage3Confirm"
              :title="allResolved ? '确认该分组的合回改动，加入最终合回队列' : '存在未解决的合回冲突，禁止确认'">
        {{ wizBusy ? '处理中...' : '确认，加入合回队列' }}
      </button>
      <button class="btn-stage-exit" @click="stageMode = ''; data = null; wizCur = ''; wizConsCur = ''" title="放弃本分组当前解决，返回分组清单">← 返回清单</button>
    </div>
    <div v-if="stageMode && stageStaleness" class="stage-staleness"><span class="alert-icon">⚠</span> {{ stageStaleness }}</div>
    <div v-if="stageMode && stageErrorGuide" class="stage-guide">
      <span class="guide-icon">💡</span>
      <span class="guide-text">{{ stageErrorGuide.text }}</span>
      <button v-if="stageErrorGuide.action" class="btn-guide" @click="runStageGuide(stageErrorGuide.action)">{{ stageErrorGuide.label }}</button>
    </div>
    <div v-if="stageMode && stageMsg && !stageErrorGuide" class="stage-msg">{{ stageMsg }}</div>

    <!-- 全局进度与统一导出：所有分组都处理完才能导出 -->
    <div class="global-bar">
      <span class="global-label">全局进度</span>
      <span class="gu-item gu-conflict" v-if="globalUnresolved.conflicts"><span class="dot dot-r"></span> 冲突 {{ globalUnresolved.conflicts }}</span>
      <span class="gu-item gu-changed" v-if="globalUnresolved.changed"><span class="dot dot-o"></span> 变更 {{ globalUnresolved.changed }}</span>
      <span class="gu-item gu-insert" v-if="globalUnresolved.inserted"><span class="dot dot-g"></span> 新增 {{ globalUnresolved.inserted }}</span>
      <span class="gu-item gu-deleted" v-if="globalUnresolved.deleted"><span class="dot dot-d"></span> 删除 {{ globalUnresolved.deleted }}</span>
      <span class="gu-item gu-missing" v-if="globalUnresolved.missing"><span class="dot dot-m"></span> 漏行 {{ globalUnresolved.missing }}（P0）</span>
      <span v-if="allResolved" class="gu-done">✅ 冲突已全部解决，可导出</span>
      <span style="flex:1"></span>
      <button class="btn-export-final" v-if="!stageMode" :disabled="!allResolved" @click="exportAllGroups"
              :title="allResolved ? '导出所有分组的合并结果（新增/删除/变更自动采用，仅冲突需人工处理）' : (globalUnresolved.missing ? '存在未补回的漏行（P0），请先在各 Sheet 点击「↩ 补回」后再导出' : '还有未解决的冲突，请先在各分组各 Sheet 中处理完毕后再导出')">
        📥 导出全部（{{ Object.keys(data.groups).length }} 个分组）
      </button>
    </div>
    <div v-if="exportMsg" class="export-result">
      <pre>{{ exportMsg }}</pre>
      <button class="sp-close" @click="exportMsg = ''" title="关闭导出结果">✕</button>
    </div>

    <div class="nav-bar">
      <span class="nav-label">分组：</span>
      <button v-for="(g, gk) in data.groups" :key="gk" class="tab" :class="{ active: activeGroup === gk }" @click="switchGroup(gk)" :title="'切换到分组 ' + gk">{{ gk }}</button>
    </div>

    <div class="nav-bar sheet-nav" v-if="curGroup">
      <span class="nav-label">Sheet：</span>
      <select class="sheet-select" :value="activeSheet" @change="onSheetSelectChange"
              :title="sheetSelectEmpty ? '当前分组所有 Sheet 均无冲突/增删改，已展示全部供查看' : '选择 Sheet（仅列有冲突或增删改的 Sheet）'">
        <option v-if="sheetSelectEmpty" value="" disabled>无冲突/增删改 sheet（已展示全部）</option>
        <option v-for="opt in sheetSelectOptions" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
      </select>
      <span class="sheet-nav-hint" v-if="!sheetSelectEmpty">仅显示有冲突/增删改的 Sheet</span>
    </div>

    <div class="stats-bar" v-if="curSheet">
      <span><span class="dot dot-r"></span> 冲突 <b>{{ sheetsLiveCounts[activeSheet]?.conflicts || 0 }}</b></span>
      <span><span class="dot dot-o"></span> 变更 <b>{{ sheetsLiveCounts[activeSheet]?.changed || 0 }}</b></span>
      <span><span class="dot dot-g"></span> 新增 <b>{{ sheetsLiveCounts[activeSheet]?.inserted || 0 }}</b></span>
      <span><span class="dot dot-d"></span> 删除 <b>{{ sheetsLiveCounts[activeSheet]?.deleted || 0 }}</b></span>
      <span v-if="sheetsLiveCounts[activeSheet]?.missing" class="stat-missing" title="trunk 基准有但衍生版缺失的行，P0 阻断导出，需补回"><span class="dot dot-m"></span> 漏行 <b>{{ sheetsLiveCounts[activeSheet].missing }}</b></span>
      <span v-if="resolvedCount" class="stat-resolved">✓ 已解决冲突 {{ resolvedCount }}</span>
      <span class="filter-radio-group">
        <label class="filter-radio" :class="{ active: filterType === 'all' }"><input type="radio" value="all" v-model="filterType" /> 全部</label>
        <label class="filter-radio fr-conflict" :class="{ active: filterType === 'conflict' }"><input type="radio" value="conflict" v-model="filterType" /> <span class="fr-dot" style="background:var(--danger)"></span> 冲突</label>
        <label class="filter-radio fr-changed" :class="{ active: filterType === 'changed' }"><input type="radio" value="changed" v-model="filterType" /> <span class="fr-dot" style="background:var(--warning)"></span> 修改</label>
        <label class="filter-radio fr-inserted" :class="{ active: filterType === 'inserted' }"><input type="radio" value="inserted" v-model="filterType" /> <span class="fr-dot" style="background:var(--success)"></span> 新增</label>
        <label class="filter-radio fr-deleted" :class="{ active: filterType === 'deleted' }"><input type="radio" value="deleted" v-model="filterType" /> <span class="fr-dot" style="background:var(--diff-del)"></span> 删除</label>
        <label v-if="sheetsLiveCounts[activeSheet]?.missing" class="filter-radio fr-missing" :class="{ active: filterType === 'missing' }"><input type="radio" value="missing" v-model="filterType" /> <span class="fr-dot" style="background:var(--danger)"></span> 漏行</label>
      </span>
      <!-- T9: 行内容搜索（本地过滤当前 sheet 所有行，与上方筛选叠加） -->
      <span class="row-search-wrap">
        <input class="row-search-input" v-model="rowSearchKey" placeholder="筛选行（Key 或单元格内容）" />
        <span v-if="rowSearchKey.trim()" class="row-search-count">找到 {{ rowSearchStats.hits }} 行 / 共 {{ rowSearchStats.total }} 行</span>
      </span>
      <button :disabled="!diffList.length" @click="goConflict(-1)" title="上一个冲突">◀</button>
      <span v-if="diffList.length">{{ conflictIdx >= 0 ? conflictIdx + 1 : 0 }}/{{ diffList.length }}</span>
      <button :disabled="!diffList.length" @click="goConflict(1)" title="下一个冲突">▶</button>
      <button :disabled="historyIdx <= 0" @click="undo" title="撤销 Ctrl+Z">⏪ 撤销</button>
      <button :disabled="historyIdx >= history.length - 1" @click="redo" title="恢复 Ctrl+Y">⏩ 恢复</button>
      <span style="flex:1"></span>
      <button class="btn-panel" :class="{ active: showConflictPanel }" @click="showConflictPanel = !showConflictPanel" :disabled="!diffList.length" title="展开当前 Sheet 的冲突列表，点击项跳转">📋 冲突列表({{ diffList.length }})</button>
      <button class="btn-panel" :class="{ active: showAuditPanel }" @click="showAuditPanel = !showAuditPanel" :disabled="!resolvedCount" title="查看本 Sheet 已解决冲突，点击「跳转」回到该位置撤销重选">✓ 已解决({{ resolvedCount }})</button>
      <label class="compact-cb" v-if="curSheet.rows.length > 500"><input type="checkbox" v-model="compactMode" /> 紧凑模式</label>
      <span class="accept-ver-label" title="将当前 Sheet 全部冲突单元格替换为指定版本内容">整表改用：</span>
      <button v-for="fn in curGroup.files" :key="fn"
              class="btn-accept-ver"
              :class="{ 'is-base': fn === curGroup?.base_file }"
              :disabled="!diffList.length"
              @click="showAcceptAllPreview(fn)"
              :title="'预览 ' + fn + ' 在冲突处的具体内容，确认后整表应用'">
        {{ versionLabel(fn) }}<span v-if="fn === curGroup?.base_file" class="ver-tag">基准</span>
      </button>
    </div>

    <div v-if="curGroup && curGroup.missing_sheets && curGroup.missing_sheets.length" class="alert-banner alert-missing-sheet">
      <span class="alert-icon">⚠</span>
      <span class="alert-text">
        <b>Sheet 缺失告警（M6）：</b>以下 sheet 在 trunk 基准存在但衍生版缺失，已从比对中排除：
        <span v-for="m in curGroup.missing_sheets" :key="m.sheet" class="alert-item">
          「{{ m.sheet }}」（缺于 {{ m.missing_in.join(', ') }}）
        </span>
      </span>
    </div>

    <div v-if="curSheet && curSheet.structure_diff" class="alert-banner alert-structure-diff">
      <span class="alert-icon">⚠</span>
      <span class="alert-text">
        <b>表头结构差异告警（M5）：</b>衍生版表头与基准不一致，当前按列号比对可能错位，请人工核实：
        <span v-for="(info, fname) in curSheet.structure_diff.files" :key="fname" class="alert-item">
          「{{ fname }}」<template v-if="info.added_cols && info.added_cols.length">+列[{{ info.added_cols.join(', ') }}]</template><template v-if="info.removed_cols && info.removed_cols.length"> -列[{{ info.removed_cols.join(', ') }}]</template><template v-if="info.reordered"> 列顺序重排</template>
        </span>
      </span>
    </div>

    <div v-if="allDone && diffList.length === 0 && !(sheetsLiveCounts[activeSheet]?.missing)" class="done-bar">
      ✅ 当前 Sheet 的冲突已全部处理完毕（新增/删除/变更默认采用，仅高亮展示）
      <button v-if="nextConflictSheet" class="btn-jump-next" @click="switchSheet(nextConflictSheet)" title="跳转到本分组下一个仍有未解决冲突的 Sheet">跳转到下一个有冲突的 Sheet →</button>
    </div>

    <div v-if="sheetsLiveCounts[activeSheet]?.missing" class="alert-banner alert-missing-row">
      <span class="alert-icon">🛑</span>
      <span class="alert-text">
        <b>P0 漏行告警（M3）：</b>当前 Sheet 有 <b>{{ sheetsLiveCounts[activeSheet].missing }}</b> 行在 trunk 基准存在但衍生版缺失。
        点击行尾「↩ 补回」从 trunk 恢复；未全部补回前<b>禁止导出</b>。
      </span>
    </div>

    <div v-if="selectedRows.size > 0" class="batch-bar">
      <span>已选 <b>{{ selectedRows.size }}</b> 行</span>
      <button class="btn-batch" @click="openRowPreview([...selectedRows])" title="预览选中行在各版本的内容，并选择版本整行应用">👁 预览内容并选择版本</button>
      <button class="btn-clear-sel" @click="clearSelection" title="清空行选择">取消</button>
    </div>

    <div class="main-area">
      <div class="table-wrap" @scroll.passive="onTableScroll">
        <table>
          <thead><tr>
            <th class="row-hd sel-col"><input type="checkbox" @change="toggleAllRows" /></th>
            <th class="row-hd">#</th>
            <th class="row-hd">Key</th>
            <th class="row-hd row-act-col">操作</th>
            <th v-for="(h, hi) in curSheet.headers" :key="hi">{{ h || colLetter(hi) }}</th>
          </tr></thead>
          <tbody>
            <tr v-for="item in visibleRows" :key="item.ri" :id="'diff-row-'+item.ri" :class="{
              'row-inserted': item.row.row_type === 'inserted',
              'row-deleted': item.row.row_type === 'deleted',
              'row-missing': item.row.row_type === 'missing_row',
              'row-sel': selectedRows.has(item.ri)
            }">
              <td class="row-hd sel-col"><input type="checkbox" :checked="selectedRows.has(item.ri)" @change="toggleRow(item.ri)" /></td>
              <td class="row-hd">{{ item.ri + 1 }}</td>
              <td class="row-hd key-cell" :title="item.row.id_remapped ? ('merge 编号冲突，原编号 ' + item.row.original_pk + ' 自增为 ' + item.row.key) : ''"><span v-html="highlightSearchHit(item.row.key, 200, rowSearchKey)"></span></td>
              <td class="row-hd row-act-col">
                <div class="row-act-wrap">
                  <button class="row-act-btn" @click.stop="openRowPreview([item.ri])" :disabled="!diffCellsOf(item.row).length" title="预览各版本在本行冲突列的具体内容，并选择版本整行应用">👁</button>
                  <span v-if="item.row.row_type === 'inserted'" class="row-tag row-tag-ins">新增</span>
                  <span v-if="item.row.row_type === 'inserted' && item.row.source_file" class="row-tag row-tag-src" :title="'新增来源：' + item.row.source_file">来源 {{ item.row.source_version ? ('v' + item.row.source_version) : item.row.source_file }}</span>
                  <span v-else-if="item.row.row_type === 'deleted'" class="row-tag row-tag-del">删除</span>
                  <span v-else-if="item.row.row_type === 'missing_row'" class="row-tag row-tag-missing" title="trunk 基准有此行但衍生版缺失，P0 漏行">漏行</span>
                  <button v-if="item.row.row_type === 'missing_row'" class="row-act-btn btn-restore" :disabled="restoringPk === item.row.key" @click.stop="restoreRow(item.row)" :title="'从 trunk 基准补回 pk=' + item.row.key + '，补回后导出保留此行'">{{ restoringPk === item.row.key ? '...' : '↩ 补回' }}</button>
                  <span v-if="item.row.id_remapped" class="row-tag row-tag-remap" :title="'merge 编号冲突，原编号 ' + item.row.original_pk + ' 自增为 ' + item.row.key">↻ {{ item.row.original_pk }}→{{ item.row.key }}</span>
                </div>
              </td>
              <td v-for="(cell, ci) in item.row.cells" :key="ci" :class="{
                'cell-conflict': cell.conflict,
                'cell-changed': cell.changed && !cell.conflict && item.row.row_type === 'matched',
                'cell-resolved': cell.resolved
              }" @click="cellClick(cell, item.ri, ci)">
                <template v-if="cell.resolved">
                  <span class="resolved-badge" @click.stop="reopenConflict(item.ri, ci)" :title="'已选 ' + (cell.resolvedBy||'') + '，点击撤销重新选择'">✓ {{ resolvedLabel(cell) }}</span>
                  <span v-html="highlightSearchHit(cell.value, 60, rowSearchKey)"></span>
                </template>
                <template v-else-if="cell.changed && item.row.row_type === 'matched'">
                  <span class="cell-version-badge" :title="'来自 ' + changedVersion(cell)">{{ versionLabel(changedVersion(cell)) }}</span>
                  <span class="cell-old" :title="'基准旧值'" v-html="highlightSearchHit(baseValOf(cell), 20, rowSearchKey)"></span><span class="cell-arrow">→</span><span class="cell-new" :title="'采纳新值'" v-html="highlightSearchHit(cell.value, 20, rowSearchKey)"></span>
                </template>
                <template v-else-if="cell.conflict">
                  <span class="cell-version-badge cell-vb-conflict" title="多版本冲突，点击查看">冲突</span>
                  <span class="cell-conflict-versions">
                    <template v-for="fn in curGroup.files" :key="fn">
                      <span class="cv-ver" :class="{ 'cv-base': fn === curGroup.base_file }">
                        <span class="cv-ver-label">{{ fn === curGroup.base_file ? '基准' : versionLabel(fn) }}</span>
                        <span class="cv-val" v-html="highlightSearchHit(String(cell.versions?.[fn] ?? '∅'), 24, rowSearchKey)"></span>
                      </span><span v-if="fn !== curGroup.files[curGroup.files.length - 1]" class="cv-sep">│</span>
                    </template>
                  </span>
                </template>
                <template v-else>
                  <span v-html="highlightSearchHit(cell.value, 80, rowSearchKey)"></span>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="visibleRows.length < filteredRowCount" class="load-more-hint">
          已加载 {{ visibleRows.length }} / {{ filteredRowCount }} 行，滚动到底自动加载更多
        </div>
      </div>

      <div class="gutter" v-if="diffList.length">
        <div v-for="(d, di) in diffList" :key="di" class="gutter-row" :class="{ 'gutter-conflict': d.cell.conflict, 'gutter-diff': d.cell.changed && !d.cell.conflict }" @click="conflictIdx=di;modalCell=d" :title="'Key: ' + d.key"></div>
      </div>
    </div>

    <div class="legend">
      <span><span class="dot dot-r"></span> 冲突（多版本改法不同，需人工选择）</span>
      <span><span class="dot dot-o"></span> 变更（仅一个版本改动，自动采用，仅高亮）</span>
      <span><span class="dot dot-g"></span> 新增（衍生新增的行，自动采用，仅高亮）</span>
      <span><span class="dot dot-d"></span> 删除（基准有衍生全无，导出时排除，仅高亮）</span>
      <span><span class="dot dot-m"></span> 漏行（trunk 有衍生全缺，P0 阻断导出，需补回）</span>
    </div>

    <!-- 冲突列表面板 -->
    <div v-if="showConflictPanel && diffList.length" class="side-panel">
      <div class="sp-header">冲突列表 ({{ diffList.length }})<button class="sp-close" @click="showConflictPanel = false" title="关闭冲突列表面板">✕</button></div>
      <div class="sp-list">
        <div v-for="(d, i) in diffList" :key="i" class="sp-item" :class="{ active: i === conflictIdx, 'is-conflict': d.cell.conflict }" @click="conflictIdx = i; modalCell = d; scrollToRow(d.ri)">
          <span class="sp-key">{{ d.key }}</span>
          <span class="sp-col">{{ d.header || colLetter(d.ci) }}</span>
          <span class="sp-tag tag-conflict">冲突</span>
        </div>
      </div>
    </div>

    <!-- 操作审计面板：已解决冲突明细，可跳转回位置并撤销 -->
    <div v-if="showAuditPanel" class="side-panel">
      <div class="sp-header">已解决冲突 ({{ resolvedList.length }})<button class="sp-close" @click="showAuditPanel = false" title="关闭已解决列表面板">✕</button></div>
      <div class="sp-body">
        <div v-if="auditStats.length" class="audit-list">
          <div v-for="s in auditStats" :key="s.source" class="audit-item">
            <span class="audit-source">{{ s.source }}</span>
            <span class="audit-count">{{ s.count }} 处</span>
          </div>
        </div>
        <div v-if="resolvedList.length" class="resolved-list">
          <div v-for="(r, i) in resolvedList" :key="i" class="resolved-item">
            <span class="ri-key">{{ r.key }}</span>
            <span class="ri-col">{{ r.header }}</span>
            <span class="ri-ver">✓ {{ versionLabel(r.source) }}</span>
            <button class="ri-jump" @click="jumpToResolved(r.ri, r.ci)" title="跳转到该位置并重新选择（撤销）">跳转</button>
          </div>
        </div>
        <p v-else class="audit-empty">暂无已解决冲突</p>
      </div>
    </div>
  </template>

  <!-- 冲突/修改弹窗：横向表格（各版本为列，行为该行各列，差异高亮） -->
  <div v-if="modalCell" class="modal-bg" @click="closeModal">
    <div class="modal cell-modal" @click.stop>
      <h3>{{ modalCell.header || (modalCell.cell.col_letter + ' 列') }} — Key: {{ modalCell.key }}
        <span v-if="modalCell.cell.changed && !modalCell.cell.conflict" class="modal-type-badge mod-changed">🟠 修改（自动采用，仅展示）</span>
        <span v-else class="modal-type-badge mod-conflict">🔴 冲突（需人工选择）</span>
      </h3>
      <p v-if="modalCell.cell.changed && !modalCell.cell.conflict" class="changed-hint">此单元格仅单个衍生版本存在修改，已自动采用该版本值。下方横向展示该行各列在各版本的取值（基准列为旧值，红删绿增标出差异）。</p>
      <div class="vm-table-wrap">
        <table class="vm-table">
          <thead>
            <tr>
              <th class="vm-col-col">列</th>
              <th v-for="fn in curGroup.files" :key="fn" class="vm-ver-th" :class="{ 'vm-base-col': fn === curGroup?.base_file }">
                <div class="vm-ver-name">{{ versionLabel(fn)
                  }}<b v-if="fn === curGroup?.base_file" class="ver-tag">基准</b
                  ><b v-else-if="(stageOrigin === 'cross_producer' || stageOrigin === 'merge_back') && fn.includes('_merged_')" class="ver-tag ver-tag-theirs">生产者</b
                  ><b v-else-if="stageOrigin === 'merge_back' && fn.includes('_consolidated')" class="ver-tag ver-tag-cons">综合</b
                  ><b v-else-if="stageOrigin === 'merge_back'" class="ver-tag ver-tag-ours">trunk</b></div>
                <div v-if="versionMeta(fn)" class="vm-ver-meta">
                  <span v-if="versionMeta(fn).author">{{ versionMeta(fn).author }}</span>
                  <span v-if="versionMeta(fn).date" :title="versionMeta(fn).date">{{ fmtRevDate(versionMeta(fn).date) }}</span>
                </div>
                <button v-if="modalCell.cell.conflict" class="vm-choose-btn" @click="chooseVersionForRow(fn)" title="将该版本应用到本行所有冲突列">整行用此版本</button>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="{ c, ci } in modalConflictCells" :key="ci" :class="{ 'vm-row-current': ci === modalCell.ci }">
              <td class="vm-col-col">
                {{ curSheet.headers[ci] || colLetter(ci) }}
                <span v-if="ci === modalCell.ci" class="vm-cur-tag">当前</span>
              </td>
              <td v-for="fn in curGroup.files" :key="fn"
                  :class="{ 'vm-diff-cell': vmIsDiff(ci, fn), 'vm-base-cell': fn === curGroup?.base_file, 'vm-cell-current': ci === modalCell.ci, 'vm-cell-clickable': modalCell.cell.conflict && fn !== curGroup?.base_file }"
                  :title="modalCell.cell.conflict && fn !== curGroup?.base_file ? '点击采用此版本的值（仅当前单元格）' : ''"
                  @click="modalCell.cell.conflict && fn !== curGroup?.base_file ? chooseVersionForCell(ci, fn) : null"
                  v-html="vmCellHtml(ci, fn)"></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="vm-hint">仅展示该行存在冲突的列。<span class="vm-cur-tag">当前</span> 标记正在处理的单元格（高亮）。两种选择方式：① 点击列首「整行用此版本」— 本行所有冲突列采用该版本；② 点击某版本单元格的值 — 仅当前单元格采用该版本。基准列为旧值，其余列相对基准做字符级 diff（红删绿增）。</p>
      <!-- M13: AI 冲突建议面板 -->
      <div v-if="modalCell.cell.conflict && (aiSuggestion || aiSuggestBusy || aiSuggestError)" class="ai-panel">
        <div v-if="aiSuggestBusy" class="ai-loading">AI 分析中…</div>
        <div v-else-if="aiSuggestError" class="ai-error">{{ aiSuggestError }}</div>
        <div v-else-if="aiSuggestion" class="ai-result">
          <div class="ai-head">
            <span class="ai-badge">💡 AI 建议</span>
            <span class="ai-ver">采用版本：{{ versionLabel(aiSuggestion.suggested_version) }}</span>
            <span class="ai-conf" :title="'置信度 0~1'">置信度 {{ (aiSuggestion.confidence * 100).toFixed(0) }}%</span>
          </div>
          <p class="ai-reason">{{ aiSuggestion.reasoning }}</p>
          <div class="ai-actions">
            <button class="btn-ai-adopt" @click="adoptAiSuggestion" title="用 AI 建议的版本值解决当前单元格">一键采纳</button>
            <span class="ai-tip">采纳后自动跳到下一个冲突</span>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <span class="kbd-hint" v-if="modalCell.cell.conflict">数字键选版本 · Space 跳过 · Esc 关闭</span>
        <button v-if="modalCell.cell.conflict" class="btn-ai" @click="requestAiSuggest" :disabled="aiSuggestBusy" title="基于列类型/值特征给出建议版本">💡 AI 建议</button>
        <button v-if="modalCell.cell.conflict" class="btn-skip" @click="advanceModal" title="跳过当前冲突，处理下一个">跳过 →</button>
        <button class="btn-close-modal" @click="closeModal" title="关闭弹窗">关闭</button>
      </div>
    </div>
  </div>

  <!-- 整表改用版本 X 预览弹窗 -->
  <div v-if="previewVersion" class="modal-bg" @click="cancelAcceptAll">
    <div class="modal preview-modal" @click.stop>
      <h3>预览整表改用版本：{{ previewVersion.replace(/\.xlsx$/i, '') }}</h3>
      <p class="preview-desc">将以下 <b>{{ previewStats.all }}</b> 处冲突的内容替换为该版本的值。基准为 <b>{{ curGroup?.base_file }}</b>（新增/删除/变更自动采用，不在此列）</p>
      <div class="preview-filter">
        <button :class="{ active: previewFilter === 'all' }" @click="previewFilter = 'all'" title="显示全部冲突与变更">全部 ({{ previewStats.all }})</button>
        <button :class="{ active: previewFilter === 'conflict' }" @click="previewFilter = 'conflict'" title="仅显示需人工选择的冲突">仅冲突 ({{ previewStats.conflict }})</button>
        <button :class="{ active: previewFilter === 'changed' }" @click="previewFilter = 'changed'" title="仅显示单向变更（自动采用）">仅变更 ({{ previewStats.changed }})</button>
      </div>
      <div class="preview-list">
        <div v-for="(it, i) in previewFiltered" :key="i" class="preview-row" :class="{ 'is-conflict': it.conflict, 'is-changed': it.changed }">
          <div class="pr-loc">
            <span class="pr-key">{{ it.key }}</span>
            <span class="pr-col">{{ it.header || (it.col_letter + ' 列') }}</span>
            <span class="pr-tag" :class="it.conflict ? 'tag-conflict' : 'tag-changed'">{{ it.conflict ? '冲突' : '变更' }}</span>
          </div>
          <div class="pr-vals">
            <div class="pr-val pr-base"><span class="pr-vlabel">基准</span><code>{{ truncated(it.base, 60) }}</code></div>
            <div class="pr-val pr-target"><span class="pr-vlabel">本版本</span><code>{{ truncated(it.target, 60) }}</code></div>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="cancelAcceptAll" title="取消整表改用版本操作">取消</button>
        <button class="btn-confirm-accept" @click="confirmAcceptAll" title="确认将所有冲突替换为该版本的值">✓ 确认全部接纳</button>
      </div>
    </div>
  </div>

  <!-- 行版本预览弹窗：选中1或多行后，每行一个表格（仅冲突列+各版本），与单元格弹窗交互一致 -->
  <div v-if="rowPreviewRis.length" class="modal-bg" @click="closeRowPreview">
    <div class="modal preview-modal" @click.stop>
      <h3>预览 {{ rowPreviewRis.length }} 行内容并选择版本</h3>
      <p class="preview-desc">下表为每个选中行列出其冲突列及各版本取值。两种选择方式：① 点击列首「整行用此版本」— 该行所有冲突列采用此版本；② 点击某版本单元格的值 — 仅该单元格采用此版本。底部按钮可将同一版本应用到全部选中行。</p>
      <div class="row-preview-list">
        <div v-for="ri in rowPreviewRis" :key="ri" class="rp-row-block">
          <div class="rp-row-title">Key: {{ curSheet.rows[ri]?.key }}<span class="rp-conf-count">冲突列 {{ diffCellsOf(curSheet.rows[ri]).length }}</span></div>
          <table class="rp-table" v-if="diffCellsOf(curSheet.rows[ri]).length">
            <thead><tr>
              <th>列</th>
              <th v-for="fn in curGroup.files" :key="fn" :class="{ 'rp-base-col': fn === curGroup.base_file }">
                {{ versionLabel(fn) }}<span v-if="fn === curGroup.base_file" class="ver-tag">基准</span>
                <button class="vm-choose-btn rp-choose-btn" @click="chooseRowVersionForRow(ri, fn)" title="将该版本应用到本行所有冲突列">整行</button>
              </th>
            </tr></thead>
            <tbody>
              <tr v-for="{ c, ci } in diffCellsOf(curSheet.rows[ri])" :key="ci">
                <td class="rp-colname">{{ curSheet.headers[ci] || colLetter(ci) }}<span class="vm-conf-tag">冲突</span></td>
                <td v-for="fn in curGroup.files" :key="fn"
                    :class="{ 'rp-diff-val': str(c.versions[fn]) !== str(c.versions[curGroup.base_file]), 'rp-base-cell': fn === curGroup.base_file, 'rp-cell-clickable': fn !== curGroup.base_file }"
                    :title="fn !== curGroup.base_file ? '点击采用此版本的值（仅本单元格）' : ''"
                    @click="fn !== curGroup.base_file ? chooseRowVersionForCell(ri, ci, fn) : null"
                    v-html="fn === curGroup.base_file ? escapeHtml(c.versions[fn]) : renderDiffFor(c.versions[fn], c.versions[curGroup.base_file])"></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="rp-nodiff">该行无冲突</div>
        </div>
      </div>
      <div class="rp-apply-btns">
        <span class="rp-apply-label">应用版本：</span>
        <button v-for="fn in curGroup.files" :key="fn" class="btn-accept-ver" :class="{ 'is-base': fn === curGroup.base_file }" @click="applyRowPreviewVersion(fn)" :title="'将 ' + versionLabel(fn) + ' 应用到所有选中行'">
          {{ versionLabel(fn) }}<span v-if="fn === curGroup.base_file" class="ver-tag">基准</span>
        </button>
      </div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="closeRowPreview" title="关闭行预览弹窗">取消</button>
      </div>
    </div>
  </div>

  <!-- merge 文件夹文件选择弹窗 -->
  <div v-if="showFolderPicker" class="modal-bg" @click="closeFolderPicker">
    <div class="modal folder-picker-modal" @click.stop>
      <h3>从 merge 文件夹加载 — 选择要比对的文件</h3>
      <p class="preview-desc">勾选需要参与比对的 Excel 文件（至少2个，需同名前缀的多版本）。默认全选。</p>
      <label class="trunk-mode-cb" title="开启后自动从 merge/trunk/ 注入同名基准，检测 trunk 有但衍生缺失的行（P0 漏行），并阻断导出直至补回">
        <input type="checkbox" v-model="trunkMode" /> 漏行检测模式（trunk 基准自动注入，仅需选 1 个衍生文件）
      </label>
      <div class="fp-toolbar">
        <button class="btn-panel" @click="toggleAllFolderFiles" title="切换全选/取消全选">
          {{ folderSelected.size === folderFiles.length && folderFiles.length ? '取消全选' : '全选' }}
        </button>
        <span class="fp-count">已选 {{ folderSelected.size }} / {{ folderFiles.length }}</span>
        <span v-if="folderError" class="error-msg">{{ folderError }}</span>
      </div>
      <div class="fp-list">
        <div v-if="folderLoading" class="fp-empty">加载中...</div>
        <div v-else-if="!folderFiles.length" class="fp-empty">merge 文件夹中没有可比对的 Excel 文件</div>
        <div v-else v-for="(fs, prefix) in folderGroups" :key="prefix" class="fp-group">
          <div class="fp-group-hd">
            <span class="fp-group-name">{{ prefix }}</span>
            <label class="fp-base-sel">基准文件：
              <select v-model="folderBaseMap[prefix]">
                <option v-for="name in fs" :key="name" :value="name">{{ name }}</option>
              </select>
            </label>
          </div>
          <label v-for="name in fs" :key="name" class="fp-item" :class="{ active: folderSelected.has(name), 'is-base': folderBaseMap[prefix] === name }">
            <input type="checkbox" :checked="folderSelected.has(name)" @change="toggleFolderFile(name)" />
            <span class="fp-name">{{ name }}</span>
            <span v-if="folderBaseMap[prefix] === name" class="fp-base-tag">基准</span>
          </label>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="closeFolderPicker" title="取消并关闭文件夹选择">取消</button>
        <button class="btn-confirm-accept" :disabled="(trunkMode ? folderSelected.size < 1 : folderSelected.size < 2) || folderLoading" @click="compareFromFolder" title="加载勾选的文件并开始比对">加载选中并比对</button>
      </div>
    </div>
  </div>

  <!-- 三阶段合并选择弹窗 -->
  <div v-if="showStagePicker" class="modal-bg" @click="closeStagePicker">
    <div class="modal folder-picker-modal" @click.stop>
      <h3>三阶段合并 — 阶段1：合并生产者多次提交</h3>
      <p class="preview-desc">选择生产者子目录与表分组，把其多次提交（{table}_1..N）合并成中间版本。解决全部「提交间冲突」后产出中间版本，再进入阶段2 跨生产者综合、阶段3 合回 trunk。三阶段隔离，可分次进行。</p>
      <div class="stage-form">
        <label>生产者：
          <select v-model="stageBranch">
            <option v-for="b in stageBranches" :key="b.branch" :value="b.branch">{{ b.branch }}</option>
          </select>
        </label>
        <label>表分组：
          <select v-model="stageGroup">
            <option v-for="g in stageGroupOptions" :key="g" :value="g">{{ g }}</option>
          </select>
        </label>
      </div>

      <!-- 基准 / 增量模式选择 -->
      <div v-if="stageStatus" class="stage-base-cfg">
        <template v-if="stageStatus.intermediate_exists && stageStatus.new_commits.length">
          <label class="stage-mode-opt">
            <input type="radio" :value="true" v-model="stageIncremental" />
            增量合入新提交（推荐）
            <span class="mode-hint">base = 已产出中间版本，仅合并 {{ stageStatus.new_commits.length }} 个新提交（{{ stageStatus.new_commits.join('、') }}），不重解已解决冲突</span>
          </label>
          <label class="stage-mode-opt">
            <input type="radio" :value="false" v-model="stageIncremental" />
            全量重合
            <span class="mode-hint">重新合并全部 {{ stageStatus.commits.length }} 个提交</span>
          </label>
        </template>
        <template v-else-if="stageStatus.intermediate_exists">
          <p class="mode-hint">已产出中间版本且无新提交。可全量重合（base 可选）覆盖重来。</p>
        </template>
        <label v-if="!stageIncremental" class="stage-base-sel">基准提交：
          <select v-model="stageBaseFile">
            <option v-for="c in stageStatus.commits" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <div v-if="!stageIncremental" class="stage-derived">
          <span class="derived-label">参与合并的衍生文件（与基准合并）：</span>
          <div class="derived-list">
            <label v-for="c in stageStatus.commits.filter(x => x !== stageBaseFile)" :key="c" class="derived-item">
              <input type="checkbox" :checked="stageDerived.has(c)" @change="toggleDerived(c)" /> {{ c }}
            </label>
          </div>
          <span class="derived-hint">已选 {{ stageDerived.size }} 个（不含基准 {{ stageBaseFile }}）</span>
        </div>
      </div>

      <div v-if="stageMsg" class="error-msg">{{ stageMsg }}</div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="closeStagePicker" title="取消并关闭">取消</button>
        <button class="btn-confirm-accept" :disabled="!stageBranch || !stageGroup || stageBusy || (!stageIncremental && stageDerived.size < 1)" @click="runStage1Compare" title="加载该生产者该分组的多次提交并开始阶段1 比对">{{ stageBusy ? '比对中...' : (stageIncremental ? '开始增量比对' : '开始阶段1 比对') }}</button>
      </div>
    </div>
  </div>

  <!-- 阶段3 合回 trunk 前的 --stat 摘要确认弹窗 -->
  <div v-if="showApplyConfirm" class="modal-bg" @click="showApplyConfirm = false">
    <div class="modal apply-confirm-modal" @click.stop>
      <h3>确认合回 trunk — {{ stageBranch }} / {{ stageGroup }}</h3>
      <p class="preview-desc">以下改动将写入 trunk（版本化产出 {table}_{下一版本}.xlsx，不覆盖主文件）。请核对无误后确认。</p>
      <div class="apply-stat">
        <div class="stat-item stat-changed"><span class="stat-num">{{ stageApplyStat.changed }}</span><span class="stat-label">变更行</span></div>
        <div class="stat-item stat-insert"><span class="stat-num">{{ stageApplyStat.inserted }}</span><span class="stat-label">新增行</span></div>
        <div class="stat-item stat-delete"><span class="stat-num">{{ stageApplyStat.deleted }}</span><span class="stat-label">删除行</span></div>
        <div class="stat-item stat-resolved"><span class="stat-num">{{ stageApplyStat.resolved }}</span><span class="stat-label">已解冲突</span></div>
      </div>

      <div class="apply-diff-wrap">
        <div class="apply-diff-head">改动明细（共 {{ stageApplyDiff.total }} 项<span v-if="stageApplyDiff.truncated">，仅列前 {{ stageApplyDiffCap }} 项</span>）</div>
        <div v-if="!stageApplyDiff.rows.length" class="apply-diff-empty">无内容改动（可能仅结构或空合回）</div>
        <table v-else class="apply-diff-table">
          <thead><tr><th>类型</th><th>Sheet</th><th>主键</th><th>列</th><th>原值</th><th></th><th>新值</th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in stageApplyDiff.rows" :key="i" :class="'diff-' + d.kind">
              <td class="dc-kind"><span :class="'kind-badge kind-' + d.kind">{{ kindText(d.kind) }}</span></td>
              <td class="dc-sheet">{{ d.sheet }}</td>
              <td class="dc-key">{{ d.key }}</td>
              <td class="dc-col">{{ d.col }}</td>
              <td class="dc-from"><span v-if="hasVal(d.from)" class="dv-old">{{ d.from }}</span><span v-else class="dv-none">—</span></td>
              <td class="dc-arrow">→</td>
              <td class="dc-to"><span v-if="hasVal(d.to)" class="dv-new">{{ d.to }}</span><span v-else class="dv-none">—</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="modal-actions">
        <button class="btn-close-modal" @click="showApplyConfirm = false" title="取消，不写入 trunk">取消</button>
        <button class="btn-confirm-accept" :disabled="stageBusy" @click="runStage3Apply" title="确认将上述改动版本化写回 trunk">{{ stageBusy ? '合回中...' : '确认合回 trunk' }}</button>
      </div>
    </div>
  </div>

  <!-- 向导最终合回汇总确认弹窗 -->
  <div v-if="wizShowSummary" class="modal-bg" @click="wizShowSummary = false">
    <div class="modal apply-confirm-modal" @click.stop>
      <h3>确认合回全部 — {{ wizBranch }}（{{ wizConsGroups.length }} 个分组）</h3>
      <p class="preview-desc">下列改动将版本化写入 trunk（各分组分别产出 {table}_{下一版本}.xlsx，不覆盖主文件）。确认后依次合回全部分组。</p>
      <div class="apply-stat">
        <div class="stat-item stat-changed"><span class="stat-num">{{ wizSummaryStat.changed }}</span><span class="stat-label">变更行</span></div>
        <div class="stat-item stat-insert"><span class="stat-num">{{ wizSummaryStat.inserted }}</span><span class="stat-label">新增行</span></div>
        <div class="stat-item stat-delete"><span class="stat-num">{{ wizSummaryStat.deleted }}</span><span class="stat-label">删除行</span></div>
        <div class="stat-item stat-resolved"><span class="stat-num">{{ wizSummaryStat.resolved }}</span><span class="stat-label">已解冲突</span></div>
      </div>
      <div class="apply-diff-wrap">
        <div class="apply-diff-head">改动明细（共 {{ wizSummaryDiff.total }} 项<span v-if="wizSummaryDiff.truncated">，仅列前 {{ wizSummaryDiffCap }} 项</span>）</div>
        <div v-if="!wizSummaryDiff.rows.length" class="apply-diff-empty">无内容改动（可能仅结构或空合回）</div>
        <table v-else class="apply-diff-table">
          <thead><tr><th>类型</th><th>分组</th><th>Sheet</th><th>主键</th><th>列</th><th>原值</th><th></th><th>新值</th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in wizSummaryDiff.rows" :key="i" :class="'diff-' + d.kind">
              <td class="dc-kind"><span :class="'kind-badge kind-' + d.kind">{{ kindText(d.kind) }}</span></td>
              <td class="dc-sheet">{{ d.group }}</td>
              <td class="dc-sheet">{{ d.sheet }}</td>
              <td class="dc-key">{{ d.key }}</td>
              <td class="dc-col">{{ d.col }}</td>
              <td class="dc-from"><span v-if="hasVal(d.from)" class="dv-old">{{ d.from }}</span><span v-else class="dv-none">—</span></td>
              <td class="dc-arrow">→</td>
              <td class="dc-to"><span v-if="hasVal(d.to)" class="dv-new">{{ d.to }}</span><span v-else class="dv-none">—</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="wizShowSummary = false" title="取消，不写入 trunk">取消</button>
        <button class="btn-confirm-accept" :disabled="wizBusy" @click="wizApplyAll" title="依次将各分组改动版本化写回 trunk">{{ wizBusy ? '合回中...' : '确认合回全部' }}</button>
      </div>
    </div>
  </div>

  <!-- 批量合回 trunk 面板 -->
  <div v-if="showBatchPanel" class="modal-bg" @click="closeBatchPanel">
    <div class="modal batch-modal" @click.stop>
      <h3>批量合回 trunk</h3>
      <p class="preview-desc">下列为已产出的综合版本（阶段2）。无冲突/漏行的分组可勾选后一键合回（版本化产出，不覆盖主文件）；有冲突的需单独进入「三阶段合并」阶段3 人工解决。</p>
      <div v-if="batchBusy" class="batch-loading">处理中...</div>
      <div v-else-if="!batchItems.length" class="apply-diff-empty">暂无待合回的综合版本（请先在「三阶段合并」中产出阶段2 综合版本）。</div>
      <table v-else class="batch-table">
        <thead><tr><th></th><th>分组 / 生产者</th><th>状态</th><th>变更</th><th>新增</th><th>删除</th><th>冲突</th><th>漏行</th></tr></thead>
        <tbody>
          <tr v-for="it in batchItems" :key="batchKey(it)" :class="{ 'batch-blocked': !it.ready || !it.trunk_exists }">
            <td><input type="checkbox" :disabled="!it.ready || !it.trunk_exists" :checked="batchSel.has(batchKey(it))" @change="toggleBatchSel(it)" /></td>
            <td class="bt-name">{{ it.group_name }}<span class="bt-producers">（{{ (it.producers || []).join('、') || '—' }}）</span><div v-if="it.staleness" class="bt-stale">⚠ {{ it.staleness }}</div></td>
            <td>
              <span v-if="!it.trunk_exists" class="bt-tag bt-err">无 trunk 基准</span>
              <span v-else-if="it.ready" class="bt-tag bt-ok">可一键合回</span>
              <span v-else class="bt-tag bt-warn">需人工解决</span>
            </td>
            <td>{{ it.changed }}</td><td>{{ it.inserted }}</td><td>{{ it.deleted }}</td>
            <td :class="{ 'bt-hot': it.conflicts }">{{ it.conflicts }}</td>
            <td :class="{ 'bt-hot': it.missing }">{{ it.missing }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="batchResult" class="batch-result">{{ batchResult }}</div>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="closeBatchPanel" title="关闭">关闭</button>
        <button class="btn-confirm-accept" :disabled="batchBusy || !batchSel.size" @click="runBatchApply" :title="batchSel.size ? '批量合回选中的分组' : '请勾选至少一个可合回的分组'">{{ batchBusy ? '合回中...' : `批量合回选中（${batchSel.size}）` }}</button>
      </div>
    </div>
  </div>

  <!-- R13: 比对历史面板（近 24h 会话，只读） -->
  <div v-if="showCompareHistory" class="modal-bg" @click="closeCompareHistoryPanel">
    <div class="modal cmphist-modal" @click.stop>
      <h3>比对历史</h3>
      <p class="preview-desc">近 24 小时的比对会话记录（最新在前）。点击"回填查看"可重新加载该会话的比对结果（只读模式，可重新导出）。</p>
      <div v-if="compareHistoryBusy" class="batch-loading">加载中...</div>
      <div v-else-if="!compareHistoryEntries.length" class="apply-diff-empty">暂无比对记录。</div>
      <table v-else class="batch-table audit-table">
        <thead><tr><th>时间</th><th>分组</th><th>基准</th><th>衍生文件</th><th>Sheet 数</th><th>剩余冲突</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="(e, i) in compareHistoryEntries" :key="i" class="audit-row">
            <td class="at-time">{{ e.ts.replace('T', ' ') }}</td>
            <td>{{ e.group_name || '—' }}</td>
            <td class="bt-name">{{ e.base_file || '—' }}</td>
            <td class="bt-name">{{ (e.derived_files || []).join(', ') || '—' }}</td>
            <td>{{ e.sheets_count }}</td>
            <td :class="{ 'bt-hot': e.conflicts_remaining }">{{ e.conflicts_remaining }}</td>
            <td><span class="bt-tag" :class="e.exported ? 'bt-ok' : 'bt-warn'">{{ e.exported ? '已导出' : '未导出' }}</span></td>
            <td><button class="btn-cmphist-restore" @click="restoreCompareSession(e.session_id)" title="回填该会话的比对结果（只读）">回填查看</button></td>
          </tr>
        </tbody>
      </table>
      <div class="batch-foot">
        <button class="btn-close-modal" @click="closeCompareHistoryPanel" title="关闭">关闭</button>
      </div>
    </div>
  </div>

  <!-- 合回历史（审计）面板 -->
  <div v-if="showMergeHistory" class="modal-bg" @click="closeAuditPanel">
    <div class="modal audit-modal" @click.stop>
      <h3>合回历史</h3>
      <p class="preview-desc">每次合回 trunk 的审计记录（最新在前）。点击一行展开查看具体改动明细。</p>
      <div v-if="auditBusy" class="batch-loading">加载中...</div>
      <div v-else-if="!auditEntries.length" class="apply-diff-empty">暂无合回记录。</div>
      <table v-else class="batch-table audit-table">
        <thead><tr><th>时间</th><th>分组 / 生产者</th><th>产出</th><th>方式</th><th>变更</th><th>新增</th><th>删除</th></tr></thead>
        <tbody>
          <template v-for="(e, i) in auditEntries" :key="i">
            <tr class="audit-row" @click="toggleAuditRow(i)">
              <td class="at-time">{{ e.time.replace('T', ' ') }}</td>
              <td class="bt-name">{{ e.group }}<span class="bt-producers">（{{ (e.producers || []).join('、') || '—' }}）</span></td>
              <td><span class="at-out">{{ e.output }}</span></td>
              <td><span class="bt-tag" :class="e.mode === 'batch' ? 'bt-ok' : 'bt-warn'">{{ e.mode === 'batch' ? '批量' : '单次' }}</span></td>
              <td>{{ e.stats.changed }}</td><td>{{ e.stats.inserted }}</td><td>{{ e.stats.deleted }}</td>
            </tr>
            <tr v-if="auditOpenIdx === i" class="audit-detail">
              <td colspan="7">
                <div class="apply-diff-head">改动明细（{{ e.changes.length }} 项<span v-if="e.changes_truncated">，已截断</span>）</div>
                <div v-if="!e.changes.length" class="apply-diff-empty">无内容改动</div>
                <table v-else class="apply-diff-table">
                  <thead><tr><th>类型</th><th>Sheet</th><th>主键</th><th>列</th><th>原值</th><th></th><th>新值</th></tr></thead>
                  <tbody>
                    <tr v-for="(d, j) in e.changes" :key="j" :class="'diff-' + d.kind">
                      <td class="dc-kind"><span :class="'kind-badge kind-' + d.kind">{{ kindText(d.kind) }}</span></td>
                      <td class="dc-sheet">{{ d.sheet }}</td><td class="dc-key">{{ d.key }}</td>
                      <td class="dc-col">{{ d.col }}</td>
                      <td class="dc-from"><span v-if="hasVal(d.from)" class="dv-old">{{ d.from }}</span><span v-else class="dv-none">—</span></td>
                      <td class="dc-arrow">→</td>
                      <td class="dc-to"><span v-if="hasVal(d.to)" class="dv-new">{{ d.to }}</span><span v-else class="dv-none">—</span></td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div class="modal-actions">
        <button class="btn-close-modal" @click="closeAuditPanel" title="关闭">关闭</button>
      </div>
    </div>
  </div>
</div>
</template>

<style scoped>
.diff-view { display: flex; flex-direction: column; height: 100%; overflow: auto; background: var(--bg-base); color: var(--text-primary); }

.toolbar {
  display: flex; align-items: center; gap: 8px; padding: 8px 16px;
    background: var(--bg-card); border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.btn-up {
  padding: 6px 14px; border-radius: 6px; background: var(--accent); color: #fff;
  cursor: pointer; font-size: 0.85rem; position: relative; overflow: hidden; border: none;
}
.btn-up input { position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.file-info { font-size: 0.8rem; color: var(--text-muted); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn-del { padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; font-size: 0.8rem; }
.btn-go { padding: 6px 16px; border: none; border-radius: 6px; background: var(--danger); color: #fff; cursor: pointer; font-size: 0.85rem; }
.btn-folder { padding: 6px 14px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.trunk-mode-cb { display: flex; align-items: center; gap: 6px; padding: 6px 10px; margin: 6px 0; background: rgba(211, 47, 47, 0.08); border: 1px solid var(--danger); border-radius: 6px; color: var(--danger); font-size: 0.85rem; cursor: pointer; }
.trunk-mode-cb input { accent-color: var(--danger); }
.btn-folder:hover:not(:disabled) { background: var(--accent-soft); }
.btn-folder:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-stage { padding: 6px 14px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.btn-stage:hover:not(:disabled) { background: var(--border); }
.btn-stage:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-batch { padding: 6px 14px; border: 1px solid var(--success); border-radius: 6px; background: var(--success-soft); color: var(--success); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.btn-batch:hover:not(:disabled) { background: var(--success-soft); }
.btn-batch:disabled { opacity: 0.5; cursor: not-allowed; }
.batch-modal { min-width: 640px; max-width: 860px; }
.batch-loading { padding: 20px; text-align: center; color: var(--text-muted); }
.batch-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 10px 0; }
.batch-table th { background: var(--bg-hover); color: var(--text-secondary); font-weight: 600; padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border); }
.batch-table td { padding: 6px 8px; border-bottom: 1px solid var(--bg-hover); }
.batch-table tr.batch-blocked { background: var(--bg-stripe); color: var(--text-muted); }
.bt-name { font-weight: 600; color: var(--text-primary); }
.bt-stale { font-weight: 400; font-size: 0.72rem; color: var(--warning); margin-top: 2px; }
.bt-tag { padding: 2px 8px; border-radius: 10px; font-size: 0.74rem; }
.bt-ok { background: var(--success-soft); color: var(--success); }
.bt-warn { background: var(--warning-soft); color: var(--warning); }
.bt-err { background: var(--danger-soft); color: var(--danger); }
.bt-hot { color: var(--danger); font-weight: 700; }
.batch-result { margin: 6px 0; padding: 8px 10px; border-radius: 6px; background: var(--bg-base); font-size: 0.82rem; color: var(--text-primary); }
.btn-audit { padding: 6px 14px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.btn-audit:hover:not(:disabled) { background: var(--accent-soft); }
.btn-audit:disabled { opacity: 0.5; cursor: not-allowed; }
/* R13: 比对历史 */
.btn-cmphist { padding: 6px 14px; border: 1px solid var(--success); border-radius: 6px; background: var(--success-soft); color: var(--success); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.btn-cmphist:hover { background: var(--success-soft); }
/* ── 工具栏"更多"下拉菜单（归并低频操作）── */
.more-wrap { position: relative; display: inline-block; }
.btn-more { padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-size: 0.85rem; font-weight: 600; }
.btn-more:hover, .btn-more.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.more-menu { position: absolute; top: calc(100% + 4px); right: 0; min-width: 168px; display: flex; flex-direction: column; gap: 2px; padding: 4px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); box-shadow: 0 6px 20px rgba(0,0,0,0.12); z-index: 40; }
.more-item { padding: 7px 12px; border: none; border-radius: 4px; background: transparent; color: var(--text-primary); cursor: pointer; font-size: 0.84rem; text-align: left; white-space: nowrap; }
.more-item:hover:not(:disabled) { background: var(--bg-base); }
.more-item:disabled { opacity: 0.5; cursor: not-allowed; }
.more-backdrop { position: fixed; inset: 0; z-index: 30; }
.btn-cmphist-restore { padding: 3px 10px; border: 1px solid var(--success); border-radius: 5px; background: var(--success-soft); color: var(--success); cursor: pointer; font-size: 0.78rem; }
.btn-cmphist-restore:hover { background: var(--success); color: #fff; }
.cmphist-modal { width: 92vw; max-width: 1400px; min-width: 760px; max-height: 88vh; display: flex; flex-direction: column; }
.cmphist-modal .batch-table { display: block; overflow-y: auto; flex: 1; min-height: 0; font-size: 0.86rem; }
.cmphist-modal .batch-table th, .cmphist-modal .batch-table td { padding: 8px 10px; }
.audit-row { cursor: pointer; }
.audit-row:hover { background: var(--bg-stripe); }
.at-time { white-space: nowrap; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.at-out { font-family: monospace; font-size: 0.8rem; color: var(--success); }
.audit-detail > td { background: var(--bg-stripe); padding: 12px 14px; }
.audit-detail .apply-diff-table { max-height: 420px; }
.audit-modal { width: 96vw; max-width: 1600px; min-width: 820px; max-height: 94vh; display: flex; flex-direction: column; }
.audit-modal .batch-table { display: block; overflow-y: auto; flex: 1; min-height: 0; font-size: 0.86rem; }
.audit-modal .batch-table th, .audit-modal .batch-table td { padding: 8px 10px; }
.apply-confirm-modal { min-width: 560px; max-width: 760px; }
.apply-stat { display: flex; gap: 12px; margin: 16px 0; }
.stat-item { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 12px 8px; border-radius: 8px; background: var(--bg-base); border: 1px solid var(--border); }
.stat-num { font-size: 1.6rem; font-weight: 700; line-height: 1; }
.stat-label { font-size: 0.78rem; color: var(--text-muted); }
.stat-changed .stat-num { color: var(--warning); }
.stat-insert .stat-num { color: var(--success); }
.stat-delete .stat-num { color: var(--text-muted); }
.stat-resolved .stat-num { color: var(--danger); }
.apply-diff-wrap { margin: 4px 0 12px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.apply-diff-head { padding: 6px 10px; background: var(--bg-base); font-size: 0.8rem; color: var(--text-secondary); border-bottom: 1px solid var(--border); }
.apply-diff-empty { padding: 14px; text-align: center; color: var(--text-muted); font-size: 0.85rem; }
.apply-diff-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; display: block; max-height: 420px; overflow-y: auto; }
.apply-diff-table thead th { position: sticky; top: 0; background: var(--bg-hover); color: var(--text-secondary); font-weight: 600; padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border); }
.apply-diff-table td { padding: 6px 10px; border-bottom: 1px solid var(--bg-hover); vertical-align: middle; word-break: break-all; }
.apply-diff-table .dc-key { font-weight: 600; color: var(--text-primary); white-space: nowrap; }
.apply-diff-table .dc-col { color: var(--text-muted); white-space: nowrap; }
.apply-diff-table .dc-arrow { color: var(--text-placeholder); text-align: center; font-weight: 700; }
/* 值色块：红删 / 绿增，一眼看出旧→新 */
.dv-old { display: inline-block; padding: 1px 7px; border-radius: 4px; background: var(--diff-del-bg); border: 1px solid var(--border); color: var(--diff-del); text-decoration: line-through; text-decoration-color: var(--diff-del); }
.dv-new { display: inline-block; padding: 1px 7px; border-radius: 4px; background: var(--diff-add-bg); border: 1px solid var(--border); color: var(--diff-add); font-weight: 600; }
.dv-none { color: var(--text-placeholder); }
/* 改动类型徽章 */
.kind-badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.72rem; font-weight: 600; white-space: nowrap; }
.kind-change { background: var(--warning-soft); color: var(--warning); }
.kind-insert { background: var(--success-soft); color: var(--success); }
.kind-delete { background: var(--bg-hover); color: var(--text-muted); }
.kind-resolved { background: var(--danger-soft); color: var(--danger); }
.apply-diff-table tr.diff-insert .dc-key { color: var(--success); }
.apply-diff-table tr.diff-delete .dc-key { color: var(--text-muted); }
.apply-diff-table tr.diff-resolved { background: var(--bg-card)af0; }
.stage-base-cfg { margin: 12px 0; padding: 12px; border-radius: 8px; background: var(--accent-soft); border: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
.stage-mode-opt { display: flex; align-items: center; gap: 6px; font-size: 0.88rem; color: var(--text-primary); cursor: pointer; flex-wrap: wrap; }
.stage-mode-opt input { accent-color: var(--accent); }
.mode-hint { font-size: 0.76rem; color: var(--text-muted); flex-basis: 100%; padding-left: 22px; }
.stage-base-sel { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; color: var(--text-primary); }
.stage-base-sel select { padding: 3px 6px; border: 1px solid var(--border); border-radius: 4px; }
.stage-derived { display: flex; flex-direction: column; gap: 4px; }
.derived-label { font-size: 0.82rem; color: var(--text-primary); }
.derived-list { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 6px 8px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; }
.derived-item { display: flex; align-items: center; gap: 4px; font-size: 0.82rem; color: var(--text-primary); cursor: pointer; }
.derived-item input { accent-color: var(--accent); }
.derived-hint { font-size: 0.74rem; color: var(--text-muted); }

/* ── 三阶段合并动作栏：stage1 紫 / stage2 橙 / stage3 蓝 ── */
.stage-bar { display: flex; align-items: center; gap: 12px; padding: 8px 16px; border-bottom: 1px solid var(--border); font-size: 0.85rem; flex-wrap: wrap; }
.stage-bar-1 { background: var(--accent-soft); border-bottom-color: var(--border); }
.wiz-switch-sep { color: var(--text-placeholder); margin: 0 4px; }
.wiz-switch-label { font-size: 0.8rem; color: var(--text-secondary); margin-right: 4px; }
.btn-wiz-switch { font-size: 0.8rem; padding: 3px 10px; border: 1px solid var(--accent); background: var(--bg-card); color: var(--accent); border-radius: 4px; cursor: pointer; margin-right: 4px; }
.btn-wiz-switch:hover:not(:disabled) { background: var(--accent); color: #fff; }
.btn-wiz-switch:disabled { opacity: 0.5; cursor: not-allowed; }
.stage-bar-2 { background: var(--warning-soft); border-bottom-color: var(--border-strong); }
.stage-bar-3 { background: var(--info-soft); border-bottom-color: var(--border-strong); }
.stage-badge { font-weight: 700; color: var(--accent); }
.stage-bar-2 .stage-badge { color: var(--warning); }
.stage-bar-3 .stage-badge { color: var(--info); }
.stage-origin { font-weight: 600; font-size: 0.8rem; }
.origin-inter { color: var(--accent); }
.origin-cross { color: var(--warning); }
.origin-back { color: var(--accent); }
.stage-ctx { font-size: 0.8rem; color: var(--text-secondary); background: var(--bg-card); padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border); }
.btn-stage-act { padding: 6px 16px; border: none; border-radius: 6px; background: var(--accent); color: #fff; cursor: pointer; font-weight: 600; font-size: 0.85rem; }
.btn-stage-act:hover:not(:disabled) { background: var(--accent); }
.btn-stage-act:disabled { background: var(--border); cursor: not-allowed; opacity: 0.8; }
/* stage2 综合按钮用橙调 */
.stage-bar-2 .btn-stage-act:not(.stage-next) { background: var(--warning); }
.stage-bar-2 .btn-stage-act:not(.stage-next):hover:not(:disabled) { background: var(--warning); }
.stage-bar-2 .btn-stage-act:not(.stage-next):disabled { background: var(--warning-soft); }
.btn-stage-act.stage-next { background: var(--accent); }
.btn-stage-act.stage-next:hover:not(:disabled) { background: var(--accent-hover); }
.btn-stage-exit { padding: 5px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; font-size: 0.8rem; }

/* ── 三阶段引导向导 ── */
.wiz-wrap { max-width: 920px; margin: 0 auto; padding: 8px 0 40px; }
.wiz-head { display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--accent-soft); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 14px; }
.wiz-steps { display: flex; align-items: center; gap: 8px; }
.wiz-step { font-size: 0.82rem; color: var(--text-placeholder); padding: 3px 10px; border-radius: 20px; background: var(--bg-card); border: 1px solid var(--border); }
.wiz-step.wiz-active { color: #fff; background: var(--accent); border-color: var(--accent); font-weight: 600; }
.wiz-step.wiz-passed { color: var(--accent); border-color: var(--border); }
.wiz-arr { color: var(--text-placeholder); }
.wiz-ctx { font-size: 0.8rem; color: var(--text-secondary); background: var(--bg-card); padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); }
.wiz-msg { padding: 8px 14px; background: var(--success-soft); color: var(--success); font-size: 0.82rem; border: 1px solid var(--success-soft); border-radius: 8px; margin-bottom: 12px; }
.wiz-title { margin: 0 0 6px; font-size: 1.05rem; color: var(--text-primary); }
.wiz-desc { margin: 0 0 16px; font-size: 0.85rem; color: var(--text-muted); line-height: 1.5; }
.wiz-empty { padding: 24px; text-align: center; color: var(--text-placeholder); font-size: 0.85rem; }
.wiz-branch-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.wiz-branch-card { text-align: left; padding: 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-card); cursor: pointer; display: flex; flex-direction: column; gap: 6px; transition: all .15s; }
.wiz-branch-card:hover:not(:disabled) { border-color: var(--accent); box-shadow: 0 6px 18px rgba(108,92,231,0.12); transform: translateY(-2px); }
.wiz-branch-card:disabled { opacity: 0.6; cursor: not-allowed; }
.wb-name { font-weight: 600; color: var(--text-primary); font-size: 0.95rem; }
.wb-groups { font-size: 0.8rem; color: var(--text-muted); }
.wiz-group { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; background: var(--bg-card); }
.wiz-group.wg-skip { opacity: 0.65; background: var(--bg-stripe); }
.wiz-group.wg-done { border-color: var(--success-soft); background: var(--success-soft); }
.wg-head { display: flex; align-items: center; gap: 10px; }
.wg-name { font-weight: 600; color: var(--text-primary); font-size: 0.9rem; }
.wg-tag { font-size: 0.76rem; padding: 2px 8px; border-radius: 4px; }
.wg-ok { color: var(--success); background: var(--success-soft); }
.wg-muted { color: var(--text-muted); background: var(--bg-hover); }
.wg-pending { color: var(--warning); background: var(--warning-soft); }
.wg-dec { font-size: 0.82rem; color: var(--text-secondary); display: flex; align-items: center; gap: 4px; }
.wg-cfg { margin-top: 10px; padding: 10px; background: var(--accent-soft); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; gap: 8px; }
.wg-mode { font-size: 0.82rem; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.wg-base { font-size: 0.82rem; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.wg-base select { padding: 4px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); font-size: 0.82rem; min-width: 140px; }
.wg-derived { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.wg-dlabel { font-size: 0.82rem; color: var(--text-secondary); }
.wg-ditem { font-size: 0.8rem; color: var(--text-primary); display: flex; align-items: center; gap: 4px; }
.wg-go { align-self: flex-start; }
.wiz-foot { display: flex; align-items: center; gap: 10px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--bg-hover); }
.wiz-actbar { display: flex; align-items: center; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--border); }
.wiz-actbar-1 { background: var(--accent-soft); }
.wiz-actbar-2 { background: var(--warning-soft); }
.wiz-actbar-3 { background: var(--info-soft); }
.wiz-actbadge { font-size: 0.85rem; font-weight: 600; color: var(--text-primary); }
.wiz-sidehint { font-size: 0.8rem; color: var(--accent); background: var(--bg-card); padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); }

.stage-staleness { padding: 8px 16px; background: var(--warning-soft); color: var(--warning); font-size: 0.82rem; border-bottom: 1px solid var(--warning-soft); display: flex; align-items: center; gap: 6px; }
.stage-msg { padding: 8px 16px; background: var(--success-soft); color: var(--success); font-size: 0.82rem; border-bottom: 1px solid var(--success-soft); }
.onboard { display: flex; justify-content: center; padding: 48px 24px; }
.onboard-hero { max-width: 900px; width: 100%; text-align: center; }
.ob-title { font-size: 1.6rem; color: var(--text-primary); margin: 0 0 6px; }
.ob-sub { color: var(--text-muted); font-size: 0.92rem; margin: 0 0 28px; }
.ob-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.ob-card { text-align: left; padding: 20px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg-card); cursor: pointer; transition: all .15s; display: block; }
.ob-card:hover { border-color: var(--accent); box-shadow: 0 6px 18px rgba(108,92,231,0.12); transform: translateY(-2px); }
.ob-primary { border-color: var(--border); background: linear-gradient(180deg,var(--accent-soft), var(--bg-card)); }
.ob-ico { font-size: 1.8rem; margin-bottom: 8px; }
.ob-h { font-size: 1rem; font-weight: 700; color: var(--text-primary); margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
.ob-badge { font-size: 0.68rem; font-weight: 600; color: #fff; background: var(--accent); border-radius: 10px; padding: 1px 8px; }
.ob-d { font-size: 0.82rem; color: var(--text-secondary); line-height: 1.5; }
.ob-flow { margin-top: 12px; font-size: 0.8rem; color: var(--accent); font-weight: 600; }
.ob-arrow { color: var(--text-placeholder); }
.ob-links { margin-top: 24px; display: flex; justify-content: center; gap: 12px; }
.ob-link { padding: 7px 16px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-size: 0.84rem; }
.ob-link:hover { background: var(--bg-base); border-color: var(--text-placeholder); }
.stage-guide { display: flex; align-items: center; gap: 10px; padding: 10px 16px; background: var(--info-soft); color: var(--info); font-size: 0.84rem; border-bottom: 1px solid var(--border); }
.stage-guide .guide-icon { font-size: 1rem; }
.stage-guide .guide-text { flex: 1; }
.btn-guide { padding: 5px 14px; border: none; border-radius: 6px; background: var(--accent); color: #fff; cursor: pointer; font-size: 0.82rem; font-weight: 600; white-space: nowrap; }
.btn-guide:hover { background: var(--accent-hover); }
.stage-steps { display: inline-flex; align-items: center; gap: 6px; margin-left: 4px; }
.stage-step { padding: 2px 10px; border-radius: 12px; font-size: 0.76rem; background: var(--bg-card); color: var(--text-muted); border: 1px solid var(--border); }
.stage-step.step-active { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }
.stage-step.step-done { background: var(--success-soft); color: var(--success); border-color: var(--border); }
.step-arrow { color: var(--text-placeholder); font-size: 0.8rem; }
.stage-form { display: flex; gap: 20px; align-items: center; margin: 12px 0; }
.stage-form label { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; color: var(--text-primary); }
.stage-form select { padding: 4px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-primary); font-size: 0.85rem; min-width: 140px; }
.btn-go:disabled { opacity: 0.5; }
.error-msg { color: var(--danger); font-size: 0.8rem; }
.base-config { display: flex; gap: 6px; align-items: center; }
.base-row { display: flex; gap: 4px; align-items: center; font-size: 0.8rem; color: var(--text-primary); }
.base-sel { padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-primary); font-size: 0.8rem; }

.empty-hint { padding: 32px; text-align: center; color: var(--text-muted); }

/* ── 全局进度条：所有分组处理完毕前导出按钮保持禁用 ── */
.global-bar { display: flex; align-items: center; gap: 14px; padding: 8px 16px; background: var(--success-soft); border-bottom: 1px solid var(--success-soft); font-size: 0.85rem; flex-wrap: wrap; }
.global-label { font-weight: 700; color: var(--success); }
.gu-item { font-size: 0.82rem; font-weight: 600; }
.gu-conflict { color: var(--danger); } .gu-changed { color: var(--warning); } .gu-insert { color: var(--success); } .gu-deleted { color: var(--diff-del); }
.gu-done { color: var(--success); font-weight: 700; }
.btn-export-final { padding: 7px 18px; border: none; border-radius: 6px; background: var(--success); color: #fff; cursor: pointer; font-weight: 600; font-size: 0.85rem; }
.btn-export-final:hover:not(:disabled) { background: var(--success); }
.btn-export-final:disabled { background: var(--success-soft); cursor: not-allowed; opacity: 0.8; }

.nav-bar { display: flex; gap: 4px; padding: 6px 16px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid var(--border); background: var(--bg-card); }
.nav-label { font-size: 0.8rem; color: var(--text-muted); }
.tab { position: relative; padding: 4px 14px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-size: 0.8rem; }
.tab:hover { border-color: var(--danger); color: var(--danger); }
.tab.active { background: var(--danger); color: #fff; border-color: var(--danger); }
.tab-dot { position: absolute; top: 2px; right: 2px; width: 8px; height: 8px; border-radius: 50%; box-shadow: 0 0 0 2px #fff, 0 0 4px currentColor; }
.tab-dot.dot-r { background: var(--danger); color: var(--danger); }
.tab-dot.dot-o { background: var(--warning); color: var(--warning); }
.tab-dot.dot-g { background: var(--success); color: var(--success); }
.tab-dot.dot-d { background: var(--diff-del); color: var(--diff-del); }
.tab-dot.dot-m { background: var(--danger); color: var(--danger); }
.filter-cb { font-size: 0.8rem; color: var(--text-muted); margin-left: 12px; }
.tab-badge { font-size: 0.62rem; padding: 1px 6px; border-radius: 10px; margin-left: 4px; vertical-align: middle; }
.tab-solved { background: var(--success); color: #fff; }
.filter-radio-group { display: flex; align-items: center; gap: 2px; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.filter-radio { padding: 3px 10px; font-size: 0.75rem; cursor: pointer; background: var(--bg-card); color: var(--text-secondary); display: flex; align-items: center; gap: 2px; border-right: 1px solid var(--border-soft); }
.filter-radio:last-child { border-right: none; }
.filter-radio input { display: none; }
.filter-radio:hover { background: var(--bg-base); }
.filter-radio.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.fr-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 2px; vertical-align: middle; }
.filter-radio.fr-conflict.active { background: var(--danger-soft); color: var(--danger); }
.filter-radio.fr-changed.active { background: var(--warning-soft); color: var(--warning); }
.filter-radio.fr-inserted.active { background: var(--success-soft); color: var(--success); }
.filter-radio.fr-deleted.active { background: var(--danger-soft); color: var(--diff-del); }

.stats-bar { display: flex; align-items: center; gap: 10px; padding: 6px 16px; font-size: 0.82rem; border-bottom: 1px solid var(--border); background: var(--bg-card); color: var(--text-primary); flex-wrap: wrap; }
.stats-bar button { padding: 3px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-size: 0.8rem; }
.stats-bar button:disabled { opacity: 0.3; }
.stat-resolved { color: var(--success); font-weight: 600; }
/* T9: 行内容搜索 */
.row-search-wrap { display: inline-flex; align-items: center; gap: 6px; margin-left: 4px; }
.row-search-input {
  padding: 3px 10px; border: 1px solid var(--border); border-radius: 4px;
  background: var(--bg-card); color: var(--text-primary); font-size: 0.8rem; outline: none; width: 200px;
}
.row-search-input:focus { border-color: var(--accent); }
mark.search-hit { background: var(--warning); color: #fff; padding: 0 1px; border-radius: 2px; }
.row-search-count { font-size: 0.75rem; color: var(--warning); white-space: nowrap; }
.accept-ver-label { font-size: 0.78rem; color: var(--text-muted); }
.btn-accept-ver { padding: 3px 10px; border: 1px solid var(--accent); border-radius: 4px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-size: 0.78rem; }
.btn-accept-ver:hover:not(:disabled) { border-color: var(--warning); background: var(--warning-soft); color: var(--warning); }
.btn-accept-ver.is-base { border-color: var(--success); color: var(--success); background: var(--success-soft); }
.btn-accept-ver:disabled { opacity: 0.35; cursor: not-allowed; }
.btn-smart-merge { padding: 4px 12px; border: none; border-radius: 4px; background: var(--accent); color: #fff; cursor: pointer; font-size: 0.82rem; font-weight: 600; }
.btn-smart-merge:hover:not(:disabled) { background: var(--accent-hover); }
.btn-smart-merge:disabled { opacity: 0.4; cursor: not-allowed; }
.merge-result { font-size: 0.78rem; color: var(--success); font-weight: 600; }
.ver-tag { font-size: 0.62rem; background: var(--success); color: #fff; padding: 0 4px; border-radius: 3px; margin-left: 4px; vertical-align: middle; }
.ver-tag-theirs { background: var(--accent); }
.ver-tag-ours { background: var(--accent); }
.ver-tag-cons { background: var(--warning); }
.bt-producers { font-weight: 400; font-size: 0.74rem; color: var(--text-muted); margin-left: 4px; }
.wg-producers { font-size: 0.76rem; color: var(--text-muted); margin-right: 8px; }

.done-bar { padding: 10px 16px; background: var(--success-soft); color: var(--success); text-align: center; border-bottom: 1px solid var(--success-soft); display: flex; align-items: center; justify-content: center; gap: 16px; }
.btn-jump-next { padding: 4px 14px; border: 1px solid var(--success); border-radius: 4px; background: var(--bg-card); color: var(--success); cursor: pointer; font-size: 0.8rem; font-weight: 600; }
.btn-jump-next:hover { background: var(--success); color: #fff; }

.batch-bar { display: flex; align-items: center; gap: 8px; padding: 6px 16px; background: var(--warning-soft); font-size: 0.82rem; border-bottom: 1px solid var(--warning-soft); color: var(--text-primary); }
.btn-batch { padding: 4px 12px; border: none; border-radius: 4px; background: var(--danger); color: #fff; cursor: pointer; }
.btn-batch:disabled { opacity: 0.4; }
.btn-clear-sel { padding: 4px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; }

.main-area { display: flex; flex: 1; overflow: hidden; background: var(--bg-card); }
.table-wrap { flex: 1; overflow: auto; }
.load-more-hint { padding: 8px 16px; text-align: center; font-size: 0.8rem; color: var(--text-secondary); border-top: 1px dashed var(--border); background: var(--bg-card); }
table { border-collapse: collapse; font-size: 0.78rem; width: max-content; min-width: 100%; background: var(--bg-card); }
th { position: sticky; top: 0; z-index: 1; background: var(--bg-hover); color: var(--text-primary); padding: 6px 8px; text-align: left; white-space: nowrap; border-bottom: 2px solid var(--border); font-weight: 600; }
td { padding: 4px 8px; border-bottom: 1px solid var(--border-soft); max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
tr:hover td { background: var(--bg-stripe); }
.row-inserted td { background: rgba(39, 174, 96, 0.15) !important; box-shadow: inset 0 0 0 2px var(--success) !important; }
.row-deleted td { background: rgba(241, 148, 138, 0.22) !important; box-shadow: inset 0 0 0 2px var(--diff-del) !important; text-decoration: line-through; color: var(--danger); }
.row-missing td { background: rgba(211, 47, 47, 0.18) !important; box-shadow: inset 0 0 0 2px var(--danger) !important; color: var(--danger); }
.row-sel td { background: var(--accent-soft) !important; }
.row-hd { background: var(--bg-base) !important; color: var(--text-muted); font-size: 0.75rem; position: sticky; left: 0; z-index: 2; }
.key-cell { max-width: 120px; overflow: hidden; text-overflow: ellipsis; }
.cell-conflict { background: rgba(146, 43, 33, 0.22) !important; box-shadow: inset 0 0 0 2px var(--danger) !important; cursor: pointer; font-weight: 600; color: var(--danger); }
.cell-changed { background: rgba(184, 134, 11, 0.20) !important; box-shadow: inset 0 0 0 2px var(--warning) !important; }
.cell-resolved { background: rgba(22, 160, 133, 0.12) !important; box-shadow: inset 0 0 0 2px var(--success) !important; position: relative; }
.reopen-badge {
  position: absolute; top: 1px; right: 1px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--danger); color: #fff; font-size: 0.65rem; cursor: pointer;
  z-index: 3; line-height: 1;
}
.reopen-badge:hover { background: var(--danger); transform: scale(1.15); }
.row-act-btn { padding: 1px 6px; border: 1px solid var(--border); border-radius: 3px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; font-size: 0.7rem; margin: 0 1px; }
.row-act-btn:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
.row-act-btn:disabled { opacity: 0.3; cursor: not-allowed; }
.row-tag { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 0.66rem; font-weight: 600; margin-left: 2px; }
.row-tag-ins { background: var(--success); color: #fff; }
.row-tag-src { background: var(--accent); color: #fff; }
.row-tag-del { background: var(--diff-del); color: var(--danger); }
.row-tag-missing { background: var(--danger); color: #fff; }
.btn-restore { color: var(--danger); border: 1px solid var(--danger); background: var(--bg-card); padding: 1px 6px; border-radius: 4px; font-size: 12px; cursor: pointer; }
.btn-restore:hover:not(:disabled) { background: var(--danger); color: #fff; }
.btn-restore:disabled { opacity: 0.5; cursor: wait; }
.row-tag-remap { background: var(--warning); color: #fff; }

.gutter { width: 14px; overflow-y: auto; background: var(--bg-hover); flex-shrink: 0; border-left: 1px solid var(--border); }
.gutter-row { height: 22px; cursor: pointer; }
.gutter-conflict { background: var(--danger); box-shadow: inset 0 0 0 1px var(--danger); }
.gutter-diff { background: var(--warning); box-shadow: inset 0 0 0 1px var(--warning); }

.legend { display: flex; gap: 16px; padding: 4px 16px; font-size: 0.75rem; color: var(--text-muted); border-top: 1px solid var(--border); background: var(--bg-card); flex-wrap: wrap; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; box-shadow: 0 0 4px currentColor; }
.dot-r { background: var(--danger); color: var(--danger); } .dot-o { background: var(--warning); color: var(--warning); } .dot-g { background: var(--success); color: var(--success); } .dot-d { background: var(--diff-del); color: var(--diff-del); } .dot-m { background: var(--danger); color: var(--danger); }
.stat-missing { color: var(--danger); font-weight: 600; }
.alert-banner { display: flex; align-items: flex-start; gap: 8px; padding: 8px 12px; margin: 4px 0; border-radius: 6px; font-size: 13px; line-height: 1.5; }
.alert-icon { font-size: 16px; flex-shrink: 0; }
.alert-text { flex: 1; }
.alert-item { margin-left: 6px; padding: 0 4px; background: rgba(0,0,0,0.06); border-radius: 3px; }
.alert-missing-row { background: rgba(211, 47, 47, 0.12); border: 1px solid var(--danger); color: var(--danger); }
.alert-missing-sheet { background: rgba(183, 28, 28, 0.08); border: 1px solid var(--danger); color: var(--danger); }
.alert-structure-diff { background: rgba(184, 134, 11, 0.1); border: 1px solid var(--warning); color: var(--warning); }

.modal-bg { position: fixed; inset: 0; background: rgba(44, 62, 80, 0.45); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; min-width: 400px; max-width: 600px; max-height: 80vh; overflow: auto; color: var(--text-primary); box-shadow: 0 8px 32px rgba(44,62,80,0.2); }
.modal h3 { color: var(--danger); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.modal-type-badge { font-size: 0.75rem; font-weight: 600; padding: 2px 10px; border-radius: 4px; }
.mod-conflict { background: var(--danger-soft); color: var(--danger); }
.mod-changed { background: var(--warning-soft); color: var(--warning); }
.changed-hint { background: var(--warning-soft); border: 1px solid var(--warning-soft); color: var(--warning); padding: 6px 10px; border-radius: 4px; font-size: 0.78rem; margin-bottom: 10px; }
.ver-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 6px; cursor: pointer; background: var(--bg-card); }
.ver-item:hover { border-color: var(--danger); background: var(--danger-soft); }
.ver-name { font-size: 0.8rem; color: var(--text-muted); min-width: 100px; }
.ver-base { color: var(--success); font-weight: bold; }
.ver-val { font-size: 0.82rem; color: var(--text-primary); flex: 1; }
.btn-acc { padding: 4px 12px; border: none; border-radius: 4px; background: var(--danger); color: #fff; cursor: pointer; }
.modal-actions { display: flex; gap: 8px; margin-top: 12px; justify-content: flex-end; }
.btn-skip { padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; }
.btn-close-modal { padding: 6px 14px; border: none; border-radius: 6px; background: var(--bg-hover); color: var(--text-secondary); cursor: pointer; }
/* M13: AI 冲突建议 */
.btn-ai { padding: 6px 14px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-weight: 600; }
.btn-ai:disabled { opacity: 0.55; cursor: wait; }
.ai-panel { margin-top: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--accent-soft); padding: 10px 12px; }
.ai-loading { color: var(--accent); font-size: 0.82rem; }
.ai-error { color: var(--danger); font-size: 0.82rem; }
.ai-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ai-badge { background: var(--accent); color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.74rem; font-weight: 600; }
.ai-ver { color: var(--accent); font-weight: 600; font-size: 0.82rem; }
.ai-conf { color: var(--text-muted); font-size: 0.74rem; margin-left: auto; }
.ai-reason { margin: 6px 0 8px; color: var(--accent); font-size: 0.8rem; line-height: 1.5; }
.ai-actions { display: flex; align-items: center; gap: 10px; }
.btn-ai-adopt { padding: 5px 14px; border: none; border-radius: 6px; background: var(--accent); color: #fff; cursor: pointer; font-weight: 600; font-size: 0.82rem; }
.btn-ai-adopt:hover { background: var(--accent-hover); }
.ai-tip { color: var(--text-muted); font-size: 0.72rem; }


/* 推荐标注 */
.kbd-hint { font-size: 0.72rem; color: var(--text-muted); margin-right: auto; }

/* 单元格 diff 可视化 */
.ver-val :deep(.diff-del) { color: var(--danger); text-decoration: line-through; background: rgba(231,76,60,0.10); }
.ver-val :deep(.diff-add) { color: var(--success); background: rgba(46,204,113,0.12); }
.ver-val :deep(.diff-up) { color: var(--success); font-weight: 700; }
.ver-val :deep(.diff-down) { color: var(--danger); font-weight: 700; }

/* ── 版本预览弹窗（整表改用版本 X） ── */
.preview-modal { min-width: 640px; max-width: 880px; }
.preview-desc { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 10px; }
.preview-filter { display: flex; gap: 6px; margin-bottom: 10px; }
.preview-filter button { padding: 3px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-muted); cursor: pointer; font-size: 0.78rem; }
.preview-filter button.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.preview-list { max-height: 50vh; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); }
.preview-row { padding: 8px 12px; border-bottom: 1px solid var(--border-soft); }
.preview-row:last-child { border-bottom: none; }
.preview-row.is-conflict { background: rgba(231, 76, 60, 0.06); }
.preview-row.is-changed { background: rgba(243, 156, 18, 0.05); }
.pr-loc { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pr-key { font-weight: 600; color: var(--text-primary); font-size: 0.82rem; }
.pr-col { color: var(--accent); font-size: 0.78rem; font-weight: 600; }
.pr-tag { padding: 1px 8px; border-radius: 3px; font-size: 0.7rem; }
.tag-conflict { background: var(--danger); color: #fff; }
.tag-changed { background: var(--warning); color: #fff; }
.pr-vals { display: flex; gap: 12px; }
.pr-val { flex: 1; padding: 4px 8px; border-radius: 4px; font-size: 0.78rem; }
.pr-base { background: rgba(22, 160, 133, 0.08); border: 1px solid var(--success-soft); }
.pr-target { background: var(--accent-soft); border: 1px solid var(--border); }
.pr-vlabel { display: inline-block; min-width: 40px; color: var(--text-muted); font-size: 0.72rem; }
.pr-val code { color: var(--text-primary); word-break: break-all; }
.btn-confirm-accept { padding: 6px 16px; border: none; border-radius: 6px; background: var(--success); color: #fff; cursor: pointer; font-weight: 600; }
.btn-confirm-accept:hover { background: var(--success); }

/* ── 行版本预览弹窗 ── */
.row-preview-list { max-height: 50vh; overflow-y: auto; margin-bottom: 10px; }
.rp-row-block { margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--border-soft); }
.rp-row-block:last-child { border-bottom: none; }
.rp-row-title { font-weight: 600; margin-bottom: 6px; color: var(--text-primary); font-size: 0.85rem; }
.rp-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
.rp-table th, .rp-table td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; }
.rp-table th { background: var(--bg-hover); }
.rp-base-col { background: transparent !important; color: inherit; }
.rp-colname { font-weight: 600; color: var(--text-primary); background: var(--bg-base); }
.rp-choose-btn { margin-left: 4px; padding: 1px 6px; font-size: 0.66rem; }
.rp-cell-clickable { cursor: pointer; }
.rp-cell-clickable:hover { background: rgba(46, 204, 113, 0.20) !important; box-shadow: inset 0 0 0 2px var(--success); }
.rp-diff-val { background: rgba(243, 156, 18, 0.14); font-weight: 600; }
.rp-nodiff { color: var(--text-muted); font-size: 0.8rem; padding: 6px 0; }
.rp-apply-btns { display: flex; align-items: center; gap: 6px; margin: 12px 0; flex-wrap: wrap; }
.rp-apply-label { font-size: 0.8rem; color: var(--text-muted); }


/* 面板按钮 / 紧凑模式 */
.btn-panel { padding: 3px 10px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text-secondary); cursor: pointer; font-size: 0.78rem; }
.btn-panel:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.btn-panel.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.btn-panel:disabled { opacity: 0.4; cursor: not-allowed; }
.compact-cb { font-size: 0.78rem; color: var(--text-muted); display: flex; align-items: center; gap: 3px; cursor: pointer; }

/* 侧栏面板（冲突列表 / 审计） */
.side-panel { position: fixed; right: 16px; top: 120px; width: 320px; max-height: 60vh; background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 24px rgba(44,62,80,0.18); z-index: 40; display: flex; flex-direction: column; }
.sp-header { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; background: var(--bg-hover); border-radius: 8px 8px 0 0; font-weight: 600; font-size: 0.82rem; color: var(--text-primary); }
.sp-close { border: none; background: transparent; color: var(--text-muted); cursor: pointer; font-size: 0.9rem; }
.sp-close:hover { color: var(--danger); }
.sp-list { overflow-y: auto; flex: 1; }
.sp-item { display: flex; align-items: center; gap: 8px; padding: 6px 12px; cursor: pointer; font-size: 0.78rem; border-bottom: 1px solid var(--bg-base); }
.sp-item:hover { background: var(--accent-soft); }
.sp-item.active { background: var(--warning-soft); border-left: 3px solid var(--warning); }
.sp-item.is-conflict { border-left: 3px solid var(--danger); }
.sp-key { font-weight: 600; color: var(--text-primary); max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-col { color: var(--accent); font-size: 0.74rem; }
.sp-tag { margin-left: auto; padding: 0 6px; border-radius: 3px; font-size: 0.68rem; }
.sp-body { padding: 12px; overflow-y: auto; }
.audit-summary { font-size: 0.82rem; color: var(--text-primary); margin-bottom: 10px; }
.audit-list { display: flex; flex-direction: column; gap: 4px; }
.audit-item { display: flex; justify-content: space-between; padding: 5px 10px; background: var(--bg-base); border-radius: 4px; font-size: 0.78rem; gap: 8px; }
.audit-source { color: var(--text-primary); word-break: break-all; }
.audit-count { color: var(--success); font-weight: 600; white-space: nowrap; }
.audit-empty { color: var(--text-muted); font-size: 0.8rem; text-align: center; padding: 16px 0; }

/* ── 已解决冲突徽标（单元格内）+ 已解决列表（审计面板，可跳转撤销）── */
.resolved-badge { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 0.64rem; font-weight: 700; margin-right: 4px; background: var(--success-soft); color: var(--success); border: 1px solid var(--success-soft); cursor: pointer; vertical-align: middle; }
.resolved-badge:hover { background: var(--success-soft); border-color: var(--success); }
.cell-resolved { background: rgba(39, 174, 96, 0.06) !important; }
.resolved-list { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; }
.resolved-item { display: flex; align-items: center; gap: 8px; padding: 5px 10px; background: var(--bg-base); border-radius: 4px; font-size: 0.78rem; }
.ri-key { font-weight: 600; color: var(--text-primary); max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ri-col { color: var(--accent); font-weight: 600; }
.ri-ver { color: var(--success); font-weight: 600; margin-left: auto; }
.ri-jump { padding: 2px 10px; border: none; border-radius: 4px; background: var(--danger); color: #fff; cursor: pointer; font-size: 0.72rem; font-weight: 600; }
.ri-jump:hover { background: var(--danger); }

/* ── Sheet 下拉选择器（替代原横排标签按钮行，释放空间给表格主体）── */
.sheet-nav { padding: 5px 16px; }
.sheet-select {
  min-width: 200px; max-width: 60vw; padding: 5px 10px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--bg-card); color: var(--text-primary);
  font-size: 0.85rem; cursor: pointer;
}
.sheet-select:hover { border-color: var(--danger); }
.sheet-select:focus { outline: none; border-color: var(--danger); box-shadow: 0 0 0 2px var(--danger-soft); }
.sheet-nav-hint { font-size: 0.72rem; color: var(--text-muted); margin-left: 10px; }

/* ── 单元格内：版本标识 + 旧→新对比 ── */
.cell-version-badge { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 0.6rem; font-weight: 700; margin-right: 3px; background: var(--warning-soft); color: var(--warning); vertical-align: middle; }
.cell-vb-conflict { background: var(--danger-soft); color: var(--danger); }
.cell-conflict-versions { display: inline-flex; align-items: center; gap: 3px; flex-wrap: wrap; font-size: 0.78rem; vertical-align: middle; }
.cv-ver { display: inline-flex; align-items: center; gap: 2px; padding: 0 4px; border-radius: 3px; }
.cv-ver-label { font-size: 0.65rem; opacity: 0.7; font-weight: 500; }
.cv-val { font-weight: 600; }
.cv-base { background: rgba(127, 140, 141, 0.12); }
.cv-base .cv-ver-label { color: var(--text-muted); }
.cv-base .cv-val { color: var(--text-muted); text-decoration: line-through; font-weight: 400; }
.cv-sep { color: var(--text-muted); opacity: 0.4; }
.cell-old { background: rgba(231, 76, 60, 0.18); color: var(--danger); text-decoration: line-through; border-radius: 3px; padding: 0 3px; }
.cell-arrow { color: var(--text-muted); margin: 0 2px; font-size: 0.7rem; }
.cell-new { background: rgba(39, 174, 96, 0.18); color: var(--success); border-radius: 3px; padding: 0 3px; font-weight: 600; }

/* ── 单元格冲突弹窗：横向版本表格（放大，保证长文本可见） ── */
.cell-modal { min-width: 720px; max-width: min(94vw, 1180px); max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
.vm-table-wrap { flex: 1 1 auto; min-height: 0; max-height: 72vh; overflow: auto; border: 1px solid var(--border); border-radius: 6px; }
.vm-table { border-collapse: collapse; font-size: 0.82rem; width: 100%; background: var(--bg-card); table-layout: auto; }
.vm-table th, .vm-table td { border: 1px solid var(--border-soft); padding: 7px 10px; text-align: left; vertical-align: top; word-break: break-word; white-space: pre-wrap; }
.vm-col-col { background: var(--bg-base) !important; color: var(--text-secondary); font-weight: 600; white-space: nowrap; position: sticky; left: 0; z-index: 2; }
.vm-ver-th { background: var(--bg-hover); text-align: center; min-width: 130px; }
.vm-ver-name { font-weight: 600; color: var(--text-primary); margin-bottom: 4px; }
.vm-ver-meta { display: flex; flex-direction: column; gap: 1px; font-size: 11px; color: var(--text-secondary); margin-bottom: 4px; line-height: 1.3; }
.vm-base-col { background: transparent !important; }
.vm-base-col .vm-ver-name { color: var(--text-secondary); }
.vm-choose-btn { padding: 3px 8px; border: none; border-radius: 4px; background: var(--danger); color: #fff; cursor: pointer; font-size: 0.72rem; font-weight: 600; }
.vm-choose-btn:hover { background: var(--danger); }
.vm-row-current { background: rgba(241, 196, 15, 0.22); box-shadow: inset 4px 0 0 var(--warning); }
.vm-row-current .vm-col-col { background: var(--warning-soft) !important; color: var(--warning); font-weight: 700; }
.vm-row-conflict { background: rgba(146, 43, 33, 0.06); }
.vm-row-conflict .vm-col-col { box-shadow: inset 3px 0 0 var(--danger); }
.vm-cur-mark { color: var(--warning); margin-left: 3px; }
.vm-cur-tag { display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 3px; font-size: 0.64rem; font-weight: 700; background: var(--warning); color: #fff; vertical-align: middle; }
.vm-cell-current { box-shadow: inset 0 0 0 2px var(--warning); }
.vm-cell-clickable { cursor: pointer; }
.vm-cell-clickable:hover { background: rgba(46, 204, 113, 0.18) !important; box-shadow: inset 0 0 0 2px var(--success); }
.vm-conf-tag { display: inline-block; margin-left: 4px; padding: 0 5px; border-radius: 3px; font-size: 0.62rem; font-weight: 700; background: var(--danger-soft); color: var(--danger); }
.vm-diff-cell { background: rgba(184, 134, 11, 0.12); }
.vm-base-cell { background: transparent; color: inherit; }
.vm-hint { font-size: 0.74rem; color: var(--text-muted); margin-top: 8px; line-height: 1.5; }
.vm-table :deep(.diff-del) { color: var(--danger); text-decoration: line-through; background: rgba(231,76,60,0.14); }
.vm-table :deep(.diff-add) { color: var(--diff-add); background: rgba(46,204,113,0.16); font-weight: 600; }
.vm-table :deep(.diff-up) { color: var(--success); font-weight: 700; }
.vm-table :deep(.diff-down) { color: var(--danger); font-weight: 700; }

/* 行预览：冲突列计数 + 基准列样式 */
.rp-conf-count { margin-left: 8px; font-size: 0.72rem; color: var(--danger); background: var(--danger-soft); padding: 1px 6px; border-radius: 3px; font-weight: 600; }
.rp-base-cell { color: inherit; }
.rp-table :deep(.diff-del) { color: var(--danger); text-decoration: line-through; background: rgba(231,76,60,0.14); }
.rp-table :deep(.diff-add) { color: var(--diff-add); background: rgba(46,204,113,0.16); font-weight: 600; }
.rp-table :deep(.diff-up) { color: var(--success); font-weight: 700; }
.rp-table :deep(.diff-down) { color: var(--danger); font-weight: 700; }

/* ── merge 文件夹文件选择弹窗 ── */
.folder-picker-modal { min-width: 460px; max-width: 640px; display: flex; flex-direction: column; max-height: 80vh; }
.fp-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.fp-count { font-size: 0.8rem; color: var(--text-muted); }
.fp-list { flex: 1; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); max-height: 50vh; }
.fp-item { display: flex; align-items: center; gap: 8px; padding: 7px 12px; cursor: pointer; font-size: 0.82rem; border-bottom: 1px solid var(--bg-base); }
.fp-item:last-child { border-bottom: none; }
.fp-item:hover { background: var(--bg-stripe); }
.fp-item.active { background: var(--accent-soft); }
.fp-item.is-base { box-shadow: inset 3px 0 0 var(--warning); }
.fp-item input { cursor: pointer; }
.fp-name { color: var(--text-primary); word-break: break-all; }
.fp-base-tag { margin-left: auto; padding: 0 6px; border-radius: 3px; font-size: 0.64rem; font-weight: 700; background: var(--warning-soft); color: var(--warning); border: 1px solid var(--warning-soft); }
.fp-group { border-bottom: 1px solid var(--border-soft); }
.fp-group:last-child { border-bottom: none; }
.fp-group-hd { display: flex; align-items: center; gap: 12px; padding: 7px 12px; background: var(--bg-base); position: sticky; top: 0; z-index: 1; }
.fp-group-name { font-weight: 700; color: var(--text-primary); font-size: 0.82rem; }
.fp-base-sel { font-size: 0.76rem; color: var(--text-secondary); display: flex; align-items: center; gap: 4px; }
.fp-base-sel select { padding: 2px 6px; border: 1px solid var(--border); border-radius: 3px; font-size: 0.76rem; max-width: 240px; }

/* ── 导出结果反馈条 ── */
.export-result { display: flex; align-items: flex-start; gap: 10px; margin: 6px 12px; padding: 8px 12px; background: var(--success-soft); border: 1px solid var(--success-soft); border-radius: 6px; }
.export-result pre { margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 0.78rem; color: var(--success); font-family: inherit; flex: 1; }
.fp-empty { padding: 20px; text-align: center; color: var(--text-muted); font-size: 0.82rem; }
</style>
