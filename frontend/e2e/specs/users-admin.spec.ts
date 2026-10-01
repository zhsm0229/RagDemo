import { expect, test } from '@playwright/test'

import { LoginPage } from '../pages/LoginPage'
import { UsersPage } from '../pages/UsersPage'

const PASSWORD = process.env.E2E_PASSWORD ?? 'E2ePass#2026'

// ADM-04/05 + AUTH-05（P0/P1）：用户管理全流程（admin storageState）。
test.describe('用户管理', () => {
  let users: UsersPage

  test.beforeEach(async ({ page }) => {
    users = new UsersPage(page)
    await users.goto()
  })

  test('ADM-04: 创建用户发放一次性初始密码（readOnly 展示，首登强改密）', async ({ page }) => {
    const username = `小张${Date.now() % 10000}`
    const initial = await users.createUser(username)
    expect(initial.length).toBeGreaterThanOrEqual(8)
    // 新账号首登应被强制改密（用初始密码登录 → 落改密页）
    await page.context().clearCookies() // 清管理员会话：/login 对已登录用户会重定向
    const login = new LoginPage(page)
    await login.goto()
    await login.login(username, initial)
    await expect(page).toHaveURL(/change-password/)
  })

  test('ADM-05: 用户名校验（重名/1 字）内联拦截', async ({ page }) => {
    const dupErr = await users.createUserExpectError('e2e-hr') // 重名（已存在种子账号）
    expect(dupErr.length).toBeGreaterThan(0) // 后端 409 提示（toast/内联均有中文文案）
    const shortErr = await users.createUserExpectError('张')
    expect(shortErr).toContain('2-32')
  })

  test('AUTH-05: 禁用即时生效（禁用后该账号登录被拒）', async ({ page }) => {
    const username = `停用员${Date.now() % 10000}`
    const initial = await users.createUser(username)
    await users.toggleActive(username) // 禁用
    await page.context().clearCookies() // 清管理员会话：/login 对已登录用户会重定向
    const login = new LoginPage(page)
    await login.goto()
    await login.login(username, initial)
    await expect(page).toHaveURL(/login/) // 禁用门：登录即拒（终审 I-1 验密后同码拒绝）
  })

  test('ADM-06: 重置密码后旧密码失效新初始密码生效', async ({ page }) => {
    const username = `重置员${Date.now() % 10000}`
    const oldPwd = await users.createUser(username)
    const newPwd = await users.resetPassword(username)
    expect(newPwd).not.toEqual(oldPwd)
    await page.context().clearCookies() // 清管理员会话：/login 对已登录用户会重定向
    const login = new LoginPage(page)
    await login.goto()
    await login.login(username, oldPwd)
    await expect(page).toHaveURL(/login/) // 旧密码失效
    await login.login(username, newPwd)
    await expect(page).toHaveURL(/change-password/) // 新初始密码生效 + 首登强改密
  })
})
