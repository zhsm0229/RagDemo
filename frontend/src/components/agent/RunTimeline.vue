<script setup lang="ts">
/**
 * 运行时间线（agent-ui-design §3.2）：一次运行记录 → el-timeline 三级层级。
 * 0 号"已提交生成任务"伪节点 + 一级步骤节点（进行中=脉冲点+进行中+实时耗时，
 * 完成=✓+耗时）+ 二级工具节点（缩进，ARGS 等宽默认折叠——native details/summary，
 * 键盘可达）。状态不唯颜色：每个节点都有文字终态（§4.2 可访问性）。
 */
import { computed } from 'vue'
import type { AgentRunRecord, TimelineNode } from '@/composables/useAgentRun'

const props = defineProps<{
  record: AgentRunRecord
  /** 页面级 1s ticker 的当前时刻（进行中节点实时耗时用，父级单一定时器驱动） */
  now: number
}>()

interface StepGroup {
  step: TimelineNode | null
  tools: TimelineNode[]
}

/** 节点扁平序 → 分组（submitted/step 作组头，工具归入其前最近的组头） */
const groups = computed<StepGroup[]>(() => {
  const out: StepGroup[] = []
  let current: StepGroup = { step: null, tools: [] }
  for (const node of props.record.nodes) {
    if (node.kind === 'tool') {
      current.tools.push(node)
      continue
    }
    if (current.step !== null || current.tools.length > 0) out.push(current)
    current = { step: node, tools: [] }
  }
  if (current.step !== null || current.tools.length > 0) out.push(current)
  return out
})

function secondsBetween(startedAt: number | null, endedAt: number | null): number {
  if (startedAt === null) return 0
  const end = endedAt ?? props.now
  return Math.max(0, Math.round((end - startedAt) / 1000))
}

function statusText(node: TimelineNode): string {
  switch (node.status) {
    case 'running':
      return `进行中 · 已耗时 ${secondsBetween(node.startedAt, null)} 秒`
    case 'done':
      return `完成 · ${secondsBetween(node.startedAt, node.endedAt)} 秒`
    case 'failed':
      return '失败'
    case 'stopped':
      return '已停止'
  }
}

function timelineType(node: TimelineNode): 'primary' | 'success' | 'danger' | 'warning' {
  switch (node.status) {
    case 'running':
      return 'primary'
    case 'done':
      return 'success'
    case 'failed':
      return 'danger'
    case 'stopped':
      return 'warning'
  }
}

/** ARGS 增量可能是分片 JSON：收齐后尽力格式化，解析失败原样展示（不可信文本只做纯文本渲染） */
function prettyArgs(node: TimelineNode): string {
  try {
    return JSON.stringify(JSON.parse(node.argsText), null, 2)
  } catch {
    return node.argsText
  }
}
</script>

<template>
  <el-timeline class="run-timeline" data-test="timeline">
    <el-timeline-item
      v-for="group in groups"
      :key="group.step?.key ?? group.tools[0]?.key ?? 0"
      :type="group.step !== null ? timelineType(group.step) : 'info'"
    >
      <template v-if="group.step !== null">
        <div class="node-line">
          <span v-if="group.step.status === 'running'" class="pulse-dot" aria-hidden="true" />
          <span class="node-title">{{ group.step.label }}</span>
          <span class="node-status">{{ statusText(group.step) }}</span>
        </div>
        <div v-for="tool in group.tools" :key="tool.key" class="tool-node">
          <div class="node-line">
            <span v-if="tool.status === 'running'" class="pulse-dot" aria-hidden="true" />
            <span class="node-title">{{ tool.label }}</span>
            <span v-if="tool.status === 'done'" class="node-done-mark" aria-hidden="true">✓</span>
            <span class="node-status">{{ statusText(tool) }}</span>
          </div>
          <details v-if="tool.argsText !== ''" class="tool-args">
            <summary>参数</summary>
            <pre>{{ prettyArgs(tool) }}</pre>
          </details>
        </div>
      </template>
    </el-timeline-item>
  </el-timeline>
</template>

<style scoped>
.run-timeline {
  padding-left: 4px;
}
.node-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.node-title {
  font-weight: 500;
}
.node-status {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.node-done-mark {
  color: var(--el-color-success);
  font-size: 12px;
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-primary);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.25;
  }
}
.tool-node {
  margin: 6px 0 2px 14px;
  padding-left: 10px;
  border-left: 2px solid var(--el-border-color-lighter);
}
.tool-args {
  margin-top: 2px;
}
.tool-args summary {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
}
.tool-args pre {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow: auto;
}
</style>
