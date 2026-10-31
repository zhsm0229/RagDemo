# 18: UI 打磨二批（长列表/黑话/可访问性/帮助守账）

**What to build:** 评审 UI P2 清单收口：企业客户日常操作的最后一批体验毛刺。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] 主列表 el-table 统一 max-height（560px）+ 表头吸顶：10 个列表页（一行属性改动）
- [ ] "badcase"黑话收口：菜单改"问题反馈管理"、点踩占位文案中文化（user-guide/help 同步）
- [ ] 👍/👎 加 aria-label；文档失败原因补点击展开替代路径（触屏可达）
- [ ] 帮助双源守账测试：抽 2-3 处界面按钮原文 vs help/user-guide 引文一致性断言（防 §19.6 漂移）
- [ ] 响应式兜底：侧栏可折叠（el-menu collapse）+ 筛选行 flex-wrap 补齐 + 主容器 min-width
- [ ] 前端四门全绿；user-guide/help 双源同步
