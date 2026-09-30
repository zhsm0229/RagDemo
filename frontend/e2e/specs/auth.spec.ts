import { expect, test } from '@playwright/test'

import { LoginPage } from '../pages/LoginPage'

// 清空继承的 storageState：AUTH-01/04 验证的是"登录动作本身"，必须从未登录态出发。
test.use({ storageState: { cookies: [], origins: [] } })

test('AUTH-01: admin 正确凭据登录进入问答页且可见管理导航', async ({ page }) => {
  const login = new LoginPage(page)
  await login.goto()
  await login.login('e2e-admin', process.env.E2E_PASSWORD ?? 'E2ePass#2026')
  await login.expectAtChat()
  // 管理员可见管理分组（member 仅智能问答，§5.5 前端路由守卫只是体验层）
  await expect(page.getByText('用户管理')).toBeVisible()
})

test('AUTH-01b: 错误凭据被拒且停留在登录页', async ({ page }) => {
  const login = new LoginPage(page)
  await login.goto()
  await login.login('e2e-admin', 'wrong-password-once') // 仅 1 次，远低于锁定阈值
  await expect(page).toHaveURL(/login/)
})

test('AUTH-04: 首登强制改密后才能进入业务页', async ({ page }) => {
  const login = new LoginPage(page)
  await login.goto()
  await login.login('e2e-fresh', process.env.E2E_PASSWORD ?? 'E2ePass#2026')
  // 守卫强制落到改密页（API 层 403/2004 的前端体验面）
  await expect(page).toHaveURL(/change-password/)
  // 直接访问业务路由仍被拦
  await page.goto('/kb/documents')
  await expect(page).toHaveURL(/change-password/)
  // 完成改密（种子会在下轮重置回 must_change，幂等）
  await page.locator('.el-form-item', { hasText: '原密码' }).locator('input').fill(
    process.env.E2E_PASSWORD ?? 'E2ePass#2026',
  )
  await page.locator('.el-form-item', { hasText: '新密码' }).locator('input').fill('E2eRotated#2026')
  await page.locator('.el-form-item', { hasText: '确认密码' }).locator('input').fill('E2eRotated#2026')
  await page.locator('button.submit').click()
  await expect(page).toHaveURL(/\/chat/)
})
