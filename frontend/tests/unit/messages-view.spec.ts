/**
 * P7 提问记录集成测试：保留策略提示条、筛选（部门/日期范围/是否点踩 §22.1）、
 * 列表渲染（问题摘要+回答类型/引用有/反馈）、分页、详情抽屉（完整对话只读置灰、
 * 引用 JSON 折叠、token/耗时 meta、assistant=null 中断占位、404 失败态）、
 * api 层契约钉死。共享 mock 后端见 helpers/messages-mock.ts（全链路走真 client.ts）。
 */
import { DOMWrapper, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getAdminMessageDetail, listAdminMessages } from '@/api/adminMessages'
import MessageDetailDrawer from '@/components/messages/MessageDetailDrawer.vue'
import {
  backend,
  detailFixture,
  fetchMock,
  lastListCall,
  listCalls,
  makeResponse,
  messageItem,
  mountMessages,
  resetBackend,
  routeFetch,
  unmountAllMessages,
  waitFor,
} from './helpers/messages-mock'

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table × 抽屉的挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  resetBackend()
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  unmountAllMessages()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('列表加载与渲染', () => {
  it('挂载即拉列表（空筛选仅 page/pageSize），行渲染时间/用户/部门/问题摘要/回答类型/引用/反馈', async () => {
    messageItem({ feedback: 'down' })
    messageItem({
      username: '新员工小李',
      deptCode: 'hr',
      answerType: null,
      hasCitations: false,
      feedback: 'up',
    })
    const wrapper = await mountMessages()
    expect(listCalls()[0]).toBe('/api/admin/messages?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('2026-09-28 10:00')
    expect(rows[0]!.text()).toContain('客服小王')
    expect(rows[0]!.text()).toContain('客服')
    expect(rows[0]!.text()).toContain('退货流程是什么？')
    expect(rows[0]!.text()).toContain('文档检索')
    expect(rows[0]!.text()).toContain('有')
    expect(rows[0]!.text()).toContain('点踩')
    expect(rows[1]!.text()).toContain('人力')
    expect(rows[1]!.text()).toContain('点赞')
    // 无回答/无引用 → "-" 占位（不编造）
    expect(rows[1]!.text()).toContain('-')
  })

  it('保留策略提示条与筛选控件集合（部门/日期范围/是否点踩 + 查询/重置 §22.1）', async () => {
    messageItem()
    const wrapper = await mountMessages()
    expect(wrapper.text()).toContain('提问记录保留 90 天')
    expect(wrapper.find('input[placeholder="开始日期"]').exists()).toBe(true)
    expect(wrapper.find('input[placeholder="结束日期"]').exists()).toBe(true)
    const texts = wrapper.findAll('button').map((b) => b.text())
    expect(texts.some((t) => t.includes('查询'))).toBe(true)
    expect(texts.some((t) => t.includes('重置'))).toBe(true)
  })

  it('空列表展示"暂无提问记录"空态', async () => {
    const wrapper = await mountMessages()
    expect(wrapper.text()).toContain('暂无提问记录')
  })
})

describe('筛选与分页（§22.1）', () => {
  it('部门/反馈筛选：deptId 与 hasFeedback 编码进 query（true=有点踩/false=无点踩）', async () => {
    messageItem({ feedback: 'down' })
    const wrapper = await mountMessages()
    // 部门下拉（teleport）：选"人力"
    await wrapper.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const hrOption = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('人力'),
    )!
    await new DOMWrapper(hrOption).trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('deptId=2')

    // 反馈下拉：选"有点踩"（等待本下拉内容渲染，防误点仍挂载的部门下拉项）
    await wrapper.findAll('.el-select')[1]!.find('.el-select__wrapper').trigger('click')
    await waitFor(
      () =>
        [...document.querySelectorAll('.el-select-dropdown')].some((d) =>
          d.textContent?.includes('有点踩'),
        ),
      '反馈下拉渲染',
    )
    const downOption = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('有点踩'),
    )!
    await new DOMWrapper(downOption).trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('hasFeedback=true')

    // 改选"无点踩" → false（补集口径；下拉变更即刷新 §22.1）
    const noneOption = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('无点踩'),
    )!
    await new DOMWrapper(noneOption).trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('hasFeedback=false')
  })

  it('日期范围筛选：dateFrom/dateTo 编码进 query；重置清空全部条件', async () => {
    messageItem()
    const wrapper = await mountMessages()
    expect(wrapper.findComponent({ name: 'ElDatePicker' }).exists()).toBe(true)
    // EP 2.14 range picker 键入提交通道在 jsdom 下不稳定（rAF 竞态，偶发不回写 v-model），
    // 视图层走与真实等价的数据路径：date-picker value-format 回写 filters.dateRange
    // （YYYY-MM-DD 双元素数组）后由"查询"发起同一条 load。
    const vm = wrapper.vm as unknown as {
      filters: { dateRange: [string, string] | null }
    }
    vm.filters.dateRange = ['2026-09-01', '2026-09-28']
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '查询')!
      .trigger('click')
    await flushPromises()
    const url = decodeURIComponent(lastListCall())
    expect(url).toContain('dateFrom=2026-09-01')
    expect(url).toContain('dateTo=2026-09-28')

    await wrapper
      .findAll('button')
      .find((b) => b.text() === '重置')!
      .trigger('click')
    await flushPromises()
    expect(lastListCall()).toBe('/api/admin/messages?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    Array.from({ length: 45 }, () => messageItem())
    const wrapper = await mountMessages()
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')
  })
})

describe('详情抽屉（只读 §8.8）', () => {
  it('正常回答：完整对话（提问原文+回答全文只读置灰）、引用 JSON 折叠 readOnly、token/耗时 meta、点踩备注', async () => {
    messageItem()
    backend.detail = detailFixture({ feedback: 'down' })
    const wrapper = await mountMessages()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '详情')!
      .trigger('click')
    const drawer = wrapper.findComponent(MessageDetailDrawer)
    expect(drawer.props('visible')).toBe(true)
    expect(backend.detailCalls).toEqual([backend.messages[0]!.id])
    await waitFor(() => drawer.text().includes('退货流程'), '详情加载完成')

    expect(drawer.text()).toContain('提问（客服小王 · 2026-09-28 10:00）')
    expect(drawer.text()).toContain('退货流程是什么？请给出完整步骤')
    expect(drawer.text()).toContain('质量问题退货：联系客服登记 → 寄回商品 → 验货后退款。')
    // 引用 JSON 原样 readOnly（折叠面板标题含条数；内容懒渲染需先展开面板——抽屉
    // 未开 append-to-body，DOM 在组件子树内，用 drawer.find 而非 document 查询）
    expect(drawer.text()).toContain('共 1 条')
    await drawer.find('.el-collapse-item__header').trigger('click')
    await flushPromises()
    const citeJson = drawer.find('textarea')
    expect(citeJson.exists()).toBe(true)
    expect((citeJson.element as HTMLTextAreaElement).value).toContain('"titlePath"')
    expect((citeJson.element as HTMLTextAreaElement).value).toContain('"refId": 33')
    expect(citeJson.attributes('readonly')).toBeDefined()
    // meta 行：token/耗时/缓存命中/拒答
    const readonlyInputs = drawer
      .findAll('input[readonly]')
      .map((i) => (i.element as HTMLInputElement).value)
    expect(readonlyInputs.some((v) => v.includes('token 输入 812 / 输出 96'))).toBe(true)
    expect(readonlyInputs.some((v) => v.includes('耗时 1840ms'))).toBe(true)
    expect(readonlyInputs.some((v) => v.includes('缓存命中：否'))).toBe(true)
    expect(readonlyInputs.some((v) => v.includes('拒答：否'))).toBe(true)
    // 点踩备注（category：comment）
    expect(readonlyInputs.some((v) => v.includes('答案过时：2026 年已改为线上退货'))).toBe(true)
  })

  it('assistant=null（生成中断）：渲染失败态占位，不渲染回答/引用/运行信息段', async () => {
    messageItem({ answerType: null, hasCitations: false })
    backend.detail = detailFixture({ assistant: null, question: '怎么开发票？' })
    const wrapper = await mountMessages()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '详情')!
      .trigger('click')
    const drawer = wrapper.findComponent(MessageDetailDrawer)
    await waitFor(() => drawer.text().includes('怎么开发票？'), '详情加载完成')

    expect(drawer.text()).toContain('该问题无有效回答（生成中断），无回答内容')
    expect(drawer.text()).not.toContain('引用 JSON')
    expect(drawer.text()).not.toContain('运行信息')
  })

  it('详情 404：抽屉内联失败提示（提问记录不存在），不渲染内容', async () => {
    messageItem()
    backend.detailError = '提问记录不存在'
    const wrapper = await mountMessages()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '详情')!
      .trigger('click')
    const drawer = wrapper.findComponent(MessageDetailDrawer)
    await waitFor(() => drawer.text().includes('提问记录加载失败'), '失败态渲染')
    expect(drawer.text()).toContain('提问记录加载失败，请稍后重试')
    // 关抽屉无未保存态：抽屉纯只读无表单状态，父级 @close 接线即关（EP 关闭按钮经
    // leave 过渡回写 update:modelValue，jsdom 无 CSS 过渡不回写——非本页接线问题）
    drawer.vm.$emit('close')
    await flushPromises()
    expect(drawer.props('visible')).toBe(false)
  })
})

describe('api 层契约（路径/方法/参数钉死）', () => {
  it('listAdminMessages：空参不发；deptId/dateFrom/dateTo/hasFeedback/page/pageSize 编码进 query', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { items: [], total: 0, page: 1, pageSize: 20 },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listAdminMessages({ page: 1, pageSize: 20 })
    expect(fetchStub.mock.calls[0]![0]).toBe('/api/admin/messages?page=1&pageSize=20')

    await listAdminMessages({
      deptId: 2,
      dateFrom: '2026-09-01',
      dateTo: '2026-09-28',
      hasFeedback: true,
      page: 3,
      pageSize: 50,
    })
    expect(fetchStub.mock.calls[1]![0]).toBe(
      '/api/admin/messages?deptId=2&dateFrom=2026-09-01&dateTo=2026-09-28&hasFeedback=true&page=3&pageSize=50',
    )
  })

  it('getAdminMessageDetail：GET /api/admin/messages/{id} 正确路径', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: detailFixture({ assistant: null }),
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await getAdminMessageDetail(901)
    expect(fetchStub.mock.calls[0]![0]).toBe('/api/admin/messages/901')
    expect(fetchStub.mock.calls[0]![1]!.method ?? 'GET').toBe('GET')
  })
})
