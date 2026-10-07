# 文档删除（F-305）设计评审材料（2026-10-06）

> 评审对象：文档删除端点（features.md F-305 细化、api.md §2.10c 已先行落稿）。
> 流程：本材料评审**通过并确认待修改文件清单**后，进入代码实施。
> 背景：known-issues 开放-7——F-305 是 MVP 承诺未兑现项，语料下线只能 DBA 软删；前端"删除"按钮置灰（正确表现）。

---

## 1. 设计决策

| 决策点 | 方案 | 依据 |
|---|---|---|
| 删除语义 | **软删**（deleted_at+is_active=false），不做物理删除 | standards §6.6 两段式；物理清理归既有 retention 任务 |
| chunks 处置 | 级联 `is_active=false`（**向量与文本行保留**） | 检索走 is_active 部分索引，软删即退出检索；保留行便于追溯与"重传同内容查重拦截"语义 |
| document_images | 行保留（不删） | 回显/原文件端点走文档闸（软删后 404），行本身无害 |
| 解析中禁止删除 | status=parsing → 422"解析中不可删除，请等待终态" | 防与后台任务竞态（任务收口会覆写状态） |
| 引用检查 | **不阻塞**：提问记录保留期内的历史引用按快照展示（chunk 行保留），仅"存活闸"不再放行软删文档 | 与 citations 存活闸（is_active+ready）一致 |
| 权限 | admin-only（与文档治理域一致）；member 403/2004 | 语料治理是管理员职责 |
| 二次确认 | 前端弹窗展示标题+"删除后不可恢复"说明 | 既有 confirm 模式 |

## 2. 待修改文件全清单（评审核心对象，9 个）

### 后端（5）
| # | 文件 | 变更 |
|---|------|------|
| 1 | app/services/ingest.py | `delete_document(session, clock, doc, actor)`：软删+级联停用 chunks+审计 `document.delete`；parsing 态 422 |
| 2 | app/api/documents.py | `DELETE /{doc_id}` 路由（AdminUser）+ `_viewable_document` 复用/admin 直查 |
| 3 | app/api/document_rebuild.py | 若有同类保护（parsing 态）对齐——确认项 |
| 4 | frontend/src/api/documents.ts | `deleteDocument(id)` helper |
| 5 | frontend/src/components/documents/UploadDialog.vue | 无改（确认项） |

### 前端（2）
| # | 文件 | 变更 |
|---|------|------|
| 6 | frontend/src/views/DocumentsView.vue | 删除按钮解除置灰（挂二次确认 ElMessageBox + deleteDocument + 刷新）；tooltip 移除 |
| 7 | tests 单测/集成 | 删除用例（软删断言/成员 403/parsing 422/审计读回） |

### 文档（3）
| # | 文件 | 变更 |
|---|------|------|
| 8 | docs/known-issues.md | 开放-7 收口 |
| 9 | docs/manual-test-guide.md | M2-T13 改为可删除（二次确认/审计） |

### 明确不做
- 物理删除（两段式 retention 归既有任务）
- 批量删除（登记 backlog）
- 解析中任务的取消再删（等终态后删，语义简单可靠）

## 3. 测试计划

| 层 | 用例 |
|---|------|
| services 单测/集成 | 软删字段断言（读回 deleted_at/is_active）；chunks 级联停用断言；parsing 态 422；审计 document.delete 读回 |
| api | admin 200/成员 403/未登录 401/parsing 422；删除后列表不显示、检索不命中、引用原文 404 |
| 前端单测 | 删除按钮可点、确认弹窗、成功后行消失 |
| E2E | DOC 系新增删除用例（删后列表无行、重传同内容不再查重拦截——软删行不占 hash） |

## 4. 回归保障

- 只增端点与前端按钮解禁：既有上传/解析/zip/引用链路零改动
- 全量回归（后端解析相关 + E2E DOC/SVC）为完成标准

## 5. 风险

| 风险 | 缓解 |
|------|------|
| 误删无法恢复 | 软删可 DBA 恢复；二次确认弹窗含标题 |
| parsing 态删除竞态 | 422 引导等终态（与实现一致） |
| 成员引用历史展示失效 | 引用历史按快照展示为既有行为，不因删除改变 |
