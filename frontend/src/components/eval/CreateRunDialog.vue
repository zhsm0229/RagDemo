<script setup lang="ts">
/**
 * 发起跑分对话框（F-602，对照原型 run 对话框 + api.md §4.2.3）：任务名称 + 视角部门。
 * 原型仅"部门范围"一项；契约 name 为必填（strip 后非空 ≤100 字）→ 补名称输入框
 * （校验走统一 src/validators，§21.3）。视角部门三档 service/hr/all（deviations #28
 * 同口径：后端枚举仅此三者，原型"全部（admin）"无对应取值）；检索范围恒为
 * [视角部门, 全员]，题面部门只进报告口径。202 语义：提交成功即关闭并 emit
 * created(id)，跑分后台执行，列表轮询看状态（原型 setTimeout 模拟完成 ≠ 真实异步）。
 */
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { createEvalRun } from '@/api/evalAdmin'
import { ADMIN_DEPT_CODE_OPTIONS } from '@/components/admin/options'
import { requiredTextValidator, requiredPick } from '@/validators'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; created: [runId: number] }>()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const error = ref('')
const form = reactive({
  name: '',
  perspectiveDeptCode: 'service',
})

const rules = {
  name: [{ validator: requiredTextValidator('任务名称', 100), trigger: 'blur' }],
  perspectiveDeptCode: [{ validator: requiredPick('视角部门'), trigger: 'change' }],
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      form.name = ''
      form.perspectiveDeptCode = 'service'
      error.value = ''
    }
  },
)

async function submit(): Promise<void> {
  if (submitting.value) return
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  error.value = ''
  try {
    const data = await createEvalRun({
      name: form.name,
      perspectiveDeptCode: form.perspectiveDeptCode,
    })
    ElMessage.success('跑分任务已创建，正在后台执行，可在列表查看进度')
    emit('created', data.id)
  } catch (e) {
    // 视角部门未播种 422/1003 等：错误内联不关弹窗（§22.2）
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="发起评测跑分"
    width="460px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="任务名称" prop="name">
        <el-input
          v-model="form.name"
          maxlength="100"
          show-word-limit
          placeholder="如：9 月客服域回归"
          @keyup.enter="submit"
        />
      </el-form-item>
      <el-form-item label="视角部门" prop="perspectiveDeptCode">
        <el-select v-model="form.perspectiveDeptCode" style="width: 100%">
          <el-option
            v-for="o in ADMIN_DEPT_CODE_OPTIONS"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
        </el-select>
      </el-form-item>
      <p class="hint">
        检索范围恒为 [视角部门, 全员]；题面部门只进报告口径。串库题（期望答案
        [拒答]）按回答是否为固定拒答话术自动判分。
      </p>
      <div v-if="error" class="err-inline" role="alert">{{ error }}</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">开始跑分</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
