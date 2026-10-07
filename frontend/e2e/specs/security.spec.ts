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

// SEC-AGENT-01（agent-evolution #09）：agent 场景的部门隔离（F-502 口径）。
// /kb/template-gen 路由 requiresAdmin（member 被 UI 守卫拦截），但 /api/agent/runs
// 面向全员——后端才是权威边界。hr 账号经 API 运行 template_draft：
// ①运行正常收尾（RUN_STARTED→RUN_FINISHED，无 RUN_ERROR）；
// ②客服专属语料（auth.setup 预置的仅客服可见文档，稳定标记 SVCONLY-7f3a92）不出现在
//   产出与事件流任何位置——检索工具 dept_ids 在服务端强制继承调用者上下文（[hr, 全员]）；
// ③草稿产物即 stub 脚本正文（确定性），工具参数只含 query、不夹带部门字段。
test('SEC-AGENT-01: hr 运行 template_draft，产出与事件流不含客服域专属语料', async ({ page }) => {
  const res = await page.request.post('/api/agent/runs', {
    data: { taskType: 'template_draft', input: '请为「客服」部门生成 2 套投诉处理话术模板草稿' },
  })
  expect(res.status()).toBe(200)
  const body = await res.text()
  expect(body).toContain('event: RUN_STARTED')
  expect(body).toContain('event: RUN_FINISHED')
  expect(body).not.toContain('event: RUN_ERROR')
  expect(body).not.toContain('SVCONLY-7f3a92') // 客服专属语料标记零越界
  expect(body).toContain('根据《考勤制度》，年假按工龄五到十天。') // 产物 = 确定性脚本正文
  expect(body).not.toContain('"dept"') // 工具参数/事件负载不夹带部门字段
})
