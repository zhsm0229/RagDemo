/**
 * 统一校验器：口径与后端对齐（standards §21.5、docs/api.md 各端点约束），
 * 纯谓词供页面预检与单测复用，Element 兼容 validator 供 el-form rules 使用。
 */

export type ValidatorResult = true | string

type Check = (value: unknown) => ValidatorResult

/** 提问：strip 后 1-2000 字（§21.5，chat 与检索测试同口径） */
export function checkQuestion(value: unknown): ValidatorResult {
  const text = typeof value === 'string' ? value.trim() : ''
  if (text.length < 1) return '问题不能为空'
  if (text.length > 2000) return '问题长度须在 1-2000 字之间'
  return true
}

/** 用户名：2-32 位中文/字母/数字（§21.5） */
export function checkUsername(value: unknown): ValidatorResult {
  const text = typeof value === 'string' ? value.trim() : ''
  if (!/^[\u4e00-\u9fa5A-Za-z0-9]{2,32}$/.test(text)) {
    return '用户名须为 2-32 位中文、字母或数字'
  }
  return true
}

/** 密码：≥8 位（72 字节上限由服务层校验，前端不重复设上限） */
export function checkPassword(value: unknown): ValidatorResult {
  const text = typeof value === 'string' ? value : ''
  if (text.length < 8) return '密码至少 8 位'
  return true
}

/** 分页：pageSize 1-100 整数（standards §3-5） */
export function checkPageSize(value: unknown): ValidatorResult {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isInteger(n) || n < 1 || n > 100) return '每页条数须为 1-100 的整数'
  return true
}

/**
 * 登录/改密必填口径：仅拦空值与超长，其余规则交后端。
 * 登录密码不设 ≥8 位——那是新密码规则，旧密码可能短于它，前端拦截会挡住合法登录
 * （后端 LoginRequest/ChangePasswordRequest 仅约束 1-64 / 1-200 字符）。
 */
export function checkRequiredText(value: unknown, label: string, max: number): ValidatorResult {
  const text = typeof value === 'string' ? value.trim() : ''
  if (text.length < 1) return `请输入${label}`
  if (text.length > max) return `${label}最长 ${max} 个字符`
  return true
}

/** 确认字段一致（新密码/确认密码等）：期望值经 getter 实时取，避免快照过期 */
export function checkConfirm(value: unknown, expected: unknown): ValidatorResult {
  return value === expected ? true : '两次输入不一致'
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/

function isCalendarDate(text: string): boolean {
  const [year, month, day] = text.split('-').map(Number)
  if (month < 1 || month > 12 || day < 1) return false
  return day <= new Date(year, month, 0).getDate()
}

/** 日期范围：两端 YYYY-MM-DD 合法（可空=不过滤），且起始不晚于结束（YYYY-MM-DD 字典序即时序） */
export function checkDateRange(from?: string | null, to?: string | null): ValidatorResult {
  if (from) {
    if (!DATE_PATTERN.test(from) || !isCalendarDate(from)) return '起始日期格式应为 YYYY-MM-DD'
  }
  if (to) {
    if (!DATE_PATTERN.test(to) || !isCalendarDate(to)) return '结束日期格式应为 YYYY-MM-DD'
  }
  if (from && to && from > to) return '起始日期不能晚于结束日期'
  return true
}

// ---- Element Plus el-form 兼容 validator（签名 (rule, value, callback)） ----
function toElementValidator(check: Check) {
  return (_rule: unknown, value: unknown, callback: (error?: Error) => void): void => {
    const result = check(value)
    callback(result === true ? undefined : new Error(result))
  }
}

export const questionValidator = toElementValidator(checkQuestion)
export const usernameValidator = toElementValidator(checkUsername)
export const passwordValidator = toElementValidator(checkPassword)
export const pageSizeValidator = toElementValidator(checkPageSize)

/** 必填文本 validator 工厂（登录账号/密码、改密原密码等仅必填口径，label 用于错误文案） */
export function requiredTextValidator(
  label: string,
  max: number,
): (rule: unknown, value: unknown, callback: (error?: Error) => void) => void {
  return toElementValidator((value) => checkRequiredText(value, label, max))
}

/** 确认字段一致 validator 工厂：expected 经 getter 实时取（如 () => form.new） */
export function confirmValidator(
  getExpected: () => unknown,
): (rule: unknown, value: unknown, callback: (error?: Error) => void) => void {
  return toElementValidator((value) => checkConfirm(value, getExpected()))
}
