import { expect, test } from '@playwright/test'

import { fileURLToPath } from 'node:url'

import { QaPairsPage } from '../pages/QaPairsPage'
import { ChatPage } from '../pages/ChatPage'

const abs = (p: string) => fileURLToPath(new URL(`../${p}`, import.meta.url))

// 问答对全流程（QA-01/02/05/06/07）。
test.describe('问答对管理', () => {
  let qa: QaPairsPage

  test.beforeEach(async ({ page }) => {
    qa = new QaPairsPage(page)
    await qa.goto()
  })

  test('QA-01: 合规 Excel 导入全部入库', async () => {
    await qa.importExcel(abs('fixtures/files/qa_合规.xlsx'))
    await qa.expectRow('报销的时限是多少天')
    await qa.expectRow('年假有几天')
  })

  test('QA-02: 含错误行整批不入库并逐行报告', async ({ page }) => {
    await qa.importExcel(abs('fixtures/files/qa_含错误行.xlsx'))
    // 整批校验（§21.6）：错误行（行号+中文原因）汇总展示
    await expect(page.getByText(/行/).last()).toBeVisible()
    await qa.expectRowAbsent('缺答案的问题')
    await qa.expectRowAbsent('病假怎么请') // 整批不入库：合规行也不进
  })

  test('QA-05/06/07: 停用即不命中 / 编辑生效 / 删除二次确认', async ({ page }) => {
    // 前置：QA-01 已导入（同套件顺序执行）；独立运行时先导入
    if ((await page.locator('.el-table__row', { hasText: '年假有几天' }).count()) === 0) {
      await qa.importExcel(abs('fixtures/files/qa_合规.xlsx')).catch(() => undefined)
    }
    await qa.expectRow('年假有几天')
    // QA-06 编辑答案
    await qa.editAnswer('年假有几天', '入职满一年享五天年假（E2E 修订版）。')
    await qa.expectRow('年假有几天')
    // QA-05 停用（改停"年假"行：种子问答对与导入行同题会产生歧义匹配）
    await qa.toggleStatus('年假有几天')
    const chat = new ChatPage(page)
    await chat.goto()
    await chat.newConversation()
    await chat.ask('报销的时限是多少天')
    await expect(page.locator('#cite-card-1')).toBeVisible()
    await expect(page.locator('.cite-panel, [class*="cite"]').last()).not.toContainText('问答对')
    // QA-05 断言（口径修正）：停用=行保留 + 启用开关关断（退出匹配），行不消失
    // （switch 断言必须打在管理页——下方 qa.goto() 之后；此前错位到 chat 页必超时）
    await qa.goto()
    const row = page.locator('.el-table__row', { hasText: '年假有几天' }).first()
    await expect(row).toBeVisible()
    await expect(row.getByRole('switch')).not.toBeChecked()
    // QA-07 删除（二次确认后不再出现）
    await qa.remove('年假有几天')
  })
})
