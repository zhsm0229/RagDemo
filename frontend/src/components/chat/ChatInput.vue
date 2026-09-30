<script setup lang="ts">
/**
 * 输入区（对照原型 chat-input）：1-2000 字（strip 口径，validators 与后端一致）
 * + 字数计数器 + 空禁发送；Enter 发送 / Shift+Enter 换行（中文输入法选词回车不发送）；
 * 发送中禁用并显示"停止"（AbortController 取消，T8 评审清单③，原型无此控件）；
 * 失焦校验空值、超长即时提示并禁发送（ui-spec §二交互表）。
 */
import { computed, ref } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'
import { questionValidator } from '@/validators'

const props = defineProps<{
  /** 发送中禁用（全局一机一流），同时作为"停止"按钮的展示开关 */
  disabled?: boolean
}>()

const emit = defineEmits<{
  send: [question: string]
  stop: []
}>()

const formRef = ref<FormInstance>()
const question = ref('')

const trimmed = computed(() => question.value.trim())
const overLimit = computed(() => trimmed.value.length > 2000)
const canSend = computed(() => !props.disabled && trimmed.value.length > 0 && !overLimit.value)

const rules: FormRules = { question: [{ validator: questionValidator, trigger: 'blur' }] }

function onEnter(event: KeyboardEvent): void {
  // 中文输入法选词回车不发送（isComposing / keyCode 229 守卫，对照原型）
  if (event.isComposing || event.keyCode === 229) return
  trySend()
}

function trySend(): void {
  if (overLimit.value || props.disabled) return
  if (trimmed.value.length === 0) {
    // 空值即点发送：按失焦同口径内联提示
    formRef.value?.validateField('question').catch(() => undefined)
    return
  }
  emit('send', trimmed.value)
  question.value = ''
  // 清空触发的 change 校验会立刻弹出"问题不能为空"，发送成功后清掉校验痕迹
  formRef.value?.clearValidate()
}
</script>

<template>
  <div class="chat-input">
    <el-form ref="formRef" :model="{ question }" :rules="rules" class="input-form" @submit.prevent>
      <el-form-item prop="question" class="input-item">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          resize="none"
          maxlength="2100"
          placeholder="输入问题（1-2000 字），Enter 发送 / Shift+Enter 换行"
          @keydown.enter.exact.prevent="onEnter"
        />
      </el-form-item>
    </el-form>
    <div class="input-side">
      <el-button v-if="disabled" size="small" plain type="warning" @click="emit('stop')"
        >停止</el-button
      >
      <el-tooltip
        :content="
          disabled ? '回答生成中，请稍候' : overLimit ? '问题长度须在 1-2000 字之间' : '发送'
        "
        :disabled="!disabled && !overLimit"
        placement="top"
      >
        <span>
          <el-button type="primary" :disabled="!canSend" @click="trySend">发送</el-button>
        </span>
      </el-tooltip>
      <span class="counter" :class="{ over: overLimit }">{{ trimmed.length }}/2000</span>
    </div>
  </div>
</template>

<style scoped>
.chat-input {
  border-top: 1px solid var(--el-border-color-light);
  padding: 12px 16px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.input-form {
  flex: 1;
}
.input-item {
  margin-bottom: 0;
}
.input-side {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-end;
  padding-bottom: 4px;
}
.counter {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.counter.over {
  color: var(--el-color-danger);
}
</style>
