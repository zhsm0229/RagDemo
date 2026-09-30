/**
 * 共享格式化工具（W6 小修包收敛）：ISO→"YYYY-MM-DD HH:mm" 此前在
 * RetrievalTestView / ZeroRecallChunkPanel / OverviewView / AuditView 四处各自内联
 * 实现（W4/W5 终审 M-5），统一收敛到此处单一口径，新增时间列一律复用。
 */

/** ISO8601 时间戳（YYYY-MM-DDTHH:mm…）→ "YYYY-MM-DD HH:mm"；空串入空串出不造值 */
export function fmtTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}
