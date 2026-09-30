/**
 * 共享格式化工具单测：fmtTime ISO8601 → "YYYY-MM-DD HH:mm"（W4/W5 终审 M-5 从
 * RetrievalTestView/ZeroRecallChunkPanel/OverviewView/AuditView 四处内联实现收敛，
 * 口径钉死：带 T 正常替换、无 T 照常截断、空值出空串不造值）。
 */
import { describe, expect, it } from 'vitest'
import { fmtTime } from '@/utils/format'

describe('fmtTime（ISO8601 → YYYY-MM-DD HH:mm）', () => {
  it('带 T 后缀的 ISO 时间串：T 换空格并截断到分钟', () => {
    expect(fmtTime('2026-09-28T10:00:00+08:00')).toBe('2026-09-28 10:00')
    expect(fmtTime('2026-08-15T09:30:00+00:00')).toBe('2026-08-15 09:30')
  })

  it('无 T 后缀（空格分隔的时间串）：照常截断到分钟', () => {
    expect(fmtTime('2026-09-28 10:00:30')).toBe('2026-09-28 10:00')
  })

  it('空值：返回空串，不造占位时间', () => {
    expect(fmtTime('')).toBe('')
  })
})
