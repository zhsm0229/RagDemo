<script setup lang="ts">
/**
 * zip 批量导入对话框（F-1007，ui-spec §三上传对话框"支持 zip 批量"）：
 * 选 .zip → 提交 → 逐文件结果表（filename/status/reason，四态中文映射）+ 汇总。
 * 422（非有效 zip / 安全超限）内联展示且弹窗不关闭（§22.2）；有入库条目时通知父级刷新。
 * 端点属 T3 分支（36da916）未合入 feat/w4-fe：入口按钮暂置灰（deviations #21），
 * 组件与调用已按 T3 契约就位，合并后启用按钮即可用。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { importDocumentsZip, type ZipImportSummary } from '@/api/documents'
import { ZIP_STATUS_TAGS } from './options'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; imported: [] }>()

const file = ref<UploadFile | null>(null)
const fileErr = ref('')
const error = ref('')
const submitting = ref(false)
const summary = ref<ZipImportSummary | null>(null)

function reset(): void {
  file.value = null
  fileErr.value = ''
  error.value = ''
  summary.value = null
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

function handleFile(uploadFile: UploadFile): void {
  if (!/\.zip$/.test(uploadFile.name.toLowerCase())) {
    fileErr.value = '仅支持 .zip 压缩包'
    file.value = null
    return
  }
  fileErr.value = ''
  file.value = uploadFile
}

async function submit(): Promise<void> {
  if (submitting.value) return
  const raw = file.value?.raw
  if (!raw) return
  error.value = ''
  submitting.value = true
  try {
    summary.value = await importDocumentsZip(raw)
    ElMessage.success(
      `导入完成：入库 ${summary.value.accepted} 份，拒绝 ${summary.value.rejected} 份，跳过 ${summary.value.skipped} 份`,
    )
    if (summary.value.accepted > 0) emit('imported')
  } catch (e) {
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}

defineExpose({ handleFile, submit })
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="zip 批量导入"
    width="640px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
    @closed="reset"
  >
    <el-upload
      drag
      :auto-upload="false"
      :limit="1"
      accept=".zip"
      :on-change="handleFile"
      :file-list="file ? [file] : []"
    >
      <div style="padding: 20px">
        将 zip 压缩包拖到此处，或 <em>点击选择</em><br />
        <span style="font-size: 12px; color: var(--el-text-color-secondary)">
          条目须为 pdf / docx；文件名按 部门_主题_日期 命名可自动补元数据，否则跳过待补
        </span>
      </div>
    </el-upload>
    <div v-if="fileErr" class="err-inline" role="alert">{{ fileErr }}</div>
    <div v-if="error" class="err-inline" role="alert">{{ error }}</div>

    <template v-if="summary">
      <div class="summary-row">
        共 {{ summary.results.length }} 个条目：入库 {{ summary.accepted }} · 拒绝
        {{ summary.rejected }} · 跳过 {{ summary.skipped }}
      </div>
      <el-table :data="summary.results" border size="small" max-height="260">
        <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="ZIP_STATUS_TAGS[row.status as keyof typeof ZIP_STATUS_TAGS]?.type"
            >
              {{ ZIP_STATUS_TAGS[row.status as keyof typeof ZIP_STATUS_TAGS]?.label ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="原因" min-width="180">
          <template #default="{ row }">
            <span>{{ row.reason ?? '-' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <template #footer>
      <el-button @click="emit('close')">关闭</el-button>
      <el-button type="primary" :disabled="!file" :loading="submitting" @click="submit">
        开始导入
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
.summary-row {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin: 10px 0 8px;
}
</style>
