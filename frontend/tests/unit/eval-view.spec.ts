/**
 * P9 评测管理集成测试：跑分列表渲染与空态、发起跑分（必填校验/202 异步语义）、
 * 列表与详情轮询（running 期 10s，收口即停；detail 无 pendingCount 键容错——W3
 * 交接契约）、详情抽屉（config 快照/逐题明细/录分三档批量端点）、报告渲染（totals
 * 四键 + byDept/byQtype）、阈值扫描（合并门禁解禁默认启用——deviations #44 置灰态
 * 结束；曲线 7+5 档/推荐值高亮/scope.note/口径注明）、题库 Excel 导入
 * （非 xlsx 拒收/422 错误行/201 成功）、api 层契约钉死。
 * 共享 mock 后端见 helpers/eval-mock.ts（全链路走真 client.ts）。
 */
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UploadFile } from 'element-plus'
import {
  createEvalRun,
  EVAL_QUESTIONS_TEMPLATE_FILENAME,
  EVAL_QUESTIONS_TEMPLATE_URL,
  getEvalReport,
  getEvalRun,
  listEvalRuns,
  runThresholdScan,
  submitJudgments,
} from '@/api/evalAdmin'
import CreateRunDialog from '@/components/eval/CreateRunDialog.vue'
import ImportEvalDialog from '@/components/eval/ImportEvalDialog.vue'
import RecordJudgmentDialog from '@/components/eval/RecordJudgmentDialog.vue'
import RunDetailDrawer from '@/components/eval/RunDetailDrawer.vue'
import {
  backend,
  detailCalls,
  detailItem,
  doneRun,
  fetchMock,
  flushAll,
  listCalls,
  makeFile,
  makeResponse,
  mountEval,
  okEnvelope,
  resetBackend,
  routeFetch,
  runningRun,
  scanFixture,
  unmountAll,
} from './helpers/eval-mock'

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
  unmountAll()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

async function openDrawer(wrapper: VueWrapper, rowIndex = 0) {
  const btn = wrapper
    .findAll('.el-table__row')
    [rowIndex]!.findAll('button')
    .find((b) => b.text() === '报告')!
  await btn.trigger('click')
  const drawer = wrapper.findComponent(RunDetailDrawer)
  expect(drawer.props('visible')).toBe(true)
  await flushAll()
  return drawer
}

/** 轮询等待条件成立（校验错误渲染等跨多轮微任务/宏任务的场景） */
async function waitUntil(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 4000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitUntil timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

describe('跑分列表与发起', () => {
  it('挂载即拉列表（page=1 默认 20），行渲染名称/状态 tag/总数正确（可半分）/准确率/时间', async () => {
    doneRun({ name: '9 月回归', correct: 7.5, accuracy: 0.75 })
    runningRun({ name: '进行中的跑分' })
    const wrapper = await mountEval()
    expect(listCalls()[0]).toBe('/api/admin/eval-runs?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.text()).toContain('进行中的跑分')
    expect(rows[0]!.text()).toContain('进行中')
    expect(rows[1]!.text()).toContain('9 月回归')
    expect(rows[1]!.text()).toContain('已完成')
    expect(rows[1]!.text()).toContain('10/7.5')
    expect(rows[1]!.text()).toContain('75.0%')
    expect(rows[1]!.text()).toContain('2026-09-28 10:00')
  })

  it('发起跑分：名称必填校验拦截不发请求；提交 POST {name, perspectiveDeptCode} 202 后回第 1 页刷新', async () => {
    const wrapper = await mountEval()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '发起跑分')!
      .trigger('click')
    const dialog = wrapper.findComponent(CreateRunDialog)
    expect(dialog.props('visible')).toBe(true)
    // 空名称：§21.3 校验拦截（错误内联），不发 POST
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始跑分')!
      .trigger('click')
    await waitUntil(() => dialog.text().includes('请输入任务名称'), '必填校验错误内联渲染')
    expect(backend.createBody).toBeNull()
    // 视角部门下拉默认第一档 service（选项经 teleport，断言请求体编码即可）
    await dialog.find('input').setValue('回归 9 月')
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始跑分')!
      .trigger('click')
    await flushPromises()
    expect(backend.createBody).toEqual({ name: '回归 9 月', perspectiveDeptCode: 'service' })
    const createCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === '/api/admin/eval-runs' && call[1]?.method === 'POST',
    )
    expect(createCall).toBeDefined()
    expect(dialog.props('visible')).toBe(false)
    // 回第 1 页刷新（§22.3）：新 running 行立即可见
    expect(listCalls()[listCalls().length - 1]).toBe('/api/admin/eval-runs?page=1&pageSize=20')
  })

  it('列表轮询：存在 running 行每 10s 刷新；全部收口 done 后停止', async () => {
    vi.useFakeTimers()
    runningRun()
    const wrapper = await mountEval()
    expect(listCalls()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(listCalls()).toHaveLength(2)
    // 收口：最后一次响应全为 done → 轮询停止
    backend.runs[0]!.status = 'done'
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(listCalls()).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(listCalls()).toHaveLength(3)
    void wrapper
  })
})

describe('详情抽屉（轮询容错/逐题/录分）', () => {
  it('running 期：detail 仅 items 无 pendingCount 键照常渲染（容错 W3 契约），每 10s 轮询详情', async () => {
    vi.useFakeTimers()
    runningRun()
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    expect(detailCalls()).toEqual(['/api/admin/eval-runs/9'])
    expect(drawer.find('[data-test="run-status-running"]').exists()).toBe(true)
    expect(drawer.text()).toContain('已跑 1 / 10 题')
    // running 期响应无 pendingCount 键 → 进度行不出现"待判定"（不造 0；
    // 报告卡内静态"待判定"标签为报告口径，与此无关）
    expect(drawer.find('[data-test="run-progress-9"]').text()).not.toContain('待判定')
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(detailCalls()).toHaveLength(2)
    // 收口 done（detail 补 pendingCount 键）→ 详情停轮询 + 通知列表刷新
    backend.runs[0]!.status = 'done'
    backend.runs[0]!.config = {
      model: 'glm-4-air',
      promptVersion: 'v1.2',
      qaThreshold: 0.85,
      refuseThreshold: 0.55,
      efSearch: 64,
      topK: 20,
      topN: 5,
      temperature: 0.3,
      manifestVersion: 'm7',
    }
    backend.runs[0]!.detail = {
      items: [detailItem({ questionId: 1, autoJudgment: 'correct' })],
      pendingCount: 1,
    }
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(detailCalls()).toHaveLength(3)
    expect(listCalls().length).toBeGreaterThan(1)
    await vi.advanceTimersByTimeAsync(10_000)
    await flushAll()
    expect(detailCalls()).toHaveLength(3)
    void wrapper
  })

  it('done 抽屉：config 快照（模型/提示词/阈值）+ 逐题明细（期望/回答/自动/人工判定）+ 引用折叠', async () => {
    doneRun()
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    expect(drawer.find('[data-test="run-status-done"]').exists()).toBe(true)
    expect(drawer.find('[data-test="run-config"]').text()).toContain('glm-4-air')
    expect(drawer.find('[data-test="run-config"]').text()).toContain('v1.2')
    expect(drawer.find('[data-test="run-config"]').text()).toContain('0.55')
    expect(drawer.text()).toContain('待判定 2')
    const card = drawer.find('[data-test="eval-item-1"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('7 个工作日')
    // 回答为只读 textarea（§8.8）：内容在 .value，不在 textContent
    const answer = card.find('textarea')
    expect(answer.exists()).toBe(true)
    expect((answer.element as HTMLTextAreaElement).value).toBe('退货周期为 7 个工作日。')
    expect(card.text()).toContain('自动：正确')
    expect(card.text()).toContain('命中引用（1 条）')
    const pending = drawer.find('[data-test="eval-item-2"]')
    expect(pending.text()).toContain('自动：待判定')
  })

  it('录分：三档单选（correct/partial/wrong）走批量端点单题提交，成功后明细即时刷新', async () => {
    doneRun()
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    await drawer
      .find('[data-test="eval-item-1"]')
      .findAll('button')
      .find((b) => b.text() === '评判')!
      .trigger('click')
    const dialog = drawer.findComponent(RecordJudgmentDialog)
    expect(dialog.props('visible')).toBe(true)
    expect(dialog.text()).toContain('人工评判录分')
    const radios = dialog.findAll('.el-radio-button')
    expect(radios).toHaveLength(3)
    // 默认"正确"；改选"部分正确"（partial）
    await radios[1]!.find('input')!.setValue('partial')
    await dialog
      .findAll('button')
      .find((b) => b.text() === '提交判定')!
      .trigger('click')
    await flushAll()
    expect(backend.judgmentCalls).toEqual([
      { runId: 9, items: [{ questionId: 1, judgment: 'partial' }] },
    ])
    // 录分后重拉明细：人工判定 tag 出现，pendingCount 即时更新（2 → 1）
    await flushAll()
    expect(drawer.find('[data-test="eval-item-1"]').text()).toContain('人工：部分正确')
    expect(drawer.text()).toContain('待判定 1')
  })
})

describe('报告与阈值扫描', () => {
  it('报告渲染：totals 四键卡 + byDept/byQtype 两表；running 态注明部分口径', async () => {
    doneRun()
    backend.report = {
      config: null,
      status: 'done',
      totals: { total: 10, correct: 8, accuracy: 0.8, pendingCount: 2 },
      byDept: [
        { deptCode: 'service', total: 8, accuracy: 0.875 },
        { deptCode: 'all', total: 2, accuracy: 0.5 },
      ],
      byQtype: [{ qtype: 'factual', total: 10, accuracy: 0.8 }],
    }
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    expect(drawer.text()).toContain('总题数')
    expect(drawer.text()).toContain('加权得分')
    expect(drawer.text()).toContain('80.0%')
    expect(drawer.text()).toContain('客服')
    expect(drawer.text()).toContain('87.5%')
    expect(drawer.text()).toContain('事实型')
    expect(drawer.find('[data-test="report-partial-note"]').exists()).toBe(false)
    void drawer
  })

  it('阈值扫描入口已解禁（合并门禁 T13 #44）：按钮可点，点击即发起扫描请求', async () => {
    doneRun()
    backend.scan = scanFixture()
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    const btn = drawer.find('[data-test="scan-btn"]')
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    await flushAll()
    expect(backend.scanCalls).toEqual([9])
  })

  it('扫描结果渲染：POST 曲线数据 → 拒答 7 档/qa 5 档表格、推荐值高亮、scope.note 与口径注明', async () => {
    doneRun()
    backend.scan = scanFixture()
    const wrapper = await mountEval()
    const drawer = await openDrawer(wrapper)
    await drawer.find('[data-test="scan-btn"]').trigger('click')
    await flushAll()
    expect(backend.scanCalls).toEqual([9])
    expect(drawer.findAll('[data-test="refuse-curve"] .el-table__row')).toHaveLength(7)
    expect(drawer.findAll('[data-test="qa-curve"] .el-table__row')).toHaveLength(5)
    // 推荐值展示 + 高亮"0.65"档（排除 refuseRate=1.0 的 0.70 全拒答档）
    expect(drawer.text()).toContain('拒答阈值 0.65 / 问答对阈值 0.75')
    const recommended = drawer.find('[data-test="refuse-curve"] .recommended')
    expect(recommended.text()).toBe('0.65')
    // T5 评审裁决：曲线 accuracy ≠ 报告 accuracy 口径注明 + scope.note 原样展示
    expect(drawer.text()).toContain('≠ 报告 accuracy')
    expect(drawer.text()).toContain('档位敏感度')
  })
})

describe('题库 Excel 导入（F-601）', () => {
  async function openImport(wrapper: VueWrapper) {
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Excel 导入题目')!
      .trigger('click')
    const dialog = wrapper.findComponent(ImportEvalDialog)
    expect(dialog.props('visible')).toBe(true)
    return dialog
  }

  it('非 .xlsx 拒收（文件级白名单）；422 错误行整批退回表格渲染', async () => {
    doneRun() // 占位避免空态干扰按钮定位
    const wrapper = await mountEval()
    const dialog = await openImport(wrapper)
    dialog.vm.handleFile({ name: '题库.pdf', raw: makeFile('题库.pdf') } as UploadFile)
    await flushPromises()
    expect(dialog.text()).toContain('仅支持 .xlsx 文件')
    dialog.vm.handleFile({ name: '题库.xlsx', raw: makeFile('题库.xlsx') } as UploadFile)
    // 等一次渲染：按钮 :disabled 随 file 状态翻转（pdf 拒收后 file 置 null，DOM 待更新）
    await flushPromises()
    backend.importErrors = [{ row: 2, field: '部门', reason: '部门须为 客服/人力/全员 之一' }]
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始导入')!
      .trigger('click')
    await flushPromises()
    expect(dialog.text()).toContain('已整批退回，未入库')
    const rows = dialog.findAll('.el-table__row')
    expect(rows).toHaveLength(1)
    expect(rows[0]!.text()).toContain('2')
    expect(rows[0]!.text()).toContain('部门')
    expect(rows[0]!.text()).toContain('客服/人力/全员')
    // 失败弹窗不关（§22.2）
    expect(dialog.props('visible')).toBe(true)
  })

  it('201 成功：提示导入条数并 emit imported', async () => {
    doneRun()
    const wrapper = await mountEval()
    const dialog = await openImport(wrapper)
    dialog.vm.handleFile({ name: '题库.xlsx', raw: makeFile('题库.xlsx') } as UploadFile)
    await flushPromises()
    await dialog
      .findAll('button')
      .find((b) => b.text() === '开始导入')!
      .trigger('click')
    await flushPromises()
    expect(dialog.text()).toContain('导入完成：本批 12 条评测题已入库')
    expect(dialog.emitted('imported')).toEqual([[{ imported: 12 }]])
  })

  it('模板下载：点击按钮以 <a download> 直链拉取 xlsx 模板（同源 Cookie 随请求携带）', async () => {
    doneRun()
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = await mountEval()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === '下载模板')!
      .trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    const anchor = clickSpy.mock.instances[0] as HTMLAnchorElement
    expect(anchor.href).toContain(EVAL_QUESTIONS_TEMPLATE_URL)
    expect(anchor.download).toBe(EVAL_QUESTIONS_TEMPLATE_FILENAME)
  })
})

describe('api 层契约（路径/方法/参数钉死）', () => {
  it('评测端点路径与请求体编码', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      okEnvelope({
        id: 1,
        name: 'x',
        status: 'done',
        total: 0,
        correct: 0,
        accuracy: 0,
        createdAt: '',
        config: null,
        detail: null,
        pendingCount: 0,
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listEvalRuns({ page: 2, pageSize: 50 })
    expect(String(fetchStub.mock.calls[0]![0])).toBe('/api/admin/eval-runs?page=2&pageSize=50')
    await getEvalRun(7)
    expect(String(fetchStub.mock.calls[1]![0])).toBe('/api/admin/eval-runs/7')
    await createEvalRun({ name: '回归', perspectiveDeptCode: 'hr' })
    expect(String(fetchStub.mock.calls[2]![0])).toBe('/api/admin/eval-runs')
    expect(fetchStub.mock.calls[2]![1]!.method).toBe('POST')
    expect(JSON.parse(String(fetchStub.mock.calls[2]![1]!.body))).toEqual({
      name: '回归',
      perspectiveDeptCode: 'hr',
    })
    await submitJudgments(7, [{ questionId: 1, judgment: 'correct' }])
    expect(String(fetchStub.mock.calls[3]![0])).toBe('/api/admin/eval-runs/7/judgments')
    await getEvalReport(7)
    expect(String(fetchStub.mock.calls[4]![0])).toBe('/api/admin/eval-runs/7/report')
    await runThresholdScan(7)
    expect(String(fetchStub.mock.calls[5]![0])).toBe('/api/admin/eval-runs/7/threshold-scan')
    expect(fetchStub.mock.calls[5]![1]!.method).toBe('POST')
  })

  it('导入错误行走统一信封 data（silent，弹窗内联展示）', async () => {
    const { importEvalQuestions } = await import('@/api/evalAdmin')
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(422, 'application/json', {
        code: 1003,
        message: '导入文件存在 1 处错误，已整批退回，未入库',
        data: [{ row: 2, field: '题型', reason: '题型枚举非法' }],
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await expect(importEvalQuestions(makeFile('q.xlsx'))).rejects.toMatchObject({
      code: 1003,
      data: [{ row: 2, field: '题型', reason: '题型枚举非法' }],
    })
    expect(String(fetchStub.mock.calls[0]![0])).toBe('/api/admin/eval-questions/import')
  })
})
