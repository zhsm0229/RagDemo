import { test } from '@playwright/test'

import { ChatPage } from '../pages/ChatPage'

// CHAT-01/02（P0）：首问自动建会话 + 种子问答对直答带引用。
// 直答确定性原理：e2e 种子问答对问题与本用例提问逐字一致，TEST_MODE 的 StubEmbedder
// 按 sha256 播种——同文本必得同向量，余弦 1.0 ≥ 0.85 稳定命中 qa 通道（L1 无语义能力，
// 语义检索归 L2 真实链路验证）。必须先开新会话：默认选中最近会话的历史会触发 stub
// 改写（返回固定文案），污染问题向量使 qa 不命中。
test('CHAT-01/02: 首问建会话 + 种子问答对直答带引用', async ({ page }) => {
  const chat = new ChatPage(page)
  await chat.goto()
  await chat.newConversation()
  await chat.ask('报销的时限是多少天')
  // L1 stub 契约：TEST_MODE 生成固定文案（StubLLMProvider.DEFAULT_CHAT_CHUNKS），
  // qa 直答与 RAG 在文本上不可区分——本层断言"qa 通道命中 + 管道完成"，
  // 答案语义正确性（直答文本=问答对答案）归 L2 真实链路（chat-real.spec.ts）。
  await chat.expectAssistantContains('根据知识库检索结果')
  await chat.expectCitationCard(1) // qa 引用恰 1 条（_qa_plan 恰一条的接口契约）
  await chat.expectConversationListed('报销的时限') // 首问自动建会话（标题取首问截断）
})
