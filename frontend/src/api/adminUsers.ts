/**
 * 管理端·用户接口（F-102，后端 app/api/admin_users.py + services/users_admin.py；
 * docs/api.md 尚未收录 P6 节，契约逐键对照后端实现核对）。
 *
 * 初始密码红线（F-102 验收）：initialPassword 仅在发放/重置的响应体出现一次，
 * 前端一次性展示（§8.8 readOnly 置灰可复制），关闭后不可再看——不缓存不回显，
 * 明文不落任何本地存储。三个写端点均 silent：错误（409 重名 / 422 禁用自己等）
 * 由页面内联展示（§22.2），不走全局 toast。
 */
import { get, post, request } from './client'
import type { PageData, UserRole } from './types'

/** 列表项（后端 _item_payload 全 10 键）：status 为后端拼好的中文态（正常/已禁用） */
export interface AdminUserItem {
  id: number
  username: string
  name: string
  deptCode: string
  role: UserRole
  status: string
  /** 按当下时刻判定的锁定态（locked_until 已过期不算），锁定清除走重置密码/自然过期 */
  locked: boolean
  mustChangePassword: boolean
  lastLoginAt: string | null
  createdAt: string
}

export interface AdminUserListQuery {
  keyword?: string
  page: number
  pageSize: number
}

/** 用户列表（F-102）：keyword 对 username ILIKE（后端已转义通配符），created_at 降序分页 */
export function listAdminUsers(query: AdminUserListQuery): Promise<PageData<AdminUserItem>> {
  const params = new URLSearchParams()
  const keyword = query.keyword?.trim() ?? ''
  if (keyword !== '') params.set('keyword', keyword)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<AdminUserItem>>(`/admin/users?${params.toString()}`)
}

/** 发放账号请求体（后端 CreateUserBody）：部门 service/hr/all，角色 admin/member */
export interface CreateUserPayload {
  username: string
  deptCode: string
  role: UserRole
}

/** 发放/重置密码的响应：明文初始密码仅此一次出现 */
export interface InitialPasswordResult {
  id: number
  initialPassword: string
}

/** 发放账号（F-102）：201 {id, initialPassword}；409/3005 用户名重复，422 用户名/部门/角色非法 */
export function createAdminUser(payload: CreateUserPayload): Promise<InitialPasswordResult> {
  return post<InitialPasswordResult>('/admin/users', payload, { silent: true })
}

/** 启用/禁用（PATCH {isActive}）：禁用即时失效；禁用自己 422 防自锁（内联展示） */
export function toggleAdminUserStatus(
  id: number,
  isActive: boolean,
): Promise<{ id: number; isActive: boolean }> {
  return request<{ id: number; isActive: boolean }>(`/admin/users/${id}/status`, {
    method: 'PATCH',
    body: { isActive },
    silent: true,
  })
}

/** 重置密码：新初始密码一次展示 + 首登强制改密（后端同时清失败计数与锁定） */
export function resetAdminUserPassword(id: number): Promise<InitialPasswordResult> {
  return post<InitialPasswordResult>(`/admin/users/${id}/reset-password`, undefined, {
    silent: true,
  })
}
