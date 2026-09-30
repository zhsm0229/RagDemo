import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 与 vite.config.ts 同插件同别名；测试环境 jsdom（ElMessage/DOM 依赖）。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
    server: {
      deps: {
        // element-plus 作为外部依赖时，其 `new AsyncValidator(...)`（el-form 校验核心）
        // 经 Node ESM-CJS 互操作拿到的是 namespace 而非类，构造抛 TypeError →
        // 校验在测试里恒放行（真实浏览器构建不受影响）。inline 交给 vite 转换后 interop 正确。
        inline: ['element-plus'],
      },
    },
  },
})
