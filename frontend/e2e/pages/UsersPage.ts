import { expect, type Page } from '@playwright/test'

/** 用户管理 PO（UsersView：创建/重置密码/启停；初始密码一次性展示弹窗）。 */
export class UsersPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/admin/users')
  }

  /** 创建用户并返回一次性初始密码（弹窗 readOnly 输入框的值）。 */
  async createUser(username: string, deptLabel = '全员', role = '普通成员'): Promise<string> {
    await this.page.getByRole('button', { name: /新建用户/ }).click()
    const dialog = this.page.locator('.el-dialog', { hasText: /新建用户|创建用户/ })
    await dialog.locator('input[placeholder*="2-32"]').fill(username)
    await dialog.locator('.el-form-item', { hasText: '部门' }).locator('.el-select').click()
    await this.page.locator('.el-select-dropdown__item:visible', { hasText: deptLabel }).click()
    if (role !== '普通成员') {
      await dialog.locator('.el-form-item', { hasText: '角色' }).locator('.el-select').click()
      await this.page.locator('.el-select-dropdown__item:visible', { hasText: role }).click()
    }
    await dialog.getByRole('button', { name: /创建并生成初始密码/ }).click()
    // 初始密码一次性展示弹窗（§8.8 readOnly 置灰可复制）
    const pwdDialog = this.page.locator('.el-dialog:has(input[aria-label="初始密码"])')
    await expect(pwdDialog).toBeVisible()
    const value = await pwdDialog.locator('input[aria-label="初始密码"]').inputValue()
    await pwdDialog.getByRole('button', { name: /我已保存/ }).click()
    await expect(pwdDialog).toBeHidden()
    return value
  }

  /** ADM-05：提交创建并返回弹窗内联错误文本（不关弹窗）。 */
  /** ADM-05：提交创建并返回错误提示（格式错=表单内联；重名=后端 409 toast）。 */
  async createUserExpectError(username: string): Promise<string> {
    // 连续调用防残留：前次 409 弹窗保持打开时旧表单值仍在，本次点击「新建用户」
    // 不重开、下拉交互落空 → 提交报"请选择部门"而非用户名校验（2026-10-11 ADM-05）
    const existing = this.page
      .locator('.el-dialog', { hasText: /新建用户|创建用户/ })
      .filter({ has: this.page.getByRole('button', { name: /创建并生成初始密码/ }) })
    if (await existing.isVisible().catch(() => false)) {
      await existing.getByRole('button', { name: /取消/ }).click()
      await expect(existing).toBeHidden()
    }
    await this.page.getByRole('button', { name: /新建用户/ }).click()
    const dialog = this.page.locator('.el-dialog', { hasText: /新建用户|创建用户/ })
    await dialog.locator('input[placeholder*="2-32"]').fill(username)
    await dialog.locator('.el-form-item', { hasText: '部门' }).locator('.el-select').click()
    await this.page.locator('.el-select-dropdown__item:visible', { hasText: '全员' }).click()
    await dialog.getByRole('button', { name: /创建并生成初始密码/ }).click()
    const inline = dialog.locator('.el-form-item__error').last()
    const toast = this.page.locator('.el-message').last()
    await Promise.race([
      inline.first().waitFor({ state: 'visible', timeout: 10_000 }),
      toast.waitFor({ state: 'visible', timeout: 10_000 }),
    ])
    const text = (await inline
      .first()
      .isVisible()
      .catch(() => false))
      ? ((await inline.first().textContent()) ?? '')
      : ((await toast.textContent()) ?? '')
    await dialog
      .getByRole('button', { name: /取消/ })
      .click()
      .catch(() => undefined)
    await expect(dialog).toBeHidden()
    return text
  }

  /** 重置密码（ADM-06）：popconfirm 确认，返回新初始密码。 */
  async resetPassword(username: string): Promise<string> {
    const row = this.page.locator('.el-table__row', { hasText: username }).first()
    await row.getByRole('button', { name: '重置密码' }).click()
    await this.page.getByRole('button', { name: /确认|确定/ }).click()
    const pwdDialog = this.page.locator('.el-dialog:has(input[aria-label="初始密码"])')
    await expect(pwdDialog).toBeVisible()
    const value = await pwdDialog.locator('input[aria-label="初始密码"]').inputValue()
    await pwdDialog.getByRole('button', { name: /我已保存/ }).click()
    return value
  }

  /** 启停开关（AUTH-05 禁用 / 恢复）。 */
  async toggleActive(username: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: username }).first()
    // 操作列 popconfirm：引用按钮（禁用/启用）→ 气泡确认
    await row.getByRole('button', { name: /禁用|启用/ }).click()
    await this.page.getByRole('button', { name: /确定/ }).last().click()
    await expect(this.page.locator('.el-message').last()).toBeVisible()
  }
}
