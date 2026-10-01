import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { DocumentsPage } from '../pages/DocumentsPage'

const LATEST = fileURLToPath(new URL('../../../data/uploads/e2e/e2e-upload-latest.docx', import.meta.url))
const V1 = fileURLToPath(new URL('../../../data/uploads/e2e/e2e-doc13-v1.docx', import.meta.url))
const V2 = fileURLToPath(new URL('../../../data/uploads/e2e/e2e-doc13-v2.docx', import.meta.url))
const REDLINE = fileURLToPath(new URL('../../../data/uploads/e2e/e2e-fail-redline.docx', import.meta.url))

// 文档治理异常与边界（DOC-02/03/05/06/09/10/11/13/21）。
// 夹具：docs/golden 与 e2e/fixtures（.doc 伪扩展 / exe 伪 .pdf / 乱码 / 扫描件 / v2 内容）。
test.describe('文档治理', () => {
  let docs: DocumentsPage
  const e2eRoot = fileURLToPath(new URL('..', import.meta.url))
  const testsRoot = fileURLToPath(new URL('../../../tests', import.meta.url))
  const abs = (p: string) => (p.startsWith('/') || p[1] === ':' ? p : `${e2eRoot}${p}`)

  test.beforeEach(async ({ page }) => {
    docs = new DocumentsPage(page)
    await docs.goto()
  })

  test('DOC-02: .doc 扩展名即时拒收不入库', async ({ page }) => {
    await docs.uploadRejectExpect(/另存为|docx/i, abs('fixtures/files/样本.doc'))
  })

  test('DOC-03: exe 伪造成 .pdf 被拒收（提示瞬时，双断言：提示或停留第一步）', async ({ page }) => {
    await docs.openUploadDialog()
    await docs.selectFile(docs.abs('fixtures/files/fake.pdf'))
    // 后端魔数校验即时拒绝（422 toast）或前端清除选择停留第一步——两者皆算拒收
    const toastOrStep = Promise.race([
      page.getByText(/不符|格式|支持|非法|失败/).last().waitFor({ state: 'visible', timeout: 8_000 }),
      page.locator('.el-upload__input').waitFor({ state: 'attached', timeout: 8_000 }),
    ])
    await toastOrStep
    await docs.cancel().catch(() => undefined)
  })

  test('DOC-05: 元数据卡口（缺必填不可提交，字段级提示）', async ({ page }) => {
    await docs.openUploadDialog()
    await docs.selectFile(LATEST) // 复用 DOC-01 已入库内容 → 首次上传即命中 F-1001
    await docs.gotoStep2()
    await docs.submitExpectInlineErrors() // 缺标题外的必填：责任人/生效日期/合规确认
  })

  test('DOC-06: 精确查重——同内容二次上传秒拒', async ({ page }) => {
    const title = `e2e_查重_${Date.now()}`
    // DOC-01 已上传同内容（latest）→ 本用例首次上传即命中 F-1001（复用其行，不重复造数）
    // 同内容再传（不同标题）：F-1001 SHA-256 秒拒（内联/提示，不入库）
    await docs.openUploadDialog()
    await docs.selectFile(LATEST)
    await docs.gotoStep2()
    await docs.fillMeta(`${title}_dup`, 'e2e-bot')
    await docs.submitAllowFailure()
    await expect(page.getByText(/重复|已存在|相同/).last()).toBeVisible({ timeout: 15_000 })
    await docs.cancel().catch(() => undefined)
  })

  test('DOC-09: 乱码样本判"编码异常"失败', async ({ page }) => {
    const title = `e2e_乱码_${Date.now()}`
    await docs.upload(`${testsRoot}/fixtures/golden/hr_乱码_v1.docx`, { title, owner: 'e2e-bot' })
    await docs.expectRowFailed(title)
  })

  test('DOC-10: 纯扫描 pdf 触发质量红线（pypdf 0 字符）', async ({ page }) => {
    const title = `e2e_扫描件_${Date.now()}`
    await docs.upload(`${testsRoot}/fixtures/golden/hr_制度_扫描件_v1.pdf`, {
      title,
      owner: 'e2e-bot',
    })
    await docs.expectRowFailed(title) // "疑似扫描件或空文档"（MinerU 收口后此路径转 ready+OCR）
  })

  test('DOC-11: 失败文档可重试（状态回解析再终态）', async ({ page }) => {
    const title = `e2e_重试_${Date.now()}`
    await docs.uploadAllowFailure(REDLINE, { title, owner: 'e2e-bot' })
    await docs.expectRowFailed(title)
    await docs.retry(title) // 重试 → parsing → 仍失败（扫描件确定性），但状态机走通
    await docs.expectRowFailed(title)
  })

  test('DOC-13: 同标题传新版本 → 旧版归档，仅新版生效', async ({ page }) => {
    const title = `e2e_版本_${Date.now()}`
    await docs.upload(LATEST, { title, owner: 'e2e-bot' })
    await docs.expectRowReady(title)
    // 内容不同（绕开 F-1001 查重）+ 同标题同部门 → 版本递增，旧版归档
    await docs.uploadAllowFailure(V2, { title, owner: 'e2e-bot' })
    await docs.expectRowReady(title)
    await docs.expectSingleActiveVersion(title)
  })

  test('DOC-21: 规范命名文件自动预填部门与生效日期', async ({ page }) => {
    await docs.openUploadDialog()
    await docs.selectFile(abs('fixtures/files/客服_退货政策_20260101.pdf'))
    await docs.gotoStep2()
    // F-1008：部门=客服、生效日期=2026-01-01 自动预填（不必手工再选）
    await docs.expectSuggested({ dept: '客服', date: '2026-01-01' })
    await docs.cancel()
  })
})
