<script setup lang="ts">
/**
 * 文档在线预览对话框（评审材料 docs/specs/2026-10-05-document-preview-review.md，方案 A）：
 * - pdf（含老格式入库转换产物）/ docx·pptx·xlsx：服务端统一 PDF inline——预览字节经
 *   fetch→Blob→objectURL 注入 iframe（不走 iframe 直指 URL：转换失败/未配置返回的
 *   JSON 错误体也能捕获成降级面板，而不是在 iframe 里渲染裸 JSON）；
 * - md/txt/csv/html：取 /file 原字节转义 <pre> 渲染（html 绝不内嵌渲染，XSS 封死）。
 * 头部常驻"下载原文件"，降级面板同款兜底。关闭即 revoke objectURL 防泄漏。
 */
import { computed, ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { docFileUrl, docPreviewUrl, fetchDocumentFile, type PreviewTarget } from '@/api/documents'

const props = defineProps<{ visible: boolean; target: PreviewTarget | null }>()
const emit = defineEmits<{ close: [] }>()

const TEXT_EXTS = ['md', 'txt', 'csv', 'html', 'htm']

const loading = ref(false)
const error = ref('')
const blobUrl = ref('')
const textContent = ref<string | null>(null)

const kind = computed<'pdf' | 'text' | null>(() => {
  const doc = props.target
  if (!doc) return null
  const ext = doc.originalFilename.split('.').pop()?.toLowerCase() ?? ''
  if (ext === 'pdf' || doc.mime === 'application/pdf') return 'pdf'
  if (TEXT_EXTS.includes(ext)) return 'text'
  return 'pdf' // docx/pptx/xlsx 与老格式（入库已转 PDF）统一走预览端点
})

function reset(): void {
  if (blobUrl.value !== '') URL.revokeObjectURL(blobUrl.value)
  blobUrl.value = ''
  textContent.value = null
  error.value = ''
}

/** 统一信封错误体 → 中文文案（网关错误页等非 JSON 走兜底，模式同 exportAuditLogs） */
async function envelopeMessage(res: Response, fallback: string): Promise<string> {
  try {
    const payload = (await res.json()) as { message?: unknown }
    if (typeof payload.message === 'string' && payload.message !== '') return payload.message
  } catch {
    // 非 JSON 错误体，走兜底文案
  }
  return fallback
}

async function load(): Promise<void> {
  const doc = props.target
  if (!doc) return
  reset()
  loading.value = true
  try {
    if (kind.value === 'text') {
      textContent.value = await (await fetchDocumentFile(doc.id)).text()
    } else {
      const res = await fetch(docPreviewUrl(doc.id), { credentials: 'include' })
      if (!res.ok) {
        throw new ApiClientError(
          res.status,
          await envelopeMessage(res, '预览加载失败，请下载原文件查看'),
        )
      }
      const blob = await res.blob()
      if (!blob.type.startsWith('application/pdf')) {
        throw new ApiClientError(1002, '预览内容异常，请下载原文件查看')
      }
      blobUrl.value = URL.createObjectURL(blob)
    }
  } catch (e) {
    error.value = e instanceof ApiClientError ? e.message : '预览加载失败，请下载原文件查看'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) void load()
    else reset()
  },
)
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="target?.title ?? '文档预览'"
    width="80%"
    top="4vh"
    append-to-body
    destroy-on-close
    :close-on-click-modal="false"
    @close="emit('close')"
  >
    <div v-if="target" class="preview-body">
      <div class="preview-toolbar">
        <span class="preview-filename">{{ target.originalFilename || target.title }}</span>
        <a :href="docFileUrl(target.id)" target="_blank" rel="noopener">下载原文件</a>
      </div>
      <div v-if="loading" class="preview-state">
        正在加载预览…（office 文档首次转换可能需要数秒）
      </div>
      <div v-else-if="error !== ''" class="preview-state preview-error">
        <p>{{ error }}</p>
        <a :href="docFileUrl(target.id)" target="_blank" rel="noopener">下载原文件查看</a>
      </div>
      <iframe v-else-if="blobUrl !== ''" :src="blobUrl" class="preview-frame" title="文档预览" />
      <pre v-else-if="textContent !== null" class="preview-text">{{ textContent }}</pre>
    </div>
  </el-dialog>
</template>

<style scoped>
.preview-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.preview-filename {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.preview-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 48px 0;
  color: var(--el-text-color-secondary);
}

.preview-error p {
  margin: 0;
}

.preview-frame {
  width: 100%;
  height: 72vh;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.preview-text {
  max-height: 72vh;
  margin: 0;
  overflow: auto;
  padding: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.7;
}
</style>
