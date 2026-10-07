# 内嵌图片 OCR 设计评审材料（F-1017，2026-10-06）

> 评审对象：知识库**内容级路由**——文档内嵌图片 OCR（features.md F-1017 已先行落稿）。
> 流程：本材料评审**通过并确认待修改文件清单**后，进入代码实施（分两批次）。
> 关联：features.md F-1017（已落稿）、standards §10.2/§10.8（已同步）、format-expansion-review backlog 状态（已更新）、manual-test-guide M2-T17（已落稿）、help 文案（已落稿，随功能上线发布）。
> 侦查依据：三轮并行代码探查（解析器图片处理现状 / MinerU 接口与配置面 / 引用展示与敏感扫描链路），关键结论均标注文件行号来源。

---

## 1. 要解决的问题与原理

**现状**：全项目存在"格式路由"（按文件类型分派解析器，已实现），但**没有"内容路由"**——各解析器只处理文本节点，内嵌图片被静默丢弃：

| 解析器 | 图片现状 | 位置 |
|---|---|---|
| DocxParser | `iter_inner_content` 只分派 Paragraph/Table，`w:drawing` 无分支 → 丢弃 | app/rag/parser.py `_collect` |
| PptxParser | `if not has_text_frame: continue` → picture shape 跳过 | app/rag/parsers_extra.py |
| XlsxParser | read_only 模式不加载 `ws._images` → 图片根本不进内存 | app/rag/parsers_extra.py |
| HtmlParser | `<img>` 不触发任何逻辑（含 base64 data URL） | app/rag/parsers_extra.py |
| pypdf（文字版 pdf） | 只取 `extract_text`，页面 XObject 图片不提取 | app/rag/parser.py |
| MinerU 路径（扫描件 pdf） | **唯一例外**：MinerU 服务内部完成"页面→图→OCR"，但产物是单块 md，图片占位 `![](images/…)` 成死文本入库 | app/rag/parser.py MinerUParser |

**方案（向量库存文字，而非图向量）**：解析时抽取内嵌图片字节 → MinerU `/file_parse` OCR（MinerU 2.5.4 pipeline 后端原生支持 png/jpg/jpeg/bmp/webp/gif 直传，来源：上游 release-2.5.4 fast_api.py `image_suffixes`）→ 提取文字以"图片N：…"补充块并入 `ParsedDoc.blocks` → 清洗/红线/敏感扫描/simhash/分块/分批嵌入**全部走既有链路**。选型理由：检索/生成/引用/展示零改动（对比 CLIP/ColPali 图文向量方案的架构级改造）；OCR 基建已就绪（MinerU 容器与扫描件路由共用）。

**顺带修复项（现状缺陷）**：MinerUParser 的 md 产物中 `![](images/xxx.jpg)` 死占位行当前原样入库污染向量与展示——F-1017 清洗阶段一并删除该形态行。

## 2. 图片抽取路径（逐格式）

| 格式 | 抽取方式 | 依赖 | 备注 |
|---|---|---|---|
| docx | `document.part.rels` 遍历 image 部件取 `.blob`（覆盖 inline+浮动图+页眉图） | python-docx 1.2.0（已有） | `word/media/` 已在既有白名单内（word/ 前缀），xmlguard 64MB 闸顺带生效 |
| pptx | picture shape 的 `shape.image.blob` | python-pptx 1.0.2（已有） | 现状 `has_text_frame=False` 被跳过，改为先判 picture |
| xlsx | **非 read_only 模式**二开 workbook 读 `ws._images`（read_only 无此属性，实测） | openpyxl 3.1.5（已有） | 内存风险见风险表 R1 |
| doc/ppt/xls | **零额外工作**——经 Gotenberg 转换链变 OOXML 后自动走上三行 | — | F-1015 组合红利 |
| pdf（文字版） | pypdf `PageObject.images`（6.19.0 已有 API） | 无新增 | **批次 2**（需评估与快速通道的关系） |
| html | `handle_starttag` 拦 `<img>`，仅处理 base64 data URL | 无新增 | **批次 2**；外链图不做（出网合规） |
| emf/wmf | **跳过**（计入"N 张未解析"） | — | MinerU 不支持；Office 矢量图需额外转换，成本>收益 |

## 3. 管线插入点（证据链）

**插入位置**：`ingest_pipeline.analyze_document` 中 `_parse_with_fallback` 返回后、`raw` 拼接前——往 `parsed.blocks` 追加图片文字块。自动获得：

1. **红线分母**：`raw` 含图片块 → `_check_quality` 口径不变（app/rag/…/ingest_pipeline.py `_check_quality`）
2. **清洗**：`clean_text` 逐块执行（`dataclasses.replace` 只覆写 text，`Block.page` 原样保留——既有注释明示）
3. **敏感扫描**：`scan_sensitive(cleaned)` 的分母 `cleaned` 天然含图片文字——截图身份证/手机号自动进"待确认"（standards §10.8"扫描对象含 OCR 识别文本"口径的延伸，无需改代码）
4. **simhash/分块/嵌入/进度**：图片块是普通 `Block`，`split()` 与 `_embed_in_batches` 零改动

**清洗坑规避（侦查确认的最大误伤点）**：`repeated_short_lines` 会删除"出现 ≥3 次且 ≤30 字的行"——若图片块用固定短前缀独立成行，同文档 ≥3 张图的前缀行会被整批删除。规避：OCR 文本以**行内形式**组织为 `图片N：文字`（带序号变体），且 OCR 多行文本合并为段；`_drop_page_number_lines`（纯数字行删除）对 OCR 碎片的影响由"行内合并"同规避。MinerU 路径的 `![](images/…)` 占位行在清洗规则中**新增一条删除规则**（带单测）。

## 4. 编排与降级语义（已确认：降级入库 + 标注）

流程（开关 `IMAGE_OCR_ENABLED=true` 时）：

```
解析完成（进度 5%）→ 抽取图片（≤50 张闸，进度 5→15"图片识别中"）
→ 逐张 OCR（单图失败：跳过计数；进度 15→25）
→ 组"图片N："块并入 → 清洗/分块（25）→ 向量化（45-95）
```

| 场景 | 行为 |
|---|---|
| 全部图片 OCR 成功 | 正常 ready，audit detail 记 `imageCount/ocrChars` |
| 部分成功 | 成功的图片文字入库；失败计数并入 degrade_note |
| **全部失败 / MinerU 不可达 / 格式不支持** | 纯文本正常 ready + `degrade_note` 追加"N 张图片未解析"（与既有 MinerU 降级语义同轨；**不 failed**） |
| 超过闸门（>50 张 / 单图 >10MB / 总量 >100MB） | 超限部分跳过并计数，不报错 |
| **开关关闭（缺省）** | 抽取器根本不调用——行为与现状**逐字节一致** |

统计落点：`PipelineOutcome` 加 `image_count/ocr_chars` → `_commit_ready` audit `detail.after` 平级追加（**不占用** parse_error=degrade_note 与 parse_error_detail 两个既有轨道）。

## 5. 待修改文件全清单（评审核心对象，14 个）

### 后端（8）
| # | 文件 | 变更 |
|---|------|------|
| 1 | app/rag/image_extract.py（**新**） | `ExtractedImage(bytes, page_hint, ext)` + 五个抽取函数（docx/pptx/xlsx/pdf/html）+ 闸门常量 |
| 2 | app/services/ocr.py（**新**） | OCR 客户端（结构照抄 convert.py：multipart `files`、超时/重试、`UpstreamError`），响应剥 `![](images/…)` 行 |
| 3 | app/rag/chunker.py | `ParsedDoc` 加可选 `images: list` 字段（缺省空，向后兼容） |
| 4 | app/rag/parser.py | DocxParser 抽取接入 + MinerUParser 占位行剥离标注 |
| 5 | app/rag/parsers_extra.py | PptxParser/XlsxParser 抽取接入（xlsx 二开 workbook）+ HtmlParser img 拦截（批次 2 用，接口先留） |
| 6 | app/services/ingest_pipeline.py | OCR 编排（开关判断/抽取/逐图 OCR/组块/进度/统计）+ 占位行清洗规则 |
| 7 | app/core/config.py | `image_ocr_enabled: bool = False`（复用 mineru_base_url/mineru_timeout_s，无新 URL） |
| 8 | app/services/ingest.py | `_commit_ready` audit detail 加 `imageCount/ocrChars` |

### 测试（4）
| # | 文件 | 变更 |
|---|------|------|
| 9 | tests/unit/test_image_extract.py（**新**） | 各格式抽取（构造含图 docx/pptx/xlsx bytes）+ 闸门（50 张/10MB/emf 跳过） |
| 10 | tests/unit/test_ocr.py（**新**） | OCR 客户端（httpx MockTransport 脚本式，照抄 test_parser._mineru 模式）+ 占位行剥离 |
| 11 | tests/integration/test_format_expansion.py（补） | 含图 docx 上传→ready→图片文字检索命中；OCR 失败→degrade；图片内身份证→待确认；开关关闭→零变化 |
| 12 | tests/integration/test_ingest_progress.py（补） | 进度断言适配（图片识别中区间，单调不减约束不变） |

### 前端（2）
| # | 文件 | 变更 |
|---|------|------|
| 13 | frontend/src/help/admin.ts | 已落稿（随功能上线发布） |
| 14 | frontend/src/help/faq.ts | 已落稿（失败对照表补 OCR 行） |

### 基础设施（0）
compose 无新服务（复用 mineru）；`.env.example` 加 `IMAGE_OCR_ENABLED=false` 登记行（随实施补）。

### 确认**无需改动**（侦查证据）
`chunker.split`（图片块是普通 Block）、`retrieval.py`（embedding 余弦+折叠 0.98 与 content 前缀无文本依赖）、`citations.py`/`SourceDialog.vue`（content 原样展示，"[图]"文本自然呈现）、`services_health.py`（OCR 复用 mineru 探活行）。

## 6. 回归保障

| 保障 | 机制 |
|------|------|
| 开关缺省 false | `analyze_document` 在开关关闭时不创建 OCR 任务、不调用抽取器——pdf/docx/全格式行为逐字节不变 |
| 既有回归 | 123 后端测试 + 前端 23 单测 + E2E DOC/SVC 5 用例全量回归为完成标准，零新增失败 |
| Mock 边界 | 全部 OCR 测试用 httpx MockTransport / MINERU_DOWN 模式，不依赖真实 MinerU（CI 可跑） |

## 6b. 评审反馈补充（2026-10-06 第二轮：图片回显 + 原文件打开）

评审追加两项能力，文件清单从 14 → 20：

| # | 文件 | 变更 |
|---|------|------|
| 15 | alembic/versions/0005_document_images.py（**新**） | document_images 表（document_id/image_index/file_path/mime/page_no/ocr_chars，唯一约束 doc+index，中文注释） |
| 16 | app/db/models/content.py | DocumentImage ORM |
| 17 | app/api/documents.py | 两个端点：GET /{id}/images/{index}（回显）、GET /{id}/file（原文件）——登录+部门隔离+就绪/激活闸 |
| 18 | app/services/image_store.py（并入 image_extract.py） | 图片落盘 `images/{doc_id}/img_{n}.{ext}` 与 document_images 行写入（ready 收口单事务） |
| 19 | frontend/src/components/chat/SourceDialog.vue | 引用弹窗解析"图片N："标记 → el-image 回显原图（在 OCR 文本上方）+ "查看原文件"链接 |
| 20 | frontend/src/views/DocumentsView.vue + api/documents.ts | 操作列"原文件"按钮 + 两个 URL helper |

数据链路：抽取图片先落盘 `images/{doc_id}/`（解析期），document_images 行在 _commit_ready 单事务写入（与 chunks 同事务）；解析失败时盘上图片文件为无害孤儿（行未写），不清理（试点权衡，登记）。回显标记映射：切片 content 含"图片N："，前端按 refId+N 构造 URL，无需改 citations DTO。

## 7. 实施批次

| 批次 | 范围 |
|------|------|
| **批次 1（本轮评审后）** | 基建（image_extract/ocr/config 开关）+ OOXML 三格式接入（doc/ppt/xls 经转换链自动受益）+ 测试 + .env.example + 帮助页文案上线 |
| **批次 2（登记后续，单独立项）** | 文字版 pdf 插图（需评估 pypdf 枚举与快速通道的协同）、html base64、图片文件直传入库（预检白名单扩展） |

## 8. 风险表

| 风险 | 缓解 |
|------|------|
| R1 xlsx 非 read_only 二开 workbook 内存（大表） | 图片抽取独立加载、抽取后即释放；单文档 50 张 + 总量 100MB 闸 |
| R2 OCR 噪声字符触发乱码红线（5%） | 红线是文档级全文判定，图片块占分母比例小；评审后若试点仍误触，对"图片N："块单独豁免乱码统计（实施时验证） |
| R3 MinerU 按需启停时含图文档全部降级 | 语义即"降级入库+标注"（已确认）；manual-guide 与帮助页明示前置条件（拉起 MinerU） |
| R4 OCR 慢拖长解析（CPU 版单图秒级×N 张） | 50 张闸 + 进度"图片识别中"真实上报；并发=1 与既有解析共用编排 |
| R5 MinerU 占位行清洗规则误删正文相似行 | 新规则精确匹配 `![](images/` 前缀形态（MinerU 专有产物），带单测与真实样本回归 |
| R6 范围膨胀 | 图片直传/URL 抓取/eml 维持 backlog；批次边界已在 §7 明确 |
