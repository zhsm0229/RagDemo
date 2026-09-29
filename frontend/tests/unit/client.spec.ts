/**
 * client 单测：统一信封解包、业务错误抛出与 ElMessage 兜底开关、credentials、
 * 网络层失败/中断/非 JSON 兜底（mock fetch，不发真请求）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), success: vi.fn() },
}))

import { ElMessage } from 'element-plus'
import {
  ABORTED_CODE,
  ApiClientError,
  BAD_RESPONSE_CODE,
  NETWORK_ERROR_CODE,
  request,
} from '@/api/client'

const fetchMock = vi.fn()

function envelopeResponse(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as unknown as Response
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('request 统一信封解包', () => {
  it('code=0 时返回 data 字段（不返回整个信封）', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: { userId: 7 } }))
    const data = await request<{ userId: number }>('/auth/me')
    expect(data).toEqual({ userId: 7 })
  })

  it('请求携带 credentials include 且路径以 /api 前缀拼接', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: null }))
    await request('/conversations')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/conversations')
    expect(init.credentials).toBe('include')
  })

  it('POST 时 body 以 JSON 序列化并带 Content-Type', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: null }))
    await request('/auth/login', { method: 'POST', body: { username: 'admin', password: 'x' } })
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ username: 'admin', password: 'x' }))
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
  })

  it('GET 无 body 时不带 Content-Type 与 body', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: null }))
    await request('/documents')
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.body).toBeUndefined()
    expect(init.headers).toBeUndefined()
  })
})

describe('request 业务错误抛出', () => {
  it('code!==0 抛 ApiClientError，code/message/data 逐字段一致', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 2003, message: '未登录', data: null }))
    const err = await request('/auth/me').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiClientError)
    const apiErr = err as ApiClientError
    expect(apiErr.code).toBe(2003)
    expect(apiErr.message).toBe('未登录')
    expect(apiErr.data).toBeNull()
  })

  it('默认（非 silent）经 ElMessage.error 兜底展示服务端 message', async () => {
    fetchMock.mockResolvedValue(
      envelopeResponse({ code: 2001, message: '用户名或密码错误', data: null }),
    )
    await expect(request('/auth/login', { method: 'POST' })).rejects.toMatchObject({ code: 2001 })
    expect(ElMessage.error).toHaveBeenCalledTimes(1)
    expect(ElMessage.error).toHaveBeenCalledWith('用户名或密码错误')
  })

  it('silent=true 抛错但不弹全局提示（页面内联展示场景）', async () => {
    fetchMock.mockResolvedValue(envelopeResponse({ code: 1003, message: '参数错误', data: [] }))
    await expect(request('/documents', { silent: true })).rejects.toMatchObject({ code: 1003 })
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('data 携带字段级错误数组原样透传（422/1003 场景）', async () => {
    const fieldErrors = [{ field: 'question', message: '问题长度须在 1-2000 字之间' }]
    fetchMock.mockResolvedValue(
      envelopeResponse({ code: 1003, message: '请求参数错误', data: fieldErrors }),
    )
    await expect(request('/chat/stream', { method: 'POST', silent: true })).rejects.toMatchObject({
      code: 1003,
      data: fieldErrors,
    })
  })
})

describe('request 网络层与异常响应兜底', () => {
  it('fetch 网络层失败抛 NETWORK_ERROR_CODE 并弹提示', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(request('/conversations')).rejects.toMatchObject({ code: NETWORK_ERROR_CODE })
    expect(ElMessage.error).toHaveBeenCalledTimes(1)
  })

  it('AbortController 中断抛 ABORTED_CODE 且不弹提示（问答流主动停止场景）', async () => {
    const abortError = new DOMException('The operation was aborted.', 'AbortError')
    fetchMock.mockRejectedValue(abortError)
    await expect(request('/chat/stream', { method: 'POST', silent: true })).rejects.toMatchObject({
      code: ABORTED_CODE,
    })
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('响应体非统一信封（JSON 解析失败）抛 BAD_RESPONSE_CODE 兜底', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError('Unexpected token < in JSON')
      },
    } as unknown as Response)
    await expect(request('/conversations')).rejects.toMatchObject({ code: BAD_RESPONSE_CODE })
  })
})
