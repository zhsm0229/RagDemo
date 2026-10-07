/**
 * 模板生成运行状态机（agent-ui-design §3.2-§3.6，agent-evolution #08）。
 *
 * - AG-UI 事件按到达顺序增量应用（applyEvent 模式对齐 useChatStream）：STEP_* 与 TOOL_CALL_*
 *   驱动时间线节点，TEXT_MESSAGE_* 驱动草稿卡增量出字；未知事件名忽略不判错（T8 口径，
 *   心跳/未来扩展天然兼容）。事件应用按组拆分（RUN/STEP/TOOL/TEXT 各一函数）。
 * - 提交后先挂 0 号伪节点"已提交生成任务"（RUN_STARTED 到达前防开流窗口全白）。
 * - 重试始终是新运行（§3.5）：旧运行记录折叠保留可查（runs 尾部即当前/最近一次）。
 * - 看门狗（§3.6，M25 回灌沉淀）：收到任意事件喂狗；45s（3×15s 心跳周期）无事件判
 *   "连接停滞"，300s 总时长兜底判"超时"，两者走 abort 进失败态（retryable=true），
 *   文案区分于一般失败。计时器与 AbortController 同生命周期，终态即拆。
 * - 停止语义对齐 useChatStream：abort → stopped 态可重试；组件卸载同样中止本地消费
 *   （服务端断连兜底置 failed、不留幽灵运行，已落库草稿不受影响——api.md §5.1）。
 */
import { computed, onUnmounted, reactive, ref } from 'vue'
import { AGENT_NON_RETRYABLE_PRESTREAM_CODES, streamAgentRun } from '@/api/agentSse'
import { ABORTED_CODE, ApiClientError } from '@/api/client'

/** 连接停滞阈值：3 个心跳周期（服务端心跳间隔 15s）无任何事件即判停 */
export const STALL_TIMEOUT_MS = 45_000
/** 单次运行总时长兜底（对齐解析超时 300s 口径），到点强判失败 */
export const TOTAL_TIMEOUT_MS = 300_000

export type RunPhase = 'running' | 'succeeded' | 'failed' | 'stopped'
export type NodeStatus = 'running' | 'done' | 'failed' | 'stopped'

export interface TimelineNode {
  key: number
  kind: 'submitted' | 'step' | 'tool'
  label: string
  status: NodeStatus
  /** 节点开始时刻（STEP_STARTED / TOOL_CALL_START 到达时），耗时计算起点 */
  startedAt: number | null
  endedAt: number | null
  /** step 节点：AG-UI step_name（step-N），STEP_FINISHED 收口匹配用 */
  stepName: string | null
  /** tool 节点：tool_call_id，ARGS 增量与 END 收口匹配用 */
  toolCallId: string | null
  /** tool 节点：TOOL_CALL_ARGS delta 拼接的参数 JSON 文本（折叠等宽展示） */
  argsText: string
}

export interface DraftCard {
  key: number
  messageId: string
  content: string
  status: 'streaming' | 'done'
}

export interface AgentRunRecord {
  key: number
  phase: RunPhase
  /** RUN_STARTED 下发的 run_id（库内 agent_runs 行 id，审核回放依据） */
  runId: number | null
  nodes: TimelineNode[]
  drafts: DraftCard[]
  /** 提交时刻（含开流窗口）；durationMs 在终态收口 */
  startedAt: number
  durationMs: number | null
  /** failed 态的脱敏失败原因（后端已脱敏，前端原样呈现） */
  failure: string | null
  retryable: boolean
}

export interface AgentRunHooks {
  /** 每个流事件应用后回调（页面用于草稿区滚动跟随） */
  onUpdate?: () => void
  /** 单次运行终态收口后回调 */
  onSettled?: (record: AgentRunRecord) => void
}

type WireEvent = { event: string } & Record<string, unknown>

function nextKey(): () => number {
  let seq = 0
  return () => ++seq
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** AG-UI step_name（step-N）→ 中文节点名；非该形态的名称原样透传（未来扩展） */
function stepLabel(stepName: string): string {
  const match = /^step-(\d+)$/.exec(stepName)
  return match !== null ? `步骤 ${match[1]}` : `步骤：${stepName}`
}

function lastIndexNode(
  nodes: TimelineNode[],
  predicate: (node: TimelineNode) => boolean,
): TimelineNode | null {
  for (let i = nodes.length - 1; i >= 0; i--) {
    if (predicate(nodes[i])) return nodes[i]
  }
  return null
}

function lastDraft(drafts: DraftCard[], messageId: string): DraftCard | null {
  for (let i = drafts.length - 1; i >= 0; i--) {
    if (drafts[i].messageId === messageId) return drafts[i]
  }
  return null
}

// ---- AG-UI 事件组应用器（按组分发，applyEvent 统一入口；组内未匹配即忽略） ----

/** RUN_STARTED / RUN_FINISHED / RUN_ERROR：运行级状态（不占时间线节点，§3.2） */
function applyRunEvent(record: AgentRunRecord, event: WireEvent, now: number): boolean {
  if (event.event === 'RUN_STARTED') {
    const id = Number.parseInt(str(event.run_id), 10)
    record.runId = Number.isNaN(id) ? null : id
    // 0 号伪节点收口（RUN_STARTED 到达即开流成功）
    const submitted = lastIndexNode(
      record.nodes,
      (n) => n.kind === 'submitted' && n.status === 'running',
    )
    if (submitted !== null) {
      submitted.status = 'done'
      submitted.endedAt = now
    }
    return true
  }
  if (event.event === 'RUN_FINISHED') {
    record.phase = 'succeeded'
    closeRunning(record, 'done', now)
    return true
  }
  if (event.event !== 'RUN_ERROR') return false
  record.phase = 'failed'
  record.failure = str(event.message) || '生成失败，可重试'
  record.retryable = true // 流中错误（5001/5000）与 chat 同口径可重试
  closeRunning(record, 'failed', now)
  return true
}

/** STEP_STARTED / STEP_FINISHED：一级时间线节点（进行中挂起、收口计时） */
function applyStepEvent(
  record: AgentRunRecord,
  event: WireEvent,
  now: number,
  keyOf: () => number,
): boolean {
  if (event.event === 'STEP_STARTED') {
    const stepName = str(event.step_name)
    record.nodes.push({
      key: keyOf(),
      kind: 'step',
      label: stepName === '' ? '步骤' : stepLabel(stepName),
      status: 'running',
      startedAt: now,
      endedAt: null,
      stepName,
      toolCallId: null,
      argsText: '',
    })
    return true
  }
  if (event.event !== 'STEP_FINISHED') return false
  const stepName = str(event.step_name)
  const step = lastIndexNode(
    record.nodes,
    (n) => n.kind === 'step' && n.stepName === stepName && n.status === 'running',
  )
  if (step !== null) {
    step.status = 'done'
    step.endedAt = now
  }
  // 收步时防御性收口仍挂在该步下的工具节点（异常路径兜底，不留假运行态）
  for (const node of record.nodes) {
    if (node.kind === 'tool' && node.stepName === stepName && node.status === 'running') {
      node.status = 'done'
      node.endedAt = now
    }
  }
  return true
}

/** TOOL_CALL_START/ARGS/END：二级工具节点（ARGS 增量拼参数文本，END 打勾收口） */
function applyToolEvent(
  record: AgentRunRecord,
  event: WireEvent,
  now: number,
  keyOf: () => number,
): boolean {
  if (event.event === 'TOOL_CALL_START') {
    // 工具挂到最近一个 step 节点下（后端时序保证 STEP_STARTED → TOOL_CALL_* → STEP_FINISHED 串行）
    const parent = lastIndexNode(record.nodes, (n) => n.kind === 'step')
    record.nodes.push({
      key: keyOf(),
      kind: 'tool',
      label: `工具：${str(event.tool_call_name) || '未知工具'}`,
      status: 'running',
      startedAt: now,
      endedAt: null,
      stepName: parent?.stepName ?? null,
      toolCallId: str(event.tool_call_id),
      argsText: '',
    })
    return true
  }
  if (event.event === 'TOOL_CALL_ARGS') {
    const id = str(event.tool_call_id)
    const node = lastIndexNode(record.nodes, (n) => n.kind === 'tool' && n.toolCallId === id)
    if (node !== null) node.argsText += str(event.delta)
    return true
  }
  if (event.event !== 'TOOL_CALL_END') return false
  const id = str(event.tool_call_id)
  const node = lastIndexNode(
    record.nodes,
    (n) => n.kind === 'tool' && n.toolCallId === id && n.status === 'running',
  )
  if (node !== null) {
    node.status = 'done'
    node.endedAt = now
  }
  return true
}

/** TEXT_MESSAGE_START/CONTENT/END：右侧草稿卡（START 建卡 / CONTENT 增量出字 / END 定稿） */
function applyDraftEvent(record: AgentRunRecord, event: WireEvent, keyOf: () => number): boolean {
  if (event.event === 'TEXT_MESSAGE_START') {
    record.drafts.push({
      key: keyOf(),
      messageId: str(event.message_id),
      content: '',
      status: 'streaming',
    })
    return true
  }
  if (event.event === 'TEXT_MESSAGE_CONTENT') {
    const draft = lastDraft(record.drafts, str(event.message_id))
    if (draft !== null) draft.content += str(event.delta)
    return true
  }
  if (event.event !== 'TEXT_MESSAGE_END') return false
  const draft = lastDraft(record.drafts, str(event.message_id))
  if (draft !== null) draft.status = 'done'
  return true
}

/** 事件应用统一入口：未知事件名（心跳/未来扩展）忽略，不判 error（T8 评审清单②） */
function applyEvent(
  record: AgentRunRecord,
  event: WireEvent,
  now: number,
  keyOf: () => number,
): void {
  if (applyRunEvent(record, event, now)) return
  if (applyStepEvent(record, event, now, keyOf)) return
  if (applyToolEvent(record, event, now, keyOf)) return
  applyDraftEvent(record, event, keyOf)
}

/** 新运行记录：仅含 0 号"已提交生成任务"伪节点（RUN_STARTED 前防开流窗口全白） */
function newRunRecord(keyOf: () => number): AgentRunRecord {
  return {
    key: keyOf(),
    phase: 'running',
    runId: null,
    nodes: [
      {
        key: keyOf(),
        kind: 'submitted',
        label: '已提交生成任务',
        status: 'running',
        startedAt: Date.now(),
        endedAt: null,
        stepName: null,
        toolCallId: null,
        argsText: '',
      },
    ],
    drafts: [],
    startedAt: Date.now(),
    durationMs: null,
    failure: null,
    retryable: false,
  }
}

/** 终态收口：仍在 running 的节点与 streaming 草稿统一置为给定终态（终态事件/abort/断流共用） */
function closeRunning(
  record: AgentRunRecord,
  status: Exclude<NodeStatus, 'running'>,
  now: number,
): void {
  for (const node of record.nodes) {
    if (node.status === 'running') {
      node.status = status
      node.endedAt = now
    }
  }
  for (const draft of record.drafts) {
    if (draft.status === 'streaming') draft.status = 'done'
  }
}

/** 异常收口：看门狗判停（停滞/超时自带文案）> 用户停止 > 业务/网络错误 > 系统兜底 */
function settleFromError(
  record: AgentRunRecord,
  error: unknown,
  stallReason: string | null,
  now: number,
): void {
  if (stallReason !== null) {
    // 看门狗判停：文案自带停滞/超时区分，均为可重试失败态
    record.phase = 'failed'
    record.failure = stallReason
    record.retryable = true
  } else if (error instanceof ApiClientError && error.code === ABORTED_CODE) {
    record.phase = 'stopped' // 用户主动停止：节点标"已停止"，已完成草稿保留，可重试
    record.retryable = true
  } else if (error instanceof ApiClientError) {
    record.phase = 'failed'
    record.failure = error.message
    // 开流前业务性拒绝（参数 1003/限额 3002）重试无意义，其余（网络/系统）可重试
    record.retryable = !AGENT_NON_RETRYABLE_PRESTREAM_CODES.has(error.code)
  } else {
    record.phase = 'failed'
    record.failure = '系统繁忙，请稍后重试'
    record.retryable = false
  }
  closeRunning(record, record.phase === 'stopped' ? 'stopped' : 'failed', now)
}

export function useAgentRun(hooks: AgentRunHooks = {}) {
  const running = ref(false)
  const runs = reactive<AgentRunRecord[]>([])
  const activeRun = computed<AgentRunRecord | null>(() => runs[runs.length - 1] ?? null)

  const keyOf = nextKey()
  let controller: AbortController | null = null
  let stallTimer: number | null = null
  let totalTimer: number | null = null
  /** 看门狗判停原因（非 null 时 abort 的收口文案取它，区别于用户主动停止） */
  let stallReason: string | null = null
  /** 最近一次运行参数（重试/再生成一批 = 同参数新运行，不做断点续跑） */
  let lastTask: { taskType: string; input: string } | null = null

  function clearTimers(): void {
    if (stallTimer !== null) window.clearTimeout(stallTimer)
    if (totalTimer !== null) window.clearTimeout(totalTimer)
    stallTimer = null
    totalTimer = null
  }

  /** 喂狗：任意事件（含心跳、STEP/TOOL 事件本身即活性信号）重置停滞计时 */
  function feedDog(): void {
    if (stallTimer !== null) window.clearTimeout(stallTimer)
    stallTimer = window.setTimeout(() => {
      stallReason = '生成长时间无响应，已停止等待'
      controller?.abort()
    }, STALL_TIMEOUT_MS)
  }

  function stop(): void {
    controller?.abort()
  }

  /** 发起一次运行：新记录（旧运行折叠保留），逐事件增量应用 */
  async function run(taskType: string, input: string): Promise<void> {
    if (running.value) return
    lastTask = { taskType, input }
    running.value = true
    stallReason = null
    controller = new AbortController()
    // 事件须作用在 reactive 代理上：直接改原始对象不触发依赖更新（useChatStream 同款教训）
    runs.push(newRunRecord(keyOf))
    const record = runs[runs.length - 1]
    feedDog()
    totalTimer = window.setTimeout(() => {
      stallReason = '生成超时（超过 5 分钟），可重试'
      controller?.abort()
    }, TOTAL_TIMEOUT_MS)
    try {
      await streamAgentRun(
        { taskType, input },
        (event) => {
          feedDog()
          applyEvent(record, event, Date.now(), keyOf)
          hooks.onUpdate?.()
        },
        controller.signal,
      )
      // 流正常结束却没收到终态事件（服务端漏发/代理截断）：按失败收口，不留假运行态
      if (record.phase === 'running') {
        record.phase = 'failed'
        record.failure = '连接中断，请重试'
        record.retryable = true
        closeRunning(record, 'failed', Date.now())
      }
    } catch (error) {
      settleFromError(record, error, stallReason, Date.now())
    } finally {
      clearTimers()
      controller = null
      record.durationMs = Date.now() - record.startedAt
      running.value = false
      hooks.onSettled?.(record)
    }
  }

  /** 重试 = 以最近一次参数新发一次运行（旧失败运行折叠保留可查，§3.5） */
  async function retryLast(): Promise<void> {
    if (running.value || lastTask === null) return
    await run(lastTask.taskType, lastTask.input)
  }

  // 组件卸载即中止本地消费：服务端断连兜底置 failed（api.md §5.1），不留后台僵尸流；
  // 已落库草稿（save_draft 各自独立提交）不受影响——离页确认文案据此承诺
  onUnmounted(() => {
    stop()
    clearTimers()
  })

  return { running, runs, activeRun, run, stop, retryLast }
}
