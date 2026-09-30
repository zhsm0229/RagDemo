import { expect, type Page } from '@playwright/test'

/** 拒答话术逐字口径（deviations #17，产品红线：知识不足绝不编造）。 */
export const REFUSE_TEXT = '知识库中未找到相关内容'

/** 问答工作台 PO（ChatView：首问自动建会话 + SSE 气泡 + cite-card 引用卡）。 */
export class ChatPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/chat')
  }

  /** 开新会话：进问答页默认选中最近会话（deviations #18），历史会让 TEST_MODE 的
   * stub 改写污染问题向量——需要"语义自足的首问"用例（如 qa 直答）必须先开新会话。 */
  async newConversation(): Promise<void> {
    await this.page.getByRole('button', { name: /新会话/ }).click()
  }

  async ask(question: string): Promise<void> {
    const input = this.page.locator('textarea[placeholder*="输入问题"]')
    await input.fill(question)
    await input.press('Enter')
  }

  /** 等最近一条 assistant 气泡包含指定片段（SSE 流式聚合后可见）。 */
  async expectAssistantContains(text: string): Promise<void> {
    await expect(this.page.getByText(text, { exact: false }).last()).toBeVisible()
  }

  /** 知识不足逐字拒答（含标点外的任何编造都算失败）。 */
  async expectRefused(): Promise<void> {
    await expect(this.page.getByText(REFUSE_TEXT).last()).toBeVisible()
  }

  /** 引用角标卡（ChatView 用 id=cite-card-{n} 程序生成，§17.2 引用编号非 LLM 自由发挥）。 */
  async expectCitationCard(n: number): Promise<void> {
    await expect(this.page.locator(`#cite-card-${n}`)).toBeVisible()
  }

  /** 会话列表出现指定标题片段（首问自动建会话，标题取首问截断）。 */
  async expectConversationListed(titlePart: string): Promise<void> {
    await expect(
      this.page.locator('.conv-item .conv-title', { hasText: titlePart }).first(),
    ).toBeVisible()
  }
}
