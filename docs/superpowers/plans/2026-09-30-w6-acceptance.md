# W6 缺陷修复与验收准备 实施计划（TDD）

> **For agowers/agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** 交付 W6 增量——eval detail-stall 缺陷修复（W6 首修）、ingest StubEmbedder 修复提交收口、语料治理落地（串库真实阈值问题的根因处置）、前端小修包、runbook/文档收尾；产出**七条硬指标状态对照表**为验收评审备料。

**Architecture:** 双车道延续——A 道（主工作树）：代码修复与语料治理；B 道（worktree）：文档收尾。主工作区有**用户并行会话在途文件**（tei.py/docker-compose.yml/golden PDF/download_models.py/AGENTS.md/.env.example/project-brief.md/compliance-checklist），全程不可触碰；A 道含一笔"接续提交"（documents.py 的 ingest embedder 修复已在工作树，补测试后收口）。

**Tech Stack:** 沿用 W1-W5。

## Global Constraints

- 修复先写复现测试（§15.8），复现脚本已存 `.zcode/accept/repro_detail_stall.py`（eval detail-stall）
- 用户在途文件神圣不可触碰；提交前 `git log` 核对 HEAD 未被并行会话推进；pathspec 限定
- 语料治理口径（W5 部署验收裁决）：**部门专属内容（考勤/休假等 HR 域）撤出全员手册**——全员手册仅保留全员通用内容；HR 域内容独立成 hr 部门文档
- 串库复测按 runbook §5.5 两场景口径（stub 下场景一结构断言+场景二空范围拒答；真实 embedding 行为判据留部署机）
- 测试读回断言/TDD 先红后绿/中文标题；ruff+前端四连全绿；函数 ≤50 行/文件 ≤300 行（app/）

---

### Task 1（A 道）: eval detail-stall 修复 + ingest embedder 修复收口

**Files:** Modify: `app/services/eval_engine.py`, `app/api/documents.py`（已在工作树，提交收口）; Test: `tests/integration/test_eval_engine.py` 追加
**背景:** 部署验收发现跑分逐题 detail 提交失效（仅首题与 _finish 落库，中途停首题快照）；复现脚本 `.zcode/accept/repro_detail_stall.py`（读它）。ingest 侧 StubEmbedder 泄漏修复已在 `app/api/documents.py` 工作树（_build_deps 走 build_embedder 工厂），未提交。

- [ ] Step 1：跑复现脚本确认现状红 → 定位根因（怀疑方向：逐题 detail 更新是**原地变异**未触发 JSONB 变更跟踪，或 commit 会话与 run 加载会话错位——以实测为准）→ 修复合跑分路径。
- [ ] Step 2：追加回归测试（多题跑分中途查询 GET /{id}：detail.items 长度随进度增长——先红后绿）。
- [ ] Step 3：documents.py 修复收口：核对工作树 diff（_build_deps 走 build_embedder）仍在→ `git log` 确认 HEAD → pathspec 提交 `fix(api): wire ingest embedder through assembly factory`；补一条 test_assembly 层断言已存在则不动。
- [ ] Step 4：全量 pytest + 前端四连 → 提交（2-3 笔）→ 报告 task-1-report.md
- [ ] 评审要点：根因解释与修复正确性；逐题进度测试判别力；documents.py 提交仅含该文件

### Task 2（A 道）: 语料治理落地 + 串库复测

**Files:** Create: `data/pilot/员工手册_全员版.docx`, `data/pilot/HR手册_试点.docx`（生成脚本内联即可）; Modify: 试点库数据（API 操作）
**Interfaces:** 治理内容（W5 部署验收裁决）：

- 全员版 v1.2：**撤出第一章 考勤与休假（HR 域）**，保留 工作时间（考勤打卡通用口径可留——裁决：工作时间属全员通用，年假/病假属 HR 域撤出）、报销制度、售后服务；加版本行 v1.2
- 新增 HR 手册（dept=hr）：考勤与休假（年假 5/10/15 天、跨年结转至次年 3 月 31 日、病假 30 天复查/80% 工资）
- 试点库操作：上传两份新文档（各对应部门）→ 等 ready → 旧全员版 v1.1 无删除端点——**登记 runbook 演练记录**（旧文档以内容前缀"v1.1 已被 v1.2 取代"认知处理，正式下线能力待 F-305 删除端点 W6 后补——本任务在报告中登记该缺口）
- [ ] 串库复测（stub 场景二口径）：perspective=service 跑分 → hr 题 10/10 拒答回归（HR 内容已撤出其可见范围）；结果登记 runbook §5 表
- [ ] 报告 task-2-report.md；提交（若产出脚本则入库，纯 API 操作仅报告）

### Task 3（A 道）: 前端小修包

**Files:** Modify: `frontend/src/components/overview/KnowledgeGapCard.vue`（load 错误态，对齐 ZeroRecallChunkPanel error 态）, `frontend/src/utils/format.ts`（新建：fmtTime 收敛）+ 三处引用（RetrievalTestView/ZeroRecallChunkPanel/OverviewView、AuditView）; Test: 对应 spec 追加
**背景:** W4/W5 终审 minors：KnowledgeGapCard load 无错误态；fmtTime 三处重复。ZeroRecallChunkPanel loaded 乐观置 true 无重试（评估一行修正：失败时 loaded 回退）。
- [ ] 实现 + vitest（错误态渲染/fmtTime 单测）→ 前端四连绿 → 提交 `fix(frontend): gap card error state and shared time format util`

### Task 4（B 道）: 文档收尾

**Files:** Modify: `docs/runbook.md`（§1/§4 登记 TEI 8091/rerank 8082/mineru 8090 用户 dev 端口——用户并行会话新增服务）, `docs/user-guide.md`（语料治理同步：全员手册内容变化提示——若手册章节有变）, `docs/specs/ui-mockups/deviations.md`（遗留登记核对）
**Interfaces:** 纯文档；B 道 worktree `../RagDemo-w5b` 续用（feat/w5-ops → 新分支 feat/w6-docs 或续用，控制器定：续用分支）
- [ ] 对照 docker-compose.yml 现状登记端口表 → runbook §4 核查清单更新 → 提交 `docs: register dev service ports and close w5 leftovers`

### Task 5（验收）: 七条硬指标状态对照 + 内测演示走查

**Files:** Create: `docs/acceptance-status.md`（七条硬指标 × 状态（本机已证/待部署机/待用户）× 证据指针）
**Interfaces:** W5 账本终审节 + 部署验收报告为据：

- #1 分部门准确率 ≥80%：**待题库**（用户 Top30-50 未到；机制就绪：评测跑分+评判+报告已实测）
- #2 引用覆盖率 100%：**本机部分已证**（E2E 引用链路通；脚本机检待真实语料）
- #3 串库 100% 拒答：**结构面本机已证**（0 泄漏）；行为面待部署机真实 embedding（且先语料治理——T2 落地后复测）
- #4 知识不足 0 编造：**待部署机**（本机已实证弱上下文编造案例——语料治理+阈值调整后复评）
- #5 golden 回归：**本机已证**（11 passed/2 skipped，真实 docx+真实 embedding）
- #6 审计可查：**本机已证**（audit API+CSV 已实测）
- #7 非功能基线：**不达标（本机 CPU TEI+负载）**——TEI GPU/独占资源需求已升级，待部署机
- [ ] 内测演示走查一遍（登录→问答→引用→点踩→管理页全走）→ 报告 task-5-report.md → 提交 `docs: acceptance status matrix for pilot review`

---

## 并行与合并纪律

- A 道（主工作树）：T1 → T2 → T3 串行；B 道（worktree）：T4 单任务——**开工即并行**
- B 道合并后 T5 验收（主工作树）
- 用户并行会话风险：每任务提交前核对 HEAD；在途文件清单见 Global Constraints
