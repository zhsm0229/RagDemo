<script setup lang="ts">
/**
 * 人工录分对话框（F-602 判定协议，api.md §4.2.6）：当前题三档单选
 * correct/partial/wrong——走批量端点单题提交（items 单元素数组）。原型三档
 * [正确/错误/拒答正确]，API 锁枚举为 correct/partial/wrong（"拒答正确"由自动
 * 判分承载，deviations #42）。仅 status=done 可录分（running/failed 422/3004），
 * 错误内联不关弹窗（§22.2）；成功 emit judged 由抽屉刷新明细与 pendingCount。
 */
import { ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { submitJudgments, type EvalJudgment, type EvalRunDetailItem } from '@/api/evalAdmin'
import { qtypeLabel, JUDGMENT_LABELS } from './options'

const props = defineProps<{
  visible: boolean
  runId: number | null
  item: EvalRunDetailItem | null
}>()
const emit = defineEmits<{ close: []; judged: [pendingCount: number] }>()

const judgment = ref<EvalJudgment>('correct')
const submitting = ref(false)
const error = ref('')

const JUDGE_OPTIONS: EvalJudgment[] = ['correct', 'partial', 'wrong']

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      // 回填已有人工判定（改判场景），无则默认"正确"
      judgment.value = props.item?.judgment ?? 'correct'
      error.value = ''
    }
  },
)

async function submit(): Promise<void> {
  if (submitting.value || props.runId === null || props.item === null) return
  submitting.value = true
  error.value = ''
  try {
    const result = await submitJudgments(props.runId, [
      { questionId: props.item.questionId, judgment: judgment.value },
    ])
    emit('judged', result.pendingCount)
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
    title="人工评判录分"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="emit('close')"
  >
    <template v-if="item">
      <p class="meta">
        <el-tag size="small" class="q-tag">题目 #{{ item.questionId }}</el-tag>
        <el-tag size="small" type="info" class="q-tag">{{ qtypeLabel(item.qtype) }}</el-tag>
        <span class="muted"
          >自动判定：{{ JUDGMENT_LABELS[item.autoJudgment] ?? item.autoJudgment }}</span
        >
      </p>
      <p class="meta">期望答案：{{ item.expected }}</p>
      <el-radio-group v-model="judgment">
        <el-radio-button v-for="option in JUDGE_OPTIONS" :key="option" :value="option">
          {{ JUDGMENT_LABELS[option] }}
        </el-radio-button>
      </el-radio-group>
      <p class="hint">折算口径：正确=1 / 部分正确=0.5 / 错误=0；提交后正确率即时重算。</p>
      <div v-if="error" class="err-inline" role="alert">{{ error }}</div>
    </template>
    <template #footer>
      <el-button @click="emit('close')">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">提交判定</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.meta {
  margin: 0 0 10px;
  font-size: 13px;
}
.q-tag {
  margin-right: 6px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.hint {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.err-inline {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 8px;
}
</style>
