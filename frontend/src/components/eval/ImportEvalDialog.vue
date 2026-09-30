<script setup lang="ts">
/**
 * 评测题库 Excel 导入对话框（F-601，对照原型 P4 同款向导折叠两步——后端
 * POST /admin/eval-questions/import 一次提交整批校验：201 {imported} 或 422/1003
 * 错误行全量零入库，无独立"校验→确认"两阶段端点，deviations #27 同款）。
 * 与问答对导入的差异（api.md §4.2.1）：部门在文件行内用中文名（客服/人力/全员），
 * 无表单 deptCode 参数；题型 factual/multi_hop/cross_reject（空缺省事实型）；
 * 期望答案 [拒答] 表示串库拒答题（与题型双向归一）。失败弹窗不关（§22.2）。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { ApiClientError } from '@/api/client'
import {
  importEvalQuestions,
  type EvalImportErrorRow,
  type EvalImportResult,
} from '@/api/evalAdmin'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; imported: [payload: EvalImportResult] }>()

const file = ref<UploadFile | null>(null)
const fileErr = ref('')
const error = ref('')
const submitting = ref(false)
/** 成功态（201）：imported（表无批次号列，不回 batchNo） */
const result = ref<EvalImportResult | null>(null)
/** 422/1003 整批退回时的错误行全量数组；文件级单一错误无 data 时为空数组 */
const errorRows = ref<EvalImportErrorRow[]>([])

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
    result.value = await importEvalQuestions(raw)
    errorRows.value = []
    ElMessage.success(`导入成功：本批 ${result.value.imported} 条评测题已入库`)
    emit('imported', result.value)
  } catch (e) {
    // 422/1003：message=统一中文提示（含"已整批退回，未入库"），data=错误行全量
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
    errorRows.value =
      e instanceof ApiClientError && Array.isArray(e.data) ? (e.data as EvalImportErrorRow[]) : []
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
    title="评测题库 Excel 导入（整批校验）"
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
          列：部门（中文名 客服/人力/全员）/ 问题 / 期望答案 / 来源 / 题型
          （factual·multi_hop·cross_reject，空缺省事实型）；期望答案填 [拒答] 表示串库拒答题；≤5000
          行；任一行不合格整批退回
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
      :title="`导入完成：本批 ${result.imported} 条评测题已入库`"
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
