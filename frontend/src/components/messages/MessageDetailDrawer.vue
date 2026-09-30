<script setup lang="ts">
/**
 * 提问详情抽屉（F-701，对照原型 detail 抽屉 + ui-spec §七）：
 * 完整对话气泡只读置灰（§8.8——提问原文 + 回答全文，纯文本不渲染富文本）、
 * 引用 JSON 折叠面板 readOnly（JSONB 原样展示）、token/耗时 meta、点踩备注。
 * 原型抽屉仅"回答全文"，按 ui-spec §七"完整对话"+ api.md §4.3.2（详情含提问原文）
 * 增提问段双气泡 → deviations #39。assistant=null（该问无有效回答——生成中断）
 * 渲染失败态占位；纯只读无未保存态，关抽屉直接弃载入数据。
 */
import { ref, watch } from 'vue'
import { getAdminMessageDetail, type MessageDetail } from '@/api/adminMessages'

const props = defineProps<{ visible: boolean; messageId: number | null }>()
const emit = defineEmits<{ close: [] }>()

const loading = ref(false)
const detail = ref<MessageDetail | null>(null)
const loadErr = ref('')

watch(
  () => props.visible,
  (visible) => {
    if (visible) void load()
  },
)

async function load(): Promise<void> {
  if (props.messageId === null) return
  loading.value = true
  detail.value = null
  loadErr.value = ''
  try {
    detail.value = await getAdminMessageDetail(props.messageId)
  } catch {
    loadErr.value = '提问记录加载失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

/** 引用 JSONB 原样序列化（§8.8 只读置灰，供核对不供编辑） */
function citationsJson(detailValue: MessageDetail): string {
  return JSON.stringify(detailValue.assistant?.citations ?? [], null, 2)
}

function metaText(detailValue: MessageDetail): string {
  const a = detailValue.assistant!
  const latency = a.latencyMs === null ? '-' : String(a.latencyMs)
  const refused = a.answerType === 'refuse' ? '是' : '否'
  return `token 输入 ${a.promptTokens} / 输出 ${a.completionTokens} · 耗时 ${latency}ms · 缓存命中：${a.cacheHit ? '是' : '否'} · 拒答：${refused}`
}

function feedbackText(detailValue: MessageDetail): string {
  const a = detailValue.assistant!
  return `${a.feedbackCategory ?? '-'}：${a.feedbackComment ?? '-'}`
}

/** DTO createdAt 为 ISO 字符串 → 原型口径 "YYYY-MM-DD HH:mm" */
function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    title="提问详情（只读）"
    size="480px"
    @update:model-value="emit('close')"
  >
    <div v-loading="loading" class="readonly-grey">
      <el-alert v-if="loadErr" type="error" :title="loadErr" :closable="false" />
      <template v-else-if="detail">
        <p class="sec">
          <b>提问（{{ detail.user.username }} · {{ formatTime(detail.user.createdAt) }}）</b>
        </p>
        <div class="bubble user">{{ detail.user.content }}</div>

        <p class="sec"><b>回答</b></p>
        <div v-if="detail.assistant" class="bubble assistant">{{ detail.assistant.content }}</div>
        <!-- 中断占位行被软删 → assistant=null：失败态占位（不渲染空回答） -->
        <el-empty v-else description="该问题无有效回答（生成中断），无回答内容" :image-size="60" />

        <template v-if="detail.assistant">
          <p class="sec"><b>引用 JSON</b></p>
          <el-collapse class="cite-collapse">
            <el-collapse-item
              :title="`共 ${detail.assistant.citations?.length ?? 0} 条`"
              name="citations"
            >
              <el-input type="textarea" :rows="6" :model-value="citationsJson(detail)" readonly />
            </el-collapse-item>
          </el-collapse>

          <p class="sec"><b>运行信息</b></p>
          <el-input :model-value="metaText(detail)" readonly />

          <template v-if="detail.assistant.feedback === 'down'">
            <p class="sec"><b>点踩备注</b></p>
            <el-input :model-value="feedbackText(detail)" readonly />
          </template>
        </template>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
/* §8.8 只读置灰：内容只读可核对（可选中复制），不可编辑 */
.readonly-grey :deep(.el-textarea__inner),
.readonly-grey :deep(.el-input__inner) {
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  cursor: text;
}
.sec {
  margin: 14px 0 8px;
  font-size: 13px;
}
.sec:first-child {
  margin-top: 0;
}
/* 完整对话气泡：提问靠右、回答靠左（纯文本，pre-wrap 保留换行） */
.bubble {
  max-width: 88%;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
}
.bubble.user {
  margin-left: auto;
  background: var(--el-color-primary-light-9);
}
.cite-collapse {
  margin-bottom: 4px;
}
</style>
