import { expect, test } from '@playwright/test'

// AGENT-01（agent-evolution #09）：模板生成工作台（TemplateGenView，AG-UI 单次运行）。
// L1 确定性层：ScriptedAgentChannel 脚本 = 首轮 knowledge_base_search 工具调用、次轮
// 固定正文——断言时间线（STEP/TOOL 事件组）与草稿卡产出（TEXT_MESSAGE 事件组）及完成
// 汇总条；来源文档下拉数据来自 auth.setup 预置的就绪文档（TEST_MODE 同步解析）。
test('AGENT-01: 发起模板生成，时间线与草稿按 AG-UI 事件序呈现', async ({ page }) => {
  await page.goto('/kb/template-gen')
  // 配置区：来源文档（就绪状态）+ 目标部门（生成数量默认 3 不动；下拉交互用
  // .el-select-dropdown__item:visible 同款惯例——getByRole 不在本仓 select 断言先例内）
  await page.locator('.cfg-doc').click()
  await page
    .locator('.el-select-dropdown__item:visible', { hasText: 'e2e_差旅报销制度_预置' })
    .click()
  // 等前一个 popper 真收起再开下一个：文档 popper 收起动画中点 .cfg-dept，
  // 部门项的 click 会落在过渡态上不生效（第五轮根因：部门未选→开始生成被前端
  // 校验静默拦截→无请求无 run，时间线断言空等超时）
  await expect(page.locator('.el-select-dropdown:visible')).toHaveCount(0)
  await page.locator('.cfg-dept').click()
  await page.locator('.el-select-dropdown__item:visible', { hasText: '客服' }).click()
  // 选中态显式断言：部门未选中时「开始生成」不发请求（上面竞态若复发在此红，不空等时间线）
  await expect(page.locator('.cfg-dept')).toContainText('客服')
  await page.getByRole('button', { name: '开始生成' }).click()

  // 时间线：0 号"已提交生成任务"伪节点收口后，脚本首轮 = STEP_STARTED + TOOL_CALL 组
  await expect(page.getByText('工具：knowledge_base_search')).toBeVisible()
  await expect(page.getByText('步骤 1')).toBeVisible()
  // 草稿产出：脚本次轮正文经 TEXT_MESSAGE 组流式成卡
  await expect(page.locator('[data-test="draft-card-1"]')).toContainText(
    '根据《考勤制度》，年假按工龄五到十天。',
  )
  // 终态：完成汇总条（草稿计数与耗时）+ 运行状态
  await expect(page.locator('[data-test="run-summary"]')).toContainText('生成完成：共 1 条草稿')
  await expect(page.locator('[data-test="run-status"]')).toContainText('已完成')
})
