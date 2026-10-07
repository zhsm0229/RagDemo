<script setup lang="ts">
/**
 * 知识库·模板生成（agent-ui-design §3，agent-evolution #08）：单次运行工作台。
 * 配置区（来源文档/目标部门/生成数量 + 开始生成，运行中整体禁用并出现停止）+
 * 左时间线（RunTimeline，STEP/TOOL 事件驱动）+ 右草稿预览（TEXT_MESSAGE 增量成卡，
 * 纯文本渲染不 rich-render——LLM 输出按不可信数据处理，§17.5 前端侧体现）+
 * 完成汇总条（去审核 → 文档管理草稿筛选，参数对齐 #05 筛选字段；再生成一批）与
 * 失败/停止/停滞三态重试。运行中离页二次确认（§3.6）；页面历史运行折叠保留可查。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listDocuments, type DocumentItem } from '@/api/documents'
import { DEPT_LABELS, DEPT_OPTIONS } from '@/components/documents/options'
import RunTimeline from '@/components/agent/RunTimeline.vue'
import { useAgentRun, type AgentRunRecord } from '@/composables/useAgentRun'

const router = useRouter()

const docs = ref<DocumentItem[]>([])
const docId = ref<number | null>(null)
const dept = ref('')
const count = ref(3)
const draftEl = ref<HTMLElement | null>(null)

// 长任务看门狗（模板生成为非流式长补全 + 多工具循环，实测服务端一轮可静默 2-5 分钟）：
// 停滞 240s / 总时长 600s（agent 工具内 CPU rerank 单次实测 ~110s，连查两次余量）
const agent = useAgentRun({ onUpdate: scrollFollow }, { stallMs: 240_000, totalMs: 600_000 })
const activeRun = computed<AgentRunRecord | null>(() => agent.activeRun.value)
const running = computed(() => agent.running.value)

/** 进行中节点的实时耗时计时（单一定时器，父级驱动、子组件展示） */
const now = ref(Date.now())
let ticker: number | null = null
watch(running, (on) => {
  if (on) {
    now.value = Date.now()
    ticker = window.setInterval(() => {
      now.value = Date.now()
    }, 1000)
  } else if (ticker !== null) {
    window.clearInterval(ticker)
    ticker = null
  }
})
onUnmounted(() => {
  if (ticker !== null) window.clearInterval(ticker)
})

onMounted(() => void loadDocs())

async function loadDocs(): Promise<void> {
  try {
    // 来源文档只取"就绪"（设计 §3.1）；试点量级 pageSize=100 一页拉满
    const page = await listDocuments({ status: 'ready', page: 1, pageSize: 100 })
    docs.value = page.items
  } catch {
    // 错误提示已由 client 统一弹出；下拉为空由用户重进页面重试
  }
}

function durationText(ms: number | null): string {
  if (ms === null) return ''
  const seconds = Math.round(ms / 1000)
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}

function scrollFollow(): void {
  void nextTick(() => {
    const el = draftEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

/** 任务输入 = 配置区的自然语言组装（后端 input 为单字符串，1-2000 字） */
function composeInput(): string {
  const doc = docs.value.find((d) => d.id === docId.value)
  const deptLabel = DEPT_LABELS[dept.value] ?? dept.value
  const title = (doc?.title ?? '').slice(0, 100) // 防超长标题顶爆 input 上限
  return `请为「${deptLabel}」部门基于知识库文档《${title}》生成 ${count.value} 套模板草稿`
}

async function start(): Promise<void> {
  if (running.value) return
  if (docId.value === null) {
    ElMessage.warning('请选择来源文档')
    return
  }
  if (dept.value === '') {
    ElMessage.warning('请选择目标部门')
    return
  }
  await agent.run('template_draft', composeInput())
}

/** 去审核：跳文档管理并携带 #05 的草稿筛选字段（sourceType=draft + reviewStatus=待审核） */
function goReview(): void {
  void router.push({
    path: '/kb/documents',
    query: { sourceType: 'draft', reviewStatus: '待审核' },
  })
}

// 运行中离页二次确认（§3.6）；确认离开后组件卸载中止本地消费（服务端断连兜底）
onBeforeRouteLeave(async () => {
  if (!running.value) return true
  try {
    await ElMessageBox.confirm(
      '离开将不再看到运行过程，已生成的草稿不受影响。确认离开？',
      '离开确认',
      {
        type: 'warning',
        confirmButtonText: '离开',
        cancelButtonText: '留下',
      },
    )
    return true
  } catch {
    return false
  }
})

const STATUS_TEXT: Record<string, string> = {
  idle: '空闲',
  running: '生成中',
  succeeded: '已完成',
  failed: '已失败',
  stopped: '已停止',
}
const phase = computed(() => activeRun.value?.phase ?? 'idle')
const statusText = computed(() => STATUS_TEXT[phase.value] ?? '空闲')
/** 历史运行（非最新一次），折叠保留可查（§3.4/§3.5） */
const pastRuns = computed(() => agent.runs.slice(0, -1))
const historySummary = (run: AgentRunRecord): string => {
  const base = `草稿 ${run.drafts.length} 条 · ${durationText(run.durationMs)}`
  if (run.phase === 'failed') return `${base} · ${run.failure ?? '生成失败'}`
  if (run.phase === 'stopped') return `${base} · 已停止`
  return base
}
</script>

<template>
  <div class="tpl-gen">
    <!-- 配置区（单行，运行中整体禁用 §3.6） -->
    <div class="config-row">
      <el-select
        v-model="docId"
        class="cfg-doc"
        placeholder="来源文档（就绪状态）"
        filterable
        :disabled="running"
      >
        <el-option v-for="doc in docs" :key="doc.id" :label="doc.title" :value="doc.id" />
      </el-select>
      <el-select v-model="dept" class="cfg-dept" placeholder="目标部门" :disabled="running">
        <el-option
          v-for="option in DEPT_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
      <el-input-number
        v-model="count"
        class="cfg-count"
        :min="1"
        :max="10"
        :disabled="running"
        aria-label="生成数量"
      />
      <el-tooltip content="生成进行中，请稍候" :disabled="!running" placement="top">
        <span>
          <el-button type="primary" :disabled="running" @click="start">开始生成</el-button>
        </span>
      </el-tooltip>
      <span class="long-task-hint" data-test="long-task-hint"
        >长任务：全过程约 1-3 分钟，期间页面静默属正常，请勿关闭</span
      >
      <el-button v-if="running" plain type="warning" @click="agent.stop()">停止</el-button>
      <span class="spacer" />
      <span class="run-status" aria-live="polite" data-test="run-status"
        >状态：{{ statusText }}</span
      >
    </div>

    <!-- 历史运行折叠摘要（第 2 次运行起出现，§3.4/§3.5） -->
    <el-collapse v-if="pastRuns.length > 0" class="history" data-test="run-history">
      <el-collapse-item
        v-for="(run, index) in pastRuns"
        :key="run.key"
        :name="String(run.key)"
        :title="`第 ${index + 1} 次运行（展开查看完整过程） · ${historySummary(run)}`"
      >
        <RunTimeline :record="run" :now="now" />
      </el-collapse-item>
    </el-collapse>

    <!-- 完成汇总条 / 失败 / 停止（对照 ServicesHealthPanel overall 三态 alert） -->
    <div
      v-if="phase === 'succeeded' && activeRun !== null"
      class="summary-row"
      data-test="run-summary"
    >
      <el-alert
        type="success"
        :closable="false"
        show-icon
        class="summary-alert"
        :title="`生成完成：共 ${activeRun.drafts.length} 条草稿，耗时 ${durationText(activeRun.durationMs)}`"
      />
      <el-button type="primary" @click="goReview">去审核</el-button>
      <el-button @click="start">再生成一批</el-button>
    </div>
    <div
      v-else-if="phase === 'failed' && activeRun !== null"
      class="summary-row"
      data-test="run-failure"
    >
      <el-alert
        type="error"
        :closable="false"
        show-icon
        class="summary-alert"
        :title="activeRun.retryable ? '生成失败，可重试' : '生成失败'"
        :description="activeRun.failure ?? undefined"
      />
      <el-button v-if="activeRun.retryable" plain type="warning" @click="agent.retryLast()"
        >重试</el-button
      >
    </div>
    <div
      v-else-if="phase === 'stopped' && activeRun !== null"
      class="summary-row"
      data-test="run-stopped"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="summary-alert"
        title="已停止，可重新生成"
      />
      <el-button plain type="warning" @click="agent.retryLast()">重试</el-button>
    </div>

    <!-- 主体两栏：左时间线 / 右草稿预览 -->
    <div v-if="activeRun !== null" class="run-body">
      <div class="timeline-pane">
        <div class="pane-title">运行过程</div>
        <RunTimeline :record="activeRun" :now="now" />
      </div>
      <div ref="draftEl" class="draft-pane" data-test="draft-pane">
        <div class="pane-title">草稿预览</div>
        <div
          v-for="(draft, index) in activeRun.drafts"
          :key="draft.key"
          class="draft-card"
          :data-test="`draft-card-${index + 1}`"
        >
          <div class="draft-head">
            <span>草稿 {{ index + 1 }}</span>
            <el-tag v-if="draft.status === 'streaming'" size="small" type="info">生成中</el-tag>
          </div>
          <!-- LLM 生成文本按不可信数据处理：仅纯文本渲染，不解析 HTML/Markdown（§3.3） -->
          <pre class="draft-body">{{ draft.content }}</pre>
        </div>
        <el-empty
          v-if="activeRun.drafts.length === 0"
          description="草稿将在这里流式呈现"
          :image-size="60"
        />
      </div>
    </div>
    <el-empty
      v-else
      class="guide-empty"
      description="选择文档后点“开始生成”，运行过程与草稿将在这里呈现"
      :image-size="60"
    />
  </div>
</template>

<style scoped>
.tpl-gen {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.config-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 12px 16px;
}
.cfg-doc {
  width: 260px;
}
.cfg-dept {
  width: 140px;
}
.cfg-count {
  width: 110px;
}
.spacer {
  flex: 1;
}
.run-status {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.long-task-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.history {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: #fff;
}
.summary-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.summary-alert {
  flex: 1;
}
.run-body {
  flex: 1;
  display: flex;
  gap: 12px;
  min-height: 0;
}
.pane-title {
  font-weight: 600;
  margin-bottom: 10px;
}
.timeline-pane {
  flex: 2;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 14px 18px;
  overflow: auto;
}
.draft-pane {
  flex: 3;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 14px 18px;
  overflow: auto;
}
.draft-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  margin-bottom: 12px;
}
.draft-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-weight: 500;
}
.draft-body {
  margin: 0;
  padding: 10px 12px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.guide-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  background: #fff;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
}
</style>
