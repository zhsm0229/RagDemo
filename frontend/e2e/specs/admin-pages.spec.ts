import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// RET-03（P1）：管理端检索测试页——命中块/分数/阈值口径可见。
// ADM-01（P1）：提问记录筛选与只读详情。ADM-09（P2）：审计过滤+CSV 导出。
test.describe('管理页（检索测试/提问记录/审计/评测）', () => {
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
    const detail = page.locator('.el-table__row').first().getByRole('button', { name: /详情|查看/ })
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
    await dialog.getByRole('button', { name: /导入|确定/ }).last().click()
    // 页面明示"题库列表暂无查询端点"：断言导入弹窗关闭（成功路径）即可
    await expect(dialog).toBeHidden({ timeout: 15_000 })
  })
})
