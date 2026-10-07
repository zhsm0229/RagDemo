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
    // 全量并发跑时 worker 争 CPU，重组件用例（模板生成时间线/用户管理表单）实测可到
    // 36s——默认 5s 超时会把"慢"误判为"红"（2026-10-06 交叉评审后全量 13 假红实证）。
    // 单文件定向跑均在 5s 内；该超时只放宽判定阈值，不改变测试行为。
    testTimeout: 40_000,
    hookTimeout: 40_000,
    // 8G 开发机全量并发时 worker 互挤，重列表页渲染可被拖到 40s+ 且失败文件逐轮轮换
    // （2026-10-06 实测：满并发 3 红→限 2 worker 后稳定全绿）——门禁稳定性优先于速度。
    maxWorkers: 2,
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
