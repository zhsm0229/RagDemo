import { expect, test } from '@playwright/test'

import { ChatPage } from '../pages/ChatPage'

// 缓存命中（CHAT-05 P1）与会话管理（CHAT-15 P1）。
test.describe('缓存与会话管理', () => {
  let chat: ChatPage
  const QUESTION = '报销的时限是多少天'

  test.beforeEach(async ({ page }) => {
    chat = new ChatPage(page)
    await chat.goto()
  })

  test('CHAT-05: 5 分钟内重复同问题命中缓存（"来自缓存"标注）', async ({ page }) => {
    await chat.newConversation() // 首问也须新会话（默认选中历史会话会污染 stub 向量）
    await chat.ask(QUESTION)
    await chat.expectCitationCard(1) // 首答完成
    await chat.newConversation() // 新会话同部门同问题 → 缓存键相同（§17.5）
    await chat.ask(QUESTION)
    // deviations #47：缓存命中仅 meta"来自缓存"标注，无 toast
    await expect(page.getByText('来自缓存')).toBeVisible()
    await chat.expectCitationCard(1) // 引用随缓存一并回放
  })

  test('CHAT-15: 删除会话需二次确认，删除后不可见', async ({ page }) => {
    await chat.newConversation()
    await chat.ask(QUESTION)
    await chat.expectConversationListed(QUESTION)
    const item = page.locator('.conv-item', { hasText: QUESTION }).first()
    await item.hover()
    await item.getByRole('button', { name: '删除' }).click({ force: true })
    // 二次确认（ElMessageBox，§22.2 禁遮罩关闭）
    await page.getByRole('button', { name: /确定|删除/ }).last().click()
    await expect(page.locator('.conv-item', { hasText: QUESTION })).toHaveCount(0)
  })
})
