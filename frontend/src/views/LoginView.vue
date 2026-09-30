<script setup lang="ts">
/**
 * P1 登录页（ui-spec §九）：居中卡片 + 账号/密码 + 错误内联（§22.2）。
 * 逐字段对照原型 login-box：标题/副标题/无标签占位输入/回车提交/整宽"登 录"按钮；
 * 原型卡底的演示提示区（demo-tip，admin/客服小王/密码 123456）为演示数据，生产不实现
 * （deviations #9）。校验仅必填+失焦（口径随后端 LoginRequest 1-64/1-200），其余交后端。
 * 登录成功统一进问答页（含 admin，§9.1 与原型 onLogged 一致）；mustChangePassword
 * 由守卫与显式分流引导至改密页，不可绕过。
 */
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { requiredTextValidator } from '@/validators'

const auth = useAuthStore()
const router = useRouter()

const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '' })
/** 服务端错误内联展示（2001 带剩余次数 / 2002 锁定 / 2005 限流，文案以服务端为准） */
const err = ref('')
const submitting = ref(false)

const rules: FormRules = {
  username: [{ validator: requiredTextValidator('用户名', 64), trigger: 'blur' }],
  password: [{ validator: requiredTextValidator('密码', 200), trigger: 'blur' }],
}

async function onSubmit(): Promise<void> {
  if (submitting.value) return
  err.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (valid !== true) return
  submitting.value = true
  try {
    const me = await auth.login({ username: form.username.trim(), password: form.password })
    await router.push(me.mustChangePassword ? { name: 'change-password' } : { name: 'chat' })
  } catch (error) {
    err.value = error instanceof ApiClientError ? error.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-box">
    <h2>企业智能工作台</h2>
    <div class="login-sub">基于企业知识库的智能问答 · 内部系统</div>
    <el-form ref="formRef" :model="form" :rules="rules" @submit.prevent>
      <el-form-item prop="username">
        <el-input v-model="form.username" placeholder="用户名" @keyup.enter="onSubmit" />
      </el-form-item>
      <el-form-item prop="password">
        <el-input
          v-model="form.password"
          type="password"
          placeholder="密码"
          show-password
          @keyup.enter="onSubmit"
        />
      </el-form-item>
      <div v-if="err" class="err-inline" role="alert">{{ err }}</div>
      <el-button type="primary" class="submit" :loading="submitting" @click="onSubmit">
        登 录
      </el-button>
    </el-form>
  </div>
</template>

<style scoped>
/* 对照原型 .login-box：380px 居中卡片，色值走主题变量（deviations #6） */
.login-box {
  width: 380px;
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
/* 对照原型 .err-inline（§22.2 错误内联，不弹窗） */
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 4px;
}
.submit {
  width: 100%;
  margin-top: 8px;
}
</style>
