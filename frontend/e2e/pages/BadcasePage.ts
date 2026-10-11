import { expect, type Page } from '@playwright/test'

/** badcase 管理 PO（BadcaseView：默认视图=待处理；处置对话框 radio+备注，已忽略必填理由）。 */
export class BadcasePage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/admin/badcase')
  }

  async expectPendingRow(questionPart: string): Promise<void> {
    await expect(
      this.page.locator('.el-table__row', { hasText: questionPart }).first(),
    ).toBeVisible()
  }

  /** 打开行处置对话框（仅待处理行有"处理"入口）。 */
  async openDispose(questionPart: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: questionPart }).first()
    await row.getByRole('button', { name: /处理/ }).click()
    return void (await expect(this.page.locator('.el-dialog').last()).toBeVisible())
  }

  /** ADM-03：忽略不填理由 → 前端内联拦截，弹窗不关。返回内联错误文本。 */
  async ignoreWithoutReason(): Promise<string> {
    const dialog = this.page.locator('.el-dialog').last()
    // 点 .el-radio 容器（EP 原生 input 隐藏，check()/getByText 都可能不切换）；
    // 不切中「忽略」则默认「更新文档」模式备注非必填，提交会直接成功关弹窗
    const ignoreRadio = dialog.locator('.el-radio', { hasText: '忽略' })
    await ignoreRadio.click()
    await expect(ignoreRadio.locator('input')).toBeChecked()
    await dialog.getByRole('button', { name: /确定|提交/ }).click()
    const err = dialog.locator('.el-form-item__error').last()
    await expect(err).toBeVisible()
    const text = (await err.textContent()) ?? ''
    await expect(dialog).toBeVisible() // 弹窗保持打开（§22.2）
    return text
  }

  /** 处置提交（radio 处理/忽略 + 理由），成功关弹窗。 */
  async dispose(reason: string, mode: '处理' | '忽略' = '处理'): Promise<void> {
    const dialog = this.page.locator('.el-dialog').last()
    // radio 实际文案：「更新文档/补问答对」=处理、「忽略（说明理由）」=忽略
    const radioText = mode === '处理' ? '更新文档' : '忽略'
    await dialog.locator('.el-radio', { hasText: radioText }).click()
    const note = dialog.locator('textarea')
    if (await note.count()) await note.fill(reason)
    await dialog.getByRole('button', { name: /确定|提交/ }).click()
    await expect(dialog).toBeHidden()
  }
}
