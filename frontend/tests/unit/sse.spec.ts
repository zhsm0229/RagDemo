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

  it('注释/心跳块（无 event:/data: 行）整块跳过不产帧，流继续（T8 评审清单②）', () => {
    const buffer = ': keep-alive\n\nevent: token\ndata: {"content":"你好"}\n\n: ping\n\n'
    const { frames, rest } = parseSseFrames(buffer)
    expect(frames).toEqual([{ event: 'token', data: '{"content":"你好"}' }])
    expect(rest).toBe('')
  })

  it('仅 data 行（无 event: 行）保留默认事件名 message 的解析行为', () => {
    const { frames } = parseSseFrames('data: 1\n\n')
    expect(frames).toEqual([{ event: 'message', data: '1' }])
  })

  it('同一事件多个 data: 行按 SSE 规范以换行拼接（M26），拼接结果可 JSON.parse', () => {
    const { frames } = parseSseFrames('event: token\ndata: {"content":\ndata: "你好"}\n\n')
    expect(frames).toEqual([{ event: 'token', data: '{"content":\n"你好"}' }])
    // 换行是合法 JSON 空白：多行负载可正常解析（此前无分隔符粘连会解析失败）
    expect(JSON.parse(frames[0]!.data)).toEqual({ content: '你好' })
  })
})
