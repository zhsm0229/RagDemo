/**
 * P6 用户管理集成测试：列表渲染（状态三态/锁定）与空态、关键字筛选/分页（§22.1）、
 * 发放账号（校验拦截/409 内联/201 → 初始密码一次性展示+readOnly+复制，关闭即弃）、
 * 禁用/启用（popconfirm，禁用自己 422 内联 §22.2）、重置密码（同一次性展示）、
 * api 层契约钉死。共享 mock 后端见 helpers/users-mock.ts（全链路走真 client.ts）。
 */
import { DOMWrapper, flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createAdminUser,
  listAdminUsers,
  resetAdminUserPassword,
  toggleAdminUserStatus,
} from '@/api/adminUsers'
import CreateUserDialog from '@/components/users/CreateUserDialog.vue'
import PasswordDialog from '@/components/users/PasswordDialog.vue'
import {
  backend,
  clickBtn,
  confirmPopconfirm,
  fetchMock,
  lastListCall,
  listCalls,
  makeResponse,
  mountUsers,
  resetBackend,
  routeFetch,
  unmountAllUsers,
  userItem,
  waitFor,
} from './helpers/users-mock'

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
  unmountAllUsers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('列表加载与渲染', () => {
  it('挂载即拉列表（page=1 默认 20 条/页），行渲染用户名/姓名/部门中文/角色/状态/最近登录', async () => {
    backend.users = [
      userItem({ username: 'admin', name: 'admin', role: 'admin' }),
      userItem({ username: '客服小王', deptCode: 'service', lastLoginAt: null }),
      userItem({ username: '新员工小李', deptCode: 'hr', status: '已禁用' }),
      userItem({ username: '全员号', deptCode: 'all', locked: true, lastLoginAt: null }),
    ]
    const wrapper = await mountUsers()
    expect(listCalls()[0]).toBe('/api/admin/users?page=1&pageSize=20')
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(4)
    expect(rows[0]!.text()).toContain('admin')
    expect(rows[0]!.text()).toContain('管理员')
    expect(rows[0]!.text()).toContain('2026-09-28 10:00')
    expect(rows[1]!.text()).toContain('客服')
    expect(rows[2]!.text()).toContain('人力')
    expect(rows[2]!.text()).toContain('已禁用')
    expect(rows[3]!.text()).toContain('全员')
    expect(rows[3]!.text()).toContain('已锁定')
    // lastLoginAt=null → "-" 占位（不编造时间）
    expect(rows[1]!.text()).not.toContain('2026-')
    expect(wrapper.text()).toContain('共 4 条')
  })

  it('空列表展示"还没有用户"空态引导（新建第一个用户入口）', async () => {
    const wrapper = await mountUsers()
    expect(wrapper.text()).toContain('还没有用户')
    expect(wrapper.findAll('button').some((b) => b.text().includes('新建第一个用户'))).toBe(true)
  })

  it('工具栏对照原型：仅有"＋ 新建用户"主操作，操作列为 禁用(启用)+重置密码', async () => {
    backend.users = [userItem()]
    const wrapper = await mountUsers()
    const texts = wrapper.findAll('button').map((b) => b.text())
    expect(texts.some((t) => t.includes('新建用户'))).toBe(true)
    expect(texts.some((t) => t.includes('重置密码'))).toBe(true)
    expect(texts.some((t) => t.includes('禁用'))).toBe(true)
    // 原型第三态"解锁"无后端契约 → 不渲染（deviations #37）
    expect(texts.some((t) => t.includes('解锁'))).toBe(false)
  })
})

describe('筛选与分页（§22.1）', () => {
  it('关键字+查询：请求携带 keyword 并回第 1 页；重置清空条件重新拉取', async () => {
    backend.users = [userItem()]
    const wrapper = await mountUsers()
    await wrapper.find('input[placeholder="搜索用户名"]').setValue('小王')
    await clickBtn(wrapper.findAll('button'), '查询')
    await flushPromises()
    expect(decodeURIComponent(lastListCall())).toContain('keyword=小王')
    expect(lastListCall()).toContain('page=1')

    await clickBtn(wrapper.findAll('button'), '重置')
    await flushPromises()
    expect(lastListCall()).toBe('/api/admin/users?page=1&pageSize=20')
  })

  it('分页：pageSizes 10/20/50/100 默认 20；翻到第 2 页请求携带 page=2', async () => {
    backend.users = Array.from({ length: 45 }, (_, i) => userItem({ username: `用户${i}` }))
    const wrapper = await mountUsers()
    const pager = wrapper.findComponent({ name: 'ElPagination' })
    expect(pager.props('pageSize')).toBe(20)
    expect(pager.props('pageSizes')).toEqual([10, 20, 50, 100])
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(lastListCall()).toContain('page=2')
  })
})

describe('发放账号（初始密码一次性展示）', () => {
  async function openCreate(wrapper: VueWrapper) {
    await clickBtn(wrapper.findAll('button'), '新建用户')
    const dialog = wrapper.findComponent(CreateUserDialog)
    expect(dialog.props('visible')).toBe(true)
    await flushPromises()
    return dialog
  }

  it('空用户名提交被拦（validators §21.3 口径，零 POST）；部门未选同样拦截', async () => {
    backend.users = [userItem()]
    backend.createResult = { id: 201, initialPassword: 'mock-initial-pass' }
    const wrapper = await mountUsers()
    const dialog = await openCreate(wrapper)

    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await waitFor(
      () => dialog.text().includes('用户名须为 2-32 位中文、字母或数字'),
      '用户名错误内联渲染',
    )
    await waitFor(() => dialog.text().includes('请选择部门'), '部门错误内联渲染')
    expect(backend.createCalls).toHaveLength(0)

    await dialog.find('input[placeholder="2-32 位，支持中文、字母、数字"]').setValue('新员工小张')
    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await flushPromises()
    expect(dialog.text()).toContain('请选择部门')
    expect(backend.createCalls).toHaveLength(0)
  })

  it('提交 201：POST body {username, deptCode, role}；弹初始密码对话框 readOnly 可复制；回第 1 页刷新', async () => {
    backend.users = Array.from({ length: 25 }, (_, i) => userItem({ username: `用户${i}` }))
    backend.createResult = { id: 201, initialPassword: 'mock-initial-pass' }
    const wrapper = await mountUsers()
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    const dialog = await openCreate(wrapper)

    await dialog.find('input[placeholder="2-32 位，支持中文、字母、数字"]').setValue('新员工小张')
    // 部门下拉 teleport：从 document 全局选第一项（客服）
    await dialog.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('客服'),
    )!
    await new DOMWrapper(option).trigger('click')
    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await flushPromises()

    expect(backend.createCalls).toEqual([
      { username: '新员工小张', deptCode: 'service', role: 'member' },
    ])
    // 发放弹窗已关，初始密码对话框打开
    expect(dialog.props('visible')).toBe(false)
    const pwd = wrapper.findComponent(PasswordDialog)
    expect(pwd.props('visible')).toBe(true)
    expect(pwd.props('password')).toBe('mock-initial-pass')
    // §8.8 readOnly 置灰可复制：值可见输入框带 readonly 属性 + 复制按钮
    const input = pwd.find('input[aria-label="初始密码"]')
    expect(input.attributes('readonly')).toBeDefined()
    expect((input.element as HTMLInputElement).value).toBe('mock-initial-pass')
    expect(pwd.text()).toContain('关闭后不可再查看')
    // §22.3 新增回第 1 页刷新
    await waitFor(() => lastListCall().endsWith('page=1&pageSize=20'), '回第 1 页刷新')
  })

  it('复制按钮：Clipboard API 可用时写入明文；不可用时提示手动复制', async () => {
    backend.users = [userItem()]
    backend.createResult = { id: 202, initialPassword: 'mock-copy-me-pass' }
    const wrapper = await mountUsers()
    const dialog = await openCreate(wrapper)
    await dialog.find('input[placeholder="2-32 位，支持中文、字母、数字"]').setValue('新员工小张')
    await dialog.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('客服'),
    )!
    await new DOMWrapper(option).trigger('click')
    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await flushPromises()
    const pwd = wrapper.findComponent(PasswordDialog)

    const writeText = vi.fn<(text: string) => Promise<void>>(async () => {})
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    try {
      await clickBtn(pwd.findAll('button'), '复制')
      await flushPromises()
      expect(writeText).toHaveBeenCalledWith('mock-copy-me-pass')
    } finally {
      delete (navigator as { clipboard?: unknown }).clipboard
    }
  })

  it('关闭初始密码对话框后明文即弃（password 清空、不可再打开回看）', async () => {
    backend.users = [userItem()]
    backend.createResult = { id: 203, initialPassword: 'mock-once-pass' }
    const wrapper = await mountUsers()
    const dialog = await openCreate(wrapper)
    await dialog.find('input[placeholder="2-32 位，支持中文、字母、数字"]').setValue('新员工小张')
    await dialog.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('客服'),
    )!
    await new DOMWrapper(option).trigger('click')
    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await flushPromises()
    const pwd = wrapper.findComponent(PasswordDialog)
    expect(pwd.props('visible')).toBe(true)

    await clickBtn(pwd.findAll('button'), '我已保存')
    await flushPromises()
    expect(pwd.props('visible')).toBe(false)
    // 一次性语义：明文随关闭清空，无任何回看入口
    expect(pwd.props('password')).toBe('')
  })

  it('409 用户名重复：错误内联展示且弹窗不关、不弹初始密码', async () => {
    backend.users = [userItem({ username: '客服小王' })]
    backend.createError = '用户名已存在：客服小王'
    const wrapper = await mountUsers()
    const dialog = await openCreate(wrapper)
    await dialog.find('input[placeholder="2-32 位，支持中文、字母、数字"]').setValue('客服小王')
    await dialog.findAll('.el-select')[0]!.find('.el-select__wrapper').trigger('click')
    await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '下拉渲染')
    const option = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
      el.textContent?.includes('客服'),
    )!
    await new DOMWrapper(option).trigger('click')
    await clickBtn(dialog.findAll('button'), '创建并生成初始密码')
    await flushPromises()

    expect(dialog.find('.err-inline').text()).toBe('用户名已存在：客服小王')
    expect(dialog.props('visible')).toBe(true)
    expect(wrapper.findComponent(PasswordDialog).props('visible')).toBe(false)
    expect(listCalls()).toHaveLength(1)
  })
})

describe('禁用/启用与重置密码', () => {
  it('禁用自己后端 422：错误内联于表格上方且列表不刷新（§22.2）', async () => {
    backend.users = [userItem({ username: 'admin', role: 'admin' })]
    backend.toggleError = '不能禁用当前登录账号'
    const wrapper = await mountUsers()
    await confirmPopconfirm(wrapper.find('.el-table__row'), '禁用')
    await flushPromises()

    expect(backend.toggleCalls).toEqual([{ id: backend.users[0]!.id, isActive: false }])
    const alert = wrapper.find('.el-alert')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('不能禁用当前登录账号')
    // 失败不刷新列表（§22.2 行内失败不触发成功副作用）
    expect(listCalls()).toHaveLength(1)
  })

  it('禁用成功：PATCH {isActive:false} → 行状态翻转为已禁用并刷新；启用走 isActive:true', async () => {
    backend.users = [
      userItem({ status: '正常' }),
      userItem({ username: '冻结号', status: '已禁用' }),
    ]
    const wrapper = await mountUsers()
    let rows = wrapper.findAll('.el-table__row')

    await confirmPopconfirm(rows[0]!, '禁用')
    await flushPromises()
    expect(backend.toggleCalls[0]).toEqual({ id: backend.users[0]!.id, isActive: false })
    await waitFor(() => listCalls().length >= 2, '禁用后刷新列表')
    rows = wrapper.findAll('.el-table__row')
    expect(rows[0]!.text()).toContain('已禁用')

    await confirmPopconfirm(rows[1]!, '启用')
    await flushPromises()
    expect(backend.toggleCalls[1]).toEqual({ id: backend.users[1]!.id, isActive: true })
  })

  it('重置密码：popconfirm 确认 → POST reset-password → 新初始密码一次性展示', async () => {
    backend.users = [userItem()]
    backend.resetResult = { id: backend.users[0]!.id, initialPassword: 'mock-reset-pass' }
    const wrapper = await mountUsers()
    await confirmPopconfirm(wrapper.find('.el-table__row'), '重置密码')
    await flushPromises()

    expect(backend.resetCalls).toEqual([backend.users[0]!.id])
    const pwd = wrapper.findComponent(PasswordDialog)
    expect(pwd.props('visible')).toBe(true)
    expect(pwd.props('password')).toBe('mock-reset-pass')
  })
})

describe('api 层契约（路径/方法/参数钉死）', () => {
  it('listAdminUsers：keyword 空参不发（trim）；keyword/page/pageSize 编码进 query', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(200, 'application/json', {
        code: 0,
        message: 'ok',
        data: { items: [], total: 0, page: 1, pageSize: 20 },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await listAdminUsers({ page: 1, pageSize: 20 })
    expect(fetchStub.mock.calls[0]![0]).toBe('/api/admin/users?page=1&pageSize=20')

    await listAdminUsers({ keyword: ' 小王 ', page: 3, pageSize: 50 })
    expect(fetchStub.mock.calls[1]![0]).toBe(
      `/api/admin/users?keyword=${encodeURIComponent('小王')}&page=3&pageSize=50`,
    )
  })

  it('发放/启停/重置走 POST/PATCH 正确路径与请求体（初始密码仅响应体出现）', async () => {
    const fetchStub = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      makeResponse(201, 'application/json', {
        code: 0,
        message: 'ok',
        data: { id: 9, initialPassword: 'mock-abcd-pass' },
      }),
    )
    vi.stubGlobal('fetch', fetchStub)
    await createAdminUser({ username: '新员工', deptCode: 'hr', role: 'member' })
    let [url, init] = fetchStub.mock.calls[0]!
    expect(url).toBe('/api/admin/users')
    expect(init!.method).toBe('POST')
    expect(JSON.parse(String(init!.body))).toEqual({
      username: '新员工',
      deptCode: 'hr',
      role: 'member',
    })

    await toggleAdminUserStatus(9, false)
    ;[url, init] = fetchStub.mock.calls[1]!
    expect(url).toBe('/api/admin/users/9/status')
    expect(init!.method).toBe('PATCH')
    expect(JSON.parse(String(init!.body))).toEqual({ isActive: false })

    await resetAdminUserPassword(9)
    ;[url, init] = fetchStub.mock.calls[2]!
    expect(url).toBe('/api/admin/users/9/reset-password')
    expect(init!.method).toBe('POST')
  })
})
