/**
 * P3 知识库·文档管理集成测试：列表渲染与状态列矩阵、筛选/分页（§22.1）、
 * 上传对话框（白名单拒收 / 元数据建议回填 F-1008 / 必填校验 / multipart 提交）、
 * 确认激活与失败重试、置灰控件、zip 导入结果表（T3 契约四态）、30s 轮询（§8.4）。
 * 共享 mock 后端见 helpers/documents-mock.ts（全链路走真 client.ts）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UploadFile } from 'element-plus'
import { defineComponent, h, ref } from 'vue'
import { suggestDocumentMetadata, uploadDocument } from '@/api/documents'
import UploadDialog from '@/components/documents/UploadDialog.vue'
import ZipImportDialog from '@/components/documents/ZipImportDialog.vue'
import {
  backend,
  docItem,
  fetchMock,
  lastListCall,
  listCalls,
  makeFile,
  makeResponse,
  mountDocuments,
  resetBackend,
  routeFetch,
  unmountAllDocuments,
  waitFor,
} from './helpers/documents-mock'

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table × 弹窗的挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  // jsdom 未实现 ResizeObserver（el-table 列宽监听依赖）
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  resetBackend()
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  unmountAllDocuments()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

async function openUpload(wrapper: Awaited<ReturnType<typeof mountDocuments>>) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes('上传文档'))
  await btn!.trigger('click')
  const dialog = wrapper.findComponent(UploadDialog)
  expect(dialog.props('visible')).toBe(true)
  return dialog
}

function inputText(dialog: Awaited<ReturnType<typeof openUpload>>, placeholder: string): string {
  const el = dialog.find(`input[placeholder*="${placeholder}"]`).element as HTMLInputElement
  return el.value
}

describe('列表加载与渲染', () => {
  it('挂载即拉列表（page=1 默认 20 条/页），行渲染标题+版本/部门中文/责任人/生效日期', async () => {
    backend.docs = [
      docItem({
        title: '客服_退货政策',
        version: 3,
        ownerName: '张三',
        effectiveDate: '2026-01-10',
      }),
      docItem({ title: 'HR_报销管理制度', deptCode: 'hr', ownerName: '李丽' }),
    ]
    const wrapper = await mountDocuments()
    expect(listCalls()[0]).toBe('/api/documents?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('客服_退货政策')
    expect(rows[0]!.text()).toContain('v3')
    expect(rows[0]!.text()).toContain('客服')
    expect(rows[0]!.text()).toContain('张三')
    expect(rows[0]!.text()).toContain('2026-01-10')
    expect(rows[1]!.text()).toContain('人力')
  })

  it('空列表展示"还没有文档"空态引导', async () => {
    const wrapper = await mountDocuments()
    expect(wrapper.text()).toContain('还没有文档')
    expect(wrapper.findAll('button').some((b) => b.text().includes('上传第一份文档'))).toBe(true)
  })

  it('状态列渲染矩阵：就绪/排队中/解析中(进度条)/失败(悬浮原因+重试)逐一对应', async () => {
    backend.docs = [
      docItem({ title: 'A就绪', status: 'ready' }),
      docItem({ title: 'B解析中', status: 'parsing' }),
      docItem({ title: 'C失败', status: 'failed', parseError: '解析失败：文档疑似扫描件或空文档' }),
      docItem({ title: 'D排队', status: 'pending' }),
    ]
    const wrapper = await mountDocuments()
    const rows = wrapper.findAll('.el-table__row')
    expect(rows[0]!.text()).toContain('● 就绪')
    expect(rows[1]!.text()).toContain('◐ 解析中')
    expect(rows[1]!.findAll('.el-progress')).toHaveLength(1)
    expect(rows[2]!.text()).toContain('✖ 失败(悬停看原因)')
    expect(rows[2]!.findAll('button').some((b) => b.text() === '重试')).toBe(true)
    const tooltips = wrapper.findAllComponents({ name: 'ElTooltip' })
    expect(
      tooltips.some((t) => String(t.props('content')).includes('文档疑似扫描件或空文档')),
    ).toBe(true)
    expect(rows[3]!.text()).toContain('◔ 排队中')
  })

  it('降级解析标注：degraded 行标题旁渲染灰色"降级解析" tag（字段就绪即生效）', async () => {
    backend.docs = [docItem({ degraded: true })]
    const wrapper = await mountDocuments()
    expect(wrapper.find('.el-table__row').text()).toContain('降级解析')
  })
})

describe('筛选与分页（§22.1）', () => {
  it('关键字+查询：请求携带 keyword 并回第 1 页；重置清空全部条件重新拉取', async () => {
    backend.docs = [docItem()]
    const wrapper = await mountDocuments()
    await wrapper.find('input[placeholder="标题关键字"]').setValue('退货')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '查询')!
      .trigger('click')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('keyword=退货')
    expect(lastListCall()).toContain('page=1')

    await wrapper
      .findAll('button')
      .find((b) => b.text() === '重置')!
      .trigger('click')
    await flushPromises()
    expect(lastListCall()).toBe('/api/documents?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    backend.docs = Array.from({ length: 45 }, (_, i) => docItem({ title: `文档${i}` }))
    const wrapper = await mountDocuments()
    expect(wrapper.text()).toContain('共 45 条')
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')
  })
})

describe('行操作', () => {
  it('待确认行（isActive=false）展示"待确认"标注，确认激活调 confirm 后刷新且标注消失', async () => {
    backend.docs = [docItem({ isActive: false })]
    const wrapper = await mountDocuments()
    expect(wrapper.find('.el-table__row').text()).toContain('待确认')
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '确认激活')!
      .trigger('click')
    await waitFor(() => !wrapper.find('.el-table__row').text().includes('待确认'), '激活后刷新')
    expect(backend.confirmCalls).toEqual([backend.docs[0]!.id])
  })

  it('失败行重试：调 retry 后行回到解析中（进度条出现）', async () => {
    const failed = docItem({ status: 'failed', parseError: '解析失败：超时' })
    backend.docs = [failed]
    const wrapper = await mountDocuments()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '重试')!
      .trigger('click')
    await waitFor(
      () => wrapper.find('.el-table__row').findAll('.el-progress').length === 1,
      '重试后行进入解析中',
    )
    expect(backend.retryCalls).toEqual([failed.id])
  })

  it('删除按钮置灰且 tooltip"暂不支持"；zip 导入按钮已解禁（无 disabled、无"后端任务未就绪" tooltip）', async () => {
    backend.docs = [docItem()]
    const wrapper = await mountDocuments()
    const del = wrapper.findAll('button').find((b) => b.text() === '删除')
    expect(del).toBeDefined()
    expect(del!.attributes('disabled')).toBeDefined()
    // 合并门禁解禁（T10 评审 M-7）：import-zip 后端已合入，按钮可点、置灰 tooltip 摘除
    const zip = wrapper.findAll('button').find((b) => b.text().includes('zip 批量导入'))
    expect(zip).toBeDefined()
    expect(zip!.attributes('disabled')).toBeUndefined()
    const contents = wrapper
      .findAllComponents({ name: 'ElTooltip' })
      .map((t) => String(t.props('content')))
    expect(contents).toContain('暂不支持')
    expect(contents).not.toContain('后端任务未就绪')
  })
})

describe('上传对话框', () => {
  it('.doc 即时拒收提示且无法进入下一步；合法 pdf 按文件名本地预填标题（F-1008 兜底）', async () => {
    const wrapper = await mountDocuments()
    const dialog = await openUpload(wrapper)
    dialog.vm.handleFile({ name: '旧政策.doc', raw: makeFile('旧政策.doc') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').text()).toBe('.doc 不支持，请另存为 docx 后上传')
    const nextBtn = dialog.findAll('button').find((b) => b.text() === '下一步')!
    expect(nextBtn.attributes('disabled')).toBeDefined()

    dialog.vm.handleFile({ name: '项目报告.pdf', raw: makeFile('项目报告.pdf') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').exists()).toBe(false)
    expect(nextBtn.attributes('disabled')).toBeUndefined()
    // 标题输入在第 2 步元数据表单中渲染
    await nextBtn.trigger('click')
    await flushPromises()
    expect(inputText(dialog, '规范命名')).toBe('项目报告')
  })

  it('选中文件后调 metadata-suggest 回填建议值（仅预填；端点失败时静默保留已填值）', async () => {
    backend.suggestion = {
      suggestedTitle: 'HR_报销管理制度',
      suggestedDeptCode: 'hr',
      suggestedEffectiveDate: '2026-04-01',
    }
    const wrapper = await mountDocuments()
    const dialog = await openUpload(wrapper)
    dialog.vm.handleFile({ name: 'x.pdf', raw: makeFile('x.pdf') } as UploadFile)
    await flushPromises()
    await dialog
      .findAll('button')
      .find((b) => b.text() === '下一步')!
      .trigger('click')
    await flushPromises()
    expect(inputText(dialog, '规范命名')).toBe('HR_报销管理制度')
    const eff = dialog.findAll('.el-date-editor input')[0]!
    expect((eff.element as HTMLInputElement).value).toBe('2026-04-01')

    // 端点不可达（T4 未合入 404 / 断网）：静默跳过，不阻塞上传主流程
    fetchMock.mockImplementation((url: string) => {
      if (url === '/api/documents/metadata-suggest') {
        return Promise.reject(new TypeError('Network Error'))
      }
      return routeFetch(url)
    })
    dialog.vm.handleFile({ name: 'y.pdf', raw: makeFile('y.pdf') } as UploadFile)
    await flushPromises()
    // 建议只预填不覆盖已填值：标题保持此前回填值
    expect(inputText(dialog, '规范命名')).toBe('HR_报销管理制度')
  })

  it('必填校验拦截提交（不发上传请求）；填全+勾合规后 multipart 字段齐备，202 后关弹窗回第 1 页刷新', async () => {
    backend.docs = Array.from({ length: 25 }, (_, i) => docItem({ title: `文档${i}` }))
    const wrapper = await mountDocuments()
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')

    backend.suggestion = { suggestedDeptCode: 'hr', suggestedEffectiveDate: '2026-01-01' }
    const dialog = await openUpload(wrapper)
    dialog.vm.handleFile({
      name: '客服_退货政策_20260101.pdf',
      raw: makeFile('客服_退货政策_20260101.pdf'),
    } as UploadFile)
    await flushPromises()
    await dialog
      .findAll('button')
      .find((b) => b.text() === '下一步')!
      .trigger('click')
    await flushPromises()

    // 未填责任人、未勾合规：先被必填校验拦下（无上传请求）
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交入库')!
      .trigger('click')
    await waitFor(() => dialog.text().includes('请输入责任人'), '必填校验错误内联渲染')
    expect(backend.uploadCalls).toHaveLength(0)

    const items = dialog.findAll('.el-form-item')
    await items
      .find((i) => i.text().includes('责任人'))!
      .find('input')
      .setValue('张三')
    await dialog.find('.el-checkbox__original').setValue(true)
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交入库')!
      .trigger('click')
    await flushPromises()

    expect(backend.uploadCalls).toHaveLength(1)
    const form = backend.uploadCalls[0]!
    expect(form.get('title')).toBe('客服_退货政策_20260101')
    expect(form.get('dept')).toBe('hr')
    expect(form.get('owner')).toBe('张三')
    expect(form.get('effectiveDate')).toBe('2026-01-01')
    expect(form.get('expiryDate')).toBeNull()
    expect(form.get('complianceConfirmed')).toBe('true')
    expect(form.get('file')).toBeInstanceOf(File)
    expect(dialog.props('visible')).toBe(false)
    // §22.3：提交后回第 1 页刷新
    expect(lastListCall()).toContain('page=1')
  })
})

describe('api 层 multipart（client.ts FormData 分支）', () => {
  it('上传请求不手工设置 Content-Type（boundary 交由浏览器生成），字段按 api.md §2.5 命名', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(202, 'application/json', {
        code: 0,
        message: 'ok',
        data: { id: 1, status: 'parsing' },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await uploadDocument(makeFile('a.pdf'), {
      title: 't',
      dept: 'hr',
      owner: 'o',
      effectiveDate: '2026-01-01',
      expiryDate: null,
    })
    expect(fetchStub).toHaveBeenCalledTimes(1)
    const [url, init] = fetchStub.mock.calls[0]!
    expect(url).toBe('/api/documents')
    expect(init!.headers).toBeUndefined()
    const form = init!.body as FormData
    expect(form.get('file')).toBeInstanceOf(File)
    expect(form.get('complianceConfirmed')).toBe('true')
  })

  it('suggestDocumentMetadata 走 /api/documents/metadata-suggest 且 FormData 携带文件', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { suggestedTitle: 'T' },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    const result = await suggestDocumentMetadata(makeFile('部门_主题_20260101.pdf'))
    expect(result.suggestedTitle).toBe('T')
    const [url, init] = fetchStub.mock.calls[0]!
    expect(url).toBe('/api/documents/metadata-suggest')
    expect((init!.body as FormData).get('file')).toBeInstanceOf(File)
  })
})

describe('zip 批量导入对话框（T3 契约）', () => {
  it('非 .zip 拒收；导入后逐行渲染四态中文映射与原因、汇总行正确并通知父级刷新', async () => {
    backend.zipSummary = {
      results: [
        { filename: 'a.pdf', status: 'accepted', documentId: 101 },
        {
          filename: 'b.pdf',
          status: 'duplicate',
          documentId: 88,
          reason: '文件与已有文档完全重复',
        },
        { filename: 'c.exe', status: 'rejected', reason: '文件格式不支持' },
        {
          filename: 'd.pdf',
          status: 'skipped_metadata',
          reason: '元数据待补：文件名须为 部门_主题_日期 格式',
        },
      ],
      accepted: 1,
      rejected: 1,
      skipped: 2,
    }
    // 以渲染函数父组件挂载并经 visible false→true 打开（el-dialog 内容懒渲染，
    // 初始即 true 时 watcher 不触发、内容不渲染）；根组件直挂时 Teleport 占位导致 wrapper 查不到内容
    const visible = ref(false)
    const harness = mount(
      defineComponent({ setup: () => () => h(ZipImportDialog, { visible: visible.value }) }),
      { global: { plugins: [[ElementPlus, { locale: zhCn }]] } },
    )
    await flushPromises()
    visible.value = true
    await flushPromises()
    const dialog = harness.findComponent(ZipImportDialog)
    dialog.vm.handleFile({ name: 'x.exe', raw: makeFile('x.exe') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').text()).toBe('仅支持 .zip 压缩包')
    dialog.vm.handleFile({ name: '批次01.zip', raw: makeFile('批次01.zip') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').exists()).toBe(false)
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始导入')!
      .trigger('click')
    await flushPromises()

    const text = dialog.text()
    expect(text).toContain('a.pdf')
    expect(text).toContain('已入库')
    expect(text).toContain('重复跳过')
    expect(text).toContain('已拒绝')
    expect(text).toContain('元数据待补')
    expect(text).toContain('文件与已有文档完全重复')
    expect(text).toContain('共 4 个条目：入库 1 · 拒绝 1 · 跳过 2')
    expect(dialog.emitted('imported')).toHaveLength(1)
  })
})

describe('状态轮询（§8.4）', () => {
  it('存在 parsing 行时每 30s 自动刷新；拉到全终态即停；卸载后清理定时器', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend.docs = [docItem({ status: 'parsing' })]
    const wrapper = await mountDocuments()
    expect(listCalls()).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(listCalls()).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(listCalls()).toHaveLength(3)

    // 后端已全部到终态：下一次轮询拉到终态后拆除定时器，不再请求
    backend.docs = [docItem({ status: 'ready' })]
    await vi.advanceTimersByTimeAsync(30_000)
    expect(listCalls()).toHaveLength(4)
    await vi.advanceTimersByTimeAsync(90_000)
    expect(listCalls()).toHaveLength(4)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(90_000)
    expect(listCalls()).toHaveLength(4)
  })

  it('无 parsing 行时不建立轮询定时器', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend.docs = [docItem({ status: 'ready' })]
    const wrapper = await mountDocuments()
    expect(listCalls()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(90_000)
    expect(listCalls()).toHaveLength(1)
    wrapper.unmount()
  })
})
