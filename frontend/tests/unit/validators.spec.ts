/**
 * 统一校验器规则表测试：边界值逐一断言（口径对齐 standards §21.5 与后端约束）。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  checkConfirm,
  checkDateRange,
  checkPassword,
  checkPageSize,
  checkQuestion,
  checkRequiredText,
  checkUsername,
  confirmValidator,
  questionValidator,
  requiredPick,
  requiredText,
  requiredTextValidator,
} from '@/validators'

describe('提问校验（strip 后 1-2000 字）', () => {
  it('1 字与 2000 字通过，2001 字拒绝', () => {
    expect(checkQuestion('问')).toBe(true)
    expect(checkQuestion('问'.repeat(2000))).toBe(true)
    expect(checkQuestion('问'.repeat(2001))).toBe('问题长度须在 1-2000 字之间')
  })

  it('空串与纯空白拒绝（strip 口径）', () => {
    expect(checkQuestion('')).toBe('问题不能为空')
    expect(checkQuestion('   ')).toBe('问题不能为空')
    expect(checkQuestion(' 报销标准 ')).toBe(true)
  })

  it('非字符串输入拒绝（防御表单脏值）', () => {
    expect(checkQuestion(null)).toBe('问题不能为空')
    expect(checkQuestion(123)).toBe('问题不能为空')
  })
})

describe('用户名校验（2-32 位中文/字母/数字）', () => {
  it('中文、字母数字组合、边界长度 2 与 32 通过', () => {
    expect(checkUsername('客服小王')).toBe(true)
    expect(checkUsername('xiaowang01')).toBe(true)
    expect(checkUsername('用户A')).toBe(true)
    expect(checkUsername('a'.repeat(32))).toBe(true)
  })

  it('1 字、33 字、含符号/下划线/空格拒绝', () => {
    expect(checkUsername('a')).toBe('用户名须为 2-32 位中文、字母或数字')
    expect(checkUsername('a'.repeat(33))).toBe('用户名须为 2-32 位中文、字母或数字')
    expect(checkUsername('xiaowang!')).toBe('用户名须为 2-32 位中文、字母或数字')
    expect(checkUsername('客服_小王')).toBe('用户名须为 2-32 位中文、字母或数字')
    expect(checkUsername('客服 小王')).toBe('用户名须为 2-32 位中文、字母或数字')
  })
})

describe('密码与分页校验', () => {
  it('密码 7 位拒绝、8 位通过', () => {
    expect(checkPassword('Pass123')).toBe('密码至少 8 位')
    expect(checkPassword('Pass1234')).toBe(true)
  })

  it('pageSize 1 与 100 通过（含数字字符串），0/101/小数拒绝', () => {
    expect(checkPageSize(1)).toBe(true)
    expect(checkPageSize(100)).toBe(true)
    expect(checkPageSize('20')).toBe(true)
    expect(checkPageSize(0)).toBe('每页条数须为 1-100 的整数')
    expect(checkPageSize(101)).toBe('每页条数须为 1-100 的整数')
    expect(checkPageSize(20.5)).toBe('每页条数须为 1-100 的整数')
  })
})

describe('日期范围校验（YYYY-MM-DD）', () => {
  it('合法范围与空端点通过', () => {
    expect(checkDateRange('2026-01-01', '2026-01-31')).toBe(true)
    expect(checkDateRange(undefined, undefined)).toBe(true)
    expect(checkDateRange('2026-01-01', null)).toBe(true)
  })

  it('起始晚于结束拒绝', () => {
    expect(checkDateRange('2026-01-31', '2026-01-01')).toBe('起始日期不能晚于结束日期')
  })

  it('格式非法与不存在的日历日期拒绝', () => {
    expect(checkDateRange('2026/01/01')).toBe('起始日期格式应为 YYYY-MM-DD')
    expect(checkDateRange('2026-02-30')).toBe('起始日期格式应为 YYYY-MM-DD')
    expect(checkDateRange(undefined, '2026-13-01')).toBe('结束日期格式应为 YYYY-MM-DD')
  })
})

describe('Element 表单 validator 适配', () => {
  it('校验失败回调 Error、通过回调 undefined', () => {
    const fail = vi.fn()
    questionValidator({}, '   ', fail)
    expect(fail).toHaveBeenCalledWith(expect.any(Error))

    const pass = vi.fn()
    questionValidator({}, '合法问题', pass)
    expect(pass).toHaveBeenCalledWith(undefined)
  })
})

describe('必填文本校验（登录/改密仅必填口径，上限随后端字段）', () => {
  it('空值与纯空白拒绝并带字段名，非字符串输入拒绝', () => {
    expect(checkRequiredText('', '用户名', 64)).toBe('请输入用户名')
    expect(checkRequiredText('   ', '原密码', 200)).toBe('请输入原密码')
    expect(checkRequiredText(null, '用户名', 64)).toBe('请输入用户名')
  })

  it('超上限拒绝（用户名 64、密码 200），边界值通过；密码不设 ≥8（旧密码可短）', () => {
    expect(checkRequiredText('a'.repeat(64), '用户名', 64)).toBe(true)
    expect(checkRequiredText('a'.repeat(65), '用户名', 64)).toBe('用户名最长 64 个字符')
    expect(checkRequiredText('p'.repeat(200), '密码', 200)).toBe(true)
    expect(checkRequiredText('p'.repeat(201), '密码', 200)).toBe('密码最长 200 个字符')
    expect(checkRequiredText('123456', '密码', 200)).toBe(true)
  })

  it('requiredTextValidator 工厂：失败回调 Error、通过回调 undefined', () => {
    const fail = vi.fn()
    requiredTextValidator('用户名', 64)({}, '', fail)
    expect(fail).toHaveBeenCalledWith(expect.any(Error))

    const pass = vi.fn()
    requiredTextValidator('用户名', 64)({}, 'admin', pass)
    expect(pass).toHaveBeenCalledWith(undefined)
  })
})

describe('无上限必填文本/必选项工厂（文档上传元数据等 Text 列字段）', () => {
  it('requiredText：空白拒绝带 label 文案，非空白通过且不设长度上限', () => {
    const fail = vi.fn()
    requiredText('标题')({}, '', fail)
    expect(fail).toHaveBeenCalledWith(expect.any(Error))
    requiredText('标题')({}, '   ', fail)
    expect(fail).toHaveBeenCalledTimes(2)
    requiredText('标题')({}, null, fail)
    expect(fail).toHaveBeenCalledTimes(3)

    const pass = vi.fn()
    requiredText('标题')({}, ' 客服_退货政策 ', pass)
    expect(pass).toHaveBeenCalledWith(undefined)
    requiredText('标题')({}, '长'.repeat(500), pass)
    expect(pass).toHaveBeenCalledTimes(2)
  })

  it('requiredPick：未选（空串/null）拒绝带"请选择"文案，任意非空值通过', () => {
    const fail = vi.fn()
    requiredPick('部门')({}, '', fail)
    expect(fail).toHaveBeenCalledWith(expect.any(Error))
    requiredPick('生效日期')({}, null, fail)
    expect(fail).toHaveBeenCalledTimes(2)

    const pass = vi.fn()
    requiredPick('部门')({}, 'hr', pass)
    expect(pass).toHaveBeenCalledWith(undefined)
    requiredPick('生效日期')({}, '2026-01-01', pass)
    expect(pass).toHaveBeenCalledTimes(2)
  })
})

describe('确认字段一致校验（改密页 新密码/确认密码）', () => {
  it('与期望值一致通过，不一致返回固定文案', () => {
    expect(checkConfirm('NewPass1234', 'NewPass1234')).toBe(true)
    expect(checkConfirm('NewPass1235', 'NewPass1234')).toBe('两次输入不一致')
  })

  it('confirmValidator 经 getter 实时取期望值（新密码改后确认位随之失效）', () => {
    let expected = 'AAAABBBB'
    const validator = confirmValidator(() => expected)
    const fail = vi.fn()
    validator({}, 'AAAABBBB', fail)
    expect(fail).toHaveBeenCalledWith(undefined)
    expected = 'CCCCDDDD'
    validator({}, 'AAAABBBB', fail)
    expect(fail).toHaveBeenCalledWith(expect.any(Error))
  })
})
