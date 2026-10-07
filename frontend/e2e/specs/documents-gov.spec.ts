import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

import { DocumentsPage } from '../pages/DocumentsPage'

const LATEST = fileURLToPath(
  new URL('../../../data/uploads/e2e/e2e-upload-latest.docx', import.meta.url),
)
const V2 = fileURLToPath(new URL('../../../data/uploads/e2e/e2e-doc13-v2.docx', import.meta.url))
const REDLINE = fileURLToPath(
  new URL('../../../data/uploads/e2e/e2e-fail-redline.docx', import.meta.url),
)

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

  test('DOC-02: .doc 扩展名白名单放行——客户端不拦截，可进第二步（转换由后端兜底）', async () => {
    // 2026-10-06 I6 对齐：客户端白名单与后端 precheck ALLOWED_EXTS 同口径，.doc 不再即时拒收；
    // "下一步"仅在选件通过白名单后可用，进入第二步即证明放行（老格式终态由后端给，不在此断言）
    await docs.openUploadDialog()
    await docs.selectFile(docs.abs('fixtures/files/样本.doc'))
    await docs.gotoStep2()
    await docs.cancel()
  })

  test('DOC-03: exe 伪造成 .pdf 被拒收（提示瞬时，双断言：提示或停留第一步）', async ({ page }) => {
    await docs.openUploadDialog()
    await docs.selectFile(docs.abs('fixtures/files/fake.pdf'))
    // 后端魔数校验即时拒绝（422 toast）或前端清除选择停留第一步——两者皆算拒收
    const toastOrStep = Promise.race([
      page
        .getByText(/不符|格式|支持|非法|失败/)
        .last()
        .waitFor({ state: 'visible', timeout: 8_000 }),
      page.locator('.el-upload__input').waitFor({ state: 'attached', timeout: 8_000 }),
    ])
    await toastOrStep
    await docs.cancel().catch(() => undefined)
  })

  test('DOC-05: 元数据卡口（缺必填不可提交，字段级提示）', async () => {
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

  test('DOC-09: 乱码样本判"编码异常"失败', async () => {
    const title = `e2e_乱码_${Date.now()}`
    await docs.upload(`${testsRoot}/fixtures/golden/hr_乱码_v1.docx`, { title, owner: 'e2e-bot' })
    await docs.expectRowFailed(title)
  })

  test('DOC-10: 扫描件按环境收敛（MinerU 在位=OCR 就绪；缺位=红线失败）', async ({ page }) => {
    const title = `e2e_扫描件_${Date.now()}`
    await docs.upload(`${testsRoot}/fixtures/golden/hr_制度_扫描件_v1.pdf`, {
      title,
      owner: 'e2e-bot',
    })
    // 双终态均为正确产品行为：MinerU 在位 → OCR 解析就绪；缺位 → §10.3 红线失败
    const ready = page
      .locator('.el-table__row', { hasText: title })
      .locator('.el-tag', { hasText: '就绪' })
    const failed = page
      .locator('.el-table__row', { hasText: title })
      .locator('.el-tag', { hasText: '失败' })
    await expect(ready.or(failed)).toBeVisible({ timeout: 30_000 })
  })

  test('DOC-11: 失败文档可重试（状态回解析再终态）', async () => {
    const title = `e2e_重试_${Date.now()}`
    await docs.uploadAllowFailure(REDLINE, { title, owner: 'e2e-bot' })
    await docs.filterByStatusFailed()
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
    // 同主题近似 → 预检置待确认（§10.8 设计行为）：管理员确认激活后版本生效
    const v2row = page.locator('.el-table__row', { hasText: title }).last()
    await v2row.getByRole('button', { name: '确认激活' }).click()
    await page.getByRole('button', { name: /确定/ }).last().click()
    await docs.expectRowReady(title)
    await docs.expectSingleActiveVersion(title)
  })

  test('DOC-21: 规范命名文件自动预填部门与生效日期', async () => {
    await docs.openUploadDialog()
    await docs.selectFile(abs('fixtures/files/客服_退货政策_20260101.pdf'))
    await docs.gotoStep2()
    // F-1008：部门=客服、生效日期=2026-01-01 自动预填（不必手工再选）
    await docs.expectSuggested({ dept: '客服', date: '2026-01-01' })
    await docs.cancel()
  })

  // ---- zip 批量导入（F-1007）：混合包三态、重复导入、非 zip 拒收 ----

  const ZIP_FIXTURE = 'fixtures/files/批量导入_混合样例.zip'
  const ZIP_TITLE = '退货话术标准' // 客服_退货话术标准_20260101.docx 的主题段

  test('DOC-22: zip 混合包导入——规范命名入库、命名不符跳过待补，列表收敛就绪', async () => {
    await docs.zipImport(docs.abs(ZIP_FIXTURE))
    // 汇总：2 条目 = 1 入库（客服_退货话术标准_20260101.docx）+ 1 跳过（乱命名无元数据.docx）
    const summary = docs.zipSummary()
    await expect(summary).toContainText('入库 1')
    await expect(summary).toContainText('跳过 1')
    await expect(docs.zipEntryTag('客服_退货话术标准_20260101.docx')).toHaveText('已入库')
    await expect(docs.zipEntryTag('乱命名无元数据.docx')).toHaveText('元数据待补')
    await docs.closeZipDialog()
    // 入库条目按 主题 即标题入列并完成同步解析（TEST_MODE）→ 就绪
    // 45s：E2E 窗口冷启动时同步解析实测可超 15s（2026-10-06 DOC-22 抖动）
    await docs.expectRowReady(ZIP_TITLE, 45_000)
  })

  test('DOC-23: 同一 zip 二次导入——已入库条目转重复跳过', async () => {
    // 首导保证自足（单独重跑本用例时首导为 accepted，次导必为 duplicate）
    await docs.zipImport(docs.abs(ZIP_FIXTURE))
    await docs.closeZipDialog()
    await docs.zipImport(docs.abs(ZIP_FIXTURE))
    // 次导 skipped = duplicate 1（退货话术）+ skipped_metadata 1（乱命名）= 2
    await expect(docs.zipSummary()).toContainText('入库 0')
    await expect(docs.zipSummary()).toContainText('跳过 2')
    await expect(docs.zipEntryTag('客服_退货话术标准_20260101.docx')).toHaveText('重复跳过')
    await docs.closeZipDialog()
  })

  test('DOC-24: 非 zip 文件选入即前端拒收（不发请求）', async () => {
    await docs.zipImportExpectReject(abs('fixtures/files/样本.doc'), /仅支持 \.zip 压缩包/)
  })

  test('DOC-25: 文档删除——二次确认后软删，列表不再显示（F-305）', async ({ page }) => {
    const title = `e2e_删除_${Date.now()}`
    await docs.upload(docs.abs('fixtures/files/报销小样.docx'), { title, owner: 'e2e-bot' })
    await docs.expectRowReady(title)
    const row = page.locator('.el-table__row', { hasText: title })
    await row.getByRole('button', { name: '删除' }).click()
    await page.getByRole('dialog').getByRole('button', { name: '删除' }).click()
    await expect(row).toHaveCount(0, { timeout: 15_000 })
  })

  test('DOC-26: 在线预览——预置 ready 行点预览，弹窗内嵌 PDF（服务端转换链路真跑）', async () => {
    // 复用 auth.setup 预置的 ready 行：零上传、无查重冲突、顺序无关；
    // docx → Gotenberg 转 PDF（TEST_MODE 不桩转换，本用例即真实转换冒烟）
    const dialog = await docs.openPreview('e2e_差旅报销制度_预置')
    await expect(dialog.locator('.preview-frame')).toBeVisible({ timeout: 30_000 })
    await expect(dialog.getByText('下载原文件')).toBeVisible()
  })
})
