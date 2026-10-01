import { expect, test } from '@playwright/test'

import { LoginPage } from '../pages/LoginPage'

// AUTH-02/03（P0/P1）：连续错误计数 → 第 5 次锁定 → 锁定期间正确密码也拒绝。
// 单用例串完 F6 流程（AUTH-02 留 4 次计数，AUTH-03 第 5 次触发锁定），账号 e2e-hr。
// 锁定态由 e2e_seed 每轮复位（AUTH-03 锁定审计已由后端测试覆盖，ADM-07 解锁无 UI
// 入口为已登记偏差 deviations #37）。
test.describe('登录锁定流程', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  const PASSWORD = process.env.E2E_PASSWORD ?? 'E2ePass#2026'

  test('AUTH-02/03: 错 4 次提示剩余次数，第 5 次锁定且正确密码也拒绝', async ({ page }) => {
    const login = new LoginPage(page)
    await login.goto()
    for (let i = 0; i < 4; i++) {
      await login.login('e2e-lock', `wrong-${i}`)
      await expect(page.getByText(/剩余尝试/)).toBeVisible() // AUTH-02：每次提示剩余次数
      await expect(page).toHaveURL(/login/)
    }
    await login.login('e2e-lock', 'wrong-5th')
    await expect(page.getByText(/锁定/)).toBeVisible() // AUTH-03：锁定提示
    // 锁定期间正确密码同样拒绝（防爆破语义）
    await login.login('e2e-lock', PASSWORD)
    await expect(page).toHaveURL(/login/)
    await expect(page.getByText(/锁定|错误/).last()).toBeVisible()
  })

  // AUTH-09（P1）：失败 3 次 → 成功清零 → 再错 1 次不锁定（账号 e2e-svc）。
  test('AUTH-09: 成功登录清零失败计数', async ({ page }) => {
    const login = new LoginPage(page)
    await login.goto()
    for (let i = 0; i < 3; i++) {
      await login.login('e2e-svc', `wrong-${i}`)
      await expect(page.getByText(/剩余尝试/)).toBeVisible()
    }
    await login.login('e2e-svc', PASSWORD)
    await login.expectAtChat()
    // 退出后用错密码 1 次：计数应从 0 重新累计（剩余 4 次，不锁定）
    await page.getByRole('button', { name: '退出' }).click()
    await expect(page).toHaveURL(/login/)
    await login.login('e2e-svc', 'wrong-once')
    await expect(page.getByText(/剩余尝试次数 \d/)).toBeVisible()
  })
})
