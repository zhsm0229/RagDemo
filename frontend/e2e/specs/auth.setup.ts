import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

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
  // 套件级复位（本地/CI 同一机制）：复位锁定态 + 软删上轮 e2e 文档（释放 content_hash）
  // + 重新生成内容唯一的上传夹具。仓库根 = specs 的上三级。
  const repoRoot = path.resolve(fileURLToPath(new URL('../../..', import.meta.url)))
  // 栈自举（守卫式）：healthz 不通才以 e2e override 拉起（TEST_MODE=true 硬编码），
  // 已健康则跳过——避免每轮重建重启打断后续 API 预置。
  const stackUp = async (): Promise<boolean> => {
    const probe = await playwright.request.newContext({ baseURL })
    try {
      const r = await probe.get('/healthz', { timeout: 5_000 }).catch(() => null)
      return r?.ok() ?? false
    } finally {
      await probe.dispose()
    }
  }
  if (!(await stackUp())) {
    execSync(
      'docker compose -f docker-compose.yml -f deploy/docker-compose.e2e.yml up -d pg app',
      { cwd: repoRoot, stdio: 'inherit', env: process.env },
    )
    for (let i = 0; i < 60; i++) {
      if (await stackUp()) break
      await new Promise((r) => setTimeout(r, 3_000))
    }
  }
  execSync(
    'docker compose exec -T -e E2E_PASSWORD -e E2E_WIPE=1 app python -m app.db.e2e_seed',
    { cwd: repoRoot, stdio: 'inherit', env: process.env },
  )
  fs.mkdirSync('.auth', { recursive: true })
  const ctx = await playwright.request.newContext({ baseURL })
  try {
    await loginAndSave(ctx, 'e2e-admin', '.auth/admin.json')

    // ---- 数据预置（清库后自足，执行顺序无关化）----
    // ① 两份文档入库（TEST_MODE 同步解析出块，供检索测试/问答引用）
    const dataDir = path.resolve(repoRoot, 'data/uploads/e2e')
    for (const [name, title] of [
      ['e2e-upload-latest.docx', 'e2e_差旅报销制度_预置'],
      ['e2e-upload-docspec.docx', 'e2e_DOC01专用文档_预置'],
    ] as const) {
      const buffer = fs.readFileSync(path.join(dataDir, name))
      const up = await ctx.post('/api/documents', {
        multipart: {
          file: { name, mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer },
          title,
          dept: 'all',
          owner: 'e2e-bot',
          effectiveDate: '2026-09-30',
          complianceConfirmed: 'true',
        },
      })
      if (up.status() !== 202 && up.status() !== 200) {
        throw new Error(`预置上传失败 ${name}: ${up.status()} ${await up.text()}`)
      }
    }
    // ② 问答对导入（年假行供 QA-05 启停用例）
    const qaXlsx = fs.readFileSync(path.resolve(repoRoot, 'frontend/e2e/fixtures/files/qa_合规.xlsx'))
    const qa = await ctx.post('/api/admin/qa-pairs/import', {
      multipart: {
        file: { name: 'qa_合规.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: qaXlsx },
        deptCode: 'all',
      },
    })
    if (qa.status() !== 201 && qa.status() !== 200) {
      throw new Error(`预置问答对导入失败: ${qa.status()} ${await qa.text()}`)
    }
    // ③ 评测题库导入（EVAL 域用例数据）
    const evalXlsx = fs.readFileSync(path.resolve(repoRoot, 'frontend/e2e/fixtures/files/eval_题库.xlsx'))
    const ev = await ctx.post('/api/admin/eval-questions/import', {
      multipart: {
        file: { name: 'eval_题库.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: evalXlsx },
      },
    })
    if (ev.status() !== 201 && ev.status() !== 200) {
      throw new Error(`预置评测题导入失败: ${ev.status()} ${await ev.text()}`)
    }
    // ④ 一轮提问（提问记录/审计/反馈闭环的展示数据）
    await ctx.post('/api/chat/stream', { data: { question: '报销的时限是多少天' } })

    await loginAndSave(ctx, 'e2e-hr', '.auth/hr.json')
  } finally {
    await ctx.dispose()
  }
})
