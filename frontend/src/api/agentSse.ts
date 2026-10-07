/**
 * AG-UI agent 任务流消费（docs/api.md §5.1，agent-evolution #08）。
 *
 * 依赖选型（工单要求"优先官方 @ag-ui/client，集成摩擦大允许自研并写明理由"）：
 * 经核对官方包 v1.0.2（npmmirror 拉取 dist 类型定义），HttpAgent 与本仓后端有三处
 * 硬性错位——① HttpAgentConfig 仅 {url, headers?, fetch?}，无 credentials 项，而本仓
 * 鉴权是 httpOnly Cookie（须 credentials:'include'）；② 请求体由内部 RunAgentInput
 * 组装（threadId/messages/tools/forwardedProps），与契约的 {taskType, input} 不同形，
 * 只能靠自定义 fetch 改写请求体绕过——等于替换掉该客户端存在的意义（除编解码外的
 * 全部传输语义）；③ 拖入 rxjs/zod/@ag-ui/proto 等一串重依赖。故按工单许可退回自研：
 * 本模块照搬 chat.ts streamChat 的鉴权、中止与开流前错误处理（toEnvelopeError 为
 * chat.ts 同款副本，不改既有模块导出面），帧解析复用 parseSseFrames（跨 chunk 行为
 * 已在 sse.spec.ts 直测）。线格式为本仓 SSE 惯例（`event:` 行携带 AG-UI EventType），
 * 载荷键名随 AG-UI 官方 snake_case（thread_id/tool_call_id/delta——协议方言，改名即
 * 破坏互操作，不套本仓 camelCase 惯例）。
 */
import {
  ABORTED_CODE,
  ABORTED_MESSAGE,
  ApiClientError,
  BAD_RESPONSE_CODE,
  FALLBACK_MESSAGE,
  NETWORK_ERROR_CODE,
  NETWORK_ERROR_MESSAGE,
  UNAUTHORIZED_CODE,
  notifyUnauthorized,
} from './client'
import { parseSseFrames } from './sse'

/** AG-UI 线事件：event 为 AG-UI EventType，其余键随协议 snake_case 原样保留 */
export type AgentWireEvent = { event: string } & Record<string, unknown>

export type AgentEventHandler = (event: AgentWireEvent) => void

export interface AgentRunPayload {
  /** 任务类型白名单：kb_qa / template_draft（api.md §5.1，未知 422/1003） */
  taskType: string
  /** 任务输入，strip 后 1-2000 字（§21.5） */
  input: string
}

/** 开流前错误中由前端判可否重试的码：参数非法(1003)/每日限额(3002)，重试无意义 */
export const AGENT_NON_RETRYABLE_PRESTREAM_CODES = new Set([1003, 3002])

async function toEnvelopeError(res: Response): Promise<ApiClientError> {
  try {
    const payload = (await res.json()) as { code?: unknown; message?: unknown }
    if (payload !== null && typeof payload === 'object' && typeof payload.code === 'number') {
      const message = typeof payload.message === 'string' ? payload.message : FALLBACK_MESSAGE
      return new ApiClientError(payload.code, message || FALLBACK_MESSAGE)
    }
  } catch {
    // 非 JSON 错误体（网关错误页等），按系统兜底码呈现
  }
  return new ApiClientError(BAD_RESPONSE_CODE, FALLBACK_MESSAGE)
}

/**
 * 消费 agent 任务流：逐 AG-UI 事件回调（事件序 RUN_STARTED → … → RUN_FINISHED，
 * 失败以 RUN_ERROR 收尾，api.md §5.1）。网络层失败 / 开流前业务错误抛
 * ApiClientError（带 code），调用方据此进失败态；流中错误经 RUN_ERROR 事件下发，
 * 不走异常路径、不触发登出。
 */
export async function streamAgentRun(
  payload: AgentRunPayload,
  onEvent: AgentEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch('/api/agent/runs', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE)
    }
    throw new ApiClientError(NETWORK_ERROR_CODE, NETWORK_ERROR_MESSAGE)
  }

  const contentType = res.headers.get('content-type') ?? ''
  if (!res.ok || !contentType.includes('text/event-stream')) {
    const error = await toEnvelopeError(res)
    // 开流前会话失效（401/2003）走与 request 相同的全局通道（chat stream 同款）；
    // 流中 RUN_ERROR 经 dispatch 下发，不经此路径、不触发登出
    if (res.status === 401 || error.code === UNAUTHORIZED_CODE) notifyUnauthorized()
    throw error
  }

  const reader = res.body?.getReader()
  if (!reader) throw new ApiClientError(BAD_RESPONSE_CODE, FALLBACK_MESSAGE)

  // 坏帧容错（T8 评审清单②）：心跳/脏 data 非 JSON 时跳过该帧，不中止整条流
  const dispatch = (frame: { event: string; data: string }): void => {
    try {
      onEvent({ ...JSON.parse(frame.data), event: frame.event } as AgentWireEvent)
    } catch {
      // 非 JSON data 帧丢弃
    }
  }

  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (value !== undefined) buffer += decoder.decode(value, { stream: true })
      const { frames, rest } = parseSseFrames(buffer)
      buffer = rest
      frames.forEach(dispatch)
      if (done) break
    }
  } catch (error) {
    // 中途断流（T8 评审清单①）：断网/代理重置映射网络错误码（可重试）；用户 abort 单独成码
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE)
    }
    throw new ApiClientError(NETWORK_ERROR_CODE, NETWORK_ERROR_MESSAGE)
  }
  // 收尾残尾：服务端若未以空行收尾也尽力解析最后一个事件
  buffer += decoder.decode()
  if (buffer.trim() !== '') parseSseFrames(`${buffer}\n\n`).frames.forEach(dispatch)
}
