/**
 * P5 检索测试集成测试：表单初始态与空值校验、请求体契约（question strip + deptCode）、
 * 问答对命中卡（similarity=embedding 余弦 + thresholdPassed）、文档块表两套分数分列
 * （rerank sigmoid 与召回余弦不互比——W3 交接红线）、thresholdPassed/folded 标记、
 * 历史命中列（W5 F-1011：hitCount+lastHitAt 读现列展示）、当前阈值只读展示、
 * qa=null 与全空两种空态、422 内联（§22.2）、零召回块清单折叠区（F-1011 块级下钻：
 * 首展懒加载/分页/空态引导）。mock 全局 fetch 按 docs/api.md §4.4.1 契约应答，
 * 全链路走真 client.ts。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import RetrievalTestView from '@/views/RetrievalTestView.vue'
import type { RetrievalTestResult } from '@/api/retrievalTest'

const fetchMock = vi.fn()

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-select/el-table × 首测的 element-plus 转换在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

// ---- 响应构造（与 qa-pairs-mock 同口径） ----

function makeResponse(status: number, json?: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
    },
    body: null,
    json:
      json === undefined
        ? async () => {
            throw new Error('no json body')
          }
        : async () => json,
  } as unknown as Response
}

function okEnvelope(data: unknown): Response {
  return makeResponse(200, { code: 0, message: 'ok', data })
}

const hitResult: RetrievalTestResult = {
  qa: { id: 1024, question: '差旅报销标准是多少？', similarity: 0.91, thresholdPassed: true },
  docs: [
    {
      chunkId: 11,
      documentTitle: '客服手册',
      page: 3,
      titlePath: '第3章>3.2 报销标准',
      rerankScore: 0.93,
      recallCosine: 0.88,
      thresholdPassed: true,
      folded: false,
      hitCount: 5,
      lastHitAt: '2026-09-25T10:30:00+00:00',
    },
    {
      chunkId: 12,
      documentTitle: 'HR 考勤制度',
      page: null,
      titlePath: null,
      rerankScore: 0.44,
      recallCosine: 0.72,
      thresholdPassed: false,
      folded: true,
      hitCount: 0,
      lastHitAt: null,
    },
  ],
  thresholds: { qa: 0.85, refuse: 0.55 },
}

const backend = {
  /** POST /api/admin/retrieval-test 的 200 应答（null = 用默认命中场景） */
  result: null as RetrievalTestResult | null,
  /** 非 null 时返回 422/1003 错误信封 */
  error: null as string | null,
}

/** 零召回块清单（GET /api/admin/zero-recall-chunks）mock：result 非 null 即按其应答 */
const zrcBackend = {
  result: null as unknown | null,
  error: null as string | null,
}
const zrcCalls: string[] = []

let lastBody: unknown = null

function resetBackend(): void {
  backend.result = null
  backend.error = null
  zrcBackend.result = null
  zrcBackend.error = null
  zrcCalls.length = 0
  lastBody = null
}

async function routeFetch(url: string, init: RequestInit = {}): Promise<Response> {
  if (url === '/api/admin/retrieval-test' && init.method === 'POST') {
    lastBody = JSON.parse(String(init.body))
    if (backend.error !== null) {
      return makeResponse(422, { code: 1003, message: backend.error, data: null })
    }
    return okEnvelope(backend.result ?? hitResult)
  }
  if (url.startsWith('/api/admin/zero-recall-chunks') && (init.method ?? 'GET') === 'GET') {
    zrcCalls.push(url)
    if (zrcBackend.error !== null) {
      return makeResponse(422, { code: 1003, message: zrcBackend.error, data: null })
    }
    return okEnvelope(zrcBackend.result ?? { items: [], total: 0, page: 1, pageSize: 10 })
  }
  throw new Error(`unexpected fetch: ${init.method ?? 'GET'} ${url}`)
}

async function mountView(): Promise<VueWrapper> {
  const wrapper = mount(RetrievalTestView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  await flushPromises()
  return wrapper
}

async function askAndRun(wrapper: VueWrapper, question: string): Promise<void> {
  await wrapper.find('input[placeholder^="输入测试问题"]').setValue(question)
  await wrapper
    .findAll('button')
    .find((b) => b.text().includes('测 试'))!
    .trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  resetBackend()
  fetchMock.mockImplementation((url: string, init?: RequestInit) => routeFetch(url, init))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('表单与校验', () => {
  it('初始态：部门范围下拉默认客服（3 个枚举选项，无原型第 4 项"全部（admin）"）、无结果区', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).not.toContain('文档块命中')
    expect(wrapper.text()).not.toContain('问答对命中')
    const options = wrapper.findAllComponents({ name: 'ElOption' }).map((o) => o.props('label'))
    expect(options).toEqual(['客服', '人力', '全员'])
    // 范围默认值进入请求体（点一次即可观测）
    await askAndRun(wrapper, '退货流程')
    expect(lastBody).toEqual({ question: '退货流程', deptCode: 'service' })
  })

  it('空问题点击测试：内联"问题不能为空"且不发请求（§21 失焦口径的前置拦截）', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '   ')
    expect(wrapper.find('.err-inline').text()).toBe('问题不能为空')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('提交前 strip：问题前后空白不进请求体', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '  退货运费谁承担  ')
    expect(lastBody).toEqual({ question: '退货运费谁承担', deptCode: 'service' })
  })
})

describe('命中结果渲染（两套分数分列，W3 红线）', () => {
  it('问答对命中卡（similarity+超阈值标记+问题全文）与文档块表同屏渲染', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '差旅报销标准')
    const text = wrapper.text()
    expect(text).toContain('FAQ-1024')
    expect(text).toContain('相似度 0.91')
    expect(text).toContain('超阈值(0.85)')
    expect(text).toContain('差旅报销标准是多少？')
    // 阈值条：响应下发的只读当前阈值
    expect(text).toContain('当前生效阈值：问答对 0.85 · 拒答 0.55')
  })

  it('文档块表：重排分数（sigmoid）与召回余弦两列分列渲染、各归各位不互比；页码/标题路径逐格成列', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '差旅报销标准')
    const headers = wrapper.findAll('.el-table__header-wrapper .cell').map((c) => c.text())
    expect(headers).toContain('重排分数（sigmoid）')
    expect(headers).toContain('召回余弦')
    const cells = wrapper
      .find('.el-table__row')
      .findAll('.cell')
      .map((c) => c.text())
    expect(cells[0]).toBe('1')
    expect(cells[1]).toContain('0.93')
    expect(cells[2]).toContain('0.88')
    expect(cells[3]).toContain('客服手册')
    expect(cells[4]).toContain('3')
    expect(cells[5]).toContain('第3章>3.2 报销标准')
    expect(cells[6]).toContain('✓ 超阈值')
    // 第 2 行：page/titlePath 空值占位、低于阈值与已折叠标记
    const row2 = wrapper.findAll('.el-table__row')[1]!
    expect(row2.text()).toContain('0.44')
    expect(row2.text()).toContain('0.72')
    expect(row2.text()).toContain('—')
    expect(row2.text()).toContain('低于阈值')
    expect(row2.text()).toContain('已折叠')
  })

  it('qa=null 且 docs 非空：展示"无超阈值命中 → 走文档检索链路"，文档表照常渲染', async () => {
    backend.result = { qa: null, docs: hitResult.docs, thresholds: hitResult.thresholds }
    const wrapper = await mountView()
    await askAndRun(wrapper, '保修期多久')
    expect(wrapper.text()).toContain('无超阈值命中 → 走文档检索链路')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(2)
  })

  it('全空结果（qa=null 且 docs=[]）：空态引导换问法/调部门范围（§8.5）', async () => {
    backend.result = { qa: null, docs: [], thresholds: hitResult.thresholds }
    const wrapper = await mountView()
    await askAndRun(wrapper, '完全不相关的问题')
    expect(wrapper.text()).toContain('未命中任何问答对或文档块')
    expect(wrapper.text()).toContain('可换个问法重试')
    expect(wrapper.text()).not.toContain('文档块命中')
  })
})

describe('错误处理', () => {
  it('后端 422（question 超长等）内联展示错误文案，首测不渲染结果区（§22.2）', async () => {
    backend.error = '问题长度须在 1-2000 字之间'
    const wrapper = await mountView()
    await askAndRun(wrapper, '差旅报销标准')
    expect(wrapper.find('.err-inline').text()).toBe('问题长度须在 1-2000 字之间')
    expect(wrapper.text()).not.toContain('文档块命中')
    expect(wrapper.text()).not.toContain('问答对命中')
  })
})

describe('历史命中列（W5 F-1011，读现列展示）', () => {
  it('文档块表渲染历史命中列：hitCount 计数 + lastHitAt 格式化（YYYY-MM-DD HH:mm）', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '差旅报销标准')
    const headers = wrapper.findAll('.el-table__header-wrapper .cell').map((c) => c.text())
    expect(headers).toContain('历史命中')
    const row1 = wrapper.findAll('.el-table__row')[0]!
    expect(row1.text()).toContain('5 次')
    expect(row1.text()).toContain('2026-09-25 10:30')
    expect(row1.text()).not.toContain('从未被召回')
  })

  it('从未被召回（lastHitAt=null）出"从未被召回"占位，不出时间文本', async () => {
    const wrapper = await mountView()
    await askAndRun(wrapper, '差旅报销标准')
    const row2 = wrapper.findAll('.el-table__row')[1]!
    expect(row2.text()).toContain('0 次')
    expect(row2.text()).toContain('从未被召回')
    expect(row2.text()).not.toContain('2026-09-25')
  })
})

describe('零召回块清单折叠区（W5 F-1011 块级下钻）', () => {
  const zrcChunk = {
    chunkId: 31,
    documentTitle: '客服手册 2023 版',
    deptCode: 'service',
    titlePath: '客服手册>第一章',
    contentPreview: '退货运费说明',
    createdAt: '2026-08-15T09:30:00+00:00',
  }

  it('默认收起不发请求（检索测试也不触发）；首展拉 days=30&page=1&pageSize=10 并渲染行', async () => {
    zrcBackend.result = {
      items: [zrcChunk],
      total: 1,
      page: 1,
      pageSize: 10,
    }
    const wrapper = await mountView()
    expect(zrcCalls).toHaveLength(0)
    await askAndRun(wrapper, '差旅报销标准')
    expect(zrcCalls).toHaveLength(0) // 检索测试不搭车拉清单
    await wrapper.find('.el-collapse-item__header').trigger('click')
    await flushPromises()
    expect(zrcCalls).toEqual(['/api/admin/zero-recall-chunks?days=30&page=1&pageSize=10'])
    const text = wrapper.text()
    expect(text).toContain('客服手册 2023 版')
    expect(text).toContain('客服') // deptCode 经共享映射出中文
    expect(text).toContain('客服手册>第一章')
    expect(text).toContain('退货运费说明')
    expect(text).toContain('2026-08-15 09:30')
  })

  it('翻下一页携带 page=2 重拉并渲染第 2 页数据，total 保持全量', async () => {
    zrcBackend.result = { items: [zrcChunk], total: 11, page: 1, pageSize: 10 }
    const wrapper = await mountView()
    await wrapper.find('.el-collapse-item__header').trigger('click')
    await flushPromises()
    zrcBackend.result = {
      items: [
        {
          chunkId: 32,
          documentTitle: 'HR 考勤制度 2022 版',
          deptCode: 'hr',
          titlePath: null,
          contentPreview: '考勤异常处理流程',
          createdAt: '2026-08-01T08:00:00+00:00',
        },
      ],
      total: 11,
      page: 2,
      pageSize: 10,
    }
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(zrcCalls).toEqual([
      '/api/admin/zero-recall-chunks?days=30&page=1&pageSize=10',
      '/api/admin/zero-recall-chunks?days=30&page=2&pageSize=10',
    ])
    const text = wrapper.text()
    expect(text).toContain('HR 考勤制度 2022 版')
    expect(text).toContain('人力')
    expect(text).not.toContain('客服手册 2023 版') // 第 1 页数据被替换不残留
  })

  it('空清单渲染空态引导（§8.5），不出表格与分页', async () => {
    const wrapper = await mountView()
    await wrapper.find('.el-collapse-item__header').trigger('click')
    await flushPromises()
    expect(zrcCalls).toHaveLength(1)
    expect(wrapper.text()).toContain('窗口内没有零召回块')
    expect(wrapper.text()).toContain('所有活跃块近期均有命中')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(0)
    expect(wrapper.find('.btn-next').exists()).toBe(false)
  })
})
