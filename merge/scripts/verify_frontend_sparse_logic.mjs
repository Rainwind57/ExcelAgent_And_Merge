/* 用真实 Vue 响应式验证 MergeGuideView 稀疏索引算法：
   buildSparseIndex → resolveCell 计数递减 → diffList/visibleRows 自动重算 → 稀疏快照 undo/redo */
import { reactive, ref, computed, effect } from '../../frontend/node_modules/@vue/reactivity/dist/reactivity.esm-bundler.js'

// ── 模拟 buildSparseIndex（与 MergeGuideView 一致） ──
const liveCounts = reactive({})
let diffCandidates = ref([])
let rowCandidates = ref([])
let activeTable = ref('big_data')
let activeSheet = ref('BigData')

function buildSparseIndex(resp) {
  for (const k of Object.keys(liveCounts)) delete liveCounts[k]
  const cands = [], rows = []
  for (const [gk, g] of Object.entries(resp.groups)) {
    liveCounts[gk] = {}
    for (const [sk, s] of Object.entries(g.sheets)) {
      const cnt = { conflicts: 0, changed: 0, inserted: 0, deleted: 0 }
      s.rows.forEach((row, ri) => {
        let hasDiff = row.row_type === 'inserted' || row.row_type === 'deleted' || row.row_type === 'missing_row'
        if (row.row_type === 'inserted') cnt.inserted++
        else if (row.row_type === 'deleted') cnt.deleted++
        const isMatched = row.row_type === 'matched'
        row.cells.forEach((cell, ci) => {
          if (cell.conflict) { cnt.conflicts++; cands.push({ gk, sk, ri, ci, key: row.key, header: s.headers[ci] || '', cell }); hasDiff = true }
          else if (isMatched && cell.changed) { cnt.changed++; hasDiff = true }
        })
        if (hasDiff) rows.push({ gk, sk, ri, row })
      })
      liveCounts[gk][sk] = cnt
    }
  }
  diffCandidates.value = cands
  rowCandidates.value = rows
}

function resolveCell(cell, newVal, source = '') {
  const wasConflict = cell.conflict
  if (newVal !== undefined) cell.value = newVal
  cell.conflict = false; cell.changed = false; cell.resolved = true; cell.resolvedBy = source
  if (wasConflict) {
    const lc = liveCounts[activeTable.value]?.[activeSheet.value]
    if (lc && lc.conflicts > 0) lc.conflicts--
  }
}

// 模拟 compare 响应：10w matched 稀疏行（1 PK 格，无 versions）+ 1 冲突行
function makeResp() {
  const rows = []
  for (let i = 1; i <= 100000; i++) {
    rows.push({ key: String(i), row_type: 'matched', presence: { a: true, b: true, c: true },
      cells: [{ col: 0, value: i, versions: {}, conflict: false, changed: false, resolved: false }] })
  }
  rows.push({ key: '100001', row_type: 'matched', presence: { a: true, b: true, c: true },
    cells: [{ col: 0, value: 1, versions: { b: 1, c: 1 }, conflict: false, changed: true, resolved: false },
            { col: 1, value: 'X', versions: { b: 'X', c: 'Y' }, conflict: true, changed: false, resolved: false }] })
  rows.push({ key: '100002', row_type: 'inserted', presence: { a: false, b: true, c: false },
    cells: [{ col: 0, value: 100002, versions: { b: 100002 }, conflict: false, changed: false, resolved: false }] })
  return { groups: { big_data: { sheets: { BigData: { headers: ['id', 'name'], rows } } } } }
}

const resp = reactive(makeResp())
buildSparseIndex(resp)

const diffList = computed(() => diffCandidates.value.filter(d =>
  d.gk === activeTable.value && d.sk === activeSheet.value && (d.cell.conflict || d.cell.resolved)))
const unresolvedCount = computed(() => {
  let n = 0
  for (const g of Object.values(liveCounts)) for (const s of Object.values(g)) n += s.conflicts
  return n
})
const visibleRows = computed(() => rowCandidates.value
  .filter(d => d.gk === activeTable.value && d.sk === activeSheet.value)
  .filter(({ row }) => row.cells.some(c => c.conflict || c.resolved || c.changed) || row.row_type === 'inserted' || row.row_type === 'deleted'))

let dl = 0, uc = 0, vr = 0
effect(() => { dl = diffList.value.length })
effect(() => { uc = unresolvedCount.value })
effect(() => { vr = visibleRows.value.length })

const assert = (cond, msg) => { if (!cond) { console.error('FAIL:', msg); process.exitCode = 1 } else console.log('PASS:', msg) }

assert(diffCandidates.value.length === 1, `diffCandidates=1 (冲突格) 实际 ${diffCandidates.value.length}`)
assert(rowCandidates.value.length === 2, `rowCandidates=2 (冲突行+inserted行) 实际 ${rowCandidates.value.length}`)
assert(uc === 1 && dl === 1 && vr === 2, `初始: conflicts=${uc} diffList=${dl} visible=${vr}`)

// 稀疏快照（模拟 pushSnapshot 捕获将变更格）
const snap = {}
const target = rowCandidates.value.find(d => d.row.key === '100001')
const conflictCell = target.row.cells[1]
snap['big_data'] = snap['big_data'] || {}
snap['big_data']['BigData'] = { [target.ri]: { [1]: { value: conflictCell.value, conflict: true, changed: false, resolved: false, resolvedBy: '' } } }

// 解决冲突
resolveCell(conflictCell, 'Z', 'tgt')
assert(uc === 0, `解决后 unresolvedCount=0 实际 ${uc}`)
assert(dl === 1, `解决后 diffList 仍含已解决项=1 实际 ${dl}`)
assert(vr === 2, `解决后 visible 行不变=2 实际 ${vr}`)

// undo：还原稀疏快照
const restored = snap['big_data']['BigData'][target.ri][1]
const c2 = resp.groups.big_data.sheets.BigData.rows[target.ri].cells[1]
c2.value = restored.value; c2.conflict = restored.conflict; c2.resolved = restored.resolved
// recomputeLiveCounts（模拟）
const cnt = { conflicts: 0, changed: 0, inserted: 0, deleted: 0 }
for (const r of resp.groups.big_data.sheets.BigData.rows) {
  if (r.row_type === 'inserted') cnt.inserted++
  const m = r.row_type === 'matched'
  for (const c of r.cells) { if (c.conflict) cnt.conflicts++; else if (m && c.changed) cnt.changed++ }
}
liveCounts['big_data']['BigData'] = cnt
assert(uc === 1, `undo 后 unresolvedCount=1 实际 ${uc}`)
assert(dl === 1, `undo 后 diffList=1 实际 ${dl}`)

console.log(process.exitCode ? '\n结果: 有失败 ❌' : '\n结果: 全部通过 ✓')
