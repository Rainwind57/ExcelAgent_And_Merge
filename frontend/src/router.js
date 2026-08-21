import { createRouter, createWebHashHistory } from 'vue-router'
import AgentChatView from './views/AgentChatView.vue'
import TablesView from './views/TablesView.vue'
import SvnHistoryView from './views/SvnHistoryView.vue'
import MergeGuideView from './views/MergeGuideView.vue'

const routes = [
  { path: '/', redirect: '/agent' },
  { path: '/agent', name: 'agent', component: AgentChatView },
  { path: '/tables', name: 'tables', component: TablesView },
  { path: '/merge-guide', name: 'merge-guide', component: MergeGuideView },
  { path: '/svn', name: 'svn', component: SvnHistoryView },
  // 旧入口重定向到统一合并引导界面（保留深链兼容）
  { path: '/branch-merge', redirect: '/merge-guide?mode=branch' },
  { path: '/subdir-merge', redirect: '/merge-guide?mode=subdir' },
  // 旧版本比对入口保留重定向（不再独立展示）
  { path: '/diff', redirect: '/merge-guide' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

export default router
