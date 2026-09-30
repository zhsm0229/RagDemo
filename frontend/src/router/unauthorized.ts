/**
 * 全局 401 装配（W4 终审 I 项③）：client 单点触发 → 清登录态 → 回登录页。
 *
 * 同 guards.ts 模式：决策纯函数 + handler 工厂独立成模块便于单测；router 经参数
 * 注入而非 client 直接 import，避免 api/client → router 循环依赖（装配在 router/index.ts）。
 * /login 与 /change-password 自身的 401 不回跳（已在该页，错误内联展示即可，防循环），
 * 但陈旧登录态仍清空——mustChangePassword 等前端态不得在会话失效后留存。
 */
import type { Router } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

/** 自身 401 不回跳的登录态页（已在页上，展示错误即可） */
const SELF_AUTH_ROUTE_NAMES: ReadonlySet<string> = new Set(['login', 'change-password'])

/** 当前路由需要回跳登录页时为 true（登录态页自身只清态不跳） */
export function shouldRedirectToLogin(routeName: unknown): boolean {
  return typeof routeName !== 'string' || !SELF_AUTH_ROUTE_NAMES.has(routeName)
}

/** 供装配层注册：清 pinia 登录态 + 按当前路由回跳 /login（handler 运行期 pinia 已激活） */
export function createUnauthorizedHandler(router: Router): () => void {
  return () => {
    useAuthStore().logout()
    if (shouldRedirectToLogin(router.currentRoute.value.name)) {
      void router.push({ name: 'login' })
    }
  }
}
