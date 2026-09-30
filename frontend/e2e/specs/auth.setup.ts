import fs from 'node:fs'

import { test as setup, type APIRequestContext } from '@playwright/test'

// 登录态工厂：storageState 注入 cookie（httpOnly JWT），业务 spec 免登录直连。
// 种子账号由 app/db/e2e_seed.py 提供（口令 E2E_PASSWORD，缺省 E2ePass#2026）。
// 本文件必须在 config 的 testDir（specs/）内：setup 是一个 project（testMatch 按名匹配）。
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:8000'
const PASSWORD = process.env.E2E_PASSWORD ?? 'E2ePass#2026'

async function loginAndSave(
  ctx: APIRequestContext,
  username: string,
  statePath: string,
): Promise<void> {
  const res = await ctx.post('/api/auth/login', {
    data: { username, password: PASSWORD },
  })
  if (!res.ok()) {
    throw new Error(`E2E 登录失败 ${username}: ${res.status()} ${await res.text()}`)
  }
  await ctx.storageState({ path: statePath })
}

setup('prepare login states', async ({ playwright }) => {
  fs.mkdirSync('.auth', { recursive: true })
  const ctx = await playwright.request.newContext({ baseURL })
  try {
    await loginAndSave(ctx, 'e2e-admin', '.auth/admin.json')
    await loginAndSave(ctx, 'e2e-hr', '.auth/hr.json')
  } finally {
    await ctx.dispose()
  }
})
