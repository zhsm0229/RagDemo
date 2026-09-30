# W3 管理后台与评测 实施计划（TDD）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 W3 增量——问答对管理（M4）、评测（M6）、管理后台（M7）后端全量 + W2 交接修复（改题语义/缓存失效/F-702 裁决/CI 收紧）+ 提问记录保留策略（F-803）+ 健康度与运营概览数据 API（F-1009/F-704）+ RAGFlow 基线环境配置。W3 完成后**后端 API 全部就绪**，W4 进纯前端冲刺。

**Architecture:** 延续五层单向依赖与依赖注入。Excel 处理收敛单一模块 `services/xlsx_io.py`（openpyxl），问答对与评测题共用整批校验器。评测跑分引擎复用 chat 管道（`prepare_turn` + 评测专用 ChatDeps 副本实现缓存旁路与日限豁免），后台执行（BackgroundTasks，§1.3），config 快照锁阈值（F-602）。管理查询全部 `AdminUser` 门（§5.5）。

**Tech Stack:** 沿用 W1/W2 + **openpyxl**（新增，§2.8 登记：xlsx 导入导出唯一用途）。

## Global Constraints（standards.md + W2 账本移交，逐条适用于每个任务）

- **Excel 整批校验（§21.5/§21.6）**：单文件 ≤5000 行；单条答案 ≤10000 字、问题 1-2000；列结构校验；错误行**全部**返回（行号+中文原因），禁止半批入库；空文件报"无数据"
- **列表规范（§22）**：筛选区（文本/日期配查询+重置，下拉即刷）、服务端分页 page≥1/pageSize 1-100 默认 20、默认 created_at DESC；关键字 ILIKE 必须转义 `%`/`_`/`\`
- **审计（§4.7）**：qa.import/qa.toggle/qa.update/qa.delete/eval.import/eval.run/eval.judge/badcase.update 100% 落 audit_logs（操作人/时间/动作/对象/detail）；审计只增不改
- **admin 门（§5.5）**：全部 `/api/admin/**` 路由挂 `AdminUser` 依赖；403 语义沿用
- **评测隔离（F-5/F-602/§17.5）**：messages.is_eval=true 落库不污染运营统计（M7 查询一律过滤 is_eval=false）；评测**旁路缓存**（独立 ChatDeps 副本）且**不占每日限额**（不调 try_consume）
- **分数口径（§4.4，RET-03）**：检索测试响应中问答对相似度=embedding 余弦、文档块分数=rerank sigmoid，两套分数分字段呈现不混用
- **检索测试不写 hit_count**（不污染零召回榜 F-1009 口径）
- **迁移仅追加**（§4.5）；新列必须可空或带默认值（§13.4 回滚红线①）
- SQL 参数绑定；拒绝裸 except；ApiError 统一错误；响应体 `{code,message,data}` camelCase；测试中文标题+读回断言+TDD 先红后绿；文件 ≤300 行/函数 ≤50 行；提交 pathspec 限定；本机无 make 用 `.venv/Scripts/python -m` 直等命令

## File Structure

```
pyproject.toml + uv.lock        # openpyxl 登记
alembic/versions/0002_qa_active.py   # qa_pairs.is_active（软删，§4.1）
app/services/
  xlsx_io.py                    # 新建：read_rows/write_template（整批校验基建）
  qa_pairs.py                   # 新建：M4 导入/列表/启停/编辑/删除
  eval_engine.py                # 新建：M6 跑分引擎（后台任务体）
  eval_service.py               # 新建：M6 题库导入/评判提交/报告聚合
  admin_views.py                # 新建：M7 提问记录/badcase/检索测试/概览/健康度查询
  retention.py                  # 新建：F-803 两段式清理任务体
  chat_cache.py                 # 修改：ExactCache.clear()（锁内）
  retrieval.py                  # 修改：match_qa 增 is_active 过滤
  chat_stream.py / chat.py      # 修改：复用会话改题语义
  scheduler.py                  # 修改：挂保留策略 sweep
  settings→config.py            # 修改：retention 两键 + 删 generation_temperature
app/api/
  admin_qa_pairs.py  admin_eval.py  admin_views.py   # 新建三组 admin 路由
  documents.py / feedback.py    # 修改：管理写端点接 cache.clear()；down 置状态条件化
  main.py                       # 修改：注册 admin 路由
deploy/docker-compose.ragflow.yml  docs/runbook.md   # 新建：RAGFlow 基线（§7.6/§12.2）
docs/api.md                    # 追加 W3 端点
tests/                         # unit/test_xlsx_io unit/test_eval_engine 等 + api/ + integration/
```

---

### Task 1: W2 交接修复波（改题语义 / F-702 裁决 / 缓存失效接线 / CI 收紧 / 冗余配置清理）

**Files:** Modify: `app/services/chat_stream.py`, `app/api/feedback.py`, `app/services/chat_cache.py`, `app/api/documents.py`, `app/core/config.py`, `.env.example`, `.github/workflows/ci.yml`, `docs/api.md`；Test: 对应用例文件
**Interfaces:** Produces: `ExactCache.clear()`（锁内整清）；复用会话首问改题；down 不重置 admin 处理态

- [ ] **Step 1（改题，先红）** 复用 POST /api/conversations 所建会话发起首轮提问 → 会话 title 仍"新会话"（现状）。改：事务①解析会话后，若 `title in ("新会话", "")` → `title=question[:20]`；第二轮提问不再改题（title 已非默认值）。用例两条：首轮改题读回、二轮保持。api.md §3.2 同步。
- [ ] **Step 2（F-702 裁决落地，先红）** feedback.py down 分支：`badcase_status` 仅在 `IS NULL 或 '待处理'` 时置'待处理'；**'已修语料'/'已忽略' 保持不动**（comment/category 照常更新，信息不丢）。用例：先 PATCH admin 状态（Task 9 前可手工 UPDATE 模拟）为'已忽略'→ 用户再次 down → 读回状态仍'已忽略'且新备注生效。控制器裁决（W2 账本移交③）：admin 处理终态不被用户改评重开；W3 Task 9 的管理界面消费同一语义。
- [ ] **Step 3（缓存失效）** `ExactCache.clear()`（锁内 `_entries.clear()`，docstring 注明并发契约）+ 单测。接线：`documents.py` 的 retry/confirm 成功响应前 `request.app.state.chat_deps.cache.clear()`（归档/激活改变可检索事实，TTL 不可依赖——W2 移交②）。用例：预热缓存→confirm→旧回答不再命中缓存（stream_calls 恢复增长）。
- [ ] **Step 4（CI 收紧）** `uv pip install pip-audit==<当前版> mypy==<当前版>` 钉版（版本查 uv.lock 或 pip index；写入 ci.yml）；lint/type job 开 `enable-cache: true` 与 test 一致。type job 保持非门禁（advisory）不变。
- [ ] **Step 5（冗余清理）** 删 `Settings.generation_temperature` 与 .env.example 对应行（I-2 修复后 manifest.temperature 为唯一生效源，死配置删除；`.env` 多余键 extra=ignore 不破坏）。
- [ ] **Step 6** 全量回归 + ruff → **Step 7** 提交（多笔，pathspec 限定）：`fix(api): title reuse conversation on first question` / `fix(api): preserve admin badcase status on re-feedback` / `feat(services): cache invalidation hooks for document ops` / `ci: pin audit tools and enable uv cache` / `chore(core): drop dead generation temperature setting`。

### Task 2: openpyxl 依赖 + xlsx 整批校验基建

**Files:** Modify: `pyproject.toml`, `uv.lock`；Create: `app/services/xlsx_io.py`, `tests/unit/test_xlsx_io.py`
**Interfaces:** Produces:

```python
class XlsxBatchError(Exception):      # 聚合错误行后统一抛，由 API 层转 422
    def __init__(self, errors: list[dict]) -> None  # [{row: int, field: str|None, reason: str}]
def read_rows(data: bytes, headers: list[str], max_rows: int, *, required: set[str]) -> list[dict[str, str]]
    # 表头逐字匹配（顺序不限但缺列/多列报错）；行号从 2 起计（含表头行）；
    # 超 max_rows → XlsxBatchError([{row:0, reason:"文件超过 N 行上限"})]；无数据行 → 报"无数据"（QA-08）
def write_template(path_headers: list[str], sample_rows: list[list[str]]) -> bytes
```

- [ ] **Step 1** pyproject 增 `openpyxl==3.1.5`（uv sync 更新 uv.lock 一并提交；§2.8 用途：问答对/评测题 Excel 导入导出）。
- [ ] **Step 2 失败测试**：①正常 3 行读取（表头乱序文件按名映射）②缺列/多列 → 结构错误 ③51 行（max 50）→ 文件级 ④空数据 → "无数据" ⑤必填列空单元格 → 行级错误行号正确 ⑥write_template 读回表头与示例行等值。
- [ ] **Step 3** 实现 → **Step 4** 绿 → **Step 5** 提交 `feat(services): xlsx batch io foundation`。

### Task 3: M4 问答对 Excel 导入（F-401）+ 模板下载（F-403）

**Files:** Create: `app/services/qa_pairs.py`, `app/api/admin_qa_pairs.py`；Test: `tests/api/test_admin_qa_import.py`
**Interfaces:** Produces:

- `POST /api/admin/qa-pairs/import`（multipart `file`，AdminUser）：xlsx 列 `问题/答案/状态`（状态 ∈ enabled|disabled，缺省 enabled）→ 整批校验（问题 1-2000、答案 1-10000、状态枚举）→ 有错误行：422/1003 `data=[{row, field, reason}]` 全部报告且**零入库**；全合格：单事务入库 + embedding **分批 ≤256 条**调 embedder + `batch_no=f"QA-{YYYYMMDD}-{4位随机大写}"` + 审计 `qa.import` detail `{total, batchNo}` → 201 `{imported, batchNo}`
- `GET /api/admin/qa-pairs/template` → xlsx 字节流（表头+3 行示例，`Content-Disposition: attachment`，QA-模板交付 §8.10）

- [ ] **Step 1 失败测试**（api 层，test_mode Stub deps 经 app.state——embedder 用 StubEmbedder）：①合规 3 行 → 201、读回 3 行含 embedding 非空向量、batch_no 相同、审计在；②含空答案行+超长答案行 → 422 且 data 恰 2 条错误行、库零新增（QA-02）；③5001 行 → 文件级拒（QA-03）；④单答案 10001 字 → 行级错（QA-03）；⑤空表 → "无数据"（QA-08）；⑥模板下载 → 200 + xlsx 可被 read_rows 读回。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): qa pair batch import and template`。

### Task 4: M4 管理（F-402/QA-06）+ is_active 迁移 + 缓存失效

**Files:** Create: `alembic/versions/0002_qa_pair_active.py`；Modify: `app/services/qa_pairs.py`, `app/api/admin_qa_pairs.py`, `app/services/retrieval.py`；Test: `tests/api/test_admin_qa_manage.py`, `tests/integration/test_retrieval.py`（追加）
**Interfaces:** Produces:

- 迁移 0002：`ALTER TABLE qa_pairs ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE`（中文注释：软删位，删除后退出检索；带默认值满足回滚红线①）；ORM `QaPair.is_active` 列
- `GET /api/admin/qa-pairs?deptId&status&keyword&page&pageSize`：keyword 对 question/answer ILIKE（**转义 %_\**），`{items:[{id,deptId,question,answer,status,batchNo,createdAt,isActive}],total,...}` DESC
- `PATCH /api/admin/qa-pairs/{id}/status {status}` → 审计 `qa.toggle {from,to}` + `cache.clear()`
- `PUT /api/admin/qa-pairs/{id} {question,answer}` → 重新向量化（单条 embed）+ 审计 `qa.update {before/after 摘要}` + `cache.clear()`（QA-06：修改后新答案生效）
- `DELETE /api/admin/qa-pairs/{id}` → `is_active=false`（软删）+ 审计 `qa.delete` + `cache.clear()`（QA-07 删除后不再命中）
- `retrieval.match_qa` 过滤追加 `is_active`（status+is_active 双闸；既有 QA-05 用例语义不变）

- [ ] **Step 1 失败测试**：①列表分页/DESC/keyword 过滤+转义（含 `%` 关键字不展开为通配）②toggle 后检索不命中（QA-05 走 API 层复验）③删除后列表不见且检索不命中（QA-07）④编辑后新答案生效（QA-06：编辑"退货时限"答案→提问命中新答案文本）⑤三操作审计读回⑥缓存清空生效（预热→toggle→同问题重新走检索）。集成追加：match_qa 过滤 is_active=false。
- [ ] **Step 2** 实现（含迁移 up/down；down 删列向前兼容）→ **Step 3** 绿 → **Step 4** 提交 `feat(admin): qa pair management with soft delete` + `feat(db): qa pair active flag`。

### Task 5: M6 评测题库导入（F-601）

**Files:** Create: `app/services/eval_service.py`, `app/api/admin_eval.py`；Test: `tests/api/test_admin_eval_import.py`
**Interfaces:** Produces:

- `GET /api/admin/eval-questions/template` → xlsx（列：`部门/问题/期望答案/来源/题型`+示例行，部门=service|hr|all 中文名映射说明）
- `POST /api/admin/eval-questions/import`：整批校验（部门枚举、问题 1-2000、期望答案非空 ≤10000、题型 ∈ factual|multi_hop|cross_reject、**拒答类约定：`qtype=cross_reject` 或 期望答案=`[拒答]` 至少满足其一，满足其一时另一字段自动归一**）→ 审计 `eval.import {total}` → 201 `{imported}`

- [ ] **Step 1 失败测试**：①合规导入读回（dept 名→id 映射、题型落库）②非法部门/题型/超长 → 行级错误全报告零入库 ③`[拒答]` 归一为 cross_reject（读回 qtype）、cross_reject 的 expected 归一为 `[拒答]`（判定协议第 3 条约定格式，features F-601）④模板读回。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): eval question import and template`。

### Task 6: M6 跑分引擎（F-602 核心）

**Files:** Create: `app/services/eval_engine.py`；Modify: `app/api/admin_eval.py`；Test: `tests/integration/test_eval_engine.py`
**Interfaces:** Consumes: `prepare_turn(db, deps, user_dept_id, all_dept_id, question, history)`、`ChatDeps`、`REFUSAL_TEXT`。Produces:

- `POST /api/admin/eval-runs {name, perspectiveDeptCode}`（AdminUser）→ 建 `eval_runs(status='running', config=null)` commit → BackgroundTasks 执行 `run_eval_run(run_id)` → 202 `{id}`
- `run_eval_run`（后台任务体，自管短会话）：
  - 评测专用 ChatDeps 副本：`replace(deps, cache=ExactCache(clock, ttl_s), counter=None)`（**旁路缓存**：put 进废弃实例；**不调 try_consume** 不占日限；hit_counter 共用）
  - 每 run 一个专用会话（user=触发者，title=f"[评测]{name}"）
  - 逐题（is_active 题）：`prepare_turn(history=[])` → refuse 直接话术不调 LLM；qa/rag 用 `llm.chat_stream` 收集全文（**取迭代器立即迭代，异常边界包住整个迭代**；UpstreamError 单题记 `error` 继续下一题，整体不失败）
  - 自动判（判定协议第 3 条）：`expected=='[拒答]' or qtype=='cross_reject'` → `answer==REFUSAL_TEXT` ? correct : wrong；否则 `autoJudgment='pending'`
  - 每题落 user+assistant 消息（`is_eval=true`，conversation=专用会话）；detail 逐题 append 后 commit（进度可查、崩溃保留已完成）
  - 收口：`total`、`correct`（auto correct 数）、`accuracy=correct/total`（pending 计 0，detail 标注 `pendingCount`）、`config={model, promptVersion, qaThreshold, refuseThreshold, efSearch, topK, topN, temperature, manifestVersion}`、status='done'；整体异常 → status='failed' + error 字段（detail 已留已完成题）
- `GET /api/admin/eval-runs?page&pageSize`（列表：id/name/status/total/correct/accuracy/createdAt DESC）
- `GET /api/admin/eval-runs/{id}`（含 detail 全量，轮询用）
- 审计 `eval.run {runId, name, perspective}` 在创建时

- [ ] **Step 1 失败测试**（integration，合成 4 题库：客服事实题×2、`[拒答]` 拒答题、cross_reject 串库题；perspective=hr 触发）：①run 完成后 status=done、total=4、correct=自动判数（拒答两题：串库题被拒→correct、事实题被答→pending 不计）、accuracy 口径正确、config 快照逐字段与当前 Settings/prompts 一致 ②拒答题自动判 wrong 的反例（构造检索必命中的拒答题）③messages 读回 is_eval=true 且 conversation 单一 ④detail 逐题结构（citations 快照与回答一致）⑤评测不写主缓存（跑完后 ExactCache 实例弃置——用 deps.cache 引用断言其 _entries 为空）⑥单题 LLM 失败（break_after）不拖垮整体（该题 error、run=done）⑦创建审计读回。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): eval run engine with config snapshot`。

### Task 7: M6 评判提交 + 分组报告（F-602/F-603）

**Files:** Modify: `app/services/eval_service.py`, `app/api/admin_eval.py`；Test: `tests/api/test_admin_eval_report.py`
**Interfaces:** Produces:

- `POST /api/admin/eval-runs/{id}/judgments {items:[{questionId, judgment}]}`（judgment ∈ correct|partial|wrong）→ 更新 detail 对应题 `judgment` + 重算 `correct = n_correct + 0.5*n_partial`、`accuracy=correct/total` + 审计 `eval.judge {runId, judged}`；仅 status='done' 可评（422）
- `GET /api/admin/eval-runs/{id}/report` → `{config, status, totals:{total, correct, accuracy, pendingCount}, byDept:[{deptCode, total, accuracy}], byQtype:[{qtype, total, accuracy}]}`（明细从 detail 聚合，auto correct 与人工 correct 合并口径：无人工判时用 autoJudgment，有人工判以人工为准）

- [ ] **Step 1 失败测试**：①批量录分后 accuracy 重算（0.5 权重验证：2 正确 1 部分 1 错 → (2+0.5)/4=0.625）②judgment 持久读回 ③byDept/byQtype 分组数字与手工计算一致（客服/HR 分开——验收硬指标口径）④running 状态录分 → 422 ⑤审计读回。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): eval judgments and grouped report`。

### Task 8: M7 提问记录（F-701）

**Files:** Create: `app/services/admin_views.py`, `app/api/admin_views.py`；Test: `tests/api/test_admin_messages.py`
**Interfaces:** Produces:

- `GET /api/admin/messages?deptId&dateFrom&dateTo&hasFeedback&page&pageSize`：以 **user 消息行**为单位（`role='user' AND is_eval=false`，按会话归属用户/部门 join users），筛选（部门/日期范围/是否点踩）→ `{items:[{id, conversationId, createdAt, username, deptCode, questionPreview(截 50 字), answerType, hasCitations, feedback}], total}` DESC
- `GET /api/admin/messages/{id}`（user 消息 id）→ `{user:{...}, assistant:{content, citations(JSONB 原样), answerType, model, promptTokens, completionTokens, latencyMs, cacheHit, feedback, feedbackCategory, feedbackComment}}`（无 assistant 回复——中断行被软删——assistant 为 null）

- [ ] **Step 1 失败测试**：①造 3 会话混合数据（含 is_eval 消息——**必须被过滤**、含点踩/无点踩、跨部门）逐筛选断言 ②分页 ③详情读回 citations JSONB 与落库全等、tokens 逐字段 ④中断消息详情 assistant=null ⑤member 访问 → 403。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): message records with detail`。

### Task 9: M7 badcase 管理（F-702）

**Files:** Modify: `app/services/admin_views.py`, `app/api/admin_views.py`；Test: `tests/api/test_admin_badcase.py`
**Interfaces:** Produces:

- `GET /api/admin/badcases?deptId&badcaseStatus&page&pageSize`：`feedback='down' AND is_eval=false` 消息 DESC → `{items:[{messageId, createdAt, deptCode, questionPreview, category, comment, badcaseStatus}], total}`（badcaseStatus 缺省由前端传'待处理'）
- `PATCH /api/admin/badcases/{messageId} {badcaseStatus, note}`（枚举：已修语料|已忽略）→ 更新 + 审计 `badcase.update {messageId, from, to, note}`（note 入审计 detail，不入 messages——admin 备注是管理信息）

- [ ] **Step 1 失败测试**：①down 两条（不同部门）→ 列表默认全部、按'待处理'筛、按部门筛 ②PATCH '已修语料'+note → 读回 + 审计（含 note）③非法状态 → 422 ④与 Task 1 裁决联动：admin 置'已忽略'后用户再 down → 状态仍'已忽略'但列表 comment 更新（API 级联验）。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): badcase queue and disposition`。

### Task 10: M7 检索测试 API（F-503/F-703，RET-03/04）

**Files:** Modify: `app/services/admin_views.py`, `app/api/admin_views.py`；Test: `tests/api/test_admin_retrieval_test.py`
**Interfaces:** Produces:

- `POST /api/admin/retrieval-test {question, deptCode}`（deptCode ∈ service|hr|all，admin 调参工具允许任意视角）→ 复用 `normalize_question → embed → match_qa → search_chunks → rerank_and_fold`（**不调 LLM、不写 hit_counter**）→
  `{qa: {id, question, similarity, thresholdPassed} | null, docs: [{chunkId, documentTitle, page, titlePath, rerankScore, recallCosine, thresholdPassed, folded: bool}], thresholds: {qa, refuse}}`
  - `recallCosine`=召回阶段余弦、`rerankScore`=fold 后 sigmoid 分（**两套分数分字段**）；`folded`=该块进了 top5 但被折叠丢弃（**仍在 docs 列表中带标记展示**——调参需看到被折叠块；实现：fold 前保留 top5 全量，fold 后打标）
  - `thresholdPassed` 对照当前 Settings 阈值逐块标记（RET-04 调参依据）
  - `docs` 展示 top_n 全量（含 folded），`检索结果为空` 时 docs=[] 且 qa=null

- [ ] **Step 1 失败测试**：①种子 qa+chunks → 命中结构与两套分数字段分正确、qa.thresholdPassed=true ②阈值边界：rerank 权重注入 0.52 → thresholdPassed=false（RET-03/04 API 级）③折叠块 folded=true 出现在结果 ④hit_counter 计数不变（读回断言）⑤member → 403。
- [ ] **Step 2** 实现（retrieval 函数如需暴露 fold 前后信息，加返回字段不破坏既有签名）→ **Step 3** 绿 → **Step 4** 提交 `feat(admin): retrieval testing endpoint`。

### Task 11: M7 运营概览（F-704）+ 健康度看板数据（F-1009）

**Files:** Modify: `app/services/admin_views.py`, `app/api/admin_views.py`；Test: `tests/api/test_admin_overview.py`
**Interfaces:** Produces:

- `GET /api/admin/overview?days=1|7` → `{totals: {questions, refuseRate, downRate, tokens}, byDept: [{deptCode, questions, refuseRate, downRate, tokens}]}`：聚合 `messages role='assistant' AND is_eval=false AND created_at >= now-days`；命中率=answer_type IN ('qa','rag') 占比、refuseRate=refuse 占比、downRate=feedback='down' 占比、tokens=sum(prompt+completion)（SQL 聚合参数绑定）
- `GET /api/admin/health` → `{totalDocs, readyCount, readyRate, pendingConfirm, zeroRecall: [{id, title, deptCode, createdAt}], expiringSoon: [{id, title, expiryDate}]}`
  - 口径定死：totalDocs=`deleted_at IS NULL`；readyRate=ready/totalDocs；pendingConfirm=`is_active=false AND deleted_at IS NULL`（待确认近似重复/脱敏）；**零召回**=文档 `is_active AND status='ready' AND created_at < now-30d` 且其全部 chunks `last_hit_at 均为 NULL 或 < now-30d`（NOT EXISTS 反连接）；expiringSoon=`expiry_date <= today+7`（含已过期未下线的）

- [ ] **Step 1 失败测试**：①造混合 messages（两部门×refuse/qa/rag/down/is_eval）→ totals 与 byDept 逐项手工核对、is_eval 不计入 ②days=7 包含 3 天前消息、days=1 不含 ③健康度：ready/归档/待确认/零召回（构造 30 天前+chunk 无 last_hit_at 的文档命中榜单、近 3 天文档不进）④expiringSoon 边界（expiry=today+7 进、+8 不进）。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(admin): operations overview and corpus health`。

### Task 12: M8 提问记录保留策略（F-803/§4.6）

**Files:** Create: `app/services/retention.py`；Modify: `app/core/config.py`, `.env.example`, `app/services/scheduler.py`；Test: `tests/integration/test_retention.py`
**Interfaces:** Produces:

- `Settings.message_retention_days: int = 90`、`message_cleanup_cooldown_days: int = 30`（.env.example 中文注释：提问记录保留/冷却，§7.3）
- `soft_archive_old_messages(db, clock, retention_days) -> int`：`created_at < now-retention AND is_active` → `is_active=false`（审计 actor 沿用 W1 到期下线 sweep 的系统 actor 先例——读 `run_due_sweeps` 对齐）
- `purge_expired_messages(db, clock, retention_days, cooldown_days) -> int`：`created_at < now-(retention+cooldown)` → 物理删除（**分批 DELETE ≤1000 行/批**防长事务；§4.6 允许冷却后物理清理，审计记录归档动作留痕）
- scheduler 挂日任务（CronTrigger 与 W1 sweeps 同风格）；审计/日志完整（§4.7）

- [ ] **Step 1 失败测试**（短 TTL：retention=0/cooldown=0，FakeClock 注入）：①3 条消息（now / 2h 前 / 25h 前）→ 归档后仅 25h 前 is_active=false ②再前拨 31h → purge 后 25h 前消息物理消失、now 消息仍在 ③归档审计读回 ④分批：1201 条（构造循环插入批量）→ 分两批删净 ⑤scheduler 挂载断言（任务名存在）。
- [ ] **Step 2** 实现 → **Step 3** 绿 → **Step 4** 提交 `feat(services): message retention two-phase cleanup`。

### Task 13: RAGFlow 基线环境配置（决策 15：错峰启停）

**Files:** Create: `deploy/docker-compose.ragflow.yml`, `docs/runbook.md`
**Interfaces:** Produces: 基线环境一键启动配置 + runbook 骨架（完整 runbook W5 演练前定稿）

- `deploy/docker-compose.ragflow.yml`：独立 compose **project**（`name: ragflow-baseline`，与主栈端口/网络/卷完全隔离）；ragflow 官方镜像**钉版本 tag**（禁 latest，§12.1）；依赖的 mysql/es/minio 等按官方 compose 裁剪为最小可用；**仅 compose 内网端口暴露管理界面所需最小集**（§12.2 端口约束）；healthcheck；注释注明：**禁止配置云 API key 灌真实语料（§7.6 出网白名单——基线环境走内网模型或脱敏/合成语料，否则构成第二条内容出网通道）**、与主栈错峰启停（§12.5 资源）
- `docs/runbook.md` 骨架：①启停主栈②**RAGFlow 基线：启动/停止/错峰顺序、资源冲突观察点（§12.5 合计上限）、数据清理**③备份/恢复（指针到 §18，W5 演练定稿）④端口暴露核查清单

- [ ] **Step 1** 写 compose + runbook → **Step 2** `docker compose -f deploy/docker-compose.ragflow.yml config` 语法校验通过（输出贴报告；镜像拉取/实跑留部署机，报告注明）→ **Step 3** 提交 `feat(deploy): ragflow baseline compose and runbook`。

### Task 14: W3 收口（api.md + 冒烟）

**Files:** Modify: `docs/api.md`；Test: 冒烟脚本（不入库）
**Interfaces:** Produces: api.md W3 全端点；真实启动冒烟证据

- [ ] **Step 1** api.md 追加 admin 组全部端点（M4/M6/M7/概览/健康度/保留策略说明），错误码表补 W3 新码（如 3004 评测状态冲突等——新增码随任务定义）
- [ ] **Step 2 冒烟**（test_mode 实启动）：登录 admin → 下载 qa 模板 → 构造 3 行 xlsx 导入 201 → 检索测试命中 → 造 3 题（含 1 拒答）导入 → 发起 eval-run → 轮询 done → 提交 judgments → report 分组出分 → 概览/健康度 → badcase PATCH → 保留策略短 TTL 触发一轮（env 覆盖启动）。命令+输出贴报告。
- [ ] **Step 3** 全量回归 + ruff → **Step 4** 提交 `docs: api reference for w3`。

---

## 里程碑风险预注册（§23.3）

| 风险 | 约束措施 |
|------|---------|
| openpyxl 大文件内存 | ≤5000 行硬闸在 read_rows 层拒绝；流式读行 |
| 评测跑分超时/阻塞 | BackgroundTasks + 逐题 commit 进度可查；单题异常不拖垮整体 |
| 评测污染运营统计/缓存/日限 | is_eval=true + 独立 ChatDeps 副本 + 不调 try_consume；M7 查询过滤有专项断言 |
| 人工评判口径漂移 | 判定协议三档枚举锁 API；config 快照锁阈值；auto 与人工合并口径在 report 内显式 |
| qa_pairs 软删迁移回滚 | 新列带默认值（红线①）；down 删列向前兼容 |
| 保留策略误删 | 两段式+冷却期+分批；短 TTL 用例先行；仅 messages（唯一物理删除点） |
