import { expect, test } from '@playwright/test'

import { BadcasePage } from '../pages/BadcasePage'
import { ChatPage } from '../pages/ChatPage'

// 反馈闭环（CHAT-09/10/11 P0/P2 + ADM-03 P0）：点赞/点踩校验/点踩落库→badcase 待处理→处置。
test.describe('反馈与 badcase 闭环', () => {
  let chat: ChatPage
  const QUESTION = '报销的时限是多少天'

  test.beforeEach(async ({ page }) => {
    chat = new ChatPage(page)
    await chat.goto()
    await chat.newConversation()
    await chat.ask(QUESTION)
    await chat.expectCitationCard(1) // 等回答完成（stub 秒级）
  })

  test('CHAT-11: 点赞直接提交（无弹窗）', async ({ page }) => {
    await page.getByRole('button', { name: '👍' }).last().click()
    await expect(page.locator('.el-message').last()).toBeVisible()
  })

  test('CHAT-09: 点踩缺分类/备注 → 字段级内联错误，弹窗不关闭', async ({ page }) => {
    await page.getByRole('button', { name: '👎' }).last().click()
    const dialog = page.locator('.el-dialog', { hasText: /反馈|点踩/ })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: /提交反馈/ }).click()
    // 前端先行校验（分类/备注 strip 后必填），错误内联渲染
    await expect(page.getByText('请选择问题分类')).toBeVisible()
    await expect(dialog).toBeVisible() // 弹窗保持打开（§22.2）
  })

  test('CHAT-10 + ADM-03: 完整点踩 → badcase 待处理 → 处置流转', async ({ page }) => {
    await page.getByRole('button', { name: '👎' }).last().click()
    const dialog = page.locator('.el-dialog', { hasText: /反馈|点踩/ })
    // 分类单选（枚举第一项）+ 备注必填
    await dialog.locator('.el-radio, .el-select').first().click()
    const option = page.locator('.el-select-dropdown__item:visible, .el-radio:visible').first()
    await option.click().catch(() => undefined)
    await dialog.locator('textarea').fill('自动化点踩：回答与制度原文不一致（E2E 造数）')
    await dialog.getByRole('button', { name: /提交反馈/ }).click()
    await expect(dialog).toBeHidden()

    // badcase 页出现"待处理"行
    const badcase = new BadcasePage(page)
    await badcase.goto()
    await badcase.expectPendingRow(QUESTION)
    // 忽略不填理由 → 内联拦截（ADM-03 前端先行校验）
    await badcase.openDispose(QUESTION)
    const inlineErr = await badcase.ignoreWithoutReason()
    expect(inlineErr).not.toBe('')
    // 补理由提交 → 流转完成，待处理视图不再出现该行
    await badcase.dispose('E2E 处置：已核对制度原文，按需修订语料')
    await expect(page.locator('.el-table__row', { hasText: QUESTION })).toHaveCount(0)
  })
})
