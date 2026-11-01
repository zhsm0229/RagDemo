/**
 * UI 三态截图基线（工单 33）：接入 LoadErrorState 的视图三态（加载/错误/空）代表性
 * 页面截屏钉基线——三态元素/文案/交互已由 vitest+Playwright DOM 断言覆盖，本 spec
 * 只补"视觉不回归"层：任何视觉漂移 CI 断言报警，把"每次发版人工自核"降为
 * "首次基线人工核收 + 回归自动盯"。
 *
 * 验收边界（票 33 钉死）：首次基线为候选——生成后须用户按 design/18 §5 清单人眼核收
 * （视觉判定不可自动化）；核收前基线视为候选，不阻塞合入。
 *
 * 稳定性口径：route 拦截注入三态（零后端数据依赖，L1 stub 栈即可）；截图
 * animations: 'disabled'（CSS 过渡禁用防抖动伪影）；视图集取三态代表
 * （documents / qa-pairs / badcase——同一 LoadErrorState 家族，样本面足够盯回归）。
 */
import { expect, test, type Page } from '@playwright/test'

/** 三态代表视图集：路径 + 列表 API（route 拦截点） */
const VIEWS = [
  { path: '/kb/documents', api: '**/api/documents*', name: 'documents', emptyText: '还没有文档' },
  { path: '/kb/qa-pairs', api: '**/api/admin/qa-pairs*', name: 'qa-pairs', emptyText: '还没有问答对' },
  {
    path: '/admin/badcase',
    api: '**/api/admin/badcases*',
    name: 'badcase',
    emptyText: '没有待处理的问题反馈',
  },
] as const

/** 加载态截图：拦截列表 API 挂起不应答（页面停留加载骨架），截图后放行 */
async function shotLoading(page: Page, v: (typeof VIEWS)[number]): Promise<void> {
  // 永久挂起列表 API（测试即弃页面：截图完成即结束，无放行收尾——规避多请求竞态下
  // "Route is already handled"；路由随测试上下文销毁清理）
  await page.route(v.api, () => new Promise<void>(() => {}))
  await page.goto(v.path)
  // 加载态 = el-table v-loading 遮罩（无"加载中"文案，B11 范式），以遮罩可见为锚
  await expect(page.locator('.el-loading-mask').first()).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveScreenshot(`tri-${v.name}-loading.png`, { animations: 'disabled' })
}

/** 错误态截图：拦截列表 API abort → LoadErrorState（"加载失败，点击重试"） */
async function shotError(page: Page, v: (typeof VIEWS)[number]): Promise<void> {
  await page.route(v.api, (route) => route.abort())
  await page.goto(v.path)
  await expect(page.getByText('加载失败，点击重试')).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveScreenshot(`tri-${v.name}-error.png`, { animations: 'disabled' })
  await page.unroute(v.api)
}

/** 空态截图：应答空列表 → 空态引导 */
async function shotEmpty(page: Page, v: (typeof VIEWS)[number]): Promise<void> {
  await page.route(v.api, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        code: 0,
        message: 'ok',
        data: { items: [], total: 0, page: 1, pageSize: 20 },
      }),
    }),
  )
  await page.goto(v.path)
  await expect(page.getByText(v.emptyText)).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveScreenshot(`tri-${v.name}-empty.png`, { animations: 'disabled' })
  await page.unroute(v.api)
}

for (const v of VIEWS) {
  test(`三态截图：${v.name} 加载态（候选基线，待人眼核收）`, async ({ page }) => {
    await shotLoading(page, v)
  })
  test(`三态截图：${v.name} 错误态`, async ({ page }) => {
    await shotError(page, v)
  })
  test(`三态截图：${v.name} 空态`, async ({ page }) => {
    await shotEmpty(page, v)
  })
}
