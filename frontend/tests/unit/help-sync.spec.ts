/**
 * 帮助双源守账（工单 18，评审 P2"界面文案与帮助引文漂移"的机制化）：
 * 界面原文（菜单/按钮）改了而 help/user-guide 引文没跟 → 本测试红。
 * 读源码文本比对（不挂载组件）：断言帮助引文包含关键界面原文——覆盖 §19.6
 * 同步义务中最高频漂移的三处（菜单名、评测域按钮、难例池动作）。
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = (p: string): string => readFileSync(`src/${p}`, 'utf-8')
const helpAdmin = read('help/admin.ts')
const layout = read('layout/MainLayout.vue')

describe('帮助引文与界面原文一致（双源守账）', () => {
  it('侧边栏菜单名改动必须同步帮助引文（菜单改名即红）', () => {
    for (const label of ['问题反馈管理', '评测管理', '运营概览']) {
      expect(layout, `MainLayout 菜单应含「${label}」`).toContain(label)
      expect(helpAdmin, `help/admin 引文应含「${label}」`).toContain(label)
    }
  })

  it('评测题库按钮原文（升为正式/降为扩充）与帮助引文一致', () => {
    const bank = read('components/eval/QuestionBankPanel.vue')
    for (const text of ['升为正式', '降为扩充']) {
      expect(bank, `题库按钮应含「${text}」`).toContain(text)
      expect(helpAdmin, `帮助引文应含「${text}」`).toContain(text)
    }
  })

  it('难例池动作原文（生成变体题/重扫待补语料/忽略）与帮助引文一致', () => {
    const pool = read('components/eval/HardcasePoolPanel.vue')
    for (const text of ['生成变体题', '重扫待补语料', '忽略']) {
      expect(pool, `难例池应含「${text}」`).toContain(text)
      expect(helpAdmin, `帮助引文应含「${text}」`).toContain(text)
    }
  })

  it('服务端限流/配额文案原文与帮助引文一致（F4 双源守账）', () => {
    // 服务端原文直出（LoginView"文案以服务端为准"），帮助引文漂移即红——
    // 后端原文在 frontend/ 之外，直接按仓库相对路径读源码文本比对
    const auth = readFileSync('../app/services/auth.py', 'utf-8')
    const chat = readFileSync('../app/api/chat.py', 'utf-8')
    const faq = read('help/faq.ts')
    for (const text of ['登录尝试过于频繁，请稍后再试', '今日提问次数已达上限，请明天再试']) {
      expect(faq, `帮助引文应含服务端原文「${text}」`).toContain(text)
    }
    expect(auth).toContain('登录尝试过于频繁，请稍后再试')
    expect(chat).toContain('今日提问次数已达上限，请明天再试')
  })
})
