/**
 * SSE 文本帧解析纯函数单测：事件切分、跨 chunk 残尾拼接、CRLF 容忍。
 * 线格式契约见 docs/api.md §1.3（每事件两行 event:/data: + 空行分隔）。
 */
import { describe, expect, it } from 'vitest'
import { parseSseFrames } from '@/api/sse'

describe('parseSseFrames（SSE 文本帧解析纯函数）', () => {
  it('按空行切分完整事件并解析 event/data 行，中文 data 原样保留', () => {
    const buffer =
      'event: citations\ndata: {"messageId":1,"items":[]}\n\n' +
      'event: token\ndata: {"messageId":1,"content":"根据资料[1]，报销标准为"}\n\n'
    const { frames, rest } = parseSseFrames(buffer)
    expect(frames).toEqual([
      { event: 'citations', data: '{"messageId":1,"items":[]}' },
      { event: 'token', data: '{"messageId":1,"content":"根据资料[1]，报销标准为"}' },
    ])
    expect(rest).toBe('')
  })

  it('事件被拆进相邻两个 chunk：首个 chunk 只出空、残尾原样返回，拼齐后完整解析不丢不重', () => {
    const whole = 'event: token\ndata: {"messageId":1,"content":"你好"}\n\n'
    const cut = 12
    const first = parseSseFrames(whole.slice(0, cut))
    expect(first.frames).toEqual([])
    expect(first.rest).toBe(whole.slice(0, cut))
    const second = parseSseFrames(first.rest + whole.slice(cut))
    expect(second.frames).toEqual([{ event: 'token', data: '{"messageId":1,"content":"你好"}' }])
    expect(second.rest).toBe('')
  })

  it('容忍 CRLF 行尾；连续空块不产生空事件；未知事件名原样透传', () => {
    const buffer = 'event: done\r\ndata: {"refused":true}\r\n\r\n\r\nevent: ping\ndata: 1\n\n'
    const { frames } = parseSseFrames(buffer)
    expect(frames).toEqual([
      { event: 'done', data: '{"refused":true}' },
      { event: 'ping', data: '1' },
    ])
  })
})
