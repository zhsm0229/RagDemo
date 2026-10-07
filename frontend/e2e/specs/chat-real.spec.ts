import { test } from '@playwright/test'

import { ChatPage } from '../pages/ChatPage'

// L2 真实链路层（E2E_REAL=1 时才跑）：TEI + DeepSeek 全真环境，部署机/每日档。
// 拒答类断言必须在真实链路验证——L1 的 stub rerank 分恒高于拒答阈值（0.55），
// 有候选即"命中"，拒答语义在 stub 下不可测（红线本身由 API 测试与评测集把守）。
test.skip(process.env.E2E_REAL !== '1', 'L2 真实链路用例：需 E2E_REAL=1（TEI+LLM 就绪）')

test('CHAT-03 @real: 无关问题逐字拒答不编造', async ({ page }) => {
  const chat = new ChatPage(page)
  await chat.goto()
  await chat.newConversation()
  await chat.ask('xqzkvh 公司钢琴课报销政策')
  await chat.expectRefused()
})

test('DOC-RAG @real: 上传文档后提问命中真实引用', async ({ page }) => {
  const chat = new ChatPage(page)
  await chat.goto()
  await chat.newConversation()
  await chat.ask('出差住宿费的标准是什么')
  // 真实检索命中的结构断言：引用卡出现且相似度为 rerank sigmoid 分（0~1 开区间）
  await chat.expectCitationCard(1)
})
