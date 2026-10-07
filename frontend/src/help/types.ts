/**
 * 帮助中心内容模型：内容（src/help/*.ts 数据文件）与渲染（HelpView.vue）分离。
 * 不引入 markdown 依赖——用可辨识联合 HelpBlock 表达有限几种排版（步骤/要点/对照表/提示），
 * 渲染器按 kind 分发到 Element Plus 组件，新增内容只改数据文件。
 */

/** 操作步骤：傻瓜式"点哪里 → 看到什么" */
export interface HelpStep {
  /** 步骤名（如"选文件"） */
  title: string
  /** 具体操作与界面提示原文 */
  detail: string
}

/** 内容块可辨识联合 */
export type HelpBlock =
  | { kind: 'steps'; steps: HelpStep[] }
  | { kind: 'tips'; items: string[] }
  | { kind: 'table'; headers: string[]; rows: string[][] }
  | { kind: 'note'; type: 'info' | 'warning' | 'success'; text: string }

/** 一个主题段落：标题 + 引言 + 若干内容块 */
export interface HelpSection {
  title: string
  intro?: string
  blocks: HelpBlock[]
}

/** 一个页签；adminOnly 页签对成员隐藏（与菜单权限矩阵同口径） */
export interface HelpTab {
  name: string
  label: string
  adminOnly?: boolean
  sections: HelpSection[]
}
