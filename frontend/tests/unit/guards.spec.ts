/**
 * 路由守卫决策单测：未登录/首登改密门/权限三分态全覆盖（纯函数，不挂真 router）。
 */
import { describe, expect, it } from 'vitest'
import { resolveGuard, type GuardAuthSnapshot } from '@/router/guards'

const anon: GuardAuthSnapshot = {
  isAuthenticated: false,
  isAdmin: false,
  mustChangePassword: false,
}
const member: GuardAuthSnapshot = { ...anon, isAuthenticated: true }
const freshMember: GuardAuthSnapshot = { ...member, mustChangePassword: true }
const freshAdmin: GuardAuthSnapshot = { ...freshMember, isAdmin: true }
const admin: GuardAuthSnapshot = { ...member, isAdmin: true }

const chat = { name: 'chat', meta: {} }
const login = { name: 'login', meta: {} }
const changePassword = { name: 'change-password', meta: {} }
const adminDocs = { name: 'kb-documents', meta: { requiresAdmin: true } }

describe('未登录拦截', () => {
  it('未登录访问业务页 → 重定向登录页', () => {
    expect(resolveGuard(chat, anon)).toEqual({ name: 'login' })
  })

  it('未登录访问管理页 → 重定向登录页（而非问答页）', () => {
    expect(resolveGuard(adminDocs, anon)).toEqual({ name: 'login' })
  })

  it('未登录访问登录页 → 放行', () => {
    expect(resolveGuard(login, anon)).toBe(true)
  })
})

describe('首登强制改密门（白名单外全拦截）', () => {
  it('未改密访问问答页 → 重定向改密页', () => {
    expect(resolveGuard(chat, freshMember)).toEqual({ name: 'change-password' })
  })

  it('未改密的 admin 访问管理页 → 仍先拦改密（改密门先于角色判定）', () => {
    expect(resolveGuard(adminDocs, freshAdmin)).toEqual({ name: 'change-password' })
  })

  it('未改密访问改密页 → 放行', () => {
    expect(resolveGuard(changePassword, freshMember)).toBe(true)
  })

  it('未改密访问登录页 → 重定向改密页', () => {
    expect(resolveGuard(login, freshMember)).toEqual({ name: 'change-password' })
  })
})

describe('角色权限拦截（体验层，后端 AdminUser 才是权威）', () => {
  it('member 访问 requiresAdmin 页 → 重定向问答页', () => {
    expect(resolveGuard(adminDocs, member)).toEqual({ name: 'chat' })
  })

  it('admin 访问 requiresAdmin 页 → 放行', () => {
    expect(resolveGuard(adminDocs, admin)).toBe(true)
  })

  it('member 访问答页 → 放行', () => {
    expect(resolveGuard(chat, member)).toBe(true)
  })
})

describe('登录页回流', () => {
  it('已登录再访登录页 → 回问答工作台', () => {
    expect(resolveGuard(login, member)).toEqual({ name: 'chat' })
  })
})
