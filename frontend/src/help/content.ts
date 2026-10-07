/**
 * 帮助内容聚合入口：HelpView 只从这里取数。
 * 内容与 docs/user-guide.md 同源维护（手册为详版底稿，此处为在线精简版）；
 * 界面文案调整时两处同步修订。
 */
import type { HelpTab } from './types'
import { employeeTabs } from './employee'
import { adminTab } from './admin'
import { faqTab } from './faq'

export type { HelpBlock, HelpSection, HelpStep, HelpTab } from './types'

export const helpTabs: HelpTab[] = [...employeeTabs, adminTab, faqTab]
