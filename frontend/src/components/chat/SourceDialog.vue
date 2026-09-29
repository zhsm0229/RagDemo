<script setup lang="ts">
/**
 * 引用原文核对弹窗（F-202，对照原型 srcDialog，kotaemon 式溯源 §8.2）：
 * 点引用卡后调 GET /citations/content 拉正文（doc → chunk 全文 / qa → 答案文本），
 * loading/成功/失败三态，失败错误内联弹窗内不关闭可重试（§22.2）；
 * 正文 readOnly 置灰可复制（§8.8）。禁遮罩关闭（§22.2）。
 */
import { ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { getCitationContent } from '@/api/chat'
import type { CitationContent, CitationItem } from '@/api/types'

const props = defineProps<{
  /** 待核对引用（n 用于标题展示；refType/refId 为查询键） */
  citation: CitationItem | null
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const loading = ref(false)
const error = ref<string | null>(null)
const detail = ref<CitationContent | null>(null)

async function load(): Promise<void> {
  const citation = props.citation
  if (citation === null) return
  loading.value = true
  error.value = null
  detail.value = null
  try {
    detail.value = await getCitationContent(citation.refType, citation.refId)
  } catch (e) {
    // 接口已 silent，错误在此内联展示（404/3003="引用不存在或无权访问"等）
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) void load()
  },
)
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="引用原文核对"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <template v-if="citation">
      <p class="cite-title">[{{ citation.n }}] {{ detail?.title ?? citation.title }}</p>
      <p class="cite-meta">
        页码/来源：{{ detail?.page ?? citation.page ?? '—' }} ｜ 位置：{{
          detail?.titlePath ?? citation.titlePath ?? '—'
        }}
        ｜ 相似度：{{ citation.score }}
      </p>
    </template>
    <div v-loading="loading" class="cite-body">
      <template v-if="error !== null">
        <div class="err-inline">{{ error }}</div>
        <el-button size="small" type="primary" @click="load">重试</el-button>
      </template>
      <div v-else-if="detail !== null" class="readonly-grey">
        <el-input type="textarea" :rows="8" :model-value="detail.content" readonly />
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.cite-title {
  margin: 0 0 8px;
  font-weight: 600;
}
.cite-meta {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.cite-body {
  min-height: 120px;
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 13px;
  margin-bottom: 8px;
}
/* readOnly 置灰可复制（§8.8，对照原型 .readonly-grey）：textarea readonly 可选中复制 */
.readonly-grey :deep(.el-textarea__inner) {
  color: var(--el-text-color-placeholder);
  background: var(--el-fill-color-light);
  cursor: text;
}
</style>
