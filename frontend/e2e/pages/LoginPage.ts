import { expect, type Page } from '@playwright/test'

/** 登录页 PO（LoginView：placeholder 用户名/密码 + 提交按钮）。 */
export class LoginPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/login')
  }

  async login(username: string, password: string): Promise<void> {
    await this.page.getByPlaceholder('用户名').fill(username)
    await this.page.getByPlaceholder('密码').fill(password)
    await this.page.getByRole('button', { name: /登\s*录/ }).click()
  }

  async expectAtChat(): Promise<void> {
    await expect(this.page).toHaveURL(/\/chat/)
  }
}
