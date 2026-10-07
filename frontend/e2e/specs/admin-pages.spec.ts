import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// RET-03（P1）：管理端检索测试页——命中块/分数/阈值口径可见。
// ADM-01（P1）：提问记录筛选与只读详情。ADM-09（P2）：审计过滤+CSV 导出。
// SVC-01（2026-10-02）：运营概览服务健康卡——依赖探活只读展示。
test.describe('管理页（检索测试/提问记录/审计/评测）', () => {
  test('SVC-01: 运营概览服务健康卡渲染（探活表+overall 横幅+刷新）', async ({ page }) => {
    await page.goto('/admin/overview')
    const panel = page.locator('[data-test="services-health-panel"]')
    await expect(panel).toBeVisible()
    // overall 横幅必有（up/degraded/down 三态之一；E2E 环境 mineru 未起时为 degraded，
    // 故不断言具体值，只断言横幅与表格渲染）
    await expect(panel.locator('[data-test="svc-overall"]')).toBeVisible()
    const table = panel.locator('[data-test="svc-table"]')
    await expect(table).toBeVisible()
    // 六个依赖行齐备且各有状态 tag；pg 行恒为"正常"（测试库在位）
    for (const label of [
      'PostgreSQL',
      '向量化 (TEI)',
      '重排 (TEI)',
      '大模型 (LLM)',
      '文档解析 (MinerU)',
      '老格式转换 (Gotenberg)',
    ]) {
      await expect(table.locator('.el-table__row', { hasText: label })).toBeVisible()
    }
    await expect(
      table
        .locator('.el-table__row', { hasText: 'PostgreSQL' })
        .locator('.el-tag', { hasText: '正常' }),
    ).toBeVisible()
    // 刷新按钮可点且重新渲染（探活异步完成，表格仍可见）
    await panel.locator('[data-test="svc-refresh"]').click()
    await expect(table).toBeVisible()
  })

  test('RET-03: 检索测试返回命中块与两套分数口径', async ({ page }) => {
    await page.goto('/admin/retrieval-test')
    await page.locator('input[placeholder*="输入测试问题"]').fill('报销的时限是多少天')
    await page.getByRole('button', { name: /测\s*试/ }).click()
    await expect(page.locator('.el-table__row').first()).toBeVisible() // 命中表非空
    await expect(page.getByText(/重排分数/)).toBeVisible() // sigmoid 口径列
    await expect(page.getByText(/召回余弦/)).toBeVisible() // embedding 口径列
  })

  test('ADM-01: 提问记录列表可见且详情只读', async ({ page }) => {
    await page.goto('/admin/messages')
    await expect(page.locator('.el-table__row').first()).toBeVisible() // 此前 E2E 提问已落库
    const detail = page
      .locator('.el-table__row')
      .first()
      .getByRole('button', { name: /详情|查看/ })
    if (await detail.count()) {
      await detail.first().click()
      const drawer = page.locator('.el-drawer').last()
      await expect(drawer).toBeVisible()
      await expect(drawer.getByText(/引用|token|耗时/).first()).toBeVisible()
      await page.keyboard.press('Escape')
    }
  })

  test('ADM-09: 审计日志过滤 + CSV 导出', async ({ page }) => {
    await page.goto('/admin/audit')
    await expect(page.locator('.el-table__row').first()).toBeVisible() // 无过滤先见记录
    await page.locator('input[placeholder*="动作"]').fill('user.create')
    await page.getByRole('button', { name: '查询' }).click()
    await expect(page.locator('.el-table__row').first()).toBeVisible() // ADM-04 建号动作已留痕
    const download = page.waitForEvent('download', { timeout: 15_000 }).catch(() => null)
    await page.locator('[data-test="export-btn"]').click()
    expect(await download).not.toBeNull() // CSV 导出可下载
  })

  test('EVAL-01（UI 子集）: 评测题库导入', async ({ page }) => {
    await page.goto('/admin/eval')
    await page.getByRole('tab', { name: '评测题库' }).click()
    await page.getByRole('button', { name: 'Excel 导入题目' }).click()
    const dialog = page.locator('.el-dialog', { hasText: /导入/ })
    const fileInput = dialog.locator('input[type="file"]')
    const xlsxPath = fileURLToPath(new URL('../fixtures/files/eval_题库.xlsx', import.meta.url))
    await fileInput.setInputFiles(xlsxPath)
    await dialog
      .getByRole('button', { name: /导入|确定/ })
      .last()
      .click()
    // 页面明示"题库列表暂无查询端点"：断言导入弹窗关闭（成功路径）即可
    await expect(dialog).toBeHidden({ timeout: 15_000 })
  })

  test('EVAL-02: 发起跑分→报告→阈值扫描曲线（扫描入口解禁回归）', async ({ page }) => {
    await page.goto('/admin/eval')
    await page.getByRole('tab', { name: '跑分记录' }).click()
    await page.getByRole('button', { name: '发起跑分' }).click()
    const createDialog = page.locator('.el-dialog', { hasText: '任务名称' })
    await createDialog.getByPlaceholder(/客服域回归/).fill(`e2e_扫描_${Date.now()}`)
    await createDialog.locator('.el-select').click()
    await page.locator('.el-select-dropdown__item:visible', { hasText: '全员' }).click()
    await createDialog.getByRole('button', { name: '开始跑分' }).click()
    await expect(createDialog).toBeHidden({ timeout: 15_000 })
    // 跑分后台执行（L1 stub 下数秒收敛）；列表轮询刷新状态
    const row = page.locator('.el-table__row', { hasText: 'e2e_扫描_' }).first()
    await expect(row.locator('.el-tag', { hasText: '已完成' })).toBeVisible({ timeout: 60_000 })
    await row.getByRole('button', { name: '报告' }).click()
    const drawer = page.locator('.el-drawer').last()
    await expect(drawer.getByText('分组报告')).toBeVisible()
    // 解禁回归点：扫描按钮历史上被 FEATURE_FLAGS 置灰过，必须保持可点
    const scanBtn = drawer.locator('[data-test="scan-btn"]')
    await expect(scanBtn).not.toBeDisabled()
    await scanBtn.click()
    await expect(drawer.locator('[data-test="refuse-curve"]')).toBeVisible({ timeout: 30_000 })
  })
})
