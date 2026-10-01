import { fileURLToPath } from 'node:url'
import path from 'node:path'

import { expect, type Page } from '@playwright/test'

/** 知识库·文档管理 PO（DocumentsView + UploadDialog 两步上传：文件 → 元数据提交入库）。 */
export class DocumentsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/kb/documents')
  }

  /** 相对路径以 e2e 目录为基准解析（进程 CWD 随调用方漂移，不可依赖）。 */
  abs(filePath: string): string {
    if (path.isAbsolute(filePath)) return filePath
    return fileURLToPath(new URL(`../${filePath}`, import.meta.url))
  }

  private dialog(): ReturnType<Page['locator']> {
    return this.page.locator('.el-dialog', { hasText: '上传文档' })
  }

  /** 一次性两步上传（成功路径）：选文件 → 下一步 → 元数据 → 提交入库。 */
  async upload(filePath: string, meta: { title: string; owner: string }): Promise<void> {
    await this.openUploadDialog()
    await this.selectFile(this.abs(filePath))
    await this.gotoStep2()
    await this.fillMeta(meta.title, meta.owner)
    await this.submit()
  }

  async openUploadDialog(): Promise<void> {
    await this.page.getByRole('button', { name: /上传文档/ }).click()
    await expect(this.dialog()).toBeVisible()
  }

  async selectFile(absPath: string): Promise<void> {
    // EP 弹窗重开会残留上一轮 file-list → 先清空再选，避免"旧文件+新文件"一起提交
    await this.dialog().evaluate((el) => {
      el.querySelectorAll('.el-upload-list__item').forEach((n) => n.remove())
    })
    await this.dialog().locator('input[type="file"]').setInputFiles(absPath)
  }

  async gotoStep2(): Promise<void> {
    await this.dialog().getByRole('button', { name: /下一步/ }).click()
  }

  async fillMeta(title: string, owner: string): Promise<void> {
    const dialog = this.dialog()
    await dialog.locator('.el-form-item', { hasText: '标题' }).locator('input').fill(title)
    await dialog.locator('.el-form-item', { hasText: '部门' }).locator('.el-select').click()
    await this.page.locator('.el-select-dropdown__item:visible', { hasText: '全员' }).click()
    await dialog.locator('.el-form-item', { hasText: '责任人' }).locator('input').fill(owner)
    const dateItem = dialog.locator('.el-form-item', { hasText: '生效日期' })
    await dateItem.locator('input').fill('2026-09-30')
    await dateItem.locator('input').press('Enter')
    await dialog.locator('.el-form-item', { hasText: '合规确认' }).locator('.el-checkbox').click()
  }

  async submit(): Promise<void> {
    const dialog = this.dialog()
    await dialog.getByRole('button', { name: '提交入库' }).click()
    await expect(dialog).toBeHidden() // 成功关弹窗（失败内联保持打开，§22.2）
  }

  /** 提交但允许失败（解析红线/查重/版本冲突）：弹窗可能内联报错保持打开。 */
  async submitAllowFailure(): Promise<void> {
    const dialog = this.dialog()
    await dialog.getByRole('button', { name: '提交入库' }).click()
    await this.page.waitForTimeout(1_500) // 同步解析终态（TEST_MODE）
  }

  /** 一次性两步上传（允许失败版）：终态断言交由调用方按行状态做。 */
  async uploadAllowFailure(filePath: string, meta: { title: string; owner: string }): Promise<void> {
    await this.openUploadDialog()
    await this.selectFile(this.abs(filePath))
    await this.gotoStep2()
    await this.fillMeta(meta.title, meta.owner)
    await this.submitAllowFailure()
    await this.cancel().catch(() => undefined) // 失败内联时手动关弹窗
  }

  async cancel(): Promise<void> {
    await this.dialog().getByRole('button', { name: '取消' }).click()
    await expect(this.dialog()).toBeHidden()
  }

  // ---- 断言 ----

  async expectRowReady(title: string): Promise<void> {
    await expect(
      this.page.locator('.el-table__row', { hasText: title }).locator('.el-tag', { hasText: '就绪' }),
    ).toBeVisible()
  }

  async expectRowFailed(title: string): Promise<void> {
    await expect(
      this.page.locator('.el-table__row', { hasText: title }).locator('.el-tag', { hasText: '失败' }),
    ).toBeVisible()
  }

  /** DOC-13：同标题仅一个生效行（旧版归档出默认列表）。 */
  async expectSingleActiveVersion(title: string): Promise<void> {
    await expect(this.page.locator('.el-table__row', { hasText: title })).toHaveCount(1)
  }

  async retry(title: string): Promise<void> {
    const row = this.page.locator('.el-table__row', { hasText: title }).first()
    await row.getByRole('button', { name: '重试' }).click()
    await expect(this.page.locator('.el-message').last()).toBeVisible()
  }

  /** 拒收类上传（扩展名/魔数/查重）：错误提示出现且上传弹窗可关（无行入库断言由调用方做）。 */
  async uploadExpectReject(absPath: string, messagePattern: RegExp): Promise<void> {
    await this.openUploadDialog()
    await this.selectFile(this.abs(absPath))
    await this.gotoStep2().catch(() => undefined) // 有的拒收发生在选文件即弹错误
    await expect(this.page.getByText(messagePattern).last()).toBeVisible()
    await this.cancel().catch(() => undefined)
  }

  async uploadRejectExpect(messagePattern: RegExp, absPath: string): Promise<void> {
    await this.openUploadDialog()
    await this.selectFile(this.abs(absPath))
    await expect(this.page.getByText(messagePattern).last()).toBeVisible()
    await this.cancel().catch(() => undefined)
  }

  async submitExpectInlineErrors(): Promise<void> {
    const dialog = this.dialog()
    await dialog.getByRole('button', { name: '提交入库' }).click()
    await expect(dialog.locator('.el-form-item__error').first()).toBeVisible()
    await expect(dialog).toBeVisible() // 不关弹窗（字段级提示，§22.2）
  }

  /** DOC-21：断言建议预填（部门/生效日期），随后取消不入库。 */
  async expectSuggested(expected: { dept: string; date: string }): Promise<void> {
    const dialog = this.dialog()
    await expect(
      dialog.locator('.el-form-item', { hasText: '部门' }).locator('.el-select').locator('..'),
    ).toContainText(expected.dept)
    await expect(
      dialog.locator('.el-form-item', { hasText: '生效日期' }).locator('input'),
    ).toHaveValue(new RegExp(expected.date))
  }
}
