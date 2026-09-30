/**
 * 问答对管理页共享枚举文案（对照原型 QaView 与 api.md §4.1 枚举口径）。
 * status 与后端枚举同源：enabled=启用（参与检索）/ disabled=停用（不参与）。
 *
 * 部门口径已切真实 deptCode（api.md §4.1.3 列表 DTO 9 键，后端 join departments
 * 取库内编码）：部门列渲染复用 admin/options 的 deptCodeLabel（编码枚举
 * service/hr/all → 中文），部门筛选下拉复用 ADMIN_DEPT_ID_OPTIONS（查询参数
 * deptId，api.md §4.1.3 契约；无部门列表端点，deviations #35）——原"种子序
 * id 映射 + 部门#N 兜底"已删（W4 T11 评审裁定脆弱点，deviations #30 兑现）。
 */
import type { QaPairStatus } from '@/api/qaPairs'

export interface QaOption<T> {
  value: T
  label: string
}

export const QA_STATUS_OPTIONS: QaOption<QaPairStatus>[] = [
  { value: 'enabled', label: '启用' },
  { value: 'disabled', label: '停用' },
]
