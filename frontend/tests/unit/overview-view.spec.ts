/**
 * P10 运营概览集成测试：挂载即拉概览（days=1）与健康度、统计卡四张（提问数/
 * 命中率/拒答率/点踩率，比率 0-1 → 百分比 1 位小数）、days 切换（近1天/近7天，
 * api.md §4.5.1 仅两档）、byDept 表格（部门中文/tokens）、健康度面板（就绪率
 * 进度条/待确认数/零召回榜/待过期清单，行点击跳转 P3 文档管理）、api 层契约钉死。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getHealth, getOverview } from '@/api/overview'
import OverviewView from '@/views/OverviewView.vue'

const { pushSpy } = vi.hoisted(() => ({ pushSpy: vi.fn() }))
// 页面仅用 useRouter 做跳转，mock 掉避免装配全量路由
vi.mock('vue-router', () => ({ useRouter: () => ({ push: pushSpy }) }))

const fetchMock = vi.fn()

const T0 = '2026-09-28T10:00:00+08:00'

const overviewData = {
  totals: { questions: 128, hitRate: 0.875, refuseRate: 0.09, downRate: 0.04, tokens: 2150000 },
  byDept: [
    {
      deptCode: 'service',
      questions: 100,
      hitRate: 0.9,
      refuseRate: 0.07,
      downRate: 0.03,
      tokens: 1200000,
    },
    {
      deptCode: 'all',
      questions: 28,
      hitRate: 0.79,
      refuseRate: 0.15,
      downRate: 0.07,
      tokens: 950000,
    },
  ],
}

const healthData = {
  totalDocs: 20,
  readyCount: 17,
  readyRate: 0.85,
  pendingConfirm: 3,
  zeroRecall: [{ id: 5, title: '旧版报销制度', deptCode: 'hr', createdAt: T0 }],
  expiringSoon: [
    { id: 6, title: '客服话术 v1', expiryDate: '2026-10-02' },
    { id: 7, title: '已到期未下线文档', expiryDate: '2026-09-01' },
  ],
}

function okEnvelope(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ code: 0, message: 'ok', data }),
  } as unknown as Response
}

function routeFetch(url: string): Response {
  if (url === '/api/admin/overview?days=1') return okEnvelope(overviewData)
  if (url === '/api/admin/overview?days=7') return okEnvelope(overviewData)
  if (url === '/api/admin/health') return okEnvelope(healthData)
  throw new Error(`unexpected fetch: ${url}`)
}

const mounted: VueWrapper[] = []

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table × el-statistic 的挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  fetchMock.mockImplementation(routeFetch)
  pushSpy.mockClear()
})

afterEach(() => {
  while (mounted.length > 0) mounted.pop()?.unmount()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

async function mountOverview(): Promise<VueWrapper> {
  const wrapper = mount(OverviewView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

describe('统计卡与 days 切换', () => {
  it('挂载即拉 overview?days=1 与 health；四卡渲染提问数与百分比（1 位小数）', async () => {
    const wrapper = await mountOverview()
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual([
      '/api/admin/overview?days=1',
      '/api/admin/health',
    ])
    const text = wrapper.text()
    expect(text).toContain('今日提问')
    expect(text).toContain('128')
    expect(text).toContain('命中率（非拒答）')
    expect(text).toContain('87.5')
    expect(text).toContain('9.0')
    expect(text).toContain('4.0')
  })

  it('切换近7天：GET days=7，提问卡标题随窗口切换为"近7天提问"', async () => {
    const wrapper = await mountOverview()
    const buttons = wrapper.findAll('.el-radio-button')
    expect(buttons).toHaveLength(2)
    await buttons[1]!.find('input')!.setValue(7)
    await flushPromises()
    expect(fetchMock.mock.calls.some((c) => String(c[0]) === '/api/admin/overview?days=7')).toBe(
      true,
    )
    expect(wrapper.text()).toContain('近7天提问')
    expect(wrapper.text()).not.toContain('今日提问')
  })
})

describe('按部门表格', () => {
  it('byDept 行渲染：部门中文/提问数/比率百分比/token 千分位', async () => {
    const wrapper = await mountOverview()
    const rows = wrapper.findAll('.el-table__row')
    expect(rows.length).toBeGreaterThanOrEqual(2)
    expect(rows[0]!.text()).toContain('客服')
    expect(rows[0]!.text()).toContain('100')
    expect(rows[0]!.text()).toContain('90.0%')
    expect(rows[0]!.text()).toContain('1,200,000')
    expect(rows[1]!.text()).toContain('全员')
    expect(rows[1]!.text()).toContain('950,000')
  })
})

describe('语料健康度面板', () => {
  it('就绪率进度条/待确认数/零召回榜/待过期清单渲染；待过期行点击跳转 P3 文档管理', async () => {
    const wrapper = await mountOverview()
    const progress = wrapper.findComponent({ name: 'ElProgress' })
    expect(progress.props('percentage')).toBe(85)
    expect(wrapper.text()).toContain('待确认文档：')
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).toContain('旧版报销制度')
    expect(wrapper.text()).toContain('客服话术 v1')
    // 已到期未下线：到期日早于今天，标红告警
    const expired = wrapper.find('.expired')
    expect(expired.exists()).toBe(true)
    expect(expired.text()).toBe('2026-09-01')
    // 零召回/待过期行点击 → 跳转文档管理（P3）
    const rows = wrapper.findAll('.el-table__row')
    await rows[rows.length - 1]!.trigger('click')
    expect(pushSpy).toHaveBeenCalledWith({ name: 'kb-documents' })
  })
})

describe('api 层契约（路径/参数钉死）', () => {
  it('getOverview 仅 1|7 两档拼参；getHealth 固定路径', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      okEnvelope(overviewData),
    )
    vi.stubGlobal('fetch', fetchStub)
    await getOverview(1)
    expect(String(fetchStub.mock.calls[0]![0])).toBe('/api/admin/overview?days=1')
    await getOverview(7)
    expect(String(fetchStub.mock.calls[1]![0])).toBe('/api/admin/overview?days=7')
    const healthStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      okEnvelope(healthData),
    )
    vi.stubGlobal('fetch', healthStub)
    await getHealth()
    expect(String(healthStub.mock.calls[0]![0])).toBe('/api/admin/health')
  })
})
