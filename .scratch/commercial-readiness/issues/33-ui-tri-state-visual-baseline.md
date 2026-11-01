# 33: UI 三态截图基线（视觉自核降为"首核+自动盯回归"）

**What to build:** 工单 18 尽项的"用户 UI 三态视觉自核"目前只能每次人工逐页看——加 Playwright 截图基线（`toHaveScreenshot`）：接入 LoadErrorState 的视图三态（加载/错误/空态）代表性页面截屏钉基线。**首次基线生成后仍需用户人眼核一次**（验收判定不可自动化），此后任何视觉回归 CI 自动报警——"每次自核"降为"首核+回归自动盯"。三态元素存在性/文案/交互已被 vitest+Playwright DOM 断言覆盖，本票只补"视觉不回归"这一层。

**Blocked by:** None (can start immediately)。

**Status:** done——候选基线已入库（2026-11-01），**待用户人眼核收**（按 design/18 §5 清单看 `frontend/e2e/specs/tri-state-visual.spec.ts-snapshots/` 9 张 PNG：documents/qa-pairs/badcase × 加载/错误/空；核收前基线视为候选，不阻塞合入；核收后任何视觉漂移 CI/本地 Playwright 自动报警）

- [x] Playwright 截图用例 `e2e/specs/tri-state-visual.spec.ts`（3 视图×3 态=9 张）：route 拦截注入三态零后端数据依赖；加载态=永久挂起列表 API（`route.continue` 放行收尾在多请求竞态下报 "Route is already handled"，弃用）以 el-loading-mask 可见为锚；错误态=abort → "加载失败，点击重试"；空态=空页应答 → 各视图文案锚（documents"还没有文档"/qa-pairs"还没有问答对"/badcase"没有待处理的问题反馈"——B11 范式空态文案分视图）；`animations: 'disabled'` 禁动画防伪影
- [x] 基线 9 张 PNG 入库（`-darwin` 平台后缀；CI linux 首跑需 `--update-snapshots` 生成对应平台基线一次——票内注明）
- [x] 本机验证：生成跑 + **无 update 复跑 10 passed（9 截图+setup）全绿**——基线与页面渲染稳定一致；e2e 隔离栈（四层 compose 叠加，宿主端口被占时第四层 `ports: !override` 钉空闲口）跑后已停恢复原状
- [ ] 用户人眼核收 9 张候选基线（验收判定本体，不可自动化）
