# 已知问题与测试发现汇总（known issues）

> 更新：2026-10-01。来源：W1-W6 各冲刺账本（`.superpowers/sdd/`）、部署验收报告（`deploy-acceptance-report.md`）、验收状态对照（`acceptance-status.md`）。
> 状态：**开放**（未解决）/ **已修复**（含修复指针）/ **登记裁决**（待决策）。

## 一、开放问题

### 开放-1：TEI 算力不足（内测放行最大阻断）
CPU TEI + 本机高负载下：首 token 57.4s（TEI 段 55.3s）、检索 38.2s、rerank 容器两次 OOM（Exited 137）、22/24 评测请求 infra 超时。远超非功能基线 #7（首 token <3s / 检索 <1.5s）。
**处置**：TEI 需 GPU 或独占 CPU 资源（资源需求已升级登记）；本机复测需在低负载窗口。

### 开放-2：串库行为面复测挂起（语料治理已落地，复测被开放-1 阻断）
真实 embedding 首轮复测：结构越界 **0/19**（部门隔离机制完好）；越视角拒答 7/9——3 例漏拒（rerank 0.6509-0.6953）全部撞在全员手册的「考勤与休假」HR 域段落上，漏拒带与对照命中带（0.63-0.71）重叠 → **0.55 阈值调不动，根因是语料治理**。
**已处置（W6 T2）**：HR 域内容撤出全员手册（v1.2 全员版 + HR 版分册，旧版已软删，重复语料已清）。
**待办**：治理后复测已解除容器阻断（tei-rerank ONNX 修复见第三节，本机实测 ORT 就绪 + 排序判别正常）；仍建议低负载窗口重跑（客服视角 hr 题 10/10 拒答为通过判据）+ hr 视角对称矩阵（W5 首轮被负载摧毁）。
**附带实证**：漏拒题回答含所引块外政策细节 1 例（弱上下文编造）——语料治理后复评归零（硬指标 #4）。

### 开放-3：eval 跑分逐题 detail 提交失效
**已修复（W6 T1，commit 12721f9）**：根因=共享 items list + `expire_on_commit=False` 下 flush 相等比较抑制 UPDATE；`flag_modified` 修复 + 逐题进度回归测试。**待部署复验**。复现脚本：`.zcode/accept/repro_detail_stall.py`。

### 开放-4：ingest 侧 StubEmbedder 泄漏（部署级缺陷）
**已修复（W6 T1，commit a3dd871）**：`documents.py:_build_deps` 走装配工厂（真实 TEI；test_mode 回 Stub）。W2 只接通问答侧、入库侧漏接，测试全在 TEST_MODE 故三个冲刺未暴露——**真实模式部署才发现的典型案例**。

### 开放-5：试点库文档标题 GBK 乱码
curl 在 Windows Git Bash 直传中文文件名导致 id=9/10 等 title/original_filename 乱码（检索不受影响，展示面缺陷）。
**处置**：运维口径——中文标题上传走 API 客户端脚本或界面；存量乱码文档重传规范化（复测窗口前顺手做）。

### 开放-6：试点库 E2E 残留
用户并行会话的 E2E 套件直接对试点库跑（users 表 44 行含 e2e-*/小张/停用员/重置员残留 40+；50 份文档含 e2e_* 前缀），且曾改掉 admin 密码。
**处置**：~~隔离方案待裁决~~ **已落地（2026-10-01 修复波）**——`deploy/docker-compose.e2e-isolated.yml` 三文件叠加隔离栈（pg-e2e 独立实例 + TEST_MODE stub，用法见文件头注释），首轮实测跑通（47 执行/32 过/12 败/3 跳，试点库零接触，跑后试点栈已恢复）；残留清理仍需与你会话协调时机；admin 已重置为 documented 密码。**12 条失败为你会话在途套件的稳定化欠账**（对话框未关/选择器层为主，documents-gov 簇 + admin-pages/chat 反馈弹窗；后端行为面经 API 抽查与落库核验全部正确），trace/截图在 `frontend/e2e/.artifacts/results/`。

### 已修复-7：documents 删除端点（F-305，2026-10-06 实施收口）
DELETE /api/documents/{id}（admin-only）：软删+级联停用 chunks+审计 document.delete；parsing 态 422；前端删除按钮解除置灰（二次确认）。设计见 docs/specs/2026-10-06-document-delete-review.md；测试 test_document_delete.py。

### 开放-8：前端零散项
①KnowledgeGapCard 周切换失败时旧周数据残留（错误可见可重试，cosmetic）；②fmtTime 尚有 6 处内联未收敛（工具已备）；③前端 load 无 catch 的 unhandled rejection 统一收口；④全仓 lint 红来自并行会话未跟踪的 `frontend/e2e/`（trace 产物），归你会话收口（eslint ignore 或清理）。

### 开放-9：50MB 解析性能与 20 并发压测
待真实语料与部署机（本机跳过）。

## 二、登记裁决（待决策）

| # | 事项 | 选项 |
|---|------|------|
| 裁决-1 | standards §12.2"tei 禁止端口发布" vs compose 生产映射 8081 注记 | 生产收回端口 or 修订标准（runbook 已标"先评审不静默择一"） |
| 裁决-2 | degraded 字段：列表 DTO 未下发 vs 用户手册描述"降级解析标注" | 后端补字段（契约变更+deviations #24 翻转）或修订手册 |
| 裁决-3 | mineru compose 构建为 pipeline 后端 vs §12.5"必须 ONNX 后端" | 换 ONNX 镜像 or 修订标准（随 MinerU 实际部署一并裁决） |
| 裁决-4 | compose 注释引用不存在的 make-dev-models 目标 | 补 Makefile 目标 or 改注释（按 download_models.py 实际入口已登记 runbook） |
| 裁决-5 | frontend/e2e lint 归口（见开放-8④） | e2e 车道自清 or eslint 例外 |

## 三、已修复关闭（本机部署验收后）

- ~~eval detail-stall~~（开放-3，已修）
- ~~ingest StubEmbedder 泄漏~~（开放-4，已修）
- ~~login 不校验 is_active~~（W6 修复波 46e8472，禁用用户登录即 401）
- ~~并发重名 500~~（W6 修复波 52e9354，IntegrityError→409）
- ~~documents 列表 ILIKE 通配符未转义~~（W4 T1）
- ~~lockedUntil 不在锁定错误响应~~（W4 T3 rider）
- ~~重排响应形状（真实 TEI 裸数组）~~（用户并行会话适配，部署验收确认）
- ~~tei-rerank 容器 OOM/无法就绪~~（2026-10-01 根因修复：`bge-reranker-v2-m3` 缺 `onnx/model.onnx`（官方仓库不发 ONNX），TEI 回退 candle 纯 CPU 冷启动十余分钟不可用、高负载下被 OOM。已用 `uvx optimum[exporters]==1.24.0 --opset 17` 从官方 safetensors 本地导出（输出单值 logits 契约），ORT 后端加载约 4 分钟 healthy，排序判别验证通过（相关 0.542 / 无关 0.00002）。**此前两次 OOM 的主因即 candle 回退**，非纯算力问题；首 token NFR #7 仍按开放-1 待 GPU/独占资源）

### 三-b、全工程双轴 code review 修复波（2026-10-01，规范轴+规格轴）

规范轴（硬违规 3）：
- ~~compose 明文凭据~~：pg/pg-test `POSTGRES_PASSWORD` 与 app `DATABASE_URL` 改 `${POSTGRES_PASSWORD:-rag}` env 注入（§12.7；dev 缺省弱口令已在注释与 .env.example 标注部署必填+换卷须知）。
- ~~api 层构建查询（系统性）~~：6 文件 select() 全部下沉 service——新增 `services/departments.py`（全员部门 id 与 code→id 单一事实来源，收口 4 份重复解析）、`services/citations.py`、`services/document_views.py`；`_load_history`/会话列表/消息列表/评测任务列表分别落 `services/chat.py`/`eval_service.py`；feedback 归属解析改 db.get（PK 直取先例）。附带：跑分视角部门错误文案统一为"部门编码无效"（与上传表单同源，测试断言已同步）。
- ~~e2e_seed TRUNCATE f-string~~：加 `_ALLOWED_WIPE` 白名单断言 + 豁免声明（标识符无法参数绑定）。
- ~~seed 初始密码进 stdout~~：登记豁免口径（bootstrap 一次性+首登强制改密，不打印无人能首登）。
- ~~fmtTime 内联~~：ConversationList 相对时间上收 `utils/format.ts` `fmtTimeRelative`（组件零内联 new Date 格式化）。

规格轴（缺失 4）：
- ~~VectorStore 第六接口缺位~~：新增 `services/vector_store.py` Protocol + `retrieval.PgVectorStore` 实现；ChatDeps 增 store 字段，prepare_turn/检索测试全部经接口消费；实现选择在装配层 app/main.py（services 协议不进 core/assembly——五层单向依赖）。Milvus 退路由纸面变接线。
- ~~F-304 重建索引未实现~~：`services/reindex.py` + `POST /documents/{id}/rebuild-index`（文档级）+ `POST /documents/rebuild-index`（全库级）；复用入库状态机按现行分块设置重刷，单事务原子切换；审计双痕（document.rebuild 含前后块数）；测试 5 例（含 failed/pending 拒绝、member 403、全库受理计数）。
- ~~F-303 版本历史只有半截~~：`GET /documents/{id}/versions`（同 dept+title 全代次 version 降序，归档在案）；测试 2 例。
- ~~F-1010 每周自动生成打折扣~~：`python -m app.services.knowledge_gap [--week-offset] [--out]` 周报导出 CLI，供 OS 调度器（schtasks/cron，同 backup.sh 口径）拉起；不引入应用内调度依赖。
- ~~backup.sh 首跑与恢复流程~~（W5/T9+部署验收实测：RTO≈4min）
- ~~知识缺口卡错误态 / fmtTime 收敛 / ZRC 重试~~（W6 T3）

## 四、待用户/待资源清单

| 项 | 责任 | 阻断 |
|----|------|------|
| TEI GPU/独占资源 | 用户+IT | 阻断 #7、串库行为面复测、编造复评 |
| 评测题库 Top30-50 | 用户+对接人 | 阻断 #1 分部门准确率 |
| 合规清单签批（compliance-checklist 草案已由用户产出） | 用户 | 合规轨道正式确认 |
| 部署机（VM）落实 | 用户+IT | 正式内测载体 |
| RAGFlow 基线报告 | 部署后 | 硬指标 #5 对照半边 |

## KI-## 解析队列不持久化：容器重启后 parsing 文档永久卡住（2026-10-02，已修待发布）

- **现象**：真实模式上传后容器重启（发布/重建/宿主机重启），BackgroundTasks 解析队列丢失，文档停留"解析中"且 UI 无重试入口（重试按钮仅失败态出现）。
- **实证**：10-05 手工测试 6 份文档全卡 parsing，chunks=0、容器 CPU 空闲；最早 3 份已卡 3 天。
- **修复**：docker/entrypoint.sh 启动自愈——alembic 后把遗留 parsing 统一转 failed+中文原因（parse_error），管理员在列表点重试即可（retry 走新任务幂等）。**随下次镜像构建生效**；当前库中 6 份已手动转为失败态可直接重试。
- **重试提示**：TEI 刚启动（冷）时首次重试可能报"向量化服务调用失败"——等 1-2 分钟再点一次即成功（冷启动特性，见 runbook）。

## 三、2026-10-05 解析可观测性与数据安全（本轮修复+新增）

### 已修复-3：大文档解析全败 + 解析不可观测（解析管道健壮性改造）
- **根因**：入库嵌入把整篇全部 chunk 一次单请求发 TEI，超 --max-batch-tokens 2048（防 OOM 调低）→ HTTP 422 全败；UpstreamError 真实原因被脱敏映射丢弃；解析管道零日志零进度字段，"解析中"进度条为固定 50% 装饰动画——失败不可定位、处理不可判断。
- **修复**：① 嵌入按字符预算分批（EMBED_BATCH_CHARS=1200，逐批调用、批级进度落库）；② 错误双轨：parse_error（用户文案）+ parse_error_detail（技术摘要：UpstreamError 原文/异常类型+消息+末栈帧，截 500 字）；③ logging.basicConfig 体系补齐，解析管道结构化日志（doc_id/阶段/耗时/批数）+ 失败 logger.exception；④ 迁移 0004：documents 加 parse_stage/parse_progress/parse_error_detail；⑤ 前端真实百分比进度条（旧数据回退不确定动画）、失败 tooltip 双行（友好原因+技术详情）；⑥ degraded 独立字段下发（deviations #24 兑现）。测试：tests/integration/test_ingest_progress.py + 前端 spec 更新；E2E DOC/SVC 回归通过。
- **同类修复**：评测跑分 running 卡死无自愈——entrypoint 自愈补齐（遗留 running → failed，逐题已完成的明细保留）。

### 开放-3（新）：E2E 与手工测试共库冲突——E2E_WIPE 清空业务表导致用户数据丢失
- **事故**：2026-10-05 手工测试库上运行 E2E 回归，auth.setup 的 E2E_WIPE=1 TRUNCATE 全部业务表，用户已上传的 6 份文档被清空且无备份（backup.sh 从未执行）。
- **缓解（已落地）**：manual-test-guide.md 明示"手工测试期间禁跑 E2E"；runbook 部署节建议生产机严禁运行 E2E 套件。
- **根治方向（登记待办）**：E2E 切换独立库（deploy/docker-compose.e2e-isolated.yml 的 pg-e2e 已具备条件，需 playwright 配置接入）；手工测试前执行 backup.sh 快照。


## 四-2、2026-10-05 文档原文件预览功能（已实施收口）

### 已修复-4：文档原文件在线预览（评审通过后当日实施）
- **交付**：预览端点 `GET /api/documents/{id}/preview`（统一 PDF inline；pdf/老格式直通、docx/pptx/xlsx 经 Gotenberg 转 PDF 并按 content_hash 缓存 previews/{doc_id}/、删除随档清理）；前端 DocPreviewDialog（页内弹窗+iframe，方案 A）+ 管理页"预览"按钮 + 引用弹窗"在线预览"入口；文本类（md/txt/csv/html）前端转义文本渲染（html 永不服务端 inline）。评审材料 docs/specs/2026-10-05-document-preview-review.md（§11 实施附记）。
- **测试**：tests/api/test_document_preview.py 5 用例 + citation 钉子更新（test_chat_flow/test_chat_stream）+ 老格式转换用例改 PDF 桩（test_format_expansion）；前端 documents-view/chat-feedback 预览与 docId 断言；e2e DOC-26（预置行预览，真跑转换链路）。

### 已修复-5：问答引用弹窗拿 chunk_id 当文档 id（随预览功能一并修复）
- **缺陷**：chat.py citation refId=chunk_id，SourceDialog 误用其拼 /documents/{id}/file|images，chunk_id≠doc_id 时引用弹窗"查看原文件"与图片回显 404 或错文档。
- **修复**：_rag_plan citation 增 docId（DocHit.document_id），前端三处 URL 改 docId 寻址；历史消息引用无 docId 时回退 refId（旧行为），在线预览仅 docId 在场时提供。

### 已修复-6：F-1015 老格式转换目标错误——真实容器上从未工作（实施预览时容器实测发现）
- **缺陷**：Gotenberg 8 libreoffice 路由只输出 PDF（容器实测 docx/txt 输入均返回 %PDF-，官方文档确认无保留原格式选项）；convert_to_ooxml 拿回 PDF 字节却按 .docx 落盘，python-docx 解析必败——doc/ppt/xls 上传必然解析失败。TEST_MODE 下转换走桩故此前未暴露。
- **修复**：转换目标改 PDF（convert.py convert_to_pdf，含 %PDF- 魔数校验防上游行为漂移），产物落盘走 pdf 解析链（文字版 pypdf 快速通道/扫描件 MinerU OCR），同份 PDF 兼作预览直通来源；api.md §2.10d 附注留档；E2E DOC-26 与手测 M2-T20 为真实链路冒烟。

### 已修复-7：docx 悬空样式引用整篇解析失败（2026-10-06 Xstudio 文档实测）
- **现象**：用户上传 Xstudio 导出的《Xstudio 从 0 到 1 短剧_漫剧生产操作手册.docx》解析失败，提示"包体结构非法或已加密"（误导）。
- **根因**：正文段落引用了 styles.xml 未定义的样式 ID（生成器常见悬空引用），python-docx `paragraph.style` 返回 None，`_heading_level` 取 `.name` 抛 AttributeError，被 `_DOCX_PARSE_ERRORS` 兜底捕成包体错误文案。zip 包体本身完全合法（python-docx 直接打开 162 段正常）。
- **修复**：parser.py `_heading_level` 样式判空——style 为 None 按无标题普通段落处理；回归测试 tests/unit/test_docx_dangling_style.py（悬空样式注入 docx 永久保留）。修复后真实文件解析 4032 字入库可检索。
- **附带提示**：该文档 10+ 内嵌图且 IMAGE_OCR_ENABLED=true，重试后"图片识别中"阶段按 CPU MinerU 逐图 OCR 需数分钟属预期（进度条可见推进）。

### 已修复-8：问答等待期气泡全白无交互（2026-10-06 用户体验缺陷）
- **现象**：发送问题后到首 token 之间助手气泡完全空白——该窗口（改写+召回+CPU 重排，本机 15-120s）内无任何活性反馈，用户无从判断是在工作还是已卡死。
- **修复（前端等待态）**：ChatMessage 增 stage 状态机（search→generate→null）：发送即示"正在检索知识库"（脉冲点+阶段文案），citations 事件到达切"正在生成回答"，超 3 秒显示"已等待 N 秒"，首 token 到达提示退场（内容+光标接管）；停止/失败态提示同步消失。组件 MessageBubble，状态机 useChatStream。
- **为何不发后端阶段事件**：prepare_turn（含最慢的重排）在 StreamingResponse 开流**之前**执行（app/api/chat.py:133），流内无法回放该窗口；真阶段事件需把 prepare_turn 移入生成器——动核心问答流的事务/错误映射时序，登记 backlog 待评审，不在本次 UX 修复中冒险。
- **测试**：chat-view.spec 新 describe（Gate 控流验证 search→generate→退场三态 + 停止态提示消失），全量单测 235 过。
