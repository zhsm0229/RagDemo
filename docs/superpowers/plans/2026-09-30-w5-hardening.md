# W5 内测加固与演练 实施计划（TDD）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 W5 增量——W4 交接清扫（全局 401 统一/部门口径统一/qa-pairs deptCode/杂项 chore）、可裁剪项转正（知识缺口报告 F-1010、chunk 召回统计收尾 F-1011）、备份与恢复基建+runbook 定稿（§18）、用户手册（§19.4）、串库评测数据包与演练流程；以**恢复演练实测**和**串库双向评测走查**收口。

**Architecture:** 双车道延续——A 道（主工作树）：前后端代码收尾；B 道（worktree）：纯文档与部署脚本（零测试）。T9 验收任务（测试者角色）在双道合并后执行真实演练。W4 已合并 frontend/ 进 main，A 道可同时动 app/ 与 frontend/。

**Tech Stack:** 沿用 W1-W4（后端 FastAPI/前端 Vue3/deploy 脚本 bash+psql）。

## Global Constraints（standards + W4 账本交接，逐条适用）

- W4 交接项按终审 triage 执行：必修/带走项见 [w4 账本终审节](../../.superpowers/sdd/2026-09-29-w4-frontend/progress.md)；本计划 A1/A2 落代码项，其余 chore 归 A5
- SQL 参数绑定；审计留痕；AdminUser 门；camelCase DTO；测试读回断言+TDD 先红后绿；中文标题；函数 ≤50 行/文件 ≤300 行（app/）；前端校验器只用 src/validators（§21.3）；禁 v-html；提交 pathspec 限定
- **部门口径统一裁决（W4 终审⑧）**：zip 导入与元数据建议共用同一别名表 `_DEPT_ALIASES`（中文与英文 code 双收），归一为 code 后走 `_resolve_dept` 枚举闸——傻瓜式目标下中文命名两处同权
- §18 备份：每日全量 pg_dump 保留 7 天、上传目录同步、备份目录权限 0700；RPO ≤24h / RTO ≤2h（W5 演练实测记录）；**runbook 先于演练产出**
- §19.4 user-guide 独立成册；§7.6 RAGFlow 禁云 key；能不加依赖就不加（备份用 pg_dump+tar 原生工具）
- 串库验收口径（features 硬指标 #3）：双向各 ≥10 题 100% 拒答 + 全员对照题组防假阴性
- 工作区预存改动（.env.example/project-brief.md staged，用户所有）任何任务不得触碰

## File Structure

```
A 道（主工作树 main）
  frontend/src/api/client.ts          # A1 全局 401 拦截
  app/services/zip_import.py + metadata_suggest.py  # A2 部门口径统一（别名表抽公共）
  app/services/qa_pairs.py + components/qa/options.ts # A2 deptCode
  app/services/audit_query.py（注释）  docs/api.md（口径句）  deviations.md（解禁标注×4）
  app/api/admin_views.py 或新 knowledge_gap.py        # A3 F-1010
  app/api/admin_retrieval_test.py + services/retrieval.py # A4 F-1011
  tests/conftest.py + tests/**（helper 替换） .gitattributes  # A5
B 道（worktree ../RagDemo-w5b，分支 feat/w5-ops）
  deploy/backup.sh  docs/runbook.md（定稿）        # B6
  docs/user-guide.md                              # B7
  tests/fixtures/tools/make_crosstalk_suite.py + docs/runbook.md 串库节  # B8
验收（合并后，主工作树）
  恢复演练实测 + 串库双向走查 + W5 收口核对             # T9
```

---

### Task 1（A 道）: 全局 401 统一（前端）

**Files:** Modify: `frontend/src/api/client.ts`, `frontend/src/stores/auth.ts`, `frontend/src/api/chat.ts`（流路径）, `frontend/src/router/`（如需）；Test: `frontend/tests/unit/`（client 守卫用例）
**Interfaces:** Produces: 任意 API 响应 401/2003（含 SSE 开流前）→ auth store 清态 → 跳 /login（**/login 与 /change-password 自身不循环**；must_change 状态除外——改密页 401 不应再循环到 /login？改密前用户本来未完整登录，落 /login 正确）；chat 流中 error 5001 不受影响（仅 401/2003 触发登出）；登出动作复用既有 logout（清 cookie 不做——后端无端点，仅前端态，登记已知）

- [ ] 失败测试：mock 401 → store 清空+路由 /login；/login 页 401 不再跳转；流路径 401 同通道 → 实现 → 四连验证绿 → 提交 `feat(frontend): global 401 handling with logout redirect`
- [ ] 同步：deviations 或 W4 遗留 #21 的 401 条目核对更新

### Task 2（A 道）: 部门口径统一 + qa-pairs deptCode + 口径说明批

**Files:** Modify: `app/services/metadata_suggest.py`（导出 DEPT_ALIASES）, `app/services/zip_import.py`（改用别名表，双收中文）, `app/services/qa_pairs.py`（DTO 补 deptCode）, `app/services/audit_query.py`（不变式注释）, `docs/api.md`（qa-pairs DTO 一键 + messages/overview 口径句）, `docs/specs/ui-mockups/deviations.md`（#21/#22/#44/#45 解禁标注）, `frontend/src/components/qa/options.ts`（切换真实 deptCode，删种子 id 映射）; Test: 对应 spec/api 测试
**Interfaces:** Produces:

- zip 条目元数据解析：`客服_主题_20260901.docx` 与 `service_...` 双收（归一 code 后仍过 `_resolve_dept` 闸）；错误文案更新（"部门须为：客服/人力/全员 或 service/hr/all"）
- qa-pairs 列表 item 增 `deptCode`；前端 options 部门下拉/列渲染改用 deptCode（移除种子 id 硬编码映射与"部门#N"兜底）
- api.md §4.3/§4.5 补一句口径："提问记录与运营概览包含已删除会话的消息（管理审计面，删除仅前端隐藏）"

- [ ] 失败测试：zip 中文名条目 accepted；qa-pairs DTO 含 deptCode 且值正确；前端 spec 断言部门列渲染真实 code → 实现 → 双端验证绿 → 提交 `feat(admin): unify dept alias handling and expose deptCode` + `docs: log resolved w4 items`

### Task 3（A 道）: 知识缺口报告（F-1010）

**Files:** Create: `app/services/knowledge_gap.py`, `app/api/admin_knowledge_gap.py`；Modify: `app/main.py`, `frontend/src/views/OverviewView.vue` + api 模块; Test: `tests/api/test_knowledge_gap.py` + 前端 vitest
**Interfaces:** Produces:

- `GET /api/admin/knowledge-gap?weekOffset=0`（AdminUser）：聚合**拒答用户问题**——messages role='user' AND is_eval=false，LATERAL 配对其 assistant 行 answer_type='refuse'（复用 admin_views 配对先例），按 content 分组 count DESC limit 50 → `{items:[{question, count}], total, weekStart, weekEnd}`（weekOffset 按 clock 推算周一为界）
- `GET /api/admin/knowledge-gap/export?weekOffset=` → CSV（BOM+中文列头 问题/出现次数；截断保护同 audit 先例）
- 前端 P10 新增"知识缺口 TOP"卡片（top10 表格+周切换+导出按钮；空态引导"暂无拒答——运行评测或等待用户提问"）
- [ ] 失败测试（拒答/非拒答/is_eval 混合种子→聚合数字/分周边界/CSV/权限门 + 前端渲染）→ 实现 → 双端绿 → 提交 `feat(admin): knowledge gap report and export`

### Task 4（A 道）: chunk 召回统计收尾（F-1011）

**Files:** Modify: `app/services/retrieval_test_views.py`（docs[] 补 hitCount/lastHitAt）, `app/api/admin_views.py` 或新路由（零召回块清单）, `frontend/src/views/RetrievalTestView.vue`; Test: `tests/api/test_retrieval_test.py` 追加 + `tests/api/test_zero_recall_chunks.py`
**Interfaces:** Produces:

- 检索测试响应 docs[] 每块补 `hitCount`/`lastHitAt`（读现列，不写）
- `GET /api/admin/zero-recall-chunks?days=30&page&pageSize`（AdminUser）：`chunks.is_active AND (last_hit_at IS NULL OR last_hit_at < now-days)` JOIN documents（title/deptCode）→ `{items:[{chunkId, documentTitle, deptCode, titlePath, createdAt}],total}`；**零召回判定口径与 F-1009 文档级一致**（docstring 互指）
- 前端 P5：结果表补"历史命中"列；页内"零召回块清单"折叠区（表格+分页）
- [ ] 失败测试（hit_count 种子→响应补字段读回/清单边界 FakeClock/权限门 + 前端列渲染）→ 实现 → 双端绿 → 提交 `feat(admin): chunk recall stats completion`

### Task 5（A 道）: chore 批（helpers 上收 + .gitattributes）

**Files:** Modify: `tests/conftest.py`（login fixture）、18 个测试文件（helper 替换）、新增 `.gitattributes`
**Interfaces:** Produces:

- `tests/conftest.py` 增 `login_as(client, username, password) -> None`（cookie jar 登录）与 `admin_login(client)` fixture 式辅助；逐文件替换本地 `def login(` 定义为 import/调用（**机械替换，断言零改动**；每文件替换后该文件测试跑绿再下一个——分批提交防大爆炸）
- `.gitattributes` 最小版：`frontend/** text eol=lf`（修 W4 autocrlf 临时解的根）；验证：`git add --renormalize frontend` 无幽灵 diff、前端四连绿
- EP 按需引入/.npmrc registry：**裁决登记不做**（内网 chunk 1MB 可接受、lock integrity 已锚）——写入 W4 账本 follow-up 终态

- [ ] 分批替换（每批 4-6 文件）+ 全量 pytest 绿 → 提交 `test: consolidate login helpers into conftest` + `chore: normalize frontend line endings`

### Task 6（B 道）: 备份与恢复基建 + runbook 定稿

**Files:** Create: `deploy/backup.sh`；Modify: `docs/runbook.md`（③备份/恢复章节定稿 + 串库演练节占位由 B8 补）
**Interfaces:** Produces:

- `deploy/backup.sh`（bash，set -euo pipefail）：①`docker exec ragdemo-pg pg_dump -U rag rag` → `backups/rag-YYYYMMDD-HHMMSS.sql`（gzip）②uploads 目录 tar（`STORAGE_DIR` 默认 ./data/uploads）③保留 7 天清理（find -mtime +7）④每步日志 + 失败非零退出；参数化（库名/容器名/目录），Windows 计划任务与 Linux cron 两版调度示例写 runbook
- runbook ③定稿：每日备份（调度示例）/手动备份/恢复步骤（**两种口径**：恢复到新建库验证用 vs 覆盖生产库——各自命令序列与前置检查）/演练记录表（日期|备份大小|恢复耗时|RTO|RPO|执行人|备注）
- [ ] `bash -n deploy/backup.sh` 语法校验 + runbook 命令逐条可复制执行核对 → 提交 `feat(deploy): backup script and runbook finalization`

### Task 7（B 道）: 用户手册（§19.4）

**Files:** Create: `docs/user-guide.md`
**Interfaces:** Produces: 面向试点员工与 impatient 管理员的操作手册（全中文步骤式）：

- 员工篇：登录与首登改密/提问（1-2000 字、引用角标与原文查看、拒答的含义——"知识不足时拒答是为了不编造"）/点踩反馈（分类+备注）/缓存标注/会话管理
- 管理员篇：知识库（上传三要素+合规勾选/状态含义 pending→parsing→ready/failed 重试/降级解析/到期日期）、问答对（模板/导入错误行/启停）、检索测试（两套分数含义）、用户管理（发放/禁用/重置）、评测（跑分/录分/阈值扫描）、提问记录与 badcase 处置、概览与健康度、审计导出
- 常见问题：为什么拒答/为什么显示"来自缓存"/上传失败原因对照/账号锁定
- **文案核对**：逐节对照 frontend/src/views 实际界面文案（术语 §8.3 一致），不一致处以界面为准并登记
- [ ] 提交 `docs: user guide for pilot`

### Task 8（B 道）: 串库评测数据包 + 演练流程

**Files:** Create: `tests/fixtures/tools/make_crosstalk_suite.py`；Modify: `docs/runbook.md`（串库演练节）
**Interfaces:** Produces:

- 生成脚本：`python tests/fixtures/tools/make_crosstalk_suite.py -o crosstalk.xlsx` → 双向题库 xlsx（**hr 视角题 10 条**：dept=hr、qtype=cross_reject、expected=[拒答]——供 perspective=service 跑分；**service 视角题 10 条**对称；**全员对照 4 条**：dept=all、qtype=factual、期望答案注明"任一视角应命中"——防假阴性）；题目内容合成（HR 制度类/客服售后类通用问题，标注"演练用合成题"）
- 脚本可重复运行（覆盖输出）；单测：生成 → xlsx_io 读回 → 行数与列结构断言
- runbook 串库演练节：导入题库 → perspective=service 跑分 → 断言 hr 题 100% 拒答 + 全员对照全对 → perspective=hr 对称 → 报告存档 eval_runs（config 快照锁定）→ 结果登记表
- [ ] 单测绿 + runbook 流程核对 → 提交 `test: crosstalk eval suite generator and drill procedure`

### Task 9（验收，双道合并后）: 恢复演练实测 + 串库走查 + W5 收口

**Files:** Modify: `docs/runbook.md`（演练记录表实填）; Test: 实测记录
**Interfaces:** Produces: RPO/RTO 实测数据 + 串库流程验证 + W5 收口核对

- [ ] **恢复演练**：起 compose pg → 造数（上传 1 文档+1 问答对+若干消息）→ 跑 backup.sh → 记备份大小/耗时 → **drop 并重建空库 → restore** → 读回断言（documents/qa_pairs/messages count 与关键行一致；alembic_version 正确）→ RTO 计时 → runbook 记录表实填一行 → 恢复后服务可登录可问答（冒烟）
- [ ] **串库走查**：test_mode 起前后端 → 导入 crosstalk.xlsx → 双 perspective 跑分 → 断言串库方向 100% 拒答 + 全员对照命中 → 报告与 runbook 登记
- [ ] **W5 收口核对**：§23.3 风险表逐项 + features 验收硬指标对照表勾状态（哪些已满足/哪些待部署机）
- [ ] 提交 `docs: w5 drill records and closure checklist`

---

## 并行与合并纪律

- **双车道**：A 道（主工作树，T1-T5 前后端代码）；B 道（worktree `../RagDemo-w5b` 分支 `feat/w5-ops`，T6-T8 纯文档部署零测试）。文件面零交集（A=app/tests/frontend/.gitattributes；B=deploy/docs-runbook/user-guide/fixtures-tools）
- B 道合并无冲突预期；T9 在合并后主工作树执行（测试者角色，真实演练）
- 评审流水线照旧：任务 N 评审（A 静态用缺省库证据/B 纯静态）∥ N+1 实现

## 里程碑风险预注册（§23.3）

| 风险 | 约束措施 |
|------|---------|
| helpers 上收大爆炸（18 文件） | 分批替换每批跑绿；断言零改动纪律 |
| .gitattributes renormalize 幽灵 diff | 最小作用域 frontend/**；renormalize 后核对无幽灵 M 才提交 |
| 恢复演练破坏数据 | 演练用测试库/专用库（禁直接对 dev 库 drop）；restore 目标新建库验证 |
| 知识缺口聚合性能（全表分组） | weekOffset 窗口内过滤先行；limit 50；试点量级可控 |
| 串库演练在 stub 下意义有限 | 流程验证为目的；真机口径（真实 embedding+阈值）在 runbook 注明部署机执行 |
