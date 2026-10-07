<script setup lang="ts">
/**
 * 引用原文核对弹窗（F-202，对照原型 srcDialog，kotaemon 式溯源 §8.2）：
 * 点引用卡后调 GET /citations/content 拉正文（doc → chunk 全文 / qa → 答案文本），
 * loading/成功/失败三态，失败错误内联弹窗内不关闭可重试（§22.2）；
 * 正文 readOnly 置灰可复制（§8.8）。禁遮罩关闭（§22.2）。
 * F-1017/F-1018（2026-10-06）：doc 引用正文含"图片N："标记时，在 OCR 文本上方回显
 * 实际原图；附"查看原文件"与"在线预览"入口（后端部门隔离闸同口径）。
 * 寻址用 citation.docId（文档主键，2026-10-05 缺陷修复：此前误用 refId=chunk id 拼
 * /documents/{id}/file|images，chunk_id≠doc_id 时 404/错文档）；历史消息引用无 docId
 * 时回退 refId（维持旧行为），在线预览仅 docId 在场时提供。
 */
import { computed, ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { getCitationContent } from '@/api/chat'
import { docFileUrl, docImageUrl, type PreviewTarget } from '@/api/documents'
import type { CitationContent, CitationItem } from '@/api/types'
import DocPreviewDialog from '@/components/documents/DocPreviewDialog.vue'

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
const previewVisible = ref(false)

/** 文档寻址 id：新引用用 docId，历史消息引用回退 refId（旧行为） */
const docAddressId = computed<number | null>(() => {
  const citation = props.citation
  if (citation === null || citation.refType !== 'doc') return null
  return citation.docId ?? citation.refId
})

/** 正文中"图片N："标记 → 回显 URL 列表（后端按 index 鉴权回原图；doc 引用才解析） */
const citeImages = computed<{ index: number; url: string }[]>(() => {
  const content = detail.value?.content
  const id = docAddressId.value
  if (!content || id === null) return []
  return [...content.matchAll(/图片(\d+)：/g)]
    .map((m) => Number(m[1]))
    .filter((n) => n >= 1)
    .map((index) => ({ index, url: docImageUrl(id, index) }))
})

/** 原文件打开链接（doc 引用；后端同闸） */
const citeFileUrl = computed<string | null>(() => {
  const id = docAddressId.value
  return id === null ? null : docFileUrl(id)
})

/** 在线预览目标：仅新引用（含 docId）提供；filename/mime 不在场，格式由预览端点裁定 */
const previewTarget = computed<PreviewTarget | null>(() => {
  const citation = props.citation
  if (citation === null || citation.refType !== 'doc' || citation.docId === undefined) return null
  return { id: citation.docId, title: citation.title, originalFilename: '', mime: '' }
})

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
        <!-- F-1017：切片命中"图片N："标记 → 回显实际原图（后端部门隔离闸兜底） -->
        <div v-if="citeImages.length" class="cite-images">
          <el-image
            v-for="img in citeImages"
            :key="img.index"
            :src="img.url"
            :preview-src-list="[img.url]"
            fit="contain"
            class="cite-image"
            lazy
          />
        </div>
        <el-input type="textarea" :rows="8" :model-value="detail.content" readonly />
        <p v-if="citeFileUrl !== null" class="cite-file">
          <a :href="citeFileUrl" target="_blank" rel="noopener">查看原文件</a>
          <el-button
            v-if="previewTarget !== null"
            link
            type="primary"
            @click="previewVisible = true"
          >
            在线预览
          </el-button>
        </p>
      </div>
    </div>
    <DocPreviewDialog
      :visible="previewVisible"
      :target="previewTarget"
      @close="previewVisible = false"
    />
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
.cite-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.cite-image {
  width: 160px;
  height: 120px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.cite-file {
  margin: 8px 0 0;
  font-size: 13px;
}

.cite-file .el-button {
  margin-left: 12px;
}
</style>
