/**
 * P4 知识库·问答对管理集成测试：列表渲染与空态、筛选/分页（§22.1）、行内启停
 * switch（F-402 即时生效+失败回滚）、编辑对话框（回填/必填校验/PUT/422 内联）、
 * 删除二次确认（popconfirm + 删空回退一页 §22.3）、模板下载直链、Excel 导入
 * （非 xlsx 拒收 / 201 成功态 / 422 错误行全量 §21.6）、api 层契约钉死。
 * 共享 mock 后端见 helpers/qa-pairs-mock.ts（全链路走真 client.ts）。
 */
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ElMessageBox } from 'element-plus'
import type { MessageBoxData } from 'element-plus'
import type { UploadFile } from 'element-plus'
import {
  deleteQaPair,
  listQaPairs,
  toggleQaPairStatus,
  QA_PAIRS_TEMPLATE_FILENAME,
  QA_PAIRS_TEMPLATE_URL,
} from '@/api/qaPairs'
import EditQaDialog from '@/components/qa/EditQaDialog.vue'
import ImportQaDialog from '@/components/qa/ImportQaDialog.vue'
import {
  backend,
  fetchMock,
  lastListCall,
  listCalls,
  makeFile,
  makeResponse,
  mountQaPairs,
  qaItem,
  resetBackend,
  routeFetch,
  unmountAllQaPairs,
  waitFor,
} from './helpers/qa-pairs-mock'

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
  unmountAllQaPairs()
  ElMessageBox.close() // 复位 MessageBox 单例（队列语义：未关的弹窗会扣住下一个调用）
  document.body
    .querySelectorAll('.el-overlay, .el-message-box, .el-message')
    .forEach((n) => n.remove())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

async function openImport(wrapper: VueWrapper) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes('Excel 批量导入'))
  await btn!.trigger('click')
  const dialog = wrapper.findComponent(ImportQaDialog)
  expect(dialog.props('visible')).toBe(true)
  return dialog
}

async function openEdit(wrapper: VueWrapper, rowIndex = 0) {
  const row = wrapper.findAll('.el-table__row')[rowIndex]!
  const btn = row.findAll('button').find((b) => b.text() === '编辑')
  await btn!.trigger('click')
  const dialog = wrapper.findComponent(EditQaDialog)
  expect(dialog.props('visible')).toBe(true)
  await flushPromises()
  return dialog
}

/** 在按钮集合中按文案模糊查找并点击（wrapper 与 dialog 的 findAll 结果均适用） */
async function clickBtn(
  buttons: { text: () => string; trigger: (event: string) => Promise<void> }[],
  label: string,
): Promise<void> {
  const btn = buttons.find((b) => b.text().includes(label))
  await btn!.trigger('click')
}

describe('列表加载与渲染', () => {
  it('挂载即拉列表（page=1 默认 20 条/页），行渲染问题/答案/部门中文/创建时间/批次/启停开关', async () => {
    backend.qaPairs = [
      qaItem({
        question: '退货流程是什么？',
        answer: '质量问题退货：联系客服登记 → 寄回商品 → 验货后退款。',
        deptId: 99, // 列渲染不再依赖 deptId（原"部门#N"兜底口径翻转）
        deptCode: 'all',
        createdAt: '2026-09-28T10:00:00+08:00',
        batchNo: 'QA-20260901-0001',
        status: 'enabled',
      }),
      qaItem({ question: '年假怎么算？', deptCode: 'hr', status: 'disabled' }),
    ]
    const wrapper = await mountQaPairs()
    expect(listCalls()[0]).toBe('/api/admin/qa-pairs?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('退货流程是什么？')
    expect(rows[0]!.text()).toContain('联系客服登记')
    expect(rows[0]!.text()).toContain('全员') // 真实 deptCode 渲染
    expect(rows[0]!.text()).not.toContain('部门#99')
    expect(rows[0]!.text()).toContain('2026-09-28 10:00')
    expect(rows[0]!.text()).toContain('QA-20260901-0001')
    expect(rows[0]!.find('.el-switch').classes()).toContain('is-checked')
    expect(rows[1]!.text()).toContain('人力')
    expect(rows[1]!.find('.el-switch').classes()).not.toContain('is-checked')
  })

  it('空列表展示"还没有问答对"空态引导（Excel 批量导入入口）', async () => {
    const wrapper = await mountQaPairs()
    expect(wrapper.text()).toContain('还没有问答对')
    expect(wrapper.findAll('button').some((b) => b.text().includes('Excel 批量导入'))).toBe(true)
  })

  it('筛选无结果空态：显示"没有匹配筛选条件的问答对"+清除筛选，回真空态（B9）', async () => {
    // 全程 0 行数据：真空态 → 筛选无结果 → 清除筛选回真空态
    const wrapper = await mountQaPairs()
    expect(wrapper.text()).toContain('还没有问答对')
    await wrapper.find('input[placeholder="问题关键字"]').setValue('不存在的关键字')
    await clickBtn(wrapper.findAll('button'), '查询')
    await flushPromises()
    expect(wrapper.find('[data-test="qa-empty-filtered"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('没有匹配筛选条件的问答对')
    expect(wrapper.text()).not.toContain('还没有问答对')
    // 清除筛选 → 回真空态分支
    await wrapper.find('[data-test="qa-empty-filtered"] button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('还没有问答对')
    expect(lastListCall()).toBe('/api/admin/qa-pairs?page=1&pageSize=20')
  })

  it('工具栏对照原型：仅有 下载导入模板/Excel 批量导入，无"新建问答对"按钮（后端无单条新建端点）', async () => {
    backend.qaPairs = [qaItem()]
    const wrapper = await mountQaPairs()
    const texts = wrapper.findAll('button').map((b) => b.text())
    expect(texts.some((t) => t.includes('下载导入模板'))).toBe(true)
    expect(texts.some((t) => t.includes('Excel 批量导入'))).toBe(true)
    expect(texts.some((t) => t.includes('新建'))).toBe(false)
  })
})

describe('筛选与分页（§22.1）', () => {
  it('关键字+查询：请求携带 keyword 并回第 1 页；重置清空全部条件重新拉取', async () => {
    backend.qaPairs = [qaItem()]
    const wrapper = await mountQaPairs()
    await wrapper.find('input[placeholder="问题关键字"]').setValue('退货')
    await clickBtn(wrapper.findAll('button'), '查询')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('keyword=退货')
    expect(lastListCall()).toContain('page=1')

    await clickBtn(wrapper.findAll('button'), '重置')
    await flushPromises()
    expect(lastListCall()).toBe('/api/admin/qa-pairs?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    backend.qaPairs = Array.from({ length: 45 }, (_, i) => qaItem({ question: `问答${i}` }))
    const wrapper = await mountQaPairs()
    expect(wrapper.text()).toContain('共 45 条')
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')
  })
})

describe('行内启停（F-402 开关即刷）', () => {
  it('switch 切换 → PATCH status=disabled → 刷新列表；行状态与库态一致', async () => {
    backend.qaPairs = [qaItem({ status: 'enabled' })]
    const wrapper = await mountQaPairs()
    await wrapper.find('.el-table__row .el-switch').trigger('click')
    await flushPromises()
    expect(backend.toggleCalls).toEqual([{ id: backend.qaPairs[0]!.id, status: 'disabled' }])
    await waitFor(() => listCalls().length >= 2, '启停后刷新列表')
    expect(wrapper.find('.el-table__row .el-switch').classes()).not.toContain('is-checked')
  })

  it('PATCH 失败回滚开关（行状态保持原值，不发刷新请求）', async () => {
    backend.qaPairs = [qaItem({ status: 'enabled' })]
    backend.mutationError = '问答对不存在或已软删'
    const wrapper = await mountQaPairs()
    await wrapper.find('.el-table__row .el-switch').trigger('click')
    await flushPromises()
    expect(backend.toggleCalls).toHaveLength(1)
    expect(wrapper.find('.el-table__row .el-switch').classes()).toContain('is-checked')
    expect(listCalls()).toHaveLength(1)
  })
})

describe('编辑对话框', () => {
  it('打开回填问题/答案；清空问题提交被拦（无 PUT 请求）；填回后 PUT 全文并刷新列表', async () => {
    backend.qaPairs = [qaItem({ question: '原问题', answer: '原答案' })]
    const wrapper = await mountQaPairs()
    const dialog = await openEdit(wrapper)
    const questionInput = dialog.findAll('.el-form-item')[0]!.find('input')
    expect((questionInput.element as HTMLInputElement).value).toBe('原问题')
    expect((dialog.find('textarea').element as HTMLTextAreaElement).value).toBe('原答案')

    await questionInput.setValue('')
    await clickBtn(dialog.findAll('button'), '保存')
    await waitFor(() => dialog.text().includes('请输入问题'), '必填校验错误内联渲染')
    expect(backend.updateCalls).toHaveLength(0)

    await questionInput.setValue('退货流程是什么？（修订）')
    await clickBtn(dialog.findAll('button'), '保存')
    await flushPromises()
    expect(backend.updateCalls).toEqual([
      { question: '退货流程是什么？（修订）', answer: '原答案' },
    ])
    expect(dialog.props('visible')).toBe(false)
    await waitFor(() => listCalls().length >= 2, '编辑后刷新列表')
    expect(wrapper.find('.el-table__row').text()).toContain('退货流程是什么？（修订）')
  })

  it('PUT 422 校验失败：错误内联展示且弹窗不关', async () => {
    backend.qaPairs = [qaItem()]
    backend.mutationError = '问题长度不能超过 2000 字'
    const wrapper = await mountQaPairs()
    const dialog = await openEdit(wrapper)
    await clickBtn(dialog.findAll('button'), '保存')
    await waitFor(() => dialog.find('.err-inline').exists(), '后端错误内联渲染')
    expect(dialog.find('.err-inline').text()).toBe('问题长度不能超过 2000 字')
    expect(dialog.props('visible')).toBe(true)
  })
})

describe('删除（ElMessageBox 二次确认，M33）', () => {
  it('确认后 DELETE → 行消失；删空当前页回退第 1 页（§22.3）；文案对齐 DocumentsView 审计口径', async () => {
    // jsdom 下 MessageBox 过渡永不收尾：照 documents-view 惯例在边界 mock（弹窗渲染是 EP 自身职责）
    const confirmSpy = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue('confirm' as MessageBoxData)
    // 21 条 → 第 2 页只剩 1 条；删除该条应回退到第 1 页
    backend.qaPairs = Array.from({ length: 21 }, (_, i) => qaItem({ question: `问答${i}` }))
    const lastId = backend.qaPairs[backend.qaPairs.length - 1]!.id
    const wrapper = await mountQaPairs()
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1)

    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '删除')!
      .trigger('click')
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const [message, title] = confirmSpy.mock.calls[0]!
    expect(String(message)).toContain('软删保留审计')
    expect(String(message)).toContain('问答20')
    expect(String(title)).toBe('删除问答对')
    expect(backend.deleteCalls).toEqual([lastId])
    await waitFor(() => lastListCall().includes('page=1'), '删空回退第 1 页')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(20)
  })

  it('取消确认不发 DELETE', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('cancel' as MessageBoxData)
    backend.qaPairs = [qaItem()]
    const wrapper = await mountQaPairs()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '删除')!
      .trigger('click')
    await flushPromises()
    expect(backend.deleteCalls).toHaveLength(0)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1)
  })
})

describe('模板下载（F-403）', () => {
  it('点击按钮以 <a download> 直链拉取 xlsx 模板（同源 Cookie 随请求携带）', async () => {
    backend.qaPairs = [qaItem()]
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = await mountQaPairs()
    await clickBtn(wrapper.findAll('button'), '下载导入模板')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    const anchor = clickSpy.mock.instances[0] as HTMLAnchorElement
    expect(anchor.href).toContain(QA_PAIRS_TEMPLATE_URL)
    expect(anchor.download).toBe(QA_PAIRS_TEMPLATE_FILENAME)
  })
})

describe('Excel 导入对话框（F-401 整批校验）', () => {
  it('非 .xlsx 即时拒收且无法提交', async () => {
    backend.qaPairs = [qaItem()]
    const wrapper = await mountQaPairs()
    const dialog = await openImport(wrapper)
    dialog.vm.handleFile({ name: '问答对.xls', raw: makeFile('问答对.xls') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').text()).toBe('仅支持 .xlsx 文件')
    const submitBtn = dialog.findAll('button').find((b) => b.text().includes('开始导入'))!
    expect(submitBtn.attributes('disabled')).toBeDefined()
  })

  it('全合格 201：multipart 携带 file+deptCode=all；成功态展示 imported+批次号；父级回第 1 页刷新', async () => {
    backend.qaPairs = Array.from({ length: 30 }, (_, i) => qaItem({ question: `问答${i}` }))
    backend.importResult = { imported: 118, batchNo: 'QA-20260928-0001' }
    const wrapper = await mountQaPairs()
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    const dialog = await openImport(wrapper)
    dialog.vm.handleFile({ name: 'qa_pairs.xlsx', raw: makeFile('qa_pairs.xlsx') } as UploadFile)
    await flushPromises()
    await clickBtn(dialog.findAll('button'), '开始导入')
    await flushPromises()

    expect(backend.importCalls).toHaveLength(1)
    const form = backend.importCalls[0]!
    expect(form.get('file')).toBeInstanceOf(File)
    expect(form.get('deptCode')).toBe('all')
    const text = dialog.text()
    expect(text).toContain('导入完成')
    expect(text).toContain('118')
    expect(text).toContain('QA-20260928-0001')
    expect(dialog.emitted('imported')).toHaveLength(1)
    // §22.3：导入成功后父级回第 1 页刷新（此前在第 2 页）
    expect(lastListCall()).toContain('page=1')
  })

  it('422 错误行整批退回：alert 展示"未入库"统一文案 + 错误表格行号/字段/原因全量渲染；弹窗不关、不发 imported', async () => {
    backend.qaPairs = [qaItem()]
    backend.importError = {
      message: '导入文件存在 2 处错误，已整批退回，未入库',
      rows: [
        { row: 3, field: 'answer', reason: '答案列为空（必填）' },
        { row: 7, field: 'answer', reason: '答案超过 10000 字上限' },
      ],
    }
    const wrapper = await mountQaPairs()
    const dialog = await openImport(wrapper)
    dialog.vm.handleFile({ name: 'bad.xlsx', raw: makeFile('bad.xlsx') } as UploadFile)
    await flushPromises()
    await clickBtn(dialog.findAll('button'), '开始导入')
    await flushPromises()

    const text = dialog.text()
    expect(text).toContain('导入文件存在 2 处错误，已整批退回，未入库')
    expect(text).toContain('3')
    expect(text).toContain('7')
    expect(text).toContain('answer')
    expect(text).toContain('答案列为空（必填）')
    expect(text).toContain('答案超过 10000 字上限')
    // 弹窗保持打开（§22.2），提交按钮变为"重新导入"可修正后重试
    expect(dialog.props('visible')).toBe(true)
    expect(dialog.findAll('button').some((b) => b.text().includes('重新导入'))).toBe(true)
    expect(dialog.emitted('imported')).toBeUndefined()
    expect(listCalls()).toHaveLength(1)
  })
})

describe('api 层契约（路径/方法/参数钉死）', () => {
  it('listQaPairs：空参不发；deptId/status/keyword/page/pageSize 编码进 query', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { items: [], total: 0, page: 1, pageSize: 20 },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listQaPairs({ page: 1, pageSize: 20 })
    expect(fetchStub.mock.calls[0]![0]).toBe('/api/admin/qa-pairs?page=1&pageSize=20')

    await listQaPairs({ deptId: 2, status: 'disabled', keyword: ' 退货 ', page: 3, pageSize: 50 })
    const [url] = fetchStub.mock.calls[1]!
    expect(String(url)).toBe(
      `/api/admin/qa-pairs?deptId=2&status=disabled&keyword=${encodeURIComponent('退货')}&page=3&pageSize=50`,
    )
  })

  it('启停/删除走 PATCH/DELETE 正确路径与请求体', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', { code: 0, message: 'ok', data: {} }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await toggleQaPairStatus(7, 'disabled')
    let [url, init] = fetchStub.mock.calls[0]!
    expect(url).toBe('/api/admin/qa-pairs/7/status')
    expect(init!.method).toBe('PATCH')
    expect(JSON.parse(String(init!.body))).toEqual({ status: 'disabled' })

    await deleteQaPair(7)
    ;[url, init] = fetchStub.mock.calls[1]!
    expect(url).toBe('/api/admin/qa-pairs/7')
    expect(init!.method).toBe('DELETE')
  })
})

// ---- 加载失败错误态（工单 08：失败 ≠ 没有数据，错误态先于空态呈现"点击重试"） ----

describe('加载失败错误态（工单 08）', () => {
  it('加载失败空态显示"加载失败，点击重试"而非"还没有问答对"，重试成功恢复列表', async () => {
    backend.qaPairs = [qaItem({ question: '恢复用问答对' })]
    fetchMock.mockImplementation((url: string, init: RequestInit = {}) => {
      if (String(url).startsWith('/api/admin/qa-pairs?') && (init.method ?? 'GET') === 'GET') {
        return Promise.reject(new TypeError('Network Error'))
      }
      return routeFetch(String(url), init)
    })
    const wrapper = await mountQaPairs()
    expect(wrapper.find('[data-test="qa-pairs-load-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载失败，点击重试')
    expect(wrapper.text()).not.toContain('还没有问答对') // 失败 ≠ 没有数据
    expect(wrapper.text()).not.toContain('没有匹配筛选条件的问答对')
    // 后端恢复后点重试：列表恢复、错误态退场
    fetchMock.mockImplementation(routeFetch)
    await wrapper.find('[data-test="qa-pairs-load-error"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="qa-pairs-load-error"]').exists()).toBe(false)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1)
    expect(wrapper.findAll('.el-table__row')[0]!.text()).toContain('恢复用问答对')
  })
})
