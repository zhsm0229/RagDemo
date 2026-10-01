import { expect, test } from '@playwright/test'

import { ChatPage } from '../pages/ChatPage'

// 输入边界（CHAT-13/14 P0/P1，§21.5）+ XSS（SEC-02 P0，§21.7）。
test.describe('提问输入边界与安全', () => {
  let chat: ChatPage

  test.beforeEach(async ({ page }) => {
    chat = new ChatPage(page)
    await chat.goto()
    await chat.newConversation()
  })

  test('CHAT-13: 1 字与 2000 字可发送，2001 字禁发送', async ({ page }) => {
    const input = page.locator('textarea[placeholder*="输入问题"]')
    const send = page.getByRole('button', { name: '发送' })
    await input.fill('假')
    await expect(send).toBeEnabled()
    await input.fill('长'.repeat(2000))
    await expect(send).toBeEnabled()
    await input.fill('长'.repeat(2001))
    await expect(send).toBeDisabled() // 前端拦截（后端 422 由 API 测试覆盖）
  })

    test('CHAT-14: 全空格输入被拦截（发送禁用）', async ({ page }) => {
    const input = page.locator('textarea[placeholder*="输入问题"]')
    const send = page.getByRole('button', { name: '发送' })
    await input.fill('   ')
    await input.blur()
    // trim 后为空 → 禁发送（ChatInput 失焦/点击同口径内联校验）
    await expect(send).toBeDisabled()
  })


  test('SEC-02: script 标签按纯文本渲染不执行（Vue 默认转义，0 处 v-html）', async ({ page }) => {
    let dialogFired = false
    page.on('dialog', () => {
      dialogFired = true
    })
    const payload = '<script>alert("xss")</script> 什么是报销'
    await chat.ask(payload)
    // 输入被按纯文本回显（用户气泡转义渲染），无 alert 执行
    await expect(page.getByText(payload).first()).toBeVisible()
    expect(dialogFired).toBe(false)
  })
})
