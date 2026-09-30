<script setup lang="ts">
/**
 * badcase 处置对话框（F-702，对照原型 handle 弹窗，flows F4 治理闭环）：
 * 处理方式 radio（已修语料=更新文档/补问答对 / 已忽略=忽略需说明理由）+ 备注
 * （ADM-03：'已忽略' 必填 strip 后 1-500 前端先行校验，'已修语料' 可选给了则
 * 同样 1-500——validators.checkBadcaseNote 统一口径 §21.3）；PATCH 422 兜底错误
 * 内联展示且弹窗不关（§22.2）；成功 emit handled 由父级刷新列表。
 * 原型按钮/成功提示含"纳入评测题库"语义，后端处置端点无该动作 → 不虚假承诺
 * （deviations #40）。
 */
import { reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ApiClientError } from '@/api/client'
import {
  patchBadcase,
  type BadcaseItem,
  type BadcasePatchPayload,
  type BadcaseStatus,
} from '@/api/badcase'
import { badcaseNoteValidator } from '@/validators'

const props = defineProps<{ visible: boolean; row: BadcaseItem | null }>()
const emit = defineEmits<{
  close: []
  handled: [payload: { messageId: number; badcaseStatus: BadcaseStatus }]
}>()

const formRef = ref<FormInstance>()
const form = reactive<{ badcaseStatus: BadcaseStatus; note: string }>({
  badcaseStatus: '已修语料',
  note: '',
})
const formErr = ref('')
const submitting = ref(false)

const rules: FormRules = {
  note: [{ validator: badcaseNoteValidator(() => form.badcaseStatus), trigger: 'blur' }],
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      form.badcaseStatus = '已修语料'
      form.note = ''
      formErr.value = ''
    }
  },
)

async function submit(): Promise<void> {
  if (submitting.value || props.row === null) return
  formErr.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  const payload: BadcasePatchPayload = {
    badcaseStatus: form.badcaseStatus,
    // '已修语料' 未填理由 → null（后端可选口径），不给空白串
    note: form.note.trim() === '' ? null : form.note.trim(),
  }
  try {
    const result = await patchBadcase(props.row.messageId, payload)
    emit('handled', result)
    emit('close')
  } catch (error) {
    formErr.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="处理 badcase"
    width="480px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <p class="question">{{ row?.questionPreview }}</p>
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px" @submit.prevent>
      <el-form-item label="处理方式">
        <el-radio-group v-model="form.badcaseStatus">
          <el-radio value="已修语料">更新文档/补问答对</el-radio>
          <el-radio value="已忽略">忽略（说明理由）</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="备注" prop="note">
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
          :placeholder="
            form.badcaseStatus === '已忽略'
              ? '忽略必须说明理由（1-500 字）'
              : '选填，说明修订内容（1-500 字）'
          "
        />
      </el-form-item>
      <div v-if="formErr" class="err-inline" role="alert">{{ formErr }}</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">提交</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.question {
  margin: 0 0 10px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
/* 对照原型 .err-inline（§22.2 错误内联，弹窗不关） */
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
</style>
