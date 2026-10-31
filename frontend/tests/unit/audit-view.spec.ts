/**
 * P11 审计日志集成测试：挂载拉列表与行渲染（时间/操作人/动作/对象/IP）、详情
 * 展开行（detail JSON 字符串解析格式化只读展示——T2 评审移交契约；解析失败原样/
 * 空值 "-"）、筛选带参与重置（§22.1）、分页、CSV 导出（fetch blob + a[download]
 * 触发、Content-Disposition 文件名、X-Truncated 截断提示、失败内联 §22.2）、
 * api 层契约钉死。审计端点在主分支 68783f9 未合入（deviations #45），本 spec
 * 按契约 mock，合并即生效。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { formatAuditDetail, listAuditLogs, type AuditLogItem } from '@/api/auditLogs'
import AuditView from '@/views/AuditView.vue'

const fetchMock = vi.fn()

const T0 = '2026-09-28T10:00:00+08:00'

function auditItem(overrides: Partial<AuditLogItem> = {}): AuditLogItem {
  return {
    id: 1,
    actorUsername: 'admin',
    action: 'qa_pair.create',
    objectType: 'qa_pair',
    objectId: 12,
    detail: '{"question":"退货流程","deptCode":"all"}',
    ip: '10.0.0.8',
    createdAt: T0,
    ...overrides,
  }
}

const backend = { rows: [] as AuditLogItem[] }

function okEnvelope(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ code: 0, message: 'ok', data }),
  } as unknown as Response
}

function csvResponse(headers: Record<string, string>): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    blob: async () => new Blob(['时间,操作人\n'], { type: 'text/csv' }),
    json: async () => {
      throw new Error('no json body')
    },
  } as unknown as Response
}

function routeFetch(url: string, init: RequestInit = {}): Response {
  if (url.startsWith('/api/admin/audit-logs?') && (init.method ?? 'GET') === 'GET') {
    const q = new URLSearchParams(url.split('?')[1] ?? '')
    const page = Number(q.get('page') ?? 1)
    const pageSize = Number(q.get('pageSize') ?? 20)
    const start = (page - 1) * pageSize
    return okEnvelope({
      items: backend.rows.slice(start, start + pageSize),
      total: backend.rows.length,
      page,
      pageSize,
    })
  }
  if (url.startsWith('/api/admin/audit-logs/export')) {
    return csvResponse({
      'content-disposition': 'attachment; filename="audit-20260928.csv"',
      'x-truncated': 'false',
    })
  }
  throw new Error(`unexpected fetch: ${init.method ?? 'GET'} ${url}`)
}

const mounted: VueWrapper[] = []

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table × 日期范围的挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

const origCreateUrl = URL.createObjectURL
const origRevokeUrl = URL.revokeObjectURL

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  backend.rows = []
  fetchMock.mockImplementation(routeFetch)
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

async function mountAudit(): Promise<VueWrapper> {
  const wrapper = mount(AuditView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

describe('列表加载与渲染', () => {
  it('挂载即拉列表（空筛选仅分页参数），行渲染时间/操作人/动作/对象/IP；objectId 为 null 出 "-"', async () => {
    backend.rows = [auditItem(), auditItem({ id: 2, objectId: null, ip: null })]
    const wrapper = await mountAudit()
    const calls = fetchMock.mock.calls.map((c) => String(c[0]))
    expect(calls[0]).toBe('/api/admin/audit-logs?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('2026-09-28 10:00')
    expect(rows[0]!.text()).toContain('admin')
    expect(rows[0]!.text()).toContain('qa_pair.create')
    expect(rows[0]!.text()).toContain('qa_pair #12')
    expect(rows[0]!.text()).toContain('10.0.0.8')
    expect(rows[1]!.text()).toContain('qa_pair -')
    expect(rows[1]!.text()).toContain('-')
  })

  it('详情展开行：detail JSON 字符串解析为缩进只读展示（非 JSON 原样、null 出 "-"）', async () => {
    backend.rows = [
      auditItem(),
      auditItem({ id: 2, detail: '自由文本{非法' }),
      auditItem({ id: 3, detail: null }),
    ]
    // 纯函数三态口径先行钉死
    expect(formatAuditDetail('{"a":1}')).toBe('{\n  "a": 1\n}')
    expect(formatAuditDetail('自由文本{非法')).toBe('自由文本{非法')
    expect(formatAuditDetail(null)).toBe('-')
    const wrapper = await mountAudit()
    await wrapper.findAll('.el-table__row')[0]!.find('.el-table__expand-icon')!.trigger('click')
    await flushPromises()
    const pre = wrapper.find('[data-test="detail-json"]')
    expect(pre.exists()).toBe(true)
    expect(pre.text()).toContain('"question": "退货流程"')
    expect(pre.text()).toContain('"deptCode": "all"')
  })
})

describe('筛选与分页（§22.1）', () => {
  it('动作/操作人ID/日期范围编入 query；重置清空回第 1 页', async () => {
    backend.rows = [auditItem()]
    const wrapper = await mountAudit()
    // EP 2.14 日期范围键入提交通道在 jsdom 下不稳定，走与真实等价的数据路径：
    // date-picker value-format 回写 filters.dateRange 后由"查询"发起同一条 load
    const vm = wrapper.vm as unknown as {
      filters: { actorId: number | undefined; action: string; dateRange: [string, string] | null }
    }
    vm.filters.actorId = 5
    vm.filters.action = 'user.create'
    vm.filters.dateRange = ['2026-09-01', '2026-09-28']
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '查询')!
      .trigger('click')
    await flushPromises()
    const url = decodeURIComponent(
      String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]![0]),
    )
    expect(url).toContain('actorId=5')
    expect(url).toContain('action=user.create')
    expect(url).toContain('dateFrom=2026-09-01')
    expect(url).toContain('dateTo=2026-09-28')

    await wrapper
      .findAll('button')
      .find((b) => b.text() === '重置')!
      .trigger('click')
    await flushPromises()
    const last = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]![0]
    expect(decodeURIComponent(String(last))).toBe('/api/admin/audit-logs?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    backend.rows = Array.from({ length: 25 }, (_, index) => auditItem({ id: index + 1 }))
    const wrapper = await mountAudit()
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    const last = fetchMock.mock.calls[fetchMock.mock.calls.length - 1]![0]
    expect(String(last)).toContain('page=2')
  })
})

describe('CSV 导出（F-802/ADM-09）', () => {
  it('导出成功：fetch blob → a[download] 触发，文件名取 Content-Disposition；未截断无提示', async () => {
    backend.rows = [auditItem()]
    const wrapper = await mountAudit()
    await wrapper.find('[data-test="export-btn"]').trigger('click')
    await flushPromises()
    const exportCall = fetchMock.mock.calls.find((c) => String(c[0]).includes('/export'))!
    // 导出与列表共用筛选拼参（空参无 query string）；page/pageSize 不属于导出契约
    expect(String(exportCall[0])).toBe('/api/admin/audit-logs/export')
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    // a[download] 触发：href 为 blob URL，文件名取 Content-Disposition
    const anchor = vi.spyOn(HTMLAnchorElement.prototype, 'click').mock
      .instances[0] as HTMLAnchorElement
    expect(anchor.href).toBe('blob:mock-url')
    expect(anchor.download).toBe('audit-20260928.csv')
    expect(wrapper.find('[data-test="export-note"]').exists()).toBe(false)
  })

  it('X-Truncated: true 提示"已截断"（内存放大保护口径）', async () => {
    backend.rows = [auditItem()]
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/export')) {
        return csvResponse({
          'content-disposition': 'attachment; filename="audit-20260928.csv"',
          'x-truncated': 'true',
        })
      }
      return routeFetch(String(url))
    })
    const wrapper = await mountAudit()
    await wrapper.find('[data-test="export-btn"]').trigger('click')
    await flushPromises()
    const note = wrapper.find('[data-test="export-note"]')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('已截断')
  })

  it('导出失败：信封中文 message 内联展示（§22.2），不触发下载', async () => {
    backend.rows = [auditItem()]
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/export')) {
        return {
          ok: false,
          status: 422,
          headers: { get: () => 'application/json' },
          json: async () => ({ code: 1003, message: '日期范围非法', data: null }),
        } as unknown as Response
      }
      return routeFetch(String(url))
    })
    const wrapper = await mountAudit()
    await wrapper.find('[data-test="export-btn"]').trigger('click')
    await flushPromises()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="export-note"]').text()).toContain('日期范围非法')
  })
})

describe('api 层契约（路径/参数钉死）', () => {
  it('listAuditLogs：空参不发；actorId/action/objectType/dateFrom/dateTo 编码进 query', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      okEnvelope({ items: [], total: 0, page: 1, pageSize: 20 }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listAuditLogs({ page: 1, pageSize: 20 })
    expect(String(fetchStub.mock.calls[0]![0])).toBe('/api/admin/audit-logs?page=1&pageSize=20')
    await listAuditLogs({
      actorId: 3,
      action: 'eval.run',
      objectType: 'eval_run',
      dateFrom: '2026-09-01',
      dateTo: '2026-09-28',
      page: 2,
      pageSize: 50,
    })
    expect(String(fetchStub.mock.calls[1]![0])).toBe(
      '/api/admin/audit-logs?actorId=3&action=eval.run&objectType=eval_run&dateFrom=2026-09-01&dateTo=2026-09-28&page=2&pageSize=50',
    )
  })
})

// ---- 加载失败错误态（工单 08：失败 ≠ 没有数据，错误态先于空态呈现"点击重试"） ----

describe('加载失败错误态（工单 08）', () => {
  it('加载失败空态显示"加载失败，点击重试"而非"暂无审计记录"，重试成功恢复列表', async () => {
    backend.rows = [auditItem({ action: 'user.create' })]
    fetchMock.mockImplementation((url: string, init: RequestInit = {}) => {
      if (String(url).startsWith('/api/admin/audit-logs?')) {
        return Promise.reject(new TypeError('Network Error'))
      }
      return routeFetch(String(url), init)
    })
    const wrapper = await mountAudit()
    expect(wrapper.find('[data-test="audit-load-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载失败，点击重试')
    expect(wrapper.text()).not.toContain('暂无审计记录') // 失败 ≠ 没有数据
    // 后端恢复后点重试：列表恢复、错误态退场
    fetchMock.mockImplementation(routeFetch)
    await wrapper.find('[data-test="audit-load-error"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="audit-load-error"]').exists()).toBe(false)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1)
    expect(wrapper.findAll('.el-table__row')[0]!.text()).toContain('user.create')
  })
})
