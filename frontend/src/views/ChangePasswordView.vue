<script setup lang="ts">
/**
 * 改密页（ui-spec §九 独立路由 /change-password）：首登强制改密与已登录自助改密共用。
 * 对照原型首登改密对话框：新密码（≥8 位）/确认密码 + 内联错误 + "确认修改并进入"；
 * 差异已登记 deviations：#10 原型为对话框、正式实现为独立路由（ui-spec §九 明确独立路由，
 * 守卫白名单外全拦截保证不可绕过）；#11 新增"原密码"必填字段——后端契约必填
 * （docs/api.md §2.2，验旧密码即身份证明）。提交失败错误内联、停留本页（§22.2）。
 */
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { confirmValidator, passwordValidator, requiredTextValidator } from '@/validators'

const auth = useAuthStore()
const router = useRouter()

/** 首登强制（守卫拦截态）与自助改密的标题/按钮文案区分 */
const forced = computed(() => auth.mustChangePassword)

const formRef = ref<FormInstance>()
const form = reactive({ old: '', new: '', confirm: '' })
/** 服务端错误内联展示（2001 原密码不正确 / 1003 新密码不足 8 位等） */
const err = ref('')
const submitting = ref(false)

const rules: FormRules = {
  old: [{ validator: requiredTextValidator('原密码', 200), trigger: 'blur' }],
  new: [{ validator: passwordValidator, trigger: 'blur' }],
  confirm: [{ validator: confirmValidator(() => form.new), trigger: 'blur' }],
}

async function onSubmit(): Promise<void> {
  if (submitting.value) return
  err.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  try {
    await auth.changePassword({ old: form.old, new: form.new })
    ElMessage.success('密码修改成功')
    await router.push({ name: 'chat' })
  } catch (error) {
    err.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-box change-box">
    <h2>{{ forced ? '首次登录请修改密码' : '修改密码' }}</h2>
    <div class="login-sub">当前账号：{{ auth.user?.username }}</div>
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px" @submit.prevent>
      <el-form-item label="原密码" prop="old">
        <el-input v-model="form.old" type="password" show-password @keyup.enter="onSubmit" />
      </el-form-item>
      <el-form-item label="新密码" prop="new">
        <el-input
          v-model="form.new"
          type="password"
          show-password
          placeholder="至少 8 位"
          @keyup.enter="onSubmit"
        />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirm">
        <el-input v-model="form.confirm" type="password" show-password @keyup.enter="onSubmit" />
      </el-form-item>
      <div v-if="err" class="err-inline" role="alert">{{ err }}</div>
    </el-form>
    <el-button type="primary" class="submit" :loading="submitting" @click="onSubmit">
      {{ forced ? '确认修改并进入' : '确认修改' }}
    </el-button>
  </div>
</template>

<style scoped>
/* 复用登录卡片观感（原型改密对话框 420px），色值走主题变量（deviations #6） */
.login-box {
  width: 420px;
  margin: 10vh auto;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 32px;
}
.login-box h2 {
  text-align: center;
  margin: 0 0 8px;
  color: var(--el-text-color-primary);
}
.login-sub {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin-bottom: 20px;
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
  margin-bottom: 8px;
}
.submit {
  width: 100%;
}
</style>
