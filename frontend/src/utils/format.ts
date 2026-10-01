/**
 * 共享格式化工具（W6 小修包收敛）：ISO→"YYYY-MM-DD HH:mm" 此前在
 * RetrievalTestView / ZeroRecallChunkPanel / OverviewView / AuditView 四处各自内联
 * 实现（W4/W5 终审 M-5），统一收敛到此处单一口径，新增时间列一律复用。
 */

/** ISO8601 时间戳（YYYY-MM-DDTHH:mm…）→ "YYYY-MM-DD HH:mm"；空串入空串出不造值 */
export function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

/**
 * 会话列表相对时间（P2 原型口径）：今天只显时分，同年"MM-DD HH:mm"，更早年带年份。
 * 自 ConversationList.vue 内联实现上收（规范审查 Duplicated Code 项）——时间格式化
 * 一律归本模块，组件不再自带 new Date 格式化。
 */
export function fmtTimeRelative(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number): string => String(n).padStart(2, '0')
  const hhmm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  const now = new Date()
  if (date.toDateString() === now.toDateString()) return hhmm
  const md = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hhmm}`
  return date.getFullYear() === now.getFullYear() ? md : `${date.getFullYear()}-${md}`
}
