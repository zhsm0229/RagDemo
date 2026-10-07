import { expect, type Page } from '@playwright/test'

/** 问答对管理 PO（QaPairsView：筛选行 + Excel 批量导入 + 行内启停/编辑/删除）。 */
export class QaPairsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/kb/qa-pairs')
  }

  async importExcel(filePath: string): Promise<void> {
    await this.page
      .getByRole('button', { name: 'Excel 批量导入' })
      .locator('visible=true')
      .first()
      .click()
    const dialog = this.page.locator('.el-dialog', { hasText: '导入' })
    const abs = path.isAbsolute(filePath)
      ? filePath
      : fileURLToPath(new URL(`../${filePath}`, import.meta.url))
    await dialog.locator('input[type="file"]').setInputFiles(abs)
    await dialog
      .getByRole('button', { name: /导入|确定/ })
      .last()
      .click()
  }

  async expectRow(question: string): Promise<void> {
    await expect(this.page.locator('.el-table__row', { hasText: question }).first()).toBeVisible()
  }

  async expectRowAbsent(question: string): Promise<void> {
    await expect(this.page.locator('.el-table__row', { hasText: question })).toHaveCount(0)
  }

  /** 行内启停开关（QA-05：停用后提问不再走问答对通道）。 */
  async toggleStatus(question: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: question }).first()
    await row.locator('.el-switch').click()
    await expect(this.page.locator('.el-message').last()).toBeVisible()
  }

  /** 行内编辑（QA-06）：改答案保存。 */
  async editAnswer(question: string, newAnswer: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: question }).first()
    await row.getByRole('button', { name: /编辑/ }).click({ force: true })
    const dialog = this.page.locator('.el-dialog', { hasText: /编辑/ })
    const answerBox = dialog.locator('textarea').last()
    await answerBox.fill(newAnswer)
    await dialog.getByRole('button', { name: /保存|确定/ }).click()
    await expect(dialog).toBeHidden()
  }

  /** 行内删除（QA-07）：二次确认。 */
  async remove(question: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: question }).first()
    await row.getByRole('button', { name: /删除/ }).click()
    await this.page.getByRole('button', { name: /确定/ }).click()
    await this.expectRowAbsent(question)
  }
}

import path from 'node:path'
import { fileURLToPath } from 'node:url'
