/**
 * 评测管理面板交互测试（工单 08）：自动评测设置面板（09 票）加载失败禁保存
 * （评审 P1-10 数据破坏路径：表单停在默认值时 PUT 会把线上配置覆写回默认）+
 * 错误 alert 与 schedule-note 口径补句；难例池面板（03 票）忽略终态二次确认
 * （取消不发请求）。数据逻辑在后端（tests/api/test_eval_admin.py），此处只锁
 * 展示与交互，全链路走真 client.ts（mock 全局 fetch）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import type { MessageBoxData } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { HardcaseSeedItem } from '@/api/evalAdmin'
import EvalAutoSettingsPanel from '@/components/eval/EvalAutoSettingsPanel.vue'
import HardcasePoolPanel from '@/components/eval/HardcasePoolPanel.vue'

const fetchMock = vi.fn()

const T0 = '2026-09-28T10:00:00+08:00'

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table 挂载在 CI/慢机上可能超过默认 5s 单测超时
vi.setConfig({ testTimeout: 30_000 })

const autoConfig = {
  enabled: false,
  synthPerSeed: 2,
  docSamplePerDept: 20,
  weeklyMaxQuestions: 100,
  regressionPp: 5,
}

function seedItem(overrides: Partial<HardcaseSeedItem> = {}): HardcaseSeedItem {
  return {
    id: 11,
    sourceType: 'refuse',
    status: 'pending',
    deptCode: 'service',
    questionText: '退货能超 7 天吗？',
    answerSnapshot: null,
    synthCount: 0,
    createdAt: T0,
    ...overrides,
  }
}

const backend = { seeds: [] as HardcaseSeedItem[] }

function makeResponse(status: number, json?: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: { get: () => 'application/json' },
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

function routeFetch(url: string, init: RequestInit = {}): Response {
  const method = init.method ?? 'GET'
  if (url.startsWith('/api/admin/hardcase-seeds?') && method === 'GET') {
    // 深拷贝模拟真实 JSON 传输（同 eval-mock 口径）：同引用二次赋值不触发重渲染
    return okEnvelope({
      items: JSON.parse(JSON.stringify(backend.seeds)) as HardcaseSeedItem[],
      total: backend.seeds.length,
      page: 1,
      pageSize: 20,
    })
  }
  const patch = /^\/api\/admin\/hardcase-seeds\/(\d+)$/.exec(url)
  if (patch !== null && method === 'PATCH') {
    const body = JSON.parse(String(init.body)) as { status: string }
    const row = backend.seeds.find((s) => s.id === Number(patch[1]))
    if (row) row.status = body.status as HardcaseSeedItem['status']
    return okEnvelope({ id: Number(patch[1]), status: body.status })
  }
  if (url === '/api/admin/eval-auto/config' && method === 'GET') return okEnvelope(autoConfig)
  if (url === '/api/admin/eval-auto/config' && method === 'PUT') return okEnvelope(autoConfig)
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

function patchCalls(): { url: string; body: unknown }[] {
  return fetchMock.mock.calls
    .filter((call) => /^\/api\/admin\/hardcase-seeds\/\d+$/.test(String(call[0])))
    .map((call) => ({ url: String(call[0]), body: JSON.parse(String(call[1]?.body ?? '{}')) }))
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  backend.seeds = []
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  // ElMessage 真实实现会往 body 挂 toast，jsdom 过渡不收尾：手动清场防串扰
  document.body
    .querySelectorAll('.el-message, .el-message-box, .el-overlay')
    .forEach((n) => n.remove())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

async function mountAuto(): Promise<VueWrapper> {
  const wrapper = mount(EvalAutoSettingsPanel, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

async function mountHardcase(): Promise<VueWrapper> {
  const wrapper = mount(HardcasePoolPanel, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

describe('自动评测设置面板（工单 08 / P1-10）', () => {
  it('配置加载失败：顶部错误 alert 可见、保存按钮禁用（防把线上配置覆写回默认值）', async () => {
    fetchMock.mockRejectedValue(new TypeError('Network Error'))
    const wrapper = await mountAuto()
    const alert = wrapper.find('[data-test="auto-config-load-err"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('配置加载失败')
    expect(alert.text()).toContain('已禁用保存')
    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('保存配置'))!
    expect(saveBtn.attributes('disabled')).toBeDefined()
    expect(patchCalls()).toHaveLength(0)
  })

  it('配置加载成功：保存按钮可用，PUT 携带全量配置；schedule-note 注明手动按钮不受本页预算约束', async () => {
    const wrapper = await mountAuto()
    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('保存配置'))!
    expect(saveBtn.attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-test="auto-config-load-err"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('「生成变体题」等手动按钮不受本页预算约束')
    await saveBtn.trigger('click')
    await flushPromises()
    const put = fetchMock.mock.calls.find(
      (call) => String(call[0]) === '/api/admin/eval-auto/config' && call[1]?.method === 'PUT',
    )
    expect(put).toBeDefined()
    expect(JSON.parse(String(put![1]!.body))).toEqual(autoConfig)
  })
})

describe('难例池忽略终态确认（工单 08）', () => {
  it('忽略前弹确认框（不可恢复警示）；用户取消时不发 PATCH 请求', async () => {
    const confirmSpy = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockRejectedValue('cancel' as unknown as MessageBoxData)
    backend.seeds = [seedItem()]
    const wrapper = await mountHardcase()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '忽略')!
      .trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    const [message, title, options] = confirmSpy.mock.calls[0]!
    expect(String(message)).toContain('不可恢复')
    expect(String(title)).toContain('忽略')
    expect((options as { type?: string }).type).toBe('warning')
    expect(patchCalls()).toHaveLength(0)
    // 种子保持待合成（终态未落）
    expect(wrapper.find('.el-table__row').text()).toContain('待合成')
  })

  it('用户确认后发 PATCH {status:"ignored"}，提示成功并刷新列表（行转已忽略）', async () => {
    const successSpy = vi.spyOn(ElMessage, 'success')
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as MessageBoxData)
    backend.seeds = [seedItem()]
    const wrapper = await mountHardcase()
    await wrapper
      .find('.el-table__row')
      .findAll('button')
      .find((b) => b.text() === '忽略')!
      .trigger('click')
    await flushPromises()
    expect(patchCalls()).toHaveLength(1)
    expect(patchCalls()[0]!.body).toEqual({ status: 'ignored' })
    expect(successSpy).toHaveBeenCalledWith('已忽略：该种子不再参与合成')
    // 刷新后行转终态：忽略按钮随 v-if 消失
    await flushPromises()
    expect(wrapper.find('.el-table__row').text()).toContain('已忽略')
    expect(
      wrapper
        .find('.el-table__row')
        .findAll('button')
        .some((b) => b.text() === '忽略'),
    ).toBe(false)
  })
})
