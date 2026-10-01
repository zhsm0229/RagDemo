import { expect, test } from '@playwright/test'

// SEC-05（P0）：匿名（未登录）访问全部页面与 API —— 登录接口除外。
test.describe('匿名访问', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  const PAGES = ['/chat', '/kb/documents', '/kb/qa-pairs', '/admin/users', '/admin/audit']

  for (const path of PAGES) {
    test(`SEC-05: 未登录访问 ${path} 被守卫重定向登录页`, async ({ page }) => {
      await page.goto(path)
      await expect(page).toHaveURL(/login/)
    })
  }

  test('SEC-05: 未登录调业务 API 返回 401', async ({ request }) => {
    const res = await request.get('/api/documents')
    expect(res.status()).toBe(401)
    const me = await request.get('/api/auth/me')
    expect(me.status()).toBe(401)
  })
})
