import { defineConfig } from '@playwright/test'

// E2E 双层约定（2026-09-30 两方案定稿）：
// L1 确定性层（默认）：TEST_MODE=true 的 app 栈，stub 全链、零外部依赖，每 MR 必跑；
// L2 真实层：E2E_REAL=1 显式开启（TEI+DeepSeek 就绪的部署机），跑 @real 标记用例。
// 栈由外部起停（本机 compose up app / CI e2e job / 部署机），本配置不管理 webServer。
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:8000'

export default defineConfig({
  testDir: './specs',
  outputDir: './.artifacts/results', // 失败产物；html 报告在本目录外（新版禁止嵌套）
  timeout: 120_000, // 上传同步解析 + SSE 全程留余量（L2 CPU rerank 更慢）
  expect: { timeout: 15_000 },
  fullyParallel: false, // 种子数据（账号/问答对）共享，串行最稳
  // workers=1 落实上一行意图：默认 4 worker 各驻留一个浏览器进程，headed 下叠加
  // TEI×2+MinerU 容器把 12G 内存打满，Edge 渲染进程被 OOM（"browser has been
  // closed" 随机命中后续用例，2026-10-11 第五轮 DOC-13/01/QA 连环崩根因）
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { outputFolder: './.artifacts/report', open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    viewport: { width: 1280, height: 800 }, // §9.5 桌面 1280+ 口径
    // 本机无外网下载 Playwright 浏览器（官方/镜像 CDN 均超时）时，置
    // E2E_CHANNEL=msedge 直用系统 Edge；CI 与部署机走默认 chromium（官方 CDN 可达）。
    channel: process.env.E2E_CHANNEL || undefined,
  },
  projects: [
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'l1-admin',
      dependencies: ['setup'],
      testIgnore: /security\.spec\.ts/,
      use: { storageState: '.auth/admin.json' },
    },
    {
      name: 'l1-hr',
      dependencies: ['setup'],
      testMatch: /security\.spec\.ts/,
      use: { storageState: '.auth/hr.json' },
    },
  ],
})
