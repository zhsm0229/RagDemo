/**
 * 服务健康卡组件测试：overall 三态横幅（up/degraded/down）、行状态 tag 中文映射、
 * 异常行展示 detail 与 runbook 修复指引、not_configured 显示"未启用"、刷新按钮
 * 重拉、加载失败内联错误。探活逻辑在后端（tests/api/test_services_health.py），
 * 此处只锁展示与交互。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getServicesHealth, type ServicesHealthData } from '@/api/overview'
import ServicesHealthPanel from '@/components/admin/ServicesHealthPanel.vue'

vi.mock('@/api/overview', () => ({
  getServicesHealth: vi.fn(),
}))

const mockedGet = vi.mocked(getServicesHealth)

const ALL_UP: ServicesHealthData = {
  overall: 'up' as const,
  services: [
    { key: 'pg', label: 'PostgreSQL', status: 'up', latencyMs: 3, detail: '' },
    {
      key: 'embed',
      label: '向量化 (TEI)',
      status: 'up',
      latencyMs: 12,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'rerank',
      label: '重排 (TEI)',
      status: 'up',
      latencyMs: 12,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'llm',
      label: '大模型 (LLM)',
      status: 'up',
      latencyMs: 30,
      detail: 'TEST_MODE 桩，未探活',
    },
    {
      key: 'mineru',
      label: '文档解析 (MinerU)',
      status: 'not_configured',
      latencyMs: 0,
      detail: '未配置，走 pypdf 降级解析',
    },
  ],
}

function mountPanel() {
  return mount(ServicesHealthPanel, { global: { plugins: [ElementPlus] } })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('服务健康卡', () => {
  it('挂载即拉探活；全绿时横幅"问答链路正常"，行状态中文、not_configured 显示未启用', async () => {
    mockedGet.mockResolvedValue(ALL_UP)
    const wrapper = mountPanel()
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledTimes(1)
    const overall = wrapper.find('[data-test="svc-overall"]')
    expect(overall.text()).toContain('问答链路正常')
    expect(wrapper.find('.el-alert--success').exists()).toBe(true)
    const text = wrapper.text()
    expect(text).toContain('正常')
    expect(text).toContain('未启用')
    expect(text).toContain('12ms')
    // not_configured 行显示启用指引（recover.sh mineru），属预期信息量
    expect(wrapper.find('[data-test="svc-table"]').text()).toContain('recover.sh mineru')
  })

  it('down 时横幅转错误态，异常行展示 detail 与 runbook 修复指引', async () => {
    mockedGet.mockResolvedValue({
      overall: 'down',
      services: [
        { key: 'pg', label: 'PostgreSQL', status: 'up', latencyMs: 3, detail: '' },
        { key: 'embed', label: '向量化 (TEI)', status: 'up', latencyMs: 12, detail: '' },
        {
          key: 'rerank',
          label: '重排 (TEI)',
          status: 'down',
          latencyMs: 2500,
          detail: 'ReadTimeout',
        },
        { key: 'llm', label: '大模型 (LLM)', status: 'up', latencyMs: 30, detail: '' },
        { key: 'mineru', label: '文档解析 (MinerU)', status: 'up', latencyMs: 40, detail: '' },
      ],
    } as ServicesHealthData)
    const wrapper = mountPanel()
    await flushPromises()
    const overall = wrapper.find('[data-test="svc-overall"]')
    expect(overall.text()).toContain('问答链路异常')
    expect(wrapper.find('.el-alert--error').exists()).toBe(true)
    const table = wrapper.find('[data-test="svc-table"]')
    expect(table.text()).toContain('ReadTimeout')
    expect(table.text()).toContain('deploy/recover.sh rerank')
  })

  it('degraded 横幅警示文案明确"问答不受影响"', async () => {
    mockedGet.mockResolvedValue({
      overall: 'degraded',
      services: [
        { key: 'pg', label: 'PostgreSQL', status: 'up', latencyMs: 3, detail: '' },
        { key: 'embed', label: '向量化 (TEI)', status: 'up', latencyMs: 12, detail: '' },
        { key: 'rerank', label: '重排 (TEI)', status: 'up', latencyMs: 12, detail: '' },
        { key: 'llm', label: '大模型 (LLM)', status: 'up', latencyMs: 30, detail: '' },
        {
          key: 'mineru',
          label: '文档解析 (MinerU)',
          status: 'down',
          latencyMs: 2500,
          detail: 'ConnectError',
        },
      ],
    } as ServicesHealthData)
    const wrapper = mountPanel()
    await flushPromises()
    const overall = wrapper.find('[data-test="svc-overall"]')
    expect(overall.text()).toContain('文档解析不可用')
    expect(wrapper.find('.el-alert--warning').exists()).toBe(true)
  })

  it('刷新按钮重拉探活；接口失败显示内联错误不白屏', async () => {
    mockedGet.mockResolvedValue(ALL_UP)
    const wrapper = mountPanel()
    await flushPromises()
    mockedGet.mockRejectedValueOnce(new ApiClientErr())
    await wrapper.find('[data-test="svc-refresh"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="svc-load-err"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('健康探活失败')
  })
})

/** ApiClientError 形状替身（组件只判断 instanceof 决定文案，语义等价即可） */
class ApiClientErr extends Error {
  constructor() {
    super('探活接口 500')
    this.name = 'ApiClientError'
  }
}
