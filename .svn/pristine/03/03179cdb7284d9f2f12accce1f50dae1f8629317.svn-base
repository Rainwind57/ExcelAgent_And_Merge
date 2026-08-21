// Vite 构建配置
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],  // 启用 Vue 3 SFC 编译插件
  build: {
    // 直接输出到后端静态目录，npm run build 后无需手动拷贝
    outDir: '../server/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // 开发模式下将 /api 请求代理到后端 FastAPI 服务（端口 8000）
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
