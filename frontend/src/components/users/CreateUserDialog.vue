<script setup lang="ts">
/**
 * 发放账号对话框（F-102，对照原型 create 弹窗）：用户名（2-32 位中文/字母/数字
 * §21.5，validators 统一收敛 §21.3）+ 部门（必选）+ 角色（默认成员）；提交 201
 * 后 emit created 由父级关本弹窗、回第 1 页刷新并弹初始密码一次性展示（§22.3）。
 * 409 重名 / 422 校验失败内联展示且弹窗不关（§22.2）。
 */
import { reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { createAdminUser, type InitialPasswordResult } from '@/api/adminUsers'
import type { UserRole } from '@/api/types'
import { ADMIN_DEPT_CODE_OPTIONS } from '@/components/admin/options'
import { requiredPick, usernameValidator } from '@/validators'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: []; created: [payload: InitialPasswordResult] }>()

const formRef = ref<FormInstance>()
const form = reactive<{ username: string; deptCode: string; role: UserRole }>({
  username: '',
  deptCode: '',
  role: 'member',
})
const formErr = ref('')
const submitting = ref(false)

const rules: FormRules = {
  username: [{ validator: usernameValidator, trigger: 'blur' }],
  deptCode: [{ validator: requiredPick('部门'), trigger: 'change' }],
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      form.username = ''
      form.deptCode = ''
      form.role = 'member'
      formErr.value = ''
    }
  },
)

async function submit(): Promise<void> {
  if (submitting.value) return
  formErr.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  try {
    const created = await createAdminUser({
      username: form.username.trim(),
      deptCode: form.deptCode,
      role: form.role,
    })
    emit('created', created)
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
    title="新建用户"
    width="460px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px" @submit.prevent>
      <el-form-item label="用户名" prop="username" required>
        <el-input
          v-model="form.username"
          placeholder="2-32 位，支持中文、字母、数字"
          maxlength="32"
        />
      </el-form-item>
      <el-form-item label="部门" prop="deptCode" required>
        <el-select v-model="form.deptCode" placeholder="请选择部门" style="width: 100%">
          <el-option
            v-for="o in ADMIN_DEPT_CODE_OPTIONS"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="角色">
        <el-radio-group v-model="form.role">
          <el-radio value="member">成员</el-radio>
          <el-radio value="admin">管理员</el-radio>
        </el-radio-group>
      </el-form-item>
      <div v-if="formErr" class="err-inline" role="alert">{{ formErr }}</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">
        创建并生成初始密码
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
</style>
