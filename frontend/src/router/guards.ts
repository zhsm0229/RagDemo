/**
 * 路由守卫决策（纯函数，便于单测三态覆盖）。
 *
 * 体验层拦截口径（ui-spec §一 / §九）：后端 require_admin 才是唯一权威；
 * 首登未改密用户除改密页外全拦截（守卫白名单外全拦截，standards F-101）。
 */
import type { RouteLocationRaw } from 'vue-router'

/** 守卫需要的最小登录态快照 */
export interface GuardAuthSnapshot {
  isAuthenticated: boolean
  isAdmin: boolean
  mustChangePassword: boolean
}

export interface GuardTarget {
  name?: string | symbol | null
  meta: { requiresAdmin?: unknown }
}

/** 返回 true = 放行；返回对象 = 重定向 */
export function resolveGuard(to: GuardTarget, auth: GuardAuthSnapshot): RouteLocationRaw | true {
  const name = typeof to.name === 'string' ? to.name : ''

  // 登录页：未登录放行；已登录按改密门分流
  if (name === 'login') {
    if (!auth.isAuthenticated) return true
    return auth.mustChangePassword ? { name: 'change-password' } : { name: 'chat' }
  }

  // 未登录访问一切业务页 → 登录页
  if (!auth.isAuthenticated) return { name: 'login' }

  // 首登强制改密：除改密页外全拦截（不可绕过）
  if (auth.mustChangePassword) {
    return name === 'change-password' ? true : { name: 'change-password' }
  }

  // member 访问管理页 → 问答工作台
  if (to.meta.requiresAdmin === true && !auth.isAdmin) return { name: 'chat' }

  return true
}
