import { expect, test } from '@playwright/test'

import { ChatPage, REFUSE_TEXT } from '../pages/ChatPage'

// SEC-01（P0 红线）UI 冒烟子集（l1-hr 项目：storageState = e2e-hr，人力 member）。
// SEC-03 串库拒答的强断言（双向各 ≥10 题真实检索 100% 拒答）在评测集与 L2 真实层——
// L1 stub 向量无语义且 rerank 分恒高，拒答语义不可测（见 specs/chat-real.spec.ts）。
test('SEC-01: hr 账号不可见管理导航且管理路由被守卫拦截', async ({ page }) => {
  await page.goto('/admin/users')
  // 守卫重定向回非管理页（router/guards.resolveGuard，member 访问 requiresAdmin）
  await expect(page).not.toHaveURL(/\/admin\//)
  // 侧边导航无任何管理分组入口（前端路由守卫只是体验层，后端 require_admin 才是权威）
  await expect(page.getByText('用户管理')).toBeHidden()
})

test('SEC-03 @real: hr 提问客服域问题被拒答（串库真实链路）', async ({ page }) => {
  test.skip(process.env.E2E_REAL !== '1', 'L2 真实链路用例：需 E2E_REAL=1')
  const chat = new ChatPage(page)
  await chat.goto()
  await chat.newConversation()
  await chat.ask('客服部门的排班与话术管理制度是什么')
  await expect(page.getByText(REFUSE_TEXT).last()).toBeVisible()
})
