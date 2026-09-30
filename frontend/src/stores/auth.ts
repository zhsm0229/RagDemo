/**
 * 登录态 store：缓存 /auth/me 结果，路由守卫消费。
 *
 * 选择说明：GET /api/auth/me 在 docs/api.md §2.3 存在（首登未改密也可访问），
 * 守卫刷新页面后经 ensureLoaded() 拉取一次；拉取失败（401/2003 等）视为未登录走 /login。
 * me 请求静默（silent），避免未登录访问业务页时弹全局错误提示。
 * 后端无 logout 端点（api.md 34 端点无登出），登出仅前端清态，jwt Cookie 由后端到期回收。
 * 会话失效跳转不在此处处理：401/2003 由 client 单点通知装配层（router/unauthorized.ts）
 * 统一清态回登录页，本 store 的 catch 只兜网络/5xx 等非 401 失败，避免守卫期未捕获异常。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { get, post } from '@/api/client'
import type { ChangePasswordRequest, LoginRequest, UserInfo } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  /** me 是否已探明（含探明为未登录），防止守卫每次导航重复请求 */
  const loaded = ref(false)

  const isAuthenticated = computed(() => user.value !== null)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const mustChangePassword = computed(() => user.value?.mustChangePassword === true)

  async function fetchMe(): Promise<UserInfo> {
    return get<UserInfo>('/auth/me', { silent: true })
  }

  /**
   * 每次会话只探一次 me；失败即按未登录处理（守卫统一重定向）。
   * 401/2003 的登出+跳转已收敛到全局通道（client → router/unauthorized.ts），
   * 此处 catch 仅兜非 401 失败（网络/5xx），与全局 logout 的清态重复是无害冗余。
   */
  async function ensureLoaded(): Promise<void> {
    if (loaded.value) return
    loaded.value = true
    try {
      user.value = await fetchMe()
    } catch {
      user.value = null
    }
  }

  /**
   * 登录成功即写状态；mustChangePassword 由路由守卫引导去改密页。
   * silent：登录/改密页错误内联展示（§22.2），不走全局 ElMessage 兜底。
   */
  async function login(payload: LoginRequest): Promise<UserInfo> {
    const me = await post<UserInfo>('/auth/login', payload, { silent: true })
    user.value = me
    loaded.value = true
    return me
  }

  /** 改密成功后刷新本地用户态（mustChangePassword 清零）；错误同样内联（§22.2） */
  async function changePassword(payload: ChangePasswordRequest): Promise<UserInfo> {
    const me = await post<UserInfo>('/auth/change-password', payload, { silent: true })
    user.value = me
    return me
  }

  function logout(): void {
    user.value = null
  }

  return {
    user,
    loaded,
    isAuthenticated,
    isAdmin,
    mustChangePassword,
    fetchMe,
    ensureLoaded,
    login,
    changePassword,
    logout,
  }
})
