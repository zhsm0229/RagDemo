import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting'
import pluginVue from 'eslint-plugin-vue'

// Lint 门禁：禁 any、禁 console（standards §20.2 / task-6 brief）。
// node 侧工程文件（vite/vitest 配置）豁免 no-console；生成物 schema.d.ts 不检。
export default defineConfigWithVueTs(
  {
    name: 'app/files-to-lint',
    files: ['**/*.{ts,vue}'],
  },
  {
    name: 'app/files-to-ignore',
    // e2e/.artifacts 为 Playwright 运行产物（trace viewer 构建物），2026-10 起出现，不属源码
    ignores: [
      'dist/**',
      'coverage/**',
      'node_modules/**',
      'src/api/schema.d.ts',
      'e2e/.artifacts/**',
    ],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  skipFormatting,
  {
    name: 'app/project-rules',
    rules: {
      'no-console': 'error',
      '@typescript-eslint/no-explicit-any': 'error',
      // 占位视图是单词名词（ChatView 等实际为多词；Placeholder 基座组件保证语义），
      // 统一关闭以避免占位/后续页面与原型命名冲突时的噪音。
      'vue/multi-word-component-names': 'off',
    },
  },
  {
    name: 'app/node-side-files',
    files: ['vite.config.ts', 'vitest.config.ts', 'eslint.config.js'],
    rules: {
      'no-console': 'off',
    },
  },
)
