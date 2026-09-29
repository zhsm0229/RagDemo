/**
 * fetch 封装：同源 /api 前缀、统一响应体解包 {code,message,data}（docs/api.md §1.2）。
 *
 * 约定：
 * - jwt 走 httpOnly Cookie，credentials: 'include' 同源携带；
 * - code !== 0 抛 ApiClientError(code, message, data)，默认 ElMessage.error 兜底，
 *   页面要内联展示错误（如登录页 §22.2）时传 silent: true 关掉；
 * - SSE 与文件下载不走本封装（text/event-stream / 字节流无统一信封）。
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

const NETWORK_ERROR_MESSAGE = '网络异常，请检查网络后重试'
const ABORTED_MESSAGE = '请求已取消'
const FALLBACK_MESSAGE = '系统繁忙，请稍后重试'

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

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** JSON 请求体；undefined 时不带 Content-Type 与 body */
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

  let res: Response
  try {
    res = await fetch(`${API_PREFIX}${path}`, {
      method,
      credentials: 'include',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
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
