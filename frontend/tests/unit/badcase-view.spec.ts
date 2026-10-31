/**
 * P8 badcase 管理集成测试：默认视图显式传 badcaseStatus='待处理'（W3 交接）、
 * 列表渲染（分类/备注/状态 tag）、筛选（部门/状态）、分页、处置对话框
 * （'已忽略' 必填理由前端先行校验 ADM-03 拦截 / '已修语料' 备注可选 / 超长拦截 /
 * PATCH 422 兜底内联不关弹窗 §22.2）、终态行无处理入口、api 层契约钉死。
 * 共享 mock 后端见 helpers/badcase-mock.ts（全链路走真 client.ts）。
 */
import { DOMWrapper, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { listBadcases, patchBadcase } from '@/api/badcase'
import HandleBadcaseDialog from '@/components/badcase/HandleBadcaseDialog.vue'
import {
  backend,
  badcaseItem,
  fetchMock,
  lastListCall,
  listCalls,
  makeResponse,
  mountBadcase,
  resetBackend,
  routeFetch,
  unmountAllBadcase,
  waitFor,
} from './helpers/badcase-mock'

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table × 弹窗的挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  resetBackend()
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  unmountAllBadcase()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('列表加载与渲染', () => {
  it('默认视图显式传 badcaseStatus=待处理（W3 交接：后端缺省不过滤），行渲染全列', async () => {
    badcaseItem({ category: '答案过时', comment: '2026 年已改线上退货', badcaseStatus: '待处理' })
    badcaseItem({
      deptCode: 'hr',
      questionPreview: '年假可以顺延吗？',
      category: null,
      comment: null,
      badcaseStatus: '待处理',
    })
    // 已处置行不进默认视图（'待处理' 过滤语义）
    badcaseItem({ questionPreview: '已处置的问题', badcaseStatus: '已修语料' })
    const wrapper = await mountBadcase()
    expect(decodeURIComponent(listCalls()[0]!)).toBe(
      '/api/admin/badcases?badcaseStatus=待处理&page=1&pageSize=20',
    )
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('2026-09-28 10:00')
    expect(rows[0]!.text()).toContain('客服')
    expect(rows[0]!.text()).toContain('退货流程是什么？')
    expect(rows[0]!.text()).toContain('答案过时')
    expect(rows[0]!.text()).toContain('2026 年已改线上退货')
    expect(rows[0]!.text()).toContain('待处理')
    expect(rows[1]!.text()).toContain('人力')
    expect(wrapper.text()).not.toContain('已处置的问题')
    // category/comment 为 null → "-" 占位
    expect(rows[1]!.text()).toContain('-')
  })

  it('仅待处理行出"处理"入口，终态行不渲染处理按钮（无重开回退）', async () => {
    badcaseItem({ badcaseStatus: '待处理' })
    badcaseItem({ badcaseStatus: '已忽略' })
    const wrapper = await mountBadcase()
    // 重置到"不过滤"视图让两态同屏（默认视图只回待处理行）
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '重置')!
      .trigger('click')
    await flushPromises()
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.findAll('button').some((b) => b.text().includes('处理'))).toBe(true)
    expect(rows[1]!.findAll('button').some((b) => b.text().includes('处理'))).toBe(false)
  })

  it('空队列展示"没有待处理的问题反馈"空态', async () => {
    const wrapper = await mountBadcase()
    expect(wrapper.text()).toContain('没有待处理的问题反馈')
  })
})

describe('筛选与分页（§22.1）', () => {
  it('部门/状态筛选编码进 query；重置清空状态（不过滤全部）', async () => {
    badcaseItem()
    const wrapper = await mountBadcase()
    // 状态下拉（teleport）：选"已忽略"
    await wrapper.findAll('.el-select')[1]!.find('.el-select__wrapper').trigger('click')
    await waitFor(
      () => document.querySelectorAll('.el-select-dropdown__item').length >= 3,
      '状态下拉渲染',
    )
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('已忽略'),
    )!
    await new DOMWrapper(option).trigger('click')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('badcaseStatus=已忽略')

    // 部门筛选：客服
    await wrapper.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(
      () => document.querySelectorAll('.el-select-dropdown__item').length >= 3,
      '部门下拉渲染',
    )
    const deptOption = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('客服'),
    )!
    await new DOMWrapper(deptOption).trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('deptId=1')

    // 重置：状态清空 → 不带 badcaseStatus（对照原型）
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '重置')!
      .trigger('click')
    await flushPromises()
    expect(lastListCall()).toBe('/api/admin/badcases?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    Array.from({ length: 45 }, () => badcaseItem())
    const wrapper = await mountBadcase()
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('badcaseStatus=待处理')
    expect(lastListCall()).toContain('page=2')
  })
})

describe('处置对话框（F-702 治理闭环）', () => {
  async function openHandle(wrapper: Awaited<ReturnType<typeof mountBadcase>>) {
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text().includes('处理'))!
      .trigger('click')
    const dialog = wrapper.findComponent(HandleBadcaseDialog)
    expect(dialog.props('visible')).toBe(true)
    await flushPromises()
    return dialog
  }

  it('打开回显问题、处理方式默认已修语料；备注空提交 → PATCH note=null（可选口径）并刷新', async () => {
    badcaseItem()
    const wrapper = await mountBadcase()
    const dialog = await openHandle(wrapper)
    expect(dialog.text()).toContain('退货流程是什么？')
    const radios = dialog.findAll('.el-radio')
    expect(radios).toHaveLength(2)
    expect(radios[0]!.text()).toContain('更新文档/补问答对')
    expect(radios[0]!.classes()).toContain('is-checked')

    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交')!
      .trigger('click')
    await flushPromises()
    expect(backend.patchCalls).toEqual([
      { messageId: backend.rows[0]!.messageId, badcaseStatus: '已修语料', note: null },
    ])
    expect(dialog.props('visible')).toBe(false)
    await waitFor(() => listCalls().length >= 2, '处置后刷新列表')
  })

  it('已修语料填写备注 → PATCH 携带 trim 后 note', async () => {
    badcaseItem()
    const wrapper = await mountBadcase()
    const dialog = await openHandle(wrapper)
    await dialog.find('textarea').setValue('  已补问答对 QA-1001  ')
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交')!
      .trigger('click')
    await flushPromises()
    expect(backend.patchCalls).toEqual([
      {
        messageId: backend.rows[0]!.messageId,
        badcaseStatus: '已修语料',
        note: '已补问答对 QA-1001',
      },
    ])
  })

  it('已忽略且理由为空：前端先行校验拦截（ADM-03），零 PATCH、弹窗不关', async () => {
    badcaseItem()
    const wrapper = await mountBadcase()
    const dialog = await openHandle(wrapper)
    // 切到"已忽略"
    await dialog.findAll('.el-radio')[1]!.find('input')!.setValue('已忽略')
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交')!
      .trigger('click')
    await waitFor(() => dialog.text().includes('忽略必须说明理由'), '必填校验错误内联渲染')
    expect(backend.patchCalls).toHaveLength(0)
    expect(dialog.props('visible')).toBe(true)
  })

  it('已忽略填写理由 → PATCH {badcaseStatus:已忽略, note}；后端 422 兜底时错误内联且弹窗不关', async () => {
    badcaseItem()
    backend.patchError = '忽略必须填写处理理由'
    const wrapper = await mountBadcase()
    const dialog = await openHandle(wrapper)
    await dialog.findAll('.el-radio')[1]!.find('input')!.setValue('已忽略')
    await dialog.find('textarea').setValue('该问题为重复提问，语料已覆盖')
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交')!
      .trigger('click')
    await flushPromises()
    expect(backend.patchCalls).toEqual([
      {
        messageId: backend.rows[0]!.messageId,
        badcaseStatus: '已忽略',
        note: '该问题为重复提问，语料已覆盖',
      },
    ])
    expect(dialog.find('.err-inline').text()).toBe('忽略必须填写处理理由')
    expect(dialog.props('visible')).toBe(true)
  })

  it('备注 textarea maxlength=500（超长拦截由 validators 单测覆盖边界，UI 层 maxlength 硬限）', async () => {
    badcaseItem()
    const wrapper = await mountBadcase()
    const dialog = await openHandle(wrapper)
    expect(dialog.find('textarea').attributes('maxlength')).toBe('500')
  })
})

describe('api 层契约（路径/方法/参数钉死）', () => {
  it('listBadcases：badcaseStatus 空串不发；deptId/badcaseStatus/page/pageSize 编码进 query', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { items: [], total: 0, page: 1, pageSize: 20 },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listBadcases({ page: 1, pageSize: 20 })
    expect(fetchStub.mock.calls[0]![0]).toBe('/api/admin/badcases?page=1&pageSize=20')

    await listBadcases({ deptId: 1, badcaseStatus: '待处理', page: 2, pageSize: 50 })
    expect(decodeURIComponent(String(fetchStub.mock.calls[1]![0]))).toBe(
      '/api/admin/badcases?deptId=1&badcaseStatus=待处理&page=2&pageSize=50',
    )
  })

  it('patchBadcase：PATCH /api/admin/badcases/{messageId} 与请求体', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { messageId: 88, badcaseStatus: '已忽略' },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await patchBadcase(88, { badcaseStatus: '已忽略', note: '重复提问' })
    const [url, init] = fetchStub.mock.calls[0]!
    expect(url).toBe('/api/admin/badcases/88')
    expect(init!.method).toBe('PATCH')
    expect(JSON.parse(String(init!.body))).toEqual({ badcaseStatus: '已忽略', note: '重复提问' })
  })
})

// ---- 加载失败错误态（工单 08：失败 ≠ 没有数据，错误态先于空态呈现"点击重试"） ----

describe('加载失败错误态（工单 08）', () => {
  it('加载失败空态显示"加载失败，点击重试"而非"没有待处理的问题反馈"，重试成功恢复列表', async () => {
    badcaseItem({ questionPreview: '恢复用 badcase' })
    fetchMock.mockImplementation((url: string, init: RequestInit = {}) => {
      if (String(url).startsWith('/api/admin/badcases?') && (init.method ?? 'GET') === 'GET') {
        return Promise.reject(new TypeError('Network Error'))
      }
      return routeFetch(String(url), init)
    })
    const wrapper = await mountBadcase()
    expect(wrapper.find('[data-test="badcases-load-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载失败，点击重试')
    expect(wrapper.text()).not.toContain('没有待处理的问题反馈') // 失败 ≠ 没有数据
    // 后端恢复后点重试：列表恢复、错误态退场
    fetchMock.mockImplementation(routeFetch)
    await wrapper.find('[data-test="badcases-load-error"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="badcases-load-error"]').exists()).toBe(false)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1)
    expect(wrapper.findAll('.el-table__row')[0]!.text()).toContain('恢复用 badcase')
  })
})
