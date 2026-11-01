# 33: UI 三态截图基线（视觉自核降为"首核+自动盯回归"）

**What to build:** 工单 18 尽项的"用户 UI 三态视觉自核"目前只能每次人工逐页看——加 Playwright 截图基线（`toHaveScreenshot`）：接入 LoadErrorState 的视图三态（加载/错误/空态）代表性页面截屏钉基线。**首次基线生成后仍需用户人眼核一次**（验收判定不可自动化），此后任何视觉回归 CI 自动报警——"每次自核"降为"首核+回归自动盯"。三态元素存在性/文案/交互已被 vitest+Playwright DOM 断言覆盖，本票只补"视觉不回归"这一层。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] Playwright 截图用例：三态代表性页面集（加载骨架/错误态+重试/空态引导）`toHaveScreenshot` 钉基线（maxDiffPixels 容差口径）；stub 环境（L1 同款 TEST_MODE）下截图稳定（禁动画/禁时钟漂移：CSS animation 禁用与 mask 动态区域）
- [ ] 基线快照入库（tests-examples 基线目录）；CI 步骤挂 Playwright 项目；README/票内注明"首次基线待用户人眼核收（工单 18 §5 清单），核收前基线视为候选"
- [ ] 用户人眼核收前不阻塞合入（基线即候选）；核收动作归用户（票保持部分完成直至用户反馈）
