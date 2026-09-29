<script setup lang="ts">
/**
 * 单条消息气泡（对照原型 bubble user/assistant + msg-meta）。
 * markdown 不渲染（纯文本+换行，white-space: pre-wrap）；引用角标 [n] 用正则分段
 * 转可点 sup（仅当 n 在本条 citations 中存在），禁 v-html。
 * meta 行按状态展示：失败（生成失败+原因+重试）/ 完成（已拒答、来自缓存标注）。
 */
import { computed } from 'vue'
import type { ChatMessage } from '@/composables/useChatStream'

const props = defineProps<{
  message: ChatMessage
  /** 当前高亮的引用编号（角标与右侧面板联动） */
  highlightN: number | null
}>()

const emit = defineEmits<{
  cite: [n: number]
  retry: []
}>()

interface Segment {
  type: 'text' | 'cite'
  text: string
  n?: number
}

/** 文本按 [n] 切段；n 不在 citations 中的（如正文里字面 [1]）按纯文本保留 */
const segments = computed<Segment[]>(() => {
  const content = props.message.content
  if (props.message.role === 'user') return [{ type: 'text', text: content }]
  const out: Segment[] = []
  let last = 0
  for (const match of content.matchAll(/\[(\d+)\]/g)) {
    const n = Number(match[1])
    if (!props.message.citations.some((c) => c.n === n)) continue
    const start = match.index ?? 0
    if (start > last) out.push({ type: 'text', text: content.slice(last, start) })
    out.push({ type: 'cite', text: match[0], n })
    last = start + match[0].length
  }
  if (last < content.length) out.push({ type: 'text', text: content.slice(last) })
  return out
})
</script>

<template>
  <div v-if="message.role === 'user'" class="bubble user">{{ message.content }}</div>
  <div v-else class="bubble assistant">
    <template v-for="(seg, i) in segments" :key="i">
      <span v-if="seg.type === 'text'">{{ seg.text }}</span>
      <sup
        v-else
        class="cite"
        :class="{ hl: highlightN === seg.n }"
        @click="seg.n !== undefined && emit('cite', seg.n)"
        >{{ seg.n }}</sup
      >
    </template>
    <span v-if="message.status === 'streaming'" class="cursor">▍</span>
    <div v-if="message.status === 'failed'" class="msg-meta">
      <el-tag type="danger" size="small">生成失败</el-tag>
      <span class="fail-text">{{ message.failure }}</span>
      <el-button v-if="message.retryable" link type="primary" size="small" @click="emit('retry')"
        >重试</el-button
      >
    </div>
    <div
      v-else-if="message.status === 'done' && (message.refused || message.cacheHit)"
      class="msg-meta"
    >
      <el-tag v-if="message.refused" size="small" type="warning">已拒答</el-tag>
      <el-tag v-if="message.cacheHit" size="small" type="info">来自缓存</el-tag>
    </div>
  </div>
</template>

<style scoped>
.bubble {
  max-width: 78%;
  margin-bottom: 14px;
  padding: 10px 14px;
  border-radius: 8px;
  line-height: 1.7;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble.user {
  background: var(--el-color-primary);
  color: #fff;
  margin-left: auto;
}
.bubble.assistant {
  background: var(--el-fill-color);
  color: var(--el-text-color-primary);
}
.cite {
  display: inline-block;
  min-width: 18px;
  text-align: center;
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  border-radius: 4px;
  font-size: 12px;
  margin: 0 2px;
  cursor: pointer;
  padding: 0 3px;
  line-height: 18px;
}
.cite.hl {
  background: var(--el-color-primary);
  color: #fff;
}
.cursor {
  display: inline-block;
  animation: blink 1s step-start infinite;
  color: var(--el-text-color-secondary);
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.msg-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 6px;
  display: flex;
  gap: 10px;
  align-items: center;
}
.fail-text {
  color: var(--el-color-danger);
}
</style>
