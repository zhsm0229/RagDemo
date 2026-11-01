# 25: 文档失败原因触屏可达（点击展开替代路径）

**What to build:** 文档列表"✖ 失败"标签的失败原因（解析失败/parseErrorDetail）目前仅 el-tooltip 悬停可见，触屏设备无法查看——补点击展开路径（点标签弹层展示原因+详情），桌面悬停行为保留，界面文案不变（无双源新增）。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：TDD 先红 1 用例 → 实现 → 前端四门绿）

- [x] 失败标签可点击：原生 button 包裹 el-tag（ElTag click 经组件 emit 在 jsdom 不可靠，button 天然键盘/触屏可达）→ el-dialog（append-to-body）双行展示 parseError 与 parseErrorDetail，"知道了"关闭；悬停 tooltip 保留（文案逐字同源）
- [x] 前端单测：点失败标签弹层含原因+详情（document.body 定位，按内容文本找面板——el-dialog 自定义 class 落点随版本漂移）；非失败行无 .fail-tag（先红验证经调试用例确认 ElTag 路径不通后实现）
- [x] 前端四门全绿（lint/type/vitest 338/build）
