<script setup lang="ts">
/**
 * 跑分详情抽屉（F-602，api.md §4.2.5）：状态与进度 + config 快照（仅 done）+
 * 逐题明细（RunItemCard）+ 报告/阈值扫描区（RunReportPanel）+ 人工录分对话框。
 * 轮询契约（W3 交接）：running 期每 10s 轮询 GET /{id}，detail 仅含 items 无
 * pendingCount 键、failed 态无 pendingCount 键——一律可选消费不假设键存在；
 * 状态收口（done/failed）即停轮询并 emit changed 供列表刷新。
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import { getEvalRun, type EvalRunDetailItem, type EvalRunDetailData } from '@/api/evalAdmin'
import { formatPercent, runStatusTag } from './options'
import RecordJudgmentDialog from './RecordJudgmentDialog.vue'
import RunItemCard from './RunItemCard.vue'
import RunReportPanel from './RunReportPanel.vue'

const POLL_INTERVAL_MS = 10_000

const props = defineProps<{ visible: boolean; runId: number | null }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const loading = ref(false)
const run = ref<EvalRunDetailData | null>(null)
const loadErr = ref('')
const judgeVisible = ref(false)
const judgeItem = ref<EvalRunDetailItem | null>(null)
let pollTimer: number | null = null

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      void load()
    } else {
      stopPolling()
    }
  },
)

onBeforeUnmount(stopPolling)

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** running 才轮询：detail 无 pendingCount 键的期次照常渲染（键一律可选） */
function syncPolling(data: EvalRunDetailData): void {
  stopPolling()
  if (props.visible && data.status === 'running') {
    pollTimer = window.setInterval(() => void load(), POLL_INTERVAL_MS)
  }
}

async function load(): Promise<void> {
  if (props.runId === null) return
  loading.value = true
  loadErr.value = ''
  try {
    const data = await getEvalRun(props.runId)
    const wasRunning = run.value?.status === 'running'
    run.value = data
    syncPolling(data)
    // running → done/failed 收口：通知列表刷新（状态与准确率已定）
    if (wasRunning && data.status !== 'running') emit('changed')
  } catch {
    loadErr.value = '跑分详情加载失败，请稍后重试'
    stopPolling()
  } finally {
    loading.value = false
  }
}

function openJudge(item: EvalRunDetailItem): void {
  judgeItem.value = item
  judgeVisible.value = true
}

/** 录分成功：响应即含重算 summary + pendingCount，重拉明细并通知列表 */
function onJudged(): void {
  judgeVisible.value = false
  void load()
  emit('changed')
}

/** 进度文案：已跑题数对照总数；pendingCount 仅在键存在时展示（轮询容错） */
function progressText(data: EvalRunDetailData): string {
  const executed = data.detail?.items.length ?? 0
  const pending =
    data.detail?.pendingCount === undefined ? '' : ` · 待判定 ${data.detail.pendingCount}`
  return `已跑 ${executed} / ${data.total} 题${pending}`
}
</script>

<template>
  <el-drawer
    :model-value="visible"
    :title="run ? `跑分详情：${run.name}` : '跑分详情'"
    size="640px"
    @update:model-value="emit('close')"
  >
    <div v-loading="loading">
      <el-alert v-if="loadErr" type="error" :title="loadErr" :closable="false" />
      <template v-else-if="run">
        <div class="head" :data-test="`run-status-${run.status}`">
          <el-tag :type="runStatusTag(run.status).type">{{ runStatusTag(run.status).text }}</el-tag>
          <span class="progress" :data-test="`run-progress-${run.id}`">{{
            progressText(run)
          }}</span>
        </div>
        <p v-if="run.config" class="config" data-test="run-config">
          {{ run.config.model }} · 提示词 {{ run.config.promptVersion }} · 阈值 拒答
          {{ run.config.refuseThreshold }} / 问答对 {{ run.config.qaThreshold }} · topK
          {{ run.config.topK }} / topN {{ run.config.topN }}
        </p>
        <p v-else class="config muted">任务收口后锁定模型与阈值快照（running 期不可比）</p>
        <p v-if="run.status === 'done'" class="config">
          总体准确率：{{ formatPercent(run.accuracy) }}（加权得分 {{ run.correct }}）
        </p>

        <h4 class="sec-title">逐题明细</h4>
        <el-empty
          v-if="!run.detail || run.detail.items.length === 0"
          description="暂无明细（running 期逐题落库中）"
          :image-size="60"
        />
        <template v-else>
          <RunItemCard
            v-for="item in run.detail.items"
            :key="item.questionId"
            :item="item"
            :judgeable="run.status === 'done'"
            @judge="openJudge"
          />
        </template>

        <RunReportPanel :run-id="runId" :visible="visible" />
      </template>
    </div>

    <RecordJudgmentDialog
      :visible="judgeVisible"
      :run-id="runId"
      :item="judgeItem"
      @close="judgeVisible = false"
      @judged="onJudged"
    />
  </el-drawer>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.progress {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.config {
  margin: 8px 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.muted {
  color: var(--el-text-color-secondary);
}
.sec-title {
  margin: 16px 0 10px;
  font-size: 14px;
}
</style>
