<script setup lang="ts">
/**
 * 问答对 Excel 导入对话框（F-401，对照原型 QaView 三步向导折叠为两步——后端
 * POST /import 一次提交整批校验：201 {imported,batchNo} 或 422/1003 错误行全量
 * 零入库，无独立"校验→确认"两阶段端点，deviations #27）。
 * ① 选择文件（.xlsx 即时白名单校验）→ ② 导入结果：成功 el-alert（imported+批次号，
 * emit imported 由父级回第 1 页刷新 §22.3）/ 失败 el-alert（后端统一中文 message +
 * "未入库"提示）+ 错误行表格（行号/字段/原因 全量可滚动 §21.6）。失败弹窗不关
 * （§22.2），可重选文件重新导入。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { importQaPairs, type QaPairImportErrorRow, type QaPairImportResult } from '@/api/qaPairs'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; imported: [payload: QaPairImportResult] }>()

const file = ref<UploadFile | null>(null)
const fileErr = ref('')
const error = ref('')
const submitting = ref(false)
/** 成功态（201）：imported + batchNo */
const result = ref<QaPairImportResult | null>(null)
/** 422/1003 整批退回时的错误行全量数组；文件级单一错误无 data 时为空数组 */
const errorRows = ref<QaPairImportErrorRow[]>([])

function reset(): void {
  file.value = null
  fileErr.value = ''
  error.value = ''
  result.value = null
  errorRows.value = []
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

function handleFile(uploadFile: UploadFile): void {
  if (!/\.xlsx$/.test(uploadFile.name.toLowerCase())) {
    fileErr.value = '仅支持 .xlsx 文件'
    file.value = null
    return
  }
  fileErr.value = ''
  file.value = uploadFile
  // 重选文件即清上一轮结果，回到干净的选择态重新提交
  result.value = null
  errorRows.value = []
  error.value = ''
}

async function submit(): Promise<void> {
  if (submitting.value) return
  const raw = file.value?.raw
  if (!raw) return
  error.value = ''
  submitting.value = true
  try {
    result.value = await importQaPairs(raw)
    errorRows.value = []
    ElMessage.success(`导入成功：本批 ${result.value.imported} 条问答对已入库并向量化`)
    emit('imported', result.value)
  } catch (e) {
    // 422/1003：message=统一中文提示（含"已整批退回，未入库"），data=错误行全量
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
    errorRows.value =
      e instanceof ApiClientError && Array.isArray(e.data) ? (e.data as QaPairImportErrorRow[]) : []
    result.value = null
  } finally {
    submitting.value = false
  }
}

defineExpose({ handleFile, submit })
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="问答对 Excel 导入（整批校验）"
    width="680px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
    @closed="reset"
  >
    <el-steps
      :active="result || error ? 2 : 0"
      align-center
      finish-status="success"
      style="margin-bottom: 18px"
    >
      <el-step title="选择文件" />
      <el-step title="导入结果" />
    </el-steps>

    <el-upload
      drag
      :auto-upload="false"
      :limit="1"
      accept=".xlsx"
      :on-change="handleFile"
      :file-list="file ? [file] : []"
    >
      <div style="padding: 20px">
        将 .xlsx 文件拖到此处，或 <em>点击选择</em><br />
        <span style="font-size: 12px; color: var(--el-text-color-secondary)">
          列：问题 / 答案 / 状态（enabled·disabled，空缺省启用）；≤5000 行；问题 ≤2000 字、答案
          ≤10000 字；任一行不合格整批退回
        </span>
      </div>
    </el-upload>
    <div v-if="fileErr" class="err-inline" role="alert">{{ fileErr }}</div>

    <el-alert
      v-if="result"
      type="success"
      :closable="false"
      show-icon
      class="result-alert"
      :title="`导入完成：本批 ${result.imported} 条问答对已入库并向量化，批次号 ${result.batchNo}`"
    />
    <template v-else-if="error">
      <el-alert type="error" :closable="false" show-icon class="result-alert" :title="error" />
      <el-table v-if="errorRows.length" :data="errorRows" border size="small" max-height="220">
        <el-table-column prop="row" label="行号" width="80" />
        <el-table-column prop="field" label="字段" width="110" />
        <el-table-column prop="reason" label="错误原因（中文）" min-width="220" />
      </el-table>
    </template>

    <template #footer>
      <el-button @click="emit('close')">关闭</el-button>
      <el-button type="primary" :disabled="!file || !!result" :loading="submitting" @click="submit">
        {{ error ? '重新导入' : '开始导入' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 对照原型 .err-inline（§22.2 错误内联，不弹窗） */
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
.result-alert {
  margin-top: 10px;
}
</style>
