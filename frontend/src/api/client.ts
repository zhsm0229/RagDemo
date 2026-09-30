/**
 * fetch 封装：同源 /api 前缀、统一响应体解包 {code,message,data}（docs/api.md §1.2）。
 *
 * 约定：
 * - jwt 走 httpOnly Cookie，credentials: 'include' 同源携带；
 * - code !== 0 抛 ApiClientError(code, message, data)，默认 ElMessage.error 兜底，
 *   页面要内联展示错误（如登录页 §22.2）时传 silent: true 关掉；
 * - body 传 FormData 时按 multipart 发送（文件上传），不设 Content-Type；
 * - SSE 与文件下载不走本封装（text/event-stream / 字节流无统一信封）；
 * - HTTP 401 或业务码 2003（W4 终审 I 项③）触发全局未授权通道：经装配层注册的
 *   onUnauthorized 回调清登录态并回登录页（本模块不感知路由，避免 client→router 循环依赖）。
 */
import { ElMessage } from 'element-plus'
import type { ApiResponse } from './types'

const API_PREFIX = '/api'

/** 网络层失败（断网/DNS） */
export const NETWORK_ERROR_CODE = -1
/** 用户中断（AbortController） */
export const ABORTED_CODE = -2
/** 响应体非统一信封（网关 502 页面等），按系统兜底码呈现 */
export const BAD_RESPONSE_CODE = 5000
/** 未登录 / 令牌无效或过期 / 账号已禁用（app/core/errors.py ERR_UNAUTHENTICATED） */
export const UNAUTHORIZED_CODE = 2003

export const NETWORK_ERROR_MESSAGE = '网络异常，请检查网络后重试'
export const ABORTED_MESSAGE = '请求已取消'
export const FALLBACK_MESSAGE = '系统繁忙，请稍后重试'

export class ApiClientError extends Error {
  readonly code: number
  readonly data: unknown

  constructor(code: number, message: string, data: unknown = null) {
    super(message)
    this.name = 'ApiClientError'
    this.code = code
    this.data = data
  }
}

/** 会话失效回调：装配层（router/unauthorized.ts）注册，清登录态并按当前路由回登录页 */
export type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/** 装配层注册入口；传 null 注销（测试复位用）。未注册时仅照常抛错 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

/** 会话失效单点通知：request 与 SSE 开流前错误共用（流中 error 事件不经此路径） */
export function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** JSON 或 FormData 请求体；undefined 时不带 Content-Type 与 body */
  body?: unknown
  signal?: AbortSignal
  /** true 时不做 ElMessage.error 兜底，由调用方内联展示 */
  silent?: boolean
}

function isEnvelope(payload: unknown): payload is ApiResponse<unknown> {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    'code' in payload &&
    typeof payload.code === 'number'
  )
}

async function parseEnvelope(res: Response): Promise<ApiResponse<unknown> | null> {
  try {
    const payload: unknown = await res.json()
    return isEnvelope(payload) ? payload : null
  } catch {
    // 非 JSON 响应体（代理错误页等）按 BAD_RESPONSE_CODE 兜底
    return null
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, silent = false } = options

  // FormData（multipart 上传）不设 Content-Type：boundary 由浏览器生成，手工设置会缺 boundary
  const isForm = body instanceof FormData

  let res: Response
  try {
    res = await fetch(`${API_PREFIX}${path}`, {
      method,
      credentials: 'include',
      headers: body !== undefined && !isForm ? { 'Content-Type': 'application/json' } : undefined,
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    // fetch 仅在网络层失败/中断时 reject；HTTP 错误状态仍走下方统一解包
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE)
    }
    if (!silent) ElMessage.error(NETWORK_ERROR_MESSAGE)
    throw new ApiClientError(NETWORK_ERROR_CODE, NETWORK_ERROR_MESSAGE)
  }

  const payload = await parseEnvelope(res)
  // 会话失效（W4 终审 I ③）：HTTP 401（含登录失败 401/2001——是否跳转由装配层按
  // 当前路由裁决）或业务码 2003。silent 请求（登录页/探活）同样触发。
  if (res.status === 401 || payload?.code === UNAUTHORIZED_CODE) notifyUnauthorized()
  if (payload === null) {
    if (!silent) ElMessage.error(FALLBACK_MESSAGE)
    throw new ApiClientError(BAD_RESPONSE_CODE, FALLBACK_MESSAGE)
  }
  if (payload.code !== 0) {
    if (!silent) ElMessage.error(payload.message)
    throw new ApiClientError(payload.code, payload.message, payload.data)
  }
  return payload.data as T
}

export function get<T>(
  path: string,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...options, method: 'GET' })
}

export function post<T>(
  path: string,
  body?: unknown,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...options, method: 'POST', body })
}
