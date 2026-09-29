<script setup lang="ts">
/**
 * P2 问答工作台（ui-spec §二，三栏布局对照原型 chat-wrap）：
 * 会话列表（新建/删除二次确认/切换/高亮）+ 对话区（SSE 流式气泡 + 输入区）
 * + 引用面板（可折叠）。SSE 事件时序与重试语义见 useChatStream。
 *
 * 首问自动建会话（api.md §3.1 conversationId 可空）：不先建会话直接提问，
 * done 后刷新列表，把暂存消息桶迁移到后端自建会话并选中（updated_at 降序首条）。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ChatInput from '@/components/chat/ChatInput.vue'
import CitationPanel from '@/components/chat/CitationPanel.vue'
import ConversationList from '@/components/chat/ConversationList.vue'
import MessageBubble from '@/components/chat/MessageBubble.vue'
import {
  createConversation,
  deleteConversation,
  getConversationMessages,
  listConversations,
  type ConversationItem,
} from '@/api/chat'
import type { CitationItem } from '@/api/types'
import type { ChatMessage } from '@/composables/useChatStream'
import { useChatStream } from '@/composables/useChatStream'

/** 会话列表单页拉满（pageSize 上限 100），W4 试点量级不做滚动分页 */
const CONV_PAGE_SIZE = 50

const conversations = ref<ConversationItem[]>([])
const activeConvId = ref<number | null>(null)
const citeCollapsed = ref(false)
const citeHl = ref<number | null>(null)
const msgsEl = ref<HTMLElement | null>(null)

function scrollBottom(): void {
  void nextTick(() => {
    const el = msgsEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

const chat = useChatStream({
  onUpdate: scrollBottom,
  onSettled: (conversationId) => void refreshAfterDone(conversationId),
})

const messages = computed<ChatMessage[]>(() => chat.messagesOf(activeConvId.value))

/** 引用面板展示最近一条 assistant 回答的引用（对照原型 lastAssistant 口径） */
const panelCitations = computed<CitationItem[]>(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i].role === 'assistant') return messages.value[i].citations
  }
  return []
})

onMounted(() => {
  void loadConversations()
})

async function loadConversations(): Promise<void> {
  const page = await listConversations(1, CONV_PAGE_SIZE)
  conversations.value = page.items
  // 默认选中最近会话（列表 updated_at 降序首条），首次进入即见历史
  if (activeConvId.value === null && page.items.length > 0) {
    await selectConversation(page.items[0].id)
  }
}

async function selectConversation(id: number): Promise<void> {
  if (id === activeConvId.value) return
  activeConvId.value = id
  citeHl.value = null
  if (chat.hasMessages(id)) return
  try {
    const data = await getConversationMessages(id)
    chat.loadHistory(id, data.items)
    scrollBottom()
  } catch {
    // 错误提示已由 client 统一弹出；会话可能已被删除，刷新列表
    conversations.value = []
    activeConvId.value = null
  }
}

async function createNewConversation(): Promise<void> {
  if (chat.streaming.value) {
    ElMessage.warning('回答生成中，请稍候')
    return
  }
  const { id } = await createConversation()
  conversations.value = [
    { id, title: '新会话', updatedAt: new Date().toISOString() },
    ...conversations.value,
  ]
  activeConvId.value = id
  citeHl.value = null
}

async function removeConversation(id: number): Promise<void> {
  try {
    await ElMessageBox.confirm('删除后会话及聊天记录不可恢复，确认删除该会话？', '删除会话', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await deleteConversation(id)
  conversations.value = conversations.value.filter((c) => c.id !== id)
  chat.dropConversation(id)
  if (activeConvId.value === id) {
    activeConvId.value = null
    const next = conversations.value[0]?.id
    if (next !== undefined) await selectConversation(next)
  }
  ElMessage.success('会话已删除')
}

/** done 后刷新列表：首问自建会话迁移消息桶并选中；复用会话同步标题/时间（首轮改题） */
async function refreshAfterDone(conversationId: number | null): Promise<void> {
  if (conversationId === null) {
    try {
      const page = await listConversations(1, CONV_PAGE_SIZE)
      conversations.value = page.items
      const created = page.items[0]
      if (created !== undefined) {
        chat.adoptCreatedConversation(created.id)
        if (activeConvId.value === null) activeConvId.value = created.id
      }
    } catch {
      // 列表刷新失败不打断对话视图，下次操作再同步
    }
    return
  }
  try {
    const page = await listConversations(1, CONV_PAGE_SIZE)
    conversations.value = page.items
  } catch {
    // 同上，静默
  }
}

function onSend(question: string): void {
  void chat.send(question, activeConvId.value)
}

function onRetry(message: ChatMessage): void {
  void chat.retryFailed(message, activeConvId.value)
}

function onCiteClick(n: number): void {
  citeHl.value = n
  if (citeCollapsed.value) return
  void nextTick(() => {
    document
      .getElementById(`cite-card-${n}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}
</script>

<template>
  <div class="chat-wrap">
    <ConversationList
      :items="conversations"
      :active-id="activeConvId"
      @select="selectConversation"
      @create="createNewConversation"
      @remove="removeConversation"
    />

    <div class="chat-main">
      <el-button
        v-if="citeCollapsed && panelCitations.length > 0"
        link
        type="primary"
        size="small"
        class="panel-restore"
        @click="citeCollapsed = false"
        >引用面板</el-button
      >
      <div ref="msgsEl" class="msgs">
        <el-empty
          v-if="messages.length === 0"
          description="问点什么吧，例如：差旅报销标准是多少？"
        />
        <MessageBubble
          v-for="m in messages"
          :key="m.key"
          :message="m"
          :highlight-n="citeHl"
          @cite="onCiteClick"
          @retry="onRetry(m)"
        />
      </div>
      <ChatInput :disabled="chat.streaming.value" @send="onSend" />
    </div>

    <CitationPanel
      v-if="!citeCollapsed"
      :citations="panelCitations"
      :highlight-n="citeHl"
      @close="citeCollapsed = true"
    />
  </div>
</template>

<style scoped>
.chat-wrap {
  display: flex;
  gap: 12px;
  height: 100%;
  min-height: 0;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  min-width: 0;
  position: relative;
}
.panel-restore {
  position: absolute;
  top: 10px;
  right: 12px;
  z-index: 5;
}
.msgs {
  flex: 1;
  overflow: auto;
  padding: 16px 20px;
}
</style>
