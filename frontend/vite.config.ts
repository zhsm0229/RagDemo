import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 代理与剥 console 约定见 task-6 brief：dev 期 /api 转发后端 8000；
// 生产构建经 esbuild drop 剥掉 console/debugger（含日志中的敏感面，standards §6）。
export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  esbuild: mode === 'production' ? { drop: ['console', 'debugger'] } : {},
}))
