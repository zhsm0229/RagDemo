<script setup lang="ts">
/**
 * 编辑问答对对话框（QA-06，对照原型 edit 弹窗）：问题（1-2000）/ 答案（1-10000）
 * 必填校验同后端导入规则（§21.5），maxlength + show-word-limit 即时限长；
 * PUT 全文提交，422 校验失败内联展示且弹窗不关（§22.2），成功 emit saved 由父级刷新。
 */
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { updateQaPair, type QaPairItem } from '@/api/qaPairs'

const props = defineProps<{ visible: boolean; qa: QaPairItem | null }>()
const emit = defineEmits<{
  close: []
  saved: [payload: { id: number; question: string; answer: string }]
}>()

const formRef = ref<FormInstance>()
const form = reactive({ question: '', answer: '' })
const formErr = ref('')
const submitting = ref(false)

/** 与后端同口径：strip 后非空 + 上限（maxlength 已即时拦超长，此处兜底） */
function requiredText(label: string, max: number) {
  return (_rule: unknown, value: unknown, callback: (error?: Error) => void): void => {
    const text = typeof value === 'string' ? value.trim() : ''
    if (text.length < 1) callback(new Error(`请输入${label}`))
    else if (text.length > max) callback(new Error(`${label}长度须在 1-${max} 字之间`))
    else callback()
  }
}

const rules: FormRules = {
  question: [{ validator: requiredText('问题', 2000), trigger: 'blur' }],
  answer: [{ validator: requiredText('答案', 10000), trigger: 'blur' }],
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      form.question = props.qa?.question ?? ''
      form.answer = props.qa?.answer ?? ''
      formErr.value = ''
    }
  },
)

async function submit(): Promise<void> {
  if (submitting.value || props.qa === null) return
  formErr.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  try {
    const saved = await updateQaPair(props.qa.id, {
      question: form.question.trim(),
      answer: form.answer.trim(),
    })
    ElMessage.success('已保存，问题已重新向量化，新答案立即生效')
    emit('saved', saved)
    emit('close')
  } catch (error) {
    formErr.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}

defineExpose({ submit })
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="编辑问答对"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="70px" @submit.prevent>
      <el-form-item label="问题" prop="question" required>
        <el-input v-model="form.question" maxlength="2000" show-word-limit />
      </el-form-item>
      <el-form-item label="答案" prop="answer" required>
        <el-input
          v-model="form.answer"
          type="textarea"
          :rows="4"
          maxlength="10000"
          show-word-limit
        />
      </el-form-item>
      <div v-if="formErr" class="err-inline" role="alert">{{ formErr }}</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
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
