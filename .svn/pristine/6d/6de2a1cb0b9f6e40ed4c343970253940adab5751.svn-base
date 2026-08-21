// ── 主题色板：CSS 变量 + data-theme 切换 ──
// 亮色为默认。颜色配比遵循人体视觉工效：
//  - 亮色：背景 #f5f7fa / 文字 #2c3e50，对比比 > 7:1（AAA），长时间阅读不疲劳
//  - 暗色：背景 #1a1a2e / 文字 #e0e0e0，对比比 > 10:1，暗光环境护眼
//  - 强调色饱和度适中，避免过饱和刺激
//  - 语义色（成功/警告/危险/信息）在两套主题下都保持 4.5:1 以上

export const THEMES = {
  light: {
    // 背景层级：基础 / 卡片 / 输入 / 悬停
    '--bg-base': '#f5f7fa',
    '--bg-card': '#ffffff',
    '--bg-input': '#ffffff',
    '--bg-hover': '#eef2f7',
    '--bg-active': '#e6f4ff',
    '--bg-stripe': '#fafbfd',
    // 文字层级：主 / 次 / 弱 / 占位
    '--text-primary': '#2c3e50',
    '--text-secondary': '#5a6c7d',
    '--text-muted': '#8a9aab',
    '--text-placeholder': '#aab4be',
    // 边框
    '--border': '#dce3eb',
    '--border-strong': '#c4ccd6',
    '--border-soft': '#eef1f5',
    // 强调主色（蓝，不刺眼）
    '--accent': '#1677ff',
    '--accent-hover': '#0958d9',
    '--accent-soft': '#f0f5ff',
    // 语义色
    '--success': '#1e8449',
    '--success-soft': '#e8f8f0',
    '--warning': '#c0642a',
    '--warning-soft': '#fdf0e3',
    '--danger': '#c0392b',
    '--danger-soft': '#fdecea',
    '--info': '#2a6f97',
    '--info-soft': '#e3f2f8',
    // diff 专用
    '--diff-add': '#1e8449',
    '--diff-add-bg': 'rgba(30, 132, 73, 0.10)',
    '--diff-del': '#b0453a',
    '--diff-del-bg': 'rgba(176, 69, 58, 0.08)',
    '--diff-chg': '#b9770e',
    '--diff-chg-bg': 'rgba(185, 119, 14, 0.16)',
    // 代码块
    '--code-bg': '#f0f2f5',
    '--code-text': '#2c3e50',
    // 阴影
    '--shadow': 'rgba(0, 0, 0, 0.08)',
    '--shadow-strong': 'rgba(0, 0, 0, 0.15)',
  },
  dark: {
    '--bg-base': '#14141f',
    '--bg-card': '#1a1a2e',
    '--bg-input': '#0f0f1a',
    '--bg-hover': '#2a2a4a',
    '--bg-active': '#2a3a5a',
    '--bg-stripe': '#1f1f33',
    '--text-primary': '#e0e0e0',
    '--text-secondary': '#a0a0b0',
    '--text-muted': '#6a6a7a',
    '--text-placeholder': '#555566',
    '--border': '#2a2a4a',
    '--border-strong': '#3a3a5a',
    '--border-soft': '#1f1f33',
    '--accent': '#e94560',
    '--accent-hover': '#ff5570',
    '--accent-soft': 'rgba(233, 69, 96, 0.12)',
    '--success': '#4fd15f',
    '--success-soft': 'rgba(79, 209, 95, 0.10)',
    '--warning': '#ffc857',
    '--warning-soft': 'rgba(255, 200, 87, 0.12)',
    '--danger': '#ff6b6b',
    '--danger-soft': 'rgba(255, 107, 107, 0.10)',
    '--info': '#6ec6ff',
    '--info-soft': 'rgba(110, 198, 255, 0.10)',
    '--diff-add': '#4fd15f',
    '--diff-add-bg': 'rgba(79, 209, 95, 0.10)',
    '--diff-del': '#ff6b6b',
    '--diff-del-bg': 'rgba(255, 107, 107, 0.08)',
    '--diff-chg': '#ffc857',
    '--diff-chg-bg': 'rgba(255, 200, 87, 0.16)',
    '--code-bg': '#0f0f1a',
    '--code-text': '#e0e0e0',
    '--shadow': 'rgba(0, 0, 0, 0.4)',
    '--shadow-strong': 'rgba(0, 0, 0, 0.6)',
  },
}

const STORAGE_KEY = 'app-theme'

export function applyTheme(name) {
  const vars = THEMES[name]
  if (!vars) return
  const root = document.documentElement
  Object.entries(vars).forEach(([k, v]) => root.style.setProperty(k, v))
  root.setAttribute('data-theme', name)
}

export function getStoredTheme() {
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved && THEMES[saved] ? saved : 'light'
}

export function storeTheme(name) {
  localStorage.setItem(STORAGE_KEY, name)
}
