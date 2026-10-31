# 08: 错误态收口——列表失败态统一 + 概览错误面板 + 自动设置禁保存

**What to build:** 管理员断网/网关超时后看到"加载失败，点击重试"而不是误导性空态（UI 评审 P1-8/9/10）。现状三处：
1. 10 个列表视图只有 DocumentsView/MemoryView 落地了错误态（B11 范式），其余 8 处 `load()` 只有 try/finally 无 catch——失败即渲染"还没有跑分任务/题库为空"。
2. OverviewView.vue:25-43 失败后四张统计卡 v-if 全隐藏，整页静默空白无重试入口。
3. EvalAutoSettingsPanel.vue:12-48 GET 失败后 5 个字段停留代码默认值且无标记，`save()` 是全量 PUT——此时点保存把真实配置覆写为默认值（数据破坏路径，三处中最优先）。

**Blocked by:** 01。

**Status:** ready-for-agent

- [ ] 抽公共"加载失败+重试"组件或组合式函数（以 DocumentsView.vue:108-118,568-577 B11 范式为模板）
- [ ] 8 个缺错误态的列表视图统一接入（EvalView/QuestionBankPanel/HardcasePoolPanel/Users/QaPairs/Messages/Badcase/Audit）
- [ ] OverviewView 失败渲染错误面板+重试按钮（对齐 ServicesHealthPanel loadErr 做法）
- [ ] EvalAutoSettingsPanel：加载失败置 error 态并禁用保存按钮（消除覆写路径）；顺带给"自动闭环总开关"提示补"手动合成不受本页预算约束"说明（开发 B P2 口径分叉的前端侧）
- [ ] 前端单测（vitest）：mock fetch 拒绝 → 错误态渲染 + 重试可用；设置页失败时保存 disabled
- [ ] 顺手项：难例池"忽略"补 Popconfirm 终态确认（UI 评审 P1-11，同域小改）
