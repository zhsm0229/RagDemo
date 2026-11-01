# 25: 文档失败原因触屏可达（点击展开替代路径）

**What to build:** 文档列表"✖ 失败"标签的失败原因（解析失败/parseErrorDetail）目前仅 el-tooltip 悬停可见，触屏设备无法查看——补点击展开路径（点标签弹层展示原因+详情），桌面悬停行为保留，界面文案不变（无双源新增）。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent（工单 18 尽项："文档失败原因补点击展开替代路径（触屏可达）"，2026-11-01 code-review 核实未落）

- [ ] 失败标签可点击：弹层（el-dialog 或 popover）展示 parseError 与 parseErrorDetail 全文，可关闭；悬停 tooltip 保留
- [ ] 前端单测：点击触发弹层、内容含解析失败原因、非失败状态无点击行为
- [ ] 前端四门全绿（lint/type/vitest/build）
