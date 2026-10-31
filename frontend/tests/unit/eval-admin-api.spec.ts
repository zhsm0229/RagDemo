/**
 * evalAdmin API 层回归测试（评测集自动更新 02/03/05 票前端侧）：
 * 三个非 GET 调用（层级升格 PATCH / 难例忽略 PATCH / 自动配置 PUT）曾因调用点预先
 * JSON.stringify、request() 封装内部再 stringify 一次 → 后端收到 JSON 字符串字面量
 * 而非对象 → 422/1003「请求参数错误」。回归断言：fetch 收到的 body 必须是单次
 * 序列化的 JSON 字符串（与 client.spec.ts 同款 mock fetch 模式）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), success: vi.fn() },
}))

import { ignoreHardcaseSeed, patchEvalQuestionTier, putEvalAutoConfig } from '@/api/evalAdmin'

const fetchMock = vi.fn()

function envelopeResponse(payload: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => payload } as unknown as Response
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: {} }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('evalAdmin 非常规方法调用 body 单次序列化（1003 回归）', () => {
  it('patchEvalQuestionTier：PATCH body 恰为一次 JSON 序列化', async () => {
    await patchEvalQuestionTier(5, 'official')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/admin/eval-questions/5/tier')
    expect(init.method).toBe('PATCH')
    expect(init.body).toBe(JSON.stringify({ tier: 'official' }))
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
  })

  it('ignoreHardcaseSeed：PATCH body 恰为一次 JSON 序列化', async () => {
    await ignoreHardcaseSeed(9)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/admin/hardcase-seeds/9')
    expect(init.method).toBe('PATCH')
    expect(init.body).toBe(JSON.stringify({ status: 'ignored' }))
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
  })

  it('putEvalAutoConfig：PUT body 恰为一次 JSON 序列化', async () => {
    const config = {
      enabled: true,
      synthPerSeed: 2,
      docSamplePerDept: 20,
      weeklyMaxQuestions: 100,
      regressionPp: 5.0,
    }
    fetchMock.mockResolvedValue(envelopeResponse({ code: 0, message: 'ok', data: config }))
    await putEvalAutoConfig(config)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/admin/eval-auto/config')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(JSON.stringify(config))
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
  })
})
