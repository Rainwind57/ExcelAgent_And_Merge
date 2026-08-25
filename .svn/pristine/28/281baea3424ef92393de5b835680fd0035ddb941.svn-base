<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { applyTheme, getStoredTheme, storeTheme } from './theme'

const router = useRouter()
const route = useRoute()

const theme = ref('light')

const tabs = [
  { path: '/agent', label: 'AI 助手' },
  { path: '/merge-guide', label: '合并引导' },
  { path: '/tables', label: '表格浏览' },
  { path: '/svn', label: 'SVN 历史' },
]

function goTab(path) {
  router.push(path)
}

function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  applyTheme(theme.value)
  storeTheme(theme.value)
}

onMounted(() => {
  theme.value = getStoredTheme()
  applyTheme(theme.value)
})
</script>

<template>
<div class="app-shell">
  <!-- 顶部导航 -->
  <header class="app-header">
    <h1 class="app-title">AI 配表助手</h1>
    <nav class="tab-bar">
      <button
        v-for="tab in tabs" :key="tab.path"
        class="tab-btn"
        :class="{ active: route.path === tab.path }"
        @click="goTab(tab.path)"
      >{{ tab.label }}</button>
    </nav>
    <button class="theme-btn" @click="toggleTheme" :title="theme === 'light' ? '切换暗色' : '切换亮色'">
      {{ theme === 'light' ? '🌙' : '☀️' }}
    </button>
  </header>

  <!-- 视图区：keep-alive 缓存各视图，切换标签页时不销毁组件，
       AI 助手请求/比对/浏览可在后台继续运行，切回即可见结果 -->
  <main class="app-main">
    <router-view v-slot="{ Component }">
      <keep-alive>
        <component :is="Component" />
      </keep-alive>
    </router-view>
  </main>

  <!-- 底部状态栏 -->
  <footer class="app-footer">
    <span>资源目录：resources/</span>
    <span class="footer-right">
      <a href="http://127.0.0.1:8000/docs" target="_blank">API 文档</a>
    </span>
  </footer>
</div>
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-base); color: var(--text-primary); }

.app-shell { display: flex; flex-direction: column; height: 100vh; }

.app-header {
  display: flex; align-items: center; gap: 16px;
  padding: 8px 20px;
  background: var(--bg-card); border-bottom: 1px solid var(--border);
}
.app-title { font-size: 1.1rem; color: var(--accent); white-space: nowrap; }
.tab-bar { display: flex; gap: 4px; flex: 1; }
.tab-btn {
  padding: 6px 16px; border: none; border-radius: 6px;
  background: transparent; color: var(--text-secondary); cursor: pointer;
  font-size: 0.9rem; transition: all 0.2s;
}
.tab-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.tab-btn.active { background: var(--accent); color: #fff; }
.theme-btn {
  padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg-card); color: var(--text-primary); cursor: pointer;
  font-size: 1rem; line-height: 1; transition: all 0.2s;
}
.theme-btn:hover { border-color: var(--accent); background: var(--accent-soft); }
.version-tag { font-size: 0.75rem; color: var(--text-muted); }

.app-main { flex: 1; overflow: hidden; }

.app-footer {
  display: flex; justify-content: space-between;
  padding: 4px 20px; font-size: 0.75rem; color: var(--text-muted);
  background: var(--bg-card); border-top: 1px solid var(--border);
}
.app-footer a { color: var(--accent); text-decoration: none; }
</style>
