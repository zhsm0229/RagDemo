/**
 * 统一校验器规则表测试：边界值逐一断言（口径对齐 standards §21.5 与后端约束）。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  checkDateRange,
  checkPassword,
  checkPageSize,
  checkQuestion,
  checkUsername,
  questionValidator,
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
