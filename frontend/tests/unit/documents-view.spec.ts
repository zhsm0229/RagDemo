/**
 * P3 知识库·文档管理集成测试：列表渲染与状态列矩阵、筛选/分页（§22.1）、
 * 上传对话框（白名单拒收 / 元数据建议回填 F-1008 / 必填校验 / multipart 提交）、
 * 确认激活与失败重试、置灰控件、zip 导入结果表（T3 契约四态）、30s 轮询（§8.4）。
 * 共享 mock 后端见 helpers/documents-mock.ts（全链路走真 client.ts）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import type { MessageBoxData } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UploadFile } from 'element-plus'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { defineComponent, h, ref } from 'vue'
import { suggestDocumentMetadata, uploadDocument } from '@/api/documents'
import UploadDialog from '@/components/documents/UploadDialog.vue'
import ZipImportDialog from '@/components/documents/ZipImportDialog.vue'
import DocumentsView from '@/views/DocumentsView.vue'
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
  ElMessageBox.close() // 复位 MessageBox 单例（队列语义：未关的弹窗会扣住下一个调用）
  // jsdom 过渡事件不触发，关闭动画兜底超时前的残留 DOM 手动清场，防串扰后续用例
  document.body
    .querySelectorAll('.el-overlay, .el-message-box, .el-message')
    .forEach((n) => n.remove())
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
    expect(rows[1]!.text()).toContain('解析中')
    expect(rows[1]!.findAll('.el-progress')).toHaveLength(1)
    expect(rows[2]!.text()).toContain('✖ 失败(悬停看原因)')
    expect(rows[2]!.findAll('button').some((b) => b.text() === '重试')).toBe(true)
    const tooltips = wrapper.findAllComponents({ name: 'ElTooltip' })
    expect(
      tooltips.some((t) => String(t.props('content')).includes('文档疑似扫描件或空文档')),
    ).toBe(true)
    expect(rows[3]!.text()).toContain('◔ 排队中')
  })

  it('解析中行展示真实阶段与百分比进度（无进度值时回退不确定动画）', async () => {
    backend.docs = [
      docItem({ title: 'E向量化', status: 'parsing', parseStage: '向量化中', parseProgress: 72 }),
      docItem({ title: 'F旧数据', status: 'parsing' }),
    ]
    const wrapper = await mountDocuments()
    const rows = wrapper.findAll('.el-table__row')
    expect(rows[0]!.text()).toContain('向量化中')
    const withProgress = rows[0]!.find('.el-progress')
    expect(withProgress.text()).toContain('72')
    // 旧数据无进度值：回退 indeterminate 动画（percentage 恒占位 50，不显示数字）
    expect(rows[1]!.findAll('.el-progress')).toHaveLength(1)
    expect(rows[1]!.text()).toContain('解析中')
  })

  it('失败 tooltip 双轨：友好原因 + 技术详情（parseErrorDetail）拼进 content', async () => {
    backend.docs = [
      docItem({
        title: 'G上游失败',
        status: 'failed',
        parseError: '向量化服务调用失败，请稍后重试',
        parseErrorDetail: 'UpstreamError: tei /embed 传输失败: ReadTimeout',
      }),
    ]
    const wrapper = await mountDocuments()
    const tooltip = wrapper
      .findAllComponents({ name: 'ElTooltip' })
      .find((t) => String(t.props('content')).includes('向量化服务调用失败'))!
    const content = String(tooltip.props('content'))
    expect(content).toContain('向量化服务调用失败')
    expect(content).toContain('UpstreamError: tei /embed 传输失败: ReadTimeout')
    expect(content).toContain('\n') // 双行：友好原因与技术详情分行展示
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

  it('删除按钮解除置灰（F-305 已实现）且无旧 tooltip；zip 导入按钮已解禁（无 disabled、无"后端任务未就绪" tooltip）', async () => {
    backend.docs = [docItem()]
    const wrapper = await mountDocuments()
    const del = wrapper.findAll('button').find((b) => b.text() === '删除')
    expect(del).toBeDefined()
    expect(del!.attributes('disabled')).toBeUndefined()
    // 合并门禁解禁（T10 评审 M-7）：import-zip 后端已合入，按钮可点、置灰 tooltip 摘除
    const zip = wrapper.findAll('button').find((b) => b.text().includes('zip 批量导入'))
    expect(zip).toBeDefined()
    expect(zip!.attributes('disabled')).toBeUndefined()
    const contents = wrapper
      .findAllComponents({ name: 'ElTooltip' })
      .map((t) => String(t.props('content')))
    // 删除功能已实现（F-305，2026-10-06）：tooltip 内容不含"暂不支持"
    expect(contents.every((c) => !c.includes('暂不支持'))).toBe(true)
    expect(contents).not.toContain('后端任务未就绪')
  })

  it('ready 行预览按钮打开预览弹窗（拉 /preview 字节流）；非 ready 行无预览按钮', async () => {
    // jsdom 无 createObjectURL：spyOn 精确替换并恢复（勿 stubGlobal 全量替换 URL）
    const coSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock-preview')
    const roSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    try {
      backend.docs = [docItem(), docItem({ status: 'parsing', parseStage: '解析中' })]
      const wrapper = await mountDocuments()
      const rows = wrapper.findAll('.el-table__row')
      const readyBtn = rows[0]!.findAll('button').find((b) => b.text() === '预览')
      expect(readyBtn).toBeDefined()
      expect(rows[1]!.findAll('button').find((b) => b.text() === '预览')).toBeUndefined()

      await readyBtn!.trigger('click')
      await flushPromises()
      expect(backend.previewCalls).toEqual([backend.docs[0]!.id])
      // 弹窗打开：标题含文档标题 + 常驻"下载原文件"链接 + iframe 已注入
      // （DocPreviewDialog append-to-body：内容挂 document.body，wrapper 内不可见）
      await waitFor(
        () => document.body.querySelector('.preview-frame') !== null,
        '预览 iframe 渲染',
      )
      const dialog = document.body.querySelector('.el-dialog')!
      expect(dialog.textContent).toContain(backend.docs[0]!.title)
      expect(dialog.textContent).toContain('下载原文件')
    } finally {
      coSpy.mockRestore()
      roSpy.mockRestore()
    }
  })
})

describe('上传对话框', () => {
  it('.doc 白名单放行可进下一步（与后端 precheck 同口径），白名单外扩展名仍拒收', async () => {
    const wrapper = await mountDocuments()
    const dialog = await openUpload(wrapper)
    // I6（2026-10-06）：.doc 不再前端特判拒收，走白名单放行，老格式转换由后端兜底
    dialog.vm.handleFile({ name: '旧政策.doc', raw: makeFile('旧政策.doc') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').exists()).toBe(false)
    const nextBtn = dialog.findAll('button').find((b) => b.text() === '下一步')!
    expect(nextBtn.attributes('disabled')).toBeUndefined()

    dialog.vm.handleFile({ name: '木马.exe', raw: makeFile('木马.exe') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').text()).toBe(
      '仅支持 pdf / docx / md / txt / csv / xlsx / pptx / html / doc / ppt / xls 文件',
    )
    expect(nextBtn.attributes('disabled')).toBeDefined()
  })

  it('合法 pdf 按文件名本地预填标题（F-1008 兜底）', async () => {
    const wrapper = await mountDocuments()
    const dialog = await openUpload(wrapper)
    dialog.vm.handleFile({ name: '项目报告.pdf', raw: makeFile('项目报告.pdf') } as UploadFile)
    await flushPromises()
    expect(dialog.find('.err-inline').exists()).toBe(false)
    const nextBtn = dialog.findAll('button').find((b) => b.text() === '下一步')!
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

  it('重开弹窗清空上传区与结果：二次选包可再次导入（limit=1 on-exceed 回归）', async () => {
    backend.zipSummary = {
      results: [{ filename: 'a.pdf', status: 'accepted', documentId: 101 }],
      accepted: 1,
      rejected: 0,
      skipped: 0,
    }
    const visible = ref(false)
    const harness = mount(
      defineComponent({ setup: () => () => h(ZipImportDialog, { visible: visible.value }) }),
      { global: { plugins: [[ElementPlus, { locale: zhCn }]] } },
    )
    await flushPromises()
    visible.value = true
    await flushPromises()
    const dialog = harness.findComponent(ZipImportDialog)
    dialog.vm.handleFile({ name: '批次01.zip', raw: makeFile('批次01.zip') } as UploadFile)
    await flushPromises() // 推进渲染解除"开始导入"的 disabled（file 初始 null，jsdom 下 disabled 按钮 click 不触发）
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始导入')!
      .trigger('click')
    await flushPromises()
    expect(dialog.emitted('imported')).toHaveLength(1)

    // 关闭→重开：watch(visible) 触发 reset（含 el-upload clearFiles）——上次文件/结果不残留
    visible.value = false
    await flushPromises()
    visible.value = true
    await flushPromises()
    // 二次选择（el-upload limit=1 下若内部列表未清，on-change 不再触发、file 不会更新）
    dialog.vm.handleFile({ name: '批次02.zip', raw: makeFile('批次02.zip') } as UploadFile)
    await flushPromises() // 同上：等渲染解除按钮 disabled
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始导入')!
      .trigger('click')
    await flushPromises()
    expect(dialog.emitted('imported')).toHaveLength(2)
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

describe('草稿审核（agent-evolution #05）', () => {
  const pendingDraft = {
    sourceType: 'draft' as const,
    reviewStatus: '待审核' as const,
    status: 'pending' as const,
    isActive: false,
  }

  it('工具栏「草稿审核」入口：点击筛 draft+待审核 并高亮，空态给"暂无待审核草稿"；再点还原全量', async () => {
    backend.docs = [docItem(pendingDraft)]
    const wrapper = await mountDocuments()
    const btn = wrapper.findAll('button').find((b) => b.text().includes('草稿审核'))!
    await btn.trigger('click')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('sourceType=draft')
    expect(decodeURIComponent(lastListCall())).toContain('reviewStatus=待审核')
    expect(btn.classes()).toContain('el-button--primary')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(1) // mock 后端按筛选过滤后仍命中

    // 空态：描述切换为"暂无待审核草稿"，且无上传引导按钮
    backend.docs = []
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '刷新')!
      .trigger('click')
    await waitFor(() => wrapper.text().includes('暂无待审核草稿'), '空态文案切换')
    expect(wrapper.findAll('button').some((b) => b.text().includes('上传第一份文档'))).toBe(false)

    await btn.trigger('click')
    await flushPromises()
    expect(lastListCall()).toBe('/api/documents?page=1&pageSize=20')
    expect(btn.classes()).not.toContain('el-button--primary')
  })

  it('待审核草稿行渲染：草稿+待审核标注齐备，通过/驳回按钮在位；不显示"确认激活"；upload 行无审核按钮', async () => {
    backend.docs = [docItem(pendingDraft), docItem({ isActive: false })]
    const wrapper = await mountDocuments()
    const rows = wrapper.findAll('.el-table__row')
    expect(rows[0]!.text()).toContain('草稿')
    expect(rows[0]!.text()).toContain('待审核')
    const draftBtns = rows[0]!.findAll('button').map((b) => b.text())
    expect(draftBtns).toContain('通过')
    expect(draftBtns).toContain('驳回')
    expect(draftBtns).not.toContain('确认激活') // 激活权在审核流，非敏感待确认语义
    expect(rows[1]!.findAll('button').map((b) => b.text())).not.toContain('通过')
    expect(rows[1]!.findAll('button').map((b) => b.text())).not.toContain('驳回')
  })

  it('通过：confirm 文案含"解析入库管道+记入审计日志"，确认后调 approve 并刷新（行转已通过）', async () => {
    // jsdom 下 MessageBox 过渡永不收尾，DOM 点击驱动不可靠：照 services-health-panel
    // 惯例在边界 mock（组件内交互与 api 接线是本测试的对象，弹窗渲染是 EP 自身职责）
    const confirmSpy = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue('confirm' as MessageBoxData)
    const draft = docItem(pendingDraft)
    backend.docs = [draft]
    const wrapper = await mountDocuments()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '通过')!
      .trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const [message, title] = confirmSpy.mock.calls[0]!
    expect(String(message)).toContain('解析入库管道')
    expect(String(message)).toContain('审计日志')
    expect(String(title)).toContain('草稿审核')
    expect(backend.draftApproveCalls).toEqual([draft.id])
    await waitFor(
      () => wrapper.find('.el-table__row').text().includes('已通过'),
      '通过后行刷新审核状态',
    )
  })

  it('通过弹窗点取消：不发请求，行保持待审核', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('cancel' as MessageBoxData)
    backend.docs = [docItem(pendingDraft)]
    const wrapper = await mountDocuments()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '通过')!
      .trigger('click')
    await flushPromises()
    expect(backend.draftApproveCalls).toHaveLength(0)
    expect(wrapper.find('.el-table__row').text()).toContain('待审核')
  })

  it('驳回：prompt 必填原因（文案含永不入库+审计）；空原因视图内联拦截不发请求；填原因调 reject 且首尾空白被剔除', async () => {
    const promptSpy = vi
      .spyOn(ElMessageBox, 'prompt')
      .mockResolvedValue({ value: '   ', action: 'confirm' } as unknown as MessageBoxData)
    const draft = docItem(pendingDraft)
    backend.docs = [draft]
    const wrapper = await mountDocuments()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '驳回')!
      .trigger('click')
    await flushPromises()
    expect(promptSpy).toHaveBeenCalledTimes(1)
    const [message, title, options] = promptSpy.mock.calls[0]!
    expect(String(message)).toContain('永不入库')
    expect(String(message)).toContain('审计日志')
    expect(String(title)).toContain('草稿审核')
    expect(options?.inputValidator?.('   ')).toBe('驳回原因不能为空') // 弹窗内联校验在位
    // 空白原因：视图内联闸拦截，不发起请求
    expect(backend.draftRejectCalls).toHaveLength(0)

    // 填有效原因（带首尾空白）再次驳回：调 reject 且原因被 trim
    promptSpy.mockResolvedValue({
      value: ' 数据来源未核实 ',
      action: 'confirm',
    } as unknown as MessageBoxData)
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '驳回')!
      .trigger('click')
    await flushPromises()
    expect(backend.draftRejectCalls).toEqual([{ id: draft.id, reason: '数据来源未核实' }])
    await waitFor(
      () => wrapper.find('.el-table__row').text().includes('已驳回'),
      '驳回后行刷新审核状态',
    )
  })

  it('已驳回草稿：危险 tag 且悬停 tooltip 展示驳回原因', async () => {
    backend.docs = [
      docItem({ sourceType: 'draft', reviewStatus: '已驳回', rejectReason: '数据来源未核实' }),
    ]
    const wrapper = await mountDocuments()
    expect(wrapper.find('.el-table__row').text()).toContain('已驳回')
    const tooltip = wrapper
      .findAllComponents({ name: 'ElTooltip' })
      .find((t) => String(t.props('content')).includes('数据来源未核实'))
    expect(tooltip).toBeDefined()
  })
})

describe('去审核入口 query 预填（agent-evolution #08 模板生成落点）', () => {
  /** 带 router 上下文挂载（useRoute 预填依赖 route.query）；直挂无 router 时预填静默跳过 */
  async function mountDocumentsAtQuery(query: Record<string, string>): Promise<VueWrapper> {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/kb/documents', name: 'kb-documents', component: DocumentsView },
        { path: '/', name: 'chat', component: defineComponent({ render: () => null }) },
      ],
    })
    await router.push({ path: '/kb/documents', query })
    await router.isReady()
    const wrapper = mount(defineComponent({ name: 'Harness', setup: () => () => h(RouterView) }), {
      global: { plugins: [[ElementPlus, { locale: zhCn }], router] },
    })
    await flushPromises()
    return wrapper
  }

  it('sourceType/reviewStatus 合法值预填筛选并随首拉生效（#05 同字段口径），与草稿审核快捷态联动', async () => {
    backend.docs = [docItem({ sourceType: 'draft', reviewStatus: '待审核' })]
    const wrapper = await mountDocumentsAtQuery({ sourceType: 'draft', reviewStatus: '待审核' })
    try {
      expect(lastListCall()).toContain('sourceType=draft')
      expect(lastListCall()).toContain(`reviewStatus=${encodeURIComponent('待审核')}`)
      // 与工具栏「草稿审核」同一激活态（isDraftReview 时按钮转 primary）
      const draftBtn = wrapper.findAll('button').find((b) => b.text().includes('草稿审核'))!
      expect(draftBtn.classes()).toContain('el-button--primary')
      expect(wrapper.find('.el-table__row').text()).toContain('待审核')
    } finally {
      wrapper.unmount()
    }
  })

  it('白名单外 query 值忽略，回落全量列表不报错', async () => {
    backend.docs = [docItem()]
    const wrapper = await mountDocumentsAtQuery({ sourceType: 'bogus', reviewStatus: 'hacked' })
    try {
      expect(lastListCall()).not.toContain('sourceType=')
      expect(lastListCall()).not.toContain('reviewStatus=')
    } finally {
      wrapper.unmount()
    }
  })
})
