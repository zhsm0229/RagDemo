/**
 * SSE 文本帧解析纯函数（standards §3 第 6 条 / docs/api.md §1.3）。
 *
 * 线格式：每事件两行 `event: {名}` + `data: {JSON}`，事件之间以空行（\n\n）分隔。
 * ReadableStream 按 chunk 到达，单个事件可能被拆进相邻两个 chunk——调用方把
 * 累计 buffer 整体传入，本函数只切出完整事件，残尾原样返回待下次拼接。
 * 单独成模块以便对跨 chunk 边界行为做直接单测。
 */

export interface SseFrame {
  event: string
  data: string
}

export function parseSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  // 归一化 CRLF（契约是 \n，但代理/实现差异可能带 \r），归一后按空行切分
  const parts = buffer.replace(/\r\n/g, '\n').split('\n\n')
  // 最后一段是没有空行收尾的残尾（不完整事件），留给下一次累计后再解析
  const rest = parts.pop() ?? ''
  const frames: SseFrame[] = []
  for (const block of parts) {
    if (block.trim() === '') continue
    let event = 'message'
    let data = ''
    let hasPayload = false
    for (const rawLine of block.split('\n')) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
      if (line.startsWith('event:')) {
        event = line.slice('event:'.length).trim()
        hasPayload = true
      } else if (line.startsWith('data:')) {
        data += line.slice('data:'.length).trim()
        hasPayload = true
      }
    }
    // 注释/心跳块（如 ": keep-alive"，无 event:/data: 行）整块跳过（T8 评审清单②）
    if (!hasPayload) continue
    frames.push({ event, data })
  }
  return { frames, rest }
}
