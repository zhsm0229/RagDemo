import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { DocumentsPage } from '../pages/DocumentsPage'

// 种子每轮生成内容唯一的上传夹具（容器写上传卷，宿主路径 data/uploads/e2e/），
// 使 DOC-01 上传不被上一轮的 F-1001 精确查重秒拒。
const DOC_SPEC = fileURLToPath(
  new URL('../../../data/uploads/e2e/e2e-upload-docspec.docx', import.meta.url),
)

// DOC-01/02（P0）：上传 docx → TEST_MODE 同步解析 → 列表"● 就绪"。
// 标题带时间戳：uq_doc_active(dept_id, title) 唯一，重复运行不撞 409。
test('DOC-01/02: 上传文档同步解析并就绪展示', async ({ page }) => {
  const docs = new DocumentsPage(page)
  await docs.goto()
  const title = `e2e_报销小样_${Date.now()}`
  await docs.upload(DOC_SPEC, { title, owner: 'e2e-bot' })
  await docs.expectRowReady(title)
})

test('DOC-04: 文档列表空状态引导（新库首访）', async ({ page }, testInfo) => {
  // 仅当运行在全新库（无任何文档）时本条有意义；非空库直接跳过——空状态口径 §8.5。
  const docs = new DocumentsPage(page)
  await docs.goto()
  const hasRows = await page.locator('.el-table__row').count()
  testInfo.skip(hasRows > 0, '库内已有文档，空状态用例不适用')
  await expect(page.getByText('上传第一份文档')).toBeVisible()
})
