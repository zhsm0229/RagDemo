# 18 尽项设计：侧栏折叠响应式兜底（补一档兜底，非全量适配）

> 依据：工单 18（`.scratch/commercial-readiness/issues/18-ui-polish-batch2.md` 末项）；评审口径"侧栏可折叠（el-menu collapse）+ 筛选行 flex-wrap 补齐 + 主容器 min-width"。现状核对基于 2026-10-31 代码，file:line 均实测。

## 0. 范围与"不做"

**做**：侧栏折叠 + 筛选行 flex-wrap 补齐 + 主容器 min-width 兜底。
**不做**（明确出圈）：移动端全适配 / 断点自动折叠（resize 监听）/ 抽屉式侧栏 / 键盘快捷键 / 侧栏拖拽调宽 / 平板触控专项优化。窄屏兜底 = 折叠省宽 + 筛选行折行 + 内容区内部横向滚动，三招到此为止。

## 1. 侧栏折叠设计

- **现状**：`frontend/src/layout/MainLayout.vue`（全文 139 行）`el-aside width="220px"` 固定（:75），`el-menu` router 模式（:77），菜单项**无图标**，admin 组"知识库/管理"带 `el-menu-item-group` 标题（:39-59），member 组无标题（:28-37）。
- **触发方式**：aside 改为 flex 纵向三段（brand / menu 占 flex:1 / 底部 toggle 条），toggle 为 aside 底部固定小按钮，图标 `Fold/Expand` 切换。不做快捷键。命名用**"折叠侧栏/展开侧栏"**，避开"收起"——`CitationPanel.vue:26` 引用面板已占用"收起"，防止未来 `getByRole('button', { name: /收起/ })` 歧义。
- **el-menu 行为**：`:collapse="collapsed"` + `:collapse-transition="false"`（aside 自身 CSS `width .2s` 过渡；两套动画叠加会抖动）。折叠态 Element Plus 自动：隐藏 group 标题、菜单项弹 popper 显示原文案（tooltip 免费获得，无需自建）。
- **折叠态图标与 tooltip**：菜单项必须补图标，否则 collapse 模式只剩 64px 空白块。**`@element-plus/icons-vue` 未安装**（package.json 仅 element-plus 2.14.6，src 中无引用）——按 AGENTS"新增依赖须登记"：官方配套、按需 tree-shake、零配置，推荐引入；若评审否决依赖，退回内联 SVG（13 个菜单项，MainLayout 有破 300 行风险，届时必须抽 `layout/menu-config.ts`）。每项图标建议：智能问答 ChatDotRound、我的记忆 Notebook、帮助 InfoFilled、文档管理 Document、问答对管理 ChatLineSquare、模板生成 MagicStick、检索测试 Search、用户管理 User、提问记录 Tickets、问题反馈管理 Warning、评测管理 Stopwatch、运营概览 TrendCharts、审计日志 DocumentCopy。
- **brand 折叠态**：`.brand` 文本（"企业智能工作台"，:76）在 64px 下换行难看，折叠时切换单字"企"居中（v-if），高度节奏不变。
- **状态持久化**：localStorage key `rag.ui.sidebarCollapsed`（值 '1'/'0'），读写均 try/catch（隐私模式/禁用容错），**缺省展开**。src 现无任何 localStorage 使用（grep 证实），此为首个，封一个 10 行内联 composable 即可，不必上 pinia。
- **对既有布局的影响面**：仅 aside 宽度 220↔64 过渡；`el-main` flex 自动跟随，无路由出口改动；el-table 列宽消化靠内部滚动。行数预算：增量约 80 行 → MainLayout ~220，≤300 达标；若走 SVG 方案则先抽 menu-config.ts。
- **member 也获得折叠**（同一控件），member 菜单仅 3 项，低风险。

## 2. 筛选行 flex-wrap 现状盘点与补齐清单

样式块实测（`display:flex; gap:8px` 基础上是否有 `flex-wrap: wrap`）：

| 文件 | 样式行号 | 现状 |
|---|---|---|
| AuditView.vue / BadcaseView.vue / DocumentsView.vue / MessagesView.vue / QaPairsView.vue / RetrievalTestView.vue / UsersView.vue | 242 / 205 / 616 / 230 / 287 / 169 / 259 | 已有 |
| **EvalView.vue** | :220-225（实例 :134 题库动作行、:151 跑分行共用） | **缺** |
| **QuestionBankPanel.vue** | :189-194（4 下拉+关键词输入+按钮 ≈666px，最易挤压） | **缺** |
| **HardcasePoolPanel.vue** | :221-226（2 下拉+spacer+2 按钮 ≈520px） | **缺** |

补齐 = 三处各加一行 `flex-wrap: wrap;`，零结构改动。EvalView 一处样式同时覆盖两个实例。

## 3. 主容器 min-width 建议值与横向滚动兜底

- `.main` 加 `min-width: 768px`。Element Plus `el-main` 自带 `overflow:auto`，横向滚动**收在内容区内**，不产生窗口级双滚动条。
- 算术锚点：1024 窗口展开态 1024−220=804≥768 不出滚动条；<1024（如平板竖屏 768 折叠态 768−64=704）仅内容区内部滚动。1366×768 基线：展开 1146 / 折叠 1302，均无滚动，**无需动任何表格列宽**。e2e 口径 1280×800 展开 1060，同样安全。

## 4. e2e 影响评估

- **既有选择器零影响**：全部 spec 均以 `page.goto('/admin/…')` 直达路由（grep 全 e2e 目录无 menuitem/aside 点击），不依赖侧栏宽度或菜单交互。
- **默认展开策略天然成立**：storageState 由 APIRequestContext 生成（`auth.setup.ts:25,109`），**不含浏览器 localStorage**；Playwright 每用例新 context → 侧栏恒为默认展开。setup 无需改动。
- **兜底约定**：若未来某 spec 点击 `sidebar-toggle`，用毕须 `page.evaluate(() => localStorage.removeItem('rag.ui.sidebarCollapsed'))` 清理，防止同 context 后续用例在意外折叠态跑。
- **data-test**：新增 `data-test="sidebar-toggle"`，全库无重名（grep 证实）。菜单项不必加 data-test（无 e2e 消费）。
- 可选 P3：一条 1366×768 冒烟（`test.use({ viewport: { width: 1366, height: 768 } })`）断言 toggle 可点 + 评测题库筛选行折行，仅当时间允许。

## 5. 视觉验收清单（人工三态核对）

- **展开 1366×768**：13 项菜单+两组标题齐；toggle 在 aside 底部；各列表页表格/筛选行无错位；brand 全文显示。
- **折叠 1366×768**：64px 纯图标；hover 弹 popper 显示菜单全名；当前路由项高亮保持；无文本截断溢出。
- **展开 1280×800（e2e 同口径）**：与 1366 无布局回归；1024 窗口下评测三页筛选行正确折行（QuestionBankPanel 重点看）。
- **通用**：刷新后折叠态保持；member 登录仅 3 项且可折叠；Tab 键可达 toggle 且读屏播报"折叠侧栏"；窗口缩到 900px 时滚动条只出现在内容区。

**双源文案同步点**（§19.6 义务）：tooltip/aria 原文"折叠侧栏/展开侧栏"为新增界面文案 → `docs/user-guide.md` 第一篇登录章节加一句（"侧边栏可点左下角按钮折叠，投屏时给表格留更多宽度"）+ `frontend/src/help/employee.ts` 快速上手节加同义句。`help-sync.spec.ts` **无需改**：守账词（问题反馈管理/评测管理/运营概览/升为正式等）全部未动，本设计不给菜单改名；若想机制化可把两个新词追加进守账（可选项，非必需）。

## 6. 实现建议（文件级）

1. `frontend/package.json`：+ `@element-plus/icons-vue`（理由登记技术栈清单）。
2. `frontend/src/layout/MainLayout.vue`：折叠状态（ref+localStorage composable）、底部 toggle（`data-test="sidebar-toggle"`、动态 aria-label + `aria-expanded`）、菜单项图标、aside 宽度 class 切换（`.aside{transition:width .2s}`，折叠 64px）、`.main{min-width:768px}`、折叠态 brand 单字。
3. （仅当破 300 行）`frontend/src/layout/menu-config.ts`：菜单数据+图标映射抽出。
4. 三处 flex-wrap：`EvalView.vue:224` / `QuestionBankPanel.vue:193` / `HardcasePoolPanel.vue:225`。
5. **新增 `frontend/tests/unit/main-layout.spec.ts`**（vitest 组件测试**值得加**：纯前端状态逻辑、零 mock 成本）——用例：默认展开；点击 toggle 后 el-menu 含 `el-menu--collapse` 且 aside 宽度 class 切换；localStorage 写入 '1' 再挂载保持折叠；预置非法值容错回展开态；aria-label 与 aria-expanded 随态切换；菜单原文仍含三个守账词（与 help-sync 形成双保险）。断言精确（class 存在/不存在逐一断言，禁 `A or B`）。
6. 双源文档：`docs/user-guide.md` + `frontend/src/help/employee.ts` 各一句。

## 7. 工作量预估

**0.5 人日**：布局改造+图标依赖 0.25；三处 flex-wrap 0.05；组件测试 0.1；双源文档同步 0.05；三态人工走查+四门自测 0.05，余缓冲。若图标依赖被否、改内联 SVG：+0.25 人日（含 menu-config.ts 抽取）。

## 8. 评审修订（2026-10-31，测试工程师 QC R9）

- **R9 main-layout.spec 挂载口径（补入 §6.5）**：MainLayout 是 router 模式 el-menu——组件测试需 `createMemoryHistory` 的 router（注入既有路由表或最小 stub 路由）+ ElementPlus 插件 + `el-menu-item-group` 组件随全量注册；afterEach 必须清理 `localStorage.removeItem('rag.ui.sidebarCollapsed')`（防用例间折叠态残留）；挂载用 `global.plugins: [router, ElementPlus]`。补用例："非法 localStorage 值容错回展开态"依赖 try/catch 解析路径，localStorage 预置 'garbage' 后挂载断言无 collapse class。
