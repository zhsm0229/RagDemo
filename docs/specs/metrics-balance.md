# 按文档自动分型平衡 RAGAS 四指标 · 设计文档 v1.1

状态：已实施（2026-10-08 三方评审裁决并入 v1.1 后按此编码；工单台账 .scratch/metrics-balance/progress.md；部署机硬项挂 known-issues 开放-10）。编码必须严格遵循本文档；冲突先评审指出，不许静默择一。
配套：W6 验收硬指标 #1（分部门准确率 ≥80%）、#2（引用覆盖率 100% 机检）。
纪律：TDD（先红后绿）；`rag/`、`services/` 强制；改检索行为须附评测跑分对比（standards §17.3）。

## 1. 目标与非目标

**目标**：① 评测管道产出四个指标——Context Recall（机检）、Context Precision（LLM judge）、Answer Relevancy（LLM judge）、Answer Correctness（LLM judge + 人工仲裁）；② 文档入库按结构信号自动分型，按型取切分档位与精排条数，全程零人工判断。

**非目标（触发线登记，本期不建）**：表格感知分块（表格类 badcase ≥5 例后立项）、句子窗口、语义分块、BM25 混合检索、LLM 自动分型（结构检测实测误判率高时再上）、页级/条款级 Context Recall（v1 为文档级口径）。

## 2. 四指标计算口径

| 指标 | 判定方式 | 口径 |
|---|---|---|
| Context Recall | 机检 | 该题最终进入 context 的 chunk 集合（contextChunkIds）中，至少一块属于 `source_doc_id` 指向的文档 → 1 分，否则 0；集均分。**拒答类题与 source_doc_id 为空的存量旧题记 null 不计入** |
| Context Precision | LLM judge | 对 context 逐块判"是否含作答所需信息"（布尔数组 `rel_i`），排名加权：`Σ rel_i/i ÷ Σ 1/i`（i 从 1 起，全相关=1） |
| Answer Relevancy | LLM judge | 三档 1 / 0.5 / 0（切题度，不含答对与否） |
| Answer Correctness | LLM judge 预填 + 人工仲裁 | 三档 correct/partial/wrong 映射 1/0.5/0，写入 autoJudgment；`final_judgment` 合并口径不变（人工优先） |

- **三判合一次调用**：每题一次 LLM 调用同时输出 `context_relevant`（逐块布尔）、`relevancy`、`correctness`。temperature=0（装配层以 `build_llm(settings, 0.0)` 构造评测专用第二实例注入，不影响主链路实例）；超时/一次重试沿用现有 llm 配置。
- **judge 输出口径（裁决）**：任一字段非法（JSON 解析失败、缺字段、枚举外值、`context_relevant` 长度≠块数）→ **整题三判记 pending + judgeError，绝不部分采纳、绝不判 wrong**。
- **拒答类旁路**：expected=`[拒答]` 或 qtype=cross_reject 不调 LLM judge，保持既有规则自动判；context 指标记 null。
- **拒答题 trace 形态（裁决）**：refuse 分支 `contextChunkIds=[]`（无 context 拼装），`topKCandidates` 照记实际检索产物（观察价值），metrics=null。
- **cache 命中题（裁决）**：评测旁路缓存命中的题，contextChunkIds 从缓存条目 citations 还原，topKCandidates 记 `[]`（无新检索），metrics 照算。
- **引用覆盖率机检**（W6 #2）：回答中每个 `[n]` 可解析且 `1 ≤ n ≤ len(context)` → citationsOk=true。**回答完全不含角标 → false**（无角标=不可核对，机检必须红）；拒答题记 null。语义对应仍走人工抽检 20%（判定协议不变）。
- **拒答话术红线不动**："知识库中未找到相关内容"。

## 3. 文档自动分型

### 3.1 检测纯函数与分层归属

核心签名 `detect_from_text(title: str, title_paths: list[str], full_text: str) -> DocProfile`，与档位常量同放 `app/rag/doc_profile.py`（**纯函数域，禁止 import services/db**）。适配器在 services 层：ingest 期适配器（ParsedDoc→三参）在 ingest_pipeline；存量回填适配器（chunks→三参）在 services 层，**拼接全文前剥离每块的 `title_path｜` 前缀**（title_path 由存储列还原），保证与 ingest 期口径一致（否则 <1500 字信号会漂移）。

信号与优先级（先命中先判）：

| 优先级 | 信号 | 判定 `DocProfile`（DB 存 code） |
|---|---|---|
| 1 | 全文命中 ≥5 处 `第[一二三四五六七八九十百千零〇0-9０-９]+条` | `policy_clause` 制度条款 |
| 2 | markdown 表格信号：`|---` 分隔行出现，或含 `|` ≥2 个的行连续 ≥3 行 | `table_report` 表格报表 |
| 3 | 全文 <1500 字 且 标题路径最大深度 ≤2 | `notice` 通知公告 |
| 4 | 标题路径最大深度 ≥2（存在子标题层级） | `operation_manual` 操作手册 |
| 5 | 兜底 | `general` 通用 |

### 3.2 档位表（常量放 `doc_profile.py`，随代码评审冻结）

| profile | 切分（入库期） | top_n（查询期） |
|---|---|---|
| policy_clause | **条款边界切**：`第X条` 处开新块；单条超 600 字句切；<120 字碎条并入下条（并入后允许超 max_len，同现有"min 优先于 max"先例）；尾块并入前块 | 5 |
| operation_manual | 标题聚合 min 400 / max 1000 | 6 |
| table_report | 标题聚合 min 300 / max 1500 | 5 |
| notice | 标题聚合 min 800 / max 2000（基本整篇） | 3 |
| general | 标题聚合 min 300 / max 800（=现状） | 5 |

**条款切分细节（架构师裁决）**：条款块 title_path 取该条所在节的 heading_path（引用溯源前缀与 rerank 文本的构成件，不得为空）；跨节条款按条款起始位置归属所在节。chunker.py 现 149 行，若条款档实现后逼近 300 行红线，条款切分函数拆至 `app/rag/doc_profile.py` 或独立 `clause_chunker.py`。

**查询期参数化（架构师裁决）**：`DocHit` 模型增加 `doc_type` 字段（search_chunks SELECT 从 documents 透出）；`rerank_and_fold` 的 `top_n` 参数扩为 `int | Callable[[list[DocHit]], int]`——ranked 排序完成后求值（此时已知 top1 命中型）；top_n=5 缺省（doc_type 缺失/general）。**top_k 全局 20 不分型**；qa_threshold 0.85 / refuse_threshold 0.55 / ef_search 40 全局不动。

### 3.3 数据与迁移

- 迁移 `0010_document_doc_type`：`documents` 加列 `doc_type VARCHAR(32) NOT NULL DEFAULT 'general'` + CHECK 约束（命名 `documents_doc_type_check`，同 status 先例）+ 中文注释；down 可回滚。
- 迁移 `0011_eval_source_doc_id`：`eval_questions` 加列 `source_doc_id BIGINT NULL`，FK 显式命名 `eval_questions_source_doc_id_fkey`（documents 只软删，NO ACTION 即可）+ 中文注释。
- **现房规**：ORM 模型列 comment 与迁移注释**逐字一致**（autogenerate 零差异守卫依赖，test_migrations.py 同步 EXPECTED_COLUMNS）；本特性新增端点须同步 docs/api.md 标记行与 `tests/unit/test_api_contract.py` 对账。DB 只存 code；camelCase DTO 字段 `docType`，文案映射放 `frontend/src/components/documents/options.ts`（文档域常量，不放 eval 域）。

### 3.4 入库、存量与端点

- ingest_pipeline 在 chunk 前调用检测，按档位切分，doc_type 随 documents 落库。
- **PATCH 端点（落锤）**：现无任何文档字段编辑端点（document_admin.py 已核实），新增 `PATCH /api/admin/documents/{id}/doc-type`（仅此一字段，走 services 层，仅管理员）。**不清问答精确缓存**——docType 只影响查询期 top_n 档位与报告分组，不改可检索事实。编辑不限文档状态（傻瓜式，改错可改回，审计留痕）。
- `POST /api/admin/documents/backfill-doc-type`：对全部 **is_active 且 ready** 文档重算分型（从 chunks 构造；ingest 进行中未 ready 的文档天然不参与），返回 `{updated, byType}`；写审计 `document.backfill_doctype`。**覆盖语义**：重算结果覆盖现有值（含手工修正），前端确认文案必须写明。

## 4. 题库与评测管道变更

### 4.1 导入校验（eval_service.import_eval_questions）

- 非拒答题（expected≠`[拒答]` 且 qtype≠cross_reject）：source_ref **必填**，逐行归一化（去扩展名/首尾空格）后匹配同部门 documents（title 或 original_filename 去扩展名，精确或唯一后缀段匹配），必须恰好命中 1 篇，source_doc_id 落列。
- **拒答类豁免（架构师裁决）**：expected=`[拒答]` 或 qtype=cross_reject 行豁免 source_ref 校验，source_doc_id 落 NULL（模板 cross_reject 示例行本就无来源）。
- **整批语义（裁决）**：维持现有"全或无"——任一行校验失败 → 整批零入库 + 行级错误全报告（与既有钉死用例一致，防半批脏数据）。

### 4.2 检索中间产物（eval_engine.run_eval_run）

- **接线缝（架构师裁决）**：`rerank_and_fold` 增 keyword-only 参数 `ranked_out: list | None = None`，评测侧传入列表收集全量 ranked `(DocHit, rerank分)`（top_n 之外的候选分现被丢弃，此为唯一获取点）；`TurnPlan` 只**追加**可选字段 `retrieval_trace`，既有字段（citations 等）零改动；`contextChunkIds` 取自 citations refId，不另造来源。
- detail 逐题落 `retrieval: {contextChunkIds: [...], topKCandidates: [{chunkId, docType, rerankScore}]}`（恒 20 条、无正文，单题 ≤2KB）。体积预算：detail 为逐题全量重写模式，5000 题上限下写放大已知可接受（~10MB 级），首批题库 ≤100 题无虞。
- 主问答链路行为零改动（普通问答不传 ranked_out、不落 trace）。

### 4.3 LLM judge（eval_judge 扩展）

`judge_eval_item(llm, question, expected_answer, answer, context_blocks) -> JudgeVerdict`；prompt 版本常量 `JUDGE_PROMPT_VERSION="v1"` 进 eval config 快照（新字段 judgePromptVersion）。**context_blocks 取材（裁决）**：按 contextChunkIds 回查 chunks 表取正文（citations 不含正文，不扩 TurnPlan 契约）；LLM 输出采集复用 `"".join(chat_stream(...))` 惯例。拒答类不调用（见 §2）。

### 4.4 报告、DTO 与前端（字段冻结）

- `fourMetrics`：`{contextRecall, contextPrecision, answerRelevancy, answerCorrectness}`，每项 `{value: number|null, evaluatedCount: int}`（null 排除后集均分）。
- `byDocType`：`[{docType, questionCount, contextRecall, contextPrecision, answerRelevancy, answerCorrectness}]`（组内四指标同口径集均分）；**source_doc_id 为 NULL 的题入 `docType="unassigned"`（未标注）组，只显 questionCount、四指标为 null**；全局 fourMetrics 仍计入这些题（recall null 自然排除）。
- 前端（设计师约定，dev 照做）：①文档列表"状态"与"责任人"列之间加"分型"列（el-tag info，110 宽，DOC_TYPE_LABELS 映射+未知兜底，列头 tooltip 说明）；②docType 编辑=列内常驻 el-select 五档，change 即保存（MemoryView 范式：先行更新→后端返回值校准→失败回滚；行级锁 editingTypeId，不复用 actingId）；③"分型回填"按钮在筛选行工具栏（"草稿审核"前），ElMessageBox.confirm（warning，文案含三点：范围为全部启用且就绪文档/覆盖现有分型含手工修正/记入审计），backfilling 全局锁，成功按 updated 提示（0 时"本次无分型变化"）；④四指标卡复用 `.total-card` 放 byQtype 表之后"按文档分型"小节，顺序固定 Recall/Precision/Relevancy/Correctness，null 显 '—'，底部"参算 N 题"，整卡 tooltip"拒答类题目记 null 不计入"；⑤byDocType 单表（同 byDept 款）：分型/题数/召回/精确/相关/正确，短列头+全称 tooltip；⑥失败沿用面板顶部 el-alert，byDocType 空数组渲染 el-empty"本跑分暂无文档分型数据"；⑦**不动** MainLayout/RunDetailDrawer/RunItemCard/RecordJudgmentDialog/router/stores；**必改** api/documents.ts、api/evalAdmin.ts、components/documents/options.ts、DocumentsView.vue、RunReportPanel.vue；新增界面文案同步 docs/user-guide.md 与 frontend/src/help/（红线 7 义务）。

## 5. 查询期 top_n 按型档（最后实施）

按 §3.2 参数化方案实施。**§17.3 纪律**：合入前同题库跑新旧两轮评测，四指标不劣化方可合入，对比结论写台账。eval config 快照含生效档位说明。

## 6. 测试 seams（冻结版：原 9 条 + 增补 6 条）

- **unit**：`detect_from_text` 五档+优先级边界；两个适配器（ParsedDoc 构造、chunks 回填构造含剥离前缀）；chunker 条款边界+各档参数+general 档与现状逐字一致；`eval_report` 四指标（null 排除/分母/precision 手算全等）；`judge_eval_item`（stub LLM 合法/非法/缺字段/拒答旁路）；**recall 纯函数与 citationsOk 纯函数**（落点 `app/services/eval_metrics.py` 新纯函数域：命中/未命中/拒答 null/source_doc_id 空 null；角标合法/越界/无角标 false/拒答 null）；**top_n 按型解析函数**（五档映射/缺省 general/无命中缺省）。
- **integration**（真 pg + MarkerReranker/BreakOneLLM 套路）：source_ref 整批退回+读回；评测 run 落 trace+autoJudgment+metrics（读回断言）；backfill 端点+审计读回；top_n 按型截取。
- **api**：docType 透出/PATCH 合法值与枚举外 422/回填端点成员 403。
- **golden**：run_chain 镜像**同步加入 detect_from_text+档位分派**（保持独立重算不 import 管线私有函数的纪律），全量跑后按需同步 manifest，清单逐样本标注预期型别。
- **键集合演进规则**：test_eval_engine ④ 的 detail 键集合、config 快照键集合、test_admin_eval_report 的 config 键集合为精确断言，随工单 03/05/06/07 计划内演进，非回归红。
- 写库测试必须带读回断言；断言精确，禁止 `A or B`。

## 7. 验收阈值（首批跑分后校准一次冻结）

Context Recall ≥0.85 ｜ Context Precision ≥0.60 ｜ Answer Relevancy ≥0.80 ｜ Answer Correctness ≥0.80（对齐 W6 #1 部门级口径）。分型只出分组报告，不设差异化及格线。

## 8. 评审裁决记录（2026-10-08，PM 裁决）

1. 拒答类豁免 source_ref 必填（否则 cross_reject 整批导入必败）；source_doc_id NULL 入 unassigned 组。
2. 导入维持"全或无"整批退回（与既有用例一致，防半批脏数据）。
3. judge 输出任一字段非法 → 整题 pending，不部分采纳。
4. citationsOk：无角标=false（机检必须红）；拒答题 null。
5. PATCH /doc-type 新增落锤（现无编辑端点）；不清问答缓存（docType 不改可检索事实）；编辑不限状态。
6. 回填覆盖手工修正（傻瓜式语义），前端确认文案写明。
7. trace 接线走 rerank_and_fold ranked_out 收集缝 + TurnPlan 追加可选字段；contextChunkIds 取 citations refId；judge context 按 chunkIds 回查 chunks。
8. 评测 run 幂等：维持现状（status!=running 直接返回），重跑=新 run 重新计费，不续跑。
9. top_n 参数化用 `int | Callable` 形态；DocHit 增 doc_type。
10. recall/citationsOk 纯函数落 `app/services/eval_metrics.py`（新文件）。
