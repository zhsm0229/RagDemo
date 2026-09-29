<script setup lang="ts">
/**
 * 上传文档对话框（对照原型 upload 两步流程，F-1001~F-1008）：
 * 第 1 步选文件（白名单即时校验，.doc 即时拒收提示 §10.1）→ 第 2 步元数据表单
 * （必填失焦校验 §21.3 + 合规确认必勾 §7.4）→ 提交 202 → emit uploaded 由父级
 * 关弹窗、回第 1 页刷新（§22.3）。
 * F-1008 元数据建议：选中文件后调 metadata-suggest 回填（建议值可改，仅预填）；
 * 端点未合入（T4 分支）时 404 静默跳过，标题始终按文件名本地预填兜底（deviations #22）。
 * 原型"同步预检结果"卡为演示数据不实现——预检在后端提交时同步执行，422/409 内联展示。
 */
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules, UploadFile } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { suggestDocumentMetadata, uploadDocument } from '@/api/documents'
import { requiredPick, requiredText } from '@/validators'
import { DEPT_OPTIONS } from './options'

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; uploaded: [payload: { id: number; status: string }] }>()

const step = ref<1 | 2>(1)
const file = ref<UploadFile | null>(null)
const fileErr = ref('')
const formErr = ref('')
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ title: '', dept: '', owner: '', effectiveDate: '', expiryDate: '' })
const comply = ref(false)

const rules: FormRules = {
  title: [{ validator: requiredText('标题'), trigger: 'blur' }],
  dept: [{ validator: requiredPick('部门'), trigger: 'change' }],
  owner: [{ validator: requiredText('责任人'), trigger: 'blur' }],
  effectiveDate: [{ validator: requiredPick('生效日期'), trigger: 'change' }],
}

function reset(): void {
  step.value = 1
  file.value = null
  fileErr.value = ''
  formErr.value = ''
  Object.assign(form, { title: '', dept: '', owner: '', effectiveDate: '', expiryDate: '' })
  comply.value = false
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

/** 文件白名单（扩展名 + 体积；魔数由后端 check_sync 把关）：.doc 即时拒收提示 */
function handleFile(uploadFile: UploadFile): void {
  const name = uploadFile.name.toLowerCase()
  if (name.endsWith('.doc')) {
    fileErr.value = '.doc 不支持，请另存为 docx 后上传'
    file.value = null
    return
  }
  if (!/\.(pdf|docx)$/.test(name)) {
    fileErr.value = '仅支持 pdf / docx 文件'
    file.value = null
    return
  }
  const raw = uploadFile.raw
  if (!raw) return
  if (raw.size > MAX_UPLOAD_BYTES) {
    fileErr.value = '文件超过 50MB'
    file.value = null
    return
  }
  fileErr.value = ''
  file.value = uploadFile
  // 标题本地预填兜底（原型口径：去扩展名）；建议接口只预填不覆盖已填值
  if (form.title === '') form.title = raw.name.replace(/\.[^.]+$/, '')
  void applySuggestion(raw)
}

/** F-1008 建议（仅预填）：端点未合入/解析失败时静默保留本地预填（deviations #22） */
async function applySuggestion(raw: File): Promise<void> {
  try {
    const s = await suggestDocumentMetadata(raw)
    if (s.suggestedTitle) form.title = s.suggestedTitle
    if (
      s.suggestedDeptCode === 'service' ||
      s.suggestedDeptCode === 'hr' ||
      s.suggestedDeptCode === 'all'
    ) {
      form.dept = s.suggestedDeptCode
    }
    if (s.suggestedEffectiveDate) form.effectiveDate = s.suggestedEffectiveDate
  } catch {
    // 静默：建议缺失不阻塞上传主流程
  }
}

async function submit(): Promise<void> {
  if (submitting.value) return
  formErr.value = ''
  const raw = file.value?.raw
  if (!raw) return
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  if (!comply.value) {
    formErr.value = '必须勾选合规确认后才能入库'
    return
  }
  submitting.value = true
  try {
    const result = await uploadDocument(raw, {
      title: form.title.trim(),
      dept: form.dept,
      owner: form.owner.trim(),
      effectiveDate: form.effectiveDate,
      expiryDate: form.expiryDate || null,
    })
    ElMessage.success('已加入解析队列')
    emit('uploaded', result)
    emit('close')
  } catch (error) {
    // 422/409（格式不支持 / 精确查重）内联展示，弹窗不关闭（§22.2）
    formErr.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}

defineExpose({ handleFile, submit })
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="上传文档"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
    @closed="reset"
  >
    <template v-if="step === 1">
      <el-upload
        drag
        :auto-upload="false"
        :limit="1"
        accept=".pdf,.docx,.doc"
        :on-change="handleFile"
        :file-list="file ? [file] : []"
      >
        <div style="padding: 20px">
          将文件拖到此处，或 <em>点击选择</em><br />
          <span style="font-size: 12px; color: var(--el-text-color-secondary)">
            仅支持 pdf / docx，≤50MB；.doc 请先另存为 docx
          </span>
        </div>
      </el-upload>
      <div v-if="fileErr" class="err-inline" role="alert">{{ fileErr }}</div>
    </template>
    <template v-else>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px" @submit.prevent>
        <el-form-item label="标题" prop="title">
          <el-input v-model="form.title" placeholder="规范命名：部门_主题（版本自动递增）" />
        </el-form-item>
        <el-form-item label="部门" prop="dept">
          <el-select v-model="form.dept" style="width: 100%">
            <el-option v-for="o in DEPT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="责任人" prop="owner">
          <el-input v-model="form.owner" />
        </el-form-item>
        <el-form-item label="生效日期" prop="effectiveDate">
          <el-date-picker
            v-model="form.effectiveDate"
            type="date"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="到期日期">
          <el-date-picker
            v-model="form.expiryDate"
            type="date"
            value-format="YYYY-MM-DD"
            style="width: 100%"
            placeholder="选填，到期自动下线"
          />
        </el-form-item>
        <el-form-item label="合规确认" required>
          <el-checkbox v-model="comply">
            我确认本文档为试点部门<b>人人可读</b>，无涉密内容
          </el-checkbox>
        </el-form-item>
        <div v-if="formErr" class="err-inline" role="alert">{{ formErr }}</div>
      </el-form>
    </template>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button v-if="step === 1" type="primary" :disabled="!file" @click="step = 2">
        下一步
      </el-button>
      <el-button v-else type="primary" :loading="submitting" @click="submit">提交入库</el-button>
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
</style>
