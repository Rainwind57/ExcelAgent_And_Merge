// Vue 3 入口文件
import { createApp } from 'vue'
import './style.css'
import { applyTheme, getStoredTheme } from './theme'
import App from './App.vue'
import router from './router'

// 启动前应用已存主题，避免闪烁
applyTheme(getStoredTheme())

// 创建 Vue 应用实例，挂载路由
createApp(App).use(router).mount('#app')
