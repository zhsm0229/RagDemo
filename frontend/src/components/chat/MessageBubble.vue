<script setup lang="ts">
/**
 * 单条消息气泡（对照原型 bubble user/assistant + msg-meta）。
 * markdown 不渲染（纯文本+换行，white-space: pre-wrap）；引用角标 [n] 用正则分段
 * 转可点 sup（仅当 n 在本条 citations 中存在），禁 v-html。
 * 等待阶段提示（2026-10-06）：流式占位期且尚无内容时展示阶段文案+已等待秒数+
 * 脉冲点——发送后到首 token 之间（CPU rerank 可达分钟级）气泡不再全白。
 * meta 行按状态展示：失败（生成失败+原因+重试）/ 已停止（可重试）/
 * 完成（已拒答、来自缓存、token·耗时、👍👎 反馈与已点踩标注）。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import type { ChatMessage } from '@/composables/useChatStream'

const props = defineProps<{
  message: ChatMessage
  /** 当前高亮的引用编号（角标与右侧面板联动） */
  highlightN: number | null
}>()

const emit = defineEmits<{
  cite: [n: number]
  retry: []
  like: []
  dislike: []
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

/**
 * 反馈入口：仅本人已落库回答（id 回填）且非拒答可反馈——中断占位/拒答行不可，
 * 与后端"仅本人 assistant 消息且 is_active"口径一致（docs/api.md §3.6）
 */
const canFeedback = computed(
  () => props.message.id !== null && props.message.status === 'done' && !props.message.refused,
)

// ---- 等待阶段提示 ----

/** 内容未开始（空气泡）且处于等待阶段才提示；token 到达后由内容与光标接管 */
const waiting = computed(
  () =>
    props.message.status === 'streaming' &&
    props.message.stage !== null &&
    props.message.content === '',
)

const STAGE_TEXT: Record<string, string> = {
  search: '正在检索知识库',
  generate: '正在生成回答',
}

const stageText = computed(() =>
  props.message.stage === null ? '' : (STAGE_TEXT[props.message.stage] ?? ''),
)

/** 已等待秒数：1s 心跳只在等待期存在；≥3s 才显示（快路径不闪计数） */
const now = ref(Date.now())
const elapsed = computed(() =>
  props.message.startedAt === null ? 0 : Math.floor((now.value - props.message.startedAt) / 1000),
)
let ticker: number | null = null
watch(
  waiting,
  (on) => {
    if (on) {
      now.value = Date.now()
      ticker = window.setInterval(() => {
        now.value = Date.now()
      }, 1000)
    } else if (ticker !== null) {
      clearInterval(ticker)
      ticker = null
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  if (ticker !== null) clearInterval(ticker)
})
</script>

<template>
  <div v-if="message.role === 'user'" class="bubble user">{{ message.content }}</div>
  <div v-else class="bubble assistant">
    <div v-if="waiting" class="stage-hint">
      <span class="pulse-dot" />
      <span>{{ stageText }}</span>
      <span class="dots">…</span>
      <span v-if="elapsed >= 3" class="elapsed"
        >已等待 {{ elapsed }} 秒（检索含 CPU 重排，较慢属正常）</span
      >
    </div>
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
    <span v-if="message.status === 'streaming' && message.content !== ''" class="cursor">▍</span>
    <div v-if="message.status === 'failed'" class="msg-meta">
      <el-tag v-if="message.stopped" size="small" type="info">已停止</el-tag>
      <template v-else>
        <el-tag type="danger" size="small">生成失败</el-tag>
        <span class="fail-text">{{ message.failure }}</span>
      </template>
      <el-button v-if="message.retryable" link type="primary" size="small" @click="emit('retry')"
        >重试</el-button
      >
    </div>
    <div v-else-if="message.status === 'done'" class="msg-meta">
      <el-tag v-if="message.refused" size="small" type="warning">已拒答</el-tag>
      <el-tag v-if="message.cacheHit" size="small" type="info">来自缓存</el-tag>
      <span v-if="message.promptTokens !== null" class="token-info">
        token: in {{ message.promptTokens }} / out {{ message.completionTokens }} ·
        {{ message.durationMs }}ms
      </span>
      <span class="spacer" />
      <template v-if="canFeedback">
        <el-button
          link
          size="small"
          :type="message.feedback === 'up' ? 'primary' : ''"
          @click="emit('like')"
          >👍</el-button
        >
        <el-button
          link
          size="small"
          :type="message.feedback === 'down' ? 'danger' : ''"
          @click="emit('dislike')"
          >👎</el-button
        >
      </template>
      <span v-if="message.feedback === 'down'" class="fb-down"
        >已点踩：{{ message.feedbackCategory }}</span
      >
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
/* 等待阶段提示：脉冲点 + 阶段文案 + 已等待秒数（发送后到首 token 的活性反馈） */
.stage-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-primary);
  animation: pulse 1.2s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.35;
    transform: scale(0.75);
  }
}
.dots {
  letter-spacing: 2px;
}
.elapsed {
  font-size: 12px;
  opacity: 0.85;
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
.token-info {
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.fb-down {
  color: var(--el-color-danger);
  white-space: nowrap;
}
</style>
