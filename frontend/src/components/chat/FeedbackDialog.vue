<script setup lang="ts">
/**
 * 点踩反馈弹窗（F-208/CHAT-09，对照原型 fbDialog，MaxKB 模式 §8.2）：
 * 分类单选（枚举与后端同源）+ 备注 strip 后 1-500 必填。前端先行校验内联拦截；
 * 后端 422/1003 兜底错误同样内联且弹窗不关闭（§22.2）。取消反馈后端无端点不做。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { FEEDBACK_CATEGORIES, leaveFeedback } from '@/api/chat'
import type { ChatMessage } from '@/composables/useChatStream'

const props = defineProps<{
  /** 被点踩的消息（仅已落库回答可反馈，入口已在气泡侧把关） */
  message: ChatMessage | null
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
  submitted: [payload: { category: string; comment: string }]
}>()

const category = ref('')
const comment = ref('')
const error = ref<string | null>(null)
const submitting = ref(false)

function reset(): void {
  category.value = ''
  comment.value = ''
  error.value = null
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

async function submit(): Promise<void> {
  const message = props.message
  if (message === null || message.id === null) return
  const trimmed = comment.value.trim()
  // 前端先行校验（后端 422 同口径：分类枚举 / 备注 strip 1-500，docs/api.md §3.6）
  if (category.value === '') {
    error.value = '请选择问题分类'
    return
  }
  if (trimmed.length === 0) {
    error.value = '点踩必须填写备注（帮助定位 badcase）'
    return
  }
  if (trimmed.length > 500) {
    error.value = '备注长度须在 1-500 字之间'
    return
  }
  submitting.value = true
  try {
    // silent：422 兜底错误在弹窗内联展示，不双弹全局 toast，且弹窗不关闭（§22.2）
    await leaveFeedback(
      message.id,
      { feedback: 'down', category: category.value, comment: trimmed },
      true,
    )
    ElMessage.success('反馈已提交，感谢帮助改进')
    emit('submitted', { category: category.value, comment: trimmed })
    emit('close')
  } catch (e) {
    error.value = e instanceof ApiClientError ? e.message : '系统繁忙，请稍后重试'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    title="反馈问题（点踩）"
    width="440px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <el-form label-width="80px">
      <el-form-item label="分类" required>
        <el-radio-group v-model="category">
          <el-radio v-for="c in FEEDBACK_CATEGORIES" :key="c" :value="c">{{ c }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="备注" required>
        <el-input
          v-model="comment"
          type="textarea"
          :rows="3"
          maxlength="500"
          placeholder="请描述问题，帮助定位 badcase"
        />
      </el-form-item>
      <div v-if="error !== null" class="err-inline">{{ error }}</div>
    </el-form>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">提交反馈</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.err-inline {
  color: var(--el-color-danger);
  font-size: 13px;
  margin-left: 80px;
}
</style>
