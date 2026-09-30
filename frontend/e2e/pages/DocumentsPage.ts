import { fileURLToPath } from 'node:url'
import path from 'node:path'

import { expect, type Page } from '@playwright/test'

/** 知识库·文档管理 PO（DocumentsView + UploadDialog 两步上传：文件 → 元数据提交入库）。 */
export class DocumentsPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/kb/documents')
  }

  /** 两步上传：选文件 → 下一步 → 元数据（TEST_MODE 下提交即同步解析出终态）。
   * 相对路径以 e2e 目录为基准解析（进程 CWD 随调用方漂移，不可依赖）。 */
  async upload(
    filePath: string,
    meta: { title: string; owner: string },
  ): Promise<void> {
    const abs = path.isAbsolute(filePath)
      ? filePath
      : fileURLToPath(new URL(`../${filePath}`, import.meta.url))
    await this.page.getByRole('button', { name: /上传文档/ }).click()
    const dialog = this.page.locator('.el-dialog', { hasText: '上传文档' })
    await dialog.locator('input[type="file"]').setInputFiles(abs)
    await dialog.getByRole('button', { name: /下一步/ }).click()
    await dialog.locator('.el-form-item', { hasText: '标题' }).locator('input').fill(meta.title)
    // Element Plus 下拉：点开 select 后选项渲染在 body 级浮层；页面可能同时存在
    // 多个 select 的隐藏浮层（筛选行/弹窗），必须限定 :visible 再选
    await dialog.locator('.el-form-item', { hasText: '部门' }).locator('.el-select').click()
    await this.page.locator('.el-select-dropdown__item:visible', { hasText: '全员' }).click()
    await dialog.locator('.el-form-item', { hasText: '责任人' }).locator('input').fill(meta.owner)
    const dateItem = dialog.locator('.el-form-item', { hasText: '生效日期' })
    await dateItem.locator('input').fill('2026-09-30')
    await dateItem.locator('input').press('Enter')
    await dialog.locator('.el-form-item', { hasText: '合规确认' }).locator('.el-checkbox').click()
    await dialog.getByRole('button', { name: '提交入库' }).click()
    await expect(dialog).toBeHidden() // 提交成功关弹窗（失败内联保持打开，§22.2）
  }

  /** 列表行出现"● 就绪"标签（TEST_MODE 同步解析，提交后立即可断言）。 */
  async expectRowReady(title: string): Promise<void> {
    await expect(
      this.page.locator('.el-table__row', { hasText: title }).locator('.el-tag', { hasText: '就绪' }),
    ).toBeVisible()
  }
}
