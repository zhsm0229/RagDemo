/**
 * P10 运营概览集成测试：挂载即拉概览（days=1）与健康度与知识缺口周榜（F-1010）、
 * 统计卡四张（提问数/命中率/拒答率/点踩率，比率 0-1 → 百分比 1 位小数）、days 切换
 * （近1天/近7天，api.md §4.5.1 仅两档）、byDept 表格（部门中文/tokens）、知识缺口
 * TOP 卡片（top10 表格/周切换 0/-1/-2/导出 CSV blob/空态引导/加载失败内联错误+重试
 * ——W5 终审 M-1）、健康度面板（就绪率
 * 进度条/待确认数/零召回榜/待过期清单，行点击跳转 P3 文档管理）、api 层契约钉死。
 */
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { exportKnowledgeGap, getKnowledgeGap } from '@/api/knowledgeGap'
import { getHealth, getOverview } from '@/api/overview'
import OverviewView from '@/views/OverviewView.vue'

const { pushSpy } = vi.hoisted(() => ({ pushSpy: vi.fn() }))
// 页面仅用 useRouter 做跳转，mock 掉避免装配全量路由
vi.mock('vue-router', () => ({ useRouter: () => ({ push: pushSpy }) }))

const fetchMock = vi.fn()

const T0 = '2026-09-28T10:00:00+08:00'

/** 相对当天偏移 N 天的 ISO 日期（YYYY-MM-DD）：待过期/已到期 fixture 用，
 * 硬编码日期跨月后断言失效（2026-10-02 事故：'2026-10-02' 写死于 9 月用例）。 */
function isoDaysFromToday(offset: number): string {
  const d = new Date()
  d.setDate(d.getDate() + offset)
  return d.toISOString().slice(0, 10)
}

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
    { id: 6, title: '客服话术 v1', expiryDate: isoDaysFromToday(3) },
    { id: 7, title: '已到期未下线文档', expiryDate: isoDaysFromToday(-7) },
  ],
}

/** 服务健康 mock（ServicesHealthPanel 挂载即拉）；日期动态生成消时间脆弱 */
const servicesHealthData = {
  overall: 'up',
  services: [
    { key: 'pg', label: 'PostgreSQL', status: 'up', latencyMs: 2, detail: '' },
    {
      key: 'embed',
      label: '向量化 (TEI)',
      status: 'up',
      latencyMs: 5,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'rerank',
      label: '重排 (TEI)',
      status: 'up',
      latencyMs: 5,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'llm',
      label: '大模型 (LLM)',
      status: 'up',
      latencyMs: 8,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'mineru',
      label: '文档解析 (MinerU)',
      status: 'not_configured',
      latencyMs: 0,
      detail: '未配置',
    },
  ],
}

/** 12 个去重拒答问题：卡片只展示 top10（total=12 触发"还有更多"提示） */
const gapData = {
  items: [
    { question: '缺口问题0', count: 30 },
    { question: '缺口问题1', count: 20 },
    { question: '缺口问题2', count: 10 },
    ...Array.from({ length: 9 }, (_, i) => ({ question: `缺口问题${i + 3}`, count: 1 })),
  ],
  total: 12,
  weekStart: '2026-09-28T00:00:00+00:00',
  weekEnd: '2026-10-05T00:00:00+00:00',
}

function okEnvelope(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ code: 0, message: 'ok', data }),
  } as unknown as Response
}

function errEnvelope(status: number, message: string): Response {
  return {
    ok: false,
    status,
    headers: { get: () => 'application/json' },
    json: async () => ({ code: 1003, message, data: null }),
  } as unknown as Response
}

function csvResponse(headers: Record<string, string>): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    blob: async () => new Blob(['问题,出现次数\n'], { type: 'text/csv' }),
    json: async () => {
      throw new Error('no json body')
    },
  } as unknown as Response
}

function routeFetch(url: string): Response {
  if (url === '/api/admin/overview?days=1') return okEnvelope(overviewData)
  if (url === '/api/admin/overview?days=7') return okEnvelope(overviewData)
  if (url === '/api/admin/health') return okEnvelope(healthData)
  if (url === '/api/admin/services/health') return okEnvelope(servicesHealthData)
  if (url === '/api/admin/knowledge-gap?weekOffset=0') return okEnvelope(gapData)
  if (url === '/api/admin/knowledge-gap?weekOffset=-1') return okEnvelope(gapData)
  if (url === '/api/admin/knowledge-gap?weekOffset=-2') return okEnvelope(gapData)
  if (url.startsWith('/api/admin/knowledge-gap/export')) {
    return csvResponse({
      'content-disposition': 'attachment; filename="knowledge-gap-20260928.csv"',
      'x-truncated': 'false',
    })
  }
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

const origCreateUrl = URL.createObjectURL
const origRevokeUrl = URL.revokeObjectURL

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  fetchMock.mockImplementation(routeFetch)
  pushSpy.mockClear()
  URL.createObjectURL = vi.fn(() => 'blob:mock-url')
  URL.revokeObjectURL = vi.fn()
  // a[download].click() 在 jsdom 触发 navigation 报错噪音，静默并保留断言能力
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})

afterEach(() => {
  while (mounted.length > 0) mounted.pop()?.unmount()
  URL.createObjectURL = origCreateUrl
  URL.revokeObjectURL = origRevokeUrl
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

/** 轮询等待下拉等 teleport 内容渲染（同 helpers/users-mock.waitFor 口径） */
async function waitFor(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 4000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitFor timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

async function mountOverview(): Promise<VueWrapper> {
  const wrapper = mount(OverviewView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

describe('统计卡与 days 切换', () => {
  it('挂载即拉 overview?days=1 / health / 知识缺口周榜 / 服务健康四接口；四卡渲染提问数与百分比（1 位小数）', async () => {
    const wrapper = await mountOverview()
    // 缺口卡/服务健康卡为子组件：Vue 先挂子组件再走父 onMounted，故其取数最先发起
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual([
      '/api/admin/knowledge-gap?weekOffset=0',
      '/api/admin/services/health',
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

describe('知识缺口卡片（F-1010）', () => {
  it('top10 表格渲染问题/出现次数（count 降序前 10）；total 超出 10 提示导出可看全部', async () => {
    const wrapper = await mountOverview()
    const panel = wrapper.find('[data-test="gap-panel"]')
    expect(panel.find('h4').text()).toContain('知识缺口 TOP')
    const rows = panel.findAll('.el-table__row')
    expect(rows).toHaveLength(10) // 端点取前 50，卡片只展示 top10
    expect(rows[0]!.text()).toContain('缺口问题0')
    expect(rows[0]!.text()).toContain('30')
    expect(rows[9]!.text()).toContain('缺口问题9')
    expect(panel.text()).toContain('共 12 个去重问题')
  })

  it('周切换下拉：选上周 → GET weekOffset=-1，卡片标题随切换更新', async () => {
    const wrapper = await mountOverview()
    const panel = wrapper.find('[data-test="gap-panel"]')
    await panel.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('上周'),
    )!
    await new DOMWrapper(option).trigger('click')
    await flushPromises()
    expect(
      fetchMock.mock.calls.some((c) => String(c[0]) === '/api/admin/knowledge-gap?weekOffset=-1'),
    ).toBe(true)
    expect(wrapper.find('[data-test="gap-panel"]').text()).toContain('上周')
  })

  it('导出 CSV：fetch blob → a[download] 触发，文件名取 Content-Disposition；未截断无提示', async () => {
    const wrapper = await mountOverview()
    await wrapper.find('[data-test="gap-export-btn"]').trigger('click')
    await flushPromises()
    const exportCall = fetchMock.mock.calls.find((c) => String(c[0]).includes('/export'))!
    expect(String(exportCall[0])).toBe('/api/admin/knowledge-gap/export?weekOffset=0')
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    const anchor = vi.spyOn(HTMLAnchorElement.prototype, 'click').mock
      .instances[0] as HTMLAnchorElement
    expect(anchor.href).toBe('blob:mock-url')
    expect(anchor.download).toBe('knowledge-gap-20260928.csv')
    expect(wrapper.find('[data-test="gap-export-note"]').exists()).toBe(false)
  })

  it('空态引导：窗口内无拒答时展示"暂无拒答——运行评测或等待用户提问"', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).startsWith('/api/admin/knowledge-gap?')) {
        return okEnvelope({ ...gapData, items: [], total: 0 })
      }
      return routeFetch(String(url))
    })
    const wrapper = await mountOverview()
    const panel = wrapper.find('[data-test="gap-panel"]')
    expect(panel.text()).toContain('暂无拒答——运行评测或等待用户提问')
    expect(panel.findAll('.el-table__row')).toHaveLength(0)
  })

  it('加载失败（W5 终审 M-1）：内联错误 + 重试，不出"暂无拒答"空态；重试成功后表格恢复', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).startsWith('/api/admin/knowledge-gap?')) {
        return errEnvelope(500, '知识缺口服务暂不可用')
      }
      return routeFetch(String(url))
    })
    const wrapper = await mountOverview()
    const panel = wrapper.find('[data-test="gap-panel"]')
    expect(wrapper.find('[data-test="gap-error"]').text()).toContain('知识缺口服务暂不可用')
    // 接口故障 ≠ 窗口内无拒答：失败不得出现空态引导（避免误读运营口径）
    expect(panel.text()).not.toContain('暂无拒答')
    expect(panel.findAll('.el-table__row')).toHaveLength(0)
    // 后端恢复后点重试：重拉当前周（weekOffset=0），错误消失、top10 表格恢复
    fetchMock.mockImplementation(routeFetch)
    await wrapper.find('[data-test="gap-retry-btn"]').trigger('click')
    await flushPromises()
    expect(
      fetchMock.mock.calls.filter((c) => String(c[0]).startsWith('/api/admin/knowledge-gap?')),
    ).toHaveLength(2)
    expect(wrapper.find('[data-test="gap-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="gap-panel"]').findAll('.el-table__row')).toHaveLength(10)
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
    expect(expired.text()).toBe(isoDaysFromToday(-7))
    // 零召回/待过期行点击 → 跳转文档管理（P3）
    const rows = wrapper.findAll('.el-table__row')
    await rows[rows.length - 1]!.trigger('click')
    expect(pushSpy).toHaveBeenCalledWith({ name: 'kb-documents' })
  })
})

describe('api 层契约（路径/参数钉死）', () => {
  it('getKnowledgeGap 周偏移拼参；exportKnowledgeGap 走独立 fetch 并解析文件名与截断头', async () => {
    const gapStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      okEnvelope(gapData),
    )
    vi.stubGlobal('fetch', gapStub)
    await getKnowledgeGap(0)
    expect(String(gapStub.mock.calls[0]![0])).toBe('/api/admin/knowledge-gap?weekOffset=0')
    await getKnowledgeGap(-1)
    expect(String(gapStub.mock.calls[1]![0])).toBe('/api/admin/knowledge-gap?weekOffset=-1')
    const exportStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      csvResponse({
        'content-disposition': 'attachment; filename="knowledge-gap-20260928.csv"',
        'x-truncated': 'true',
      }),
    )
    vi.stubGlobal('fetch', exportStub)
    const result = await exportKnowledgeGap(-2)
    expect(String(exportStub.mock.calls[0]![0])).toBe(
      '/api/admin/knowledge-gap/export?weekOffset=-2',
    )
    expect(result.filename).toBe('knowledge-gap-20260928.csv')
    expect(result.truncated).toBe(true)
  })

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
