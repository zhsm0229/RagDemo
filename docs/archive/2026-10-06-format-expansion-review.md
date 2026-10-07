# 格式扩展设计评审材料（2026-10-06）

> 评审对象：知识库解析格式扩展（F-1013~F-1016，specs/features.md 已先行落稿）。
> 流程：本材料评审**通过并确认待修改文件清单**后，才进入代码实施（阶段二）。
> 关联：standards §10.1/§6.3/§10.2（已同步修订）、api.md §1.4/§2.5（已同步）、deploy-requirements.md §1（已同步 Gotenberg）。

---

## 1. 背景与决策溯源

- 原 v1 决策（standards §10.1）：仅 pdf/docx；.doc 拒收；xlsx 仅问答对通道——试点范围收敛。
- 2026-10-05 试点反馈：知识库需支持 doc/xls/xlsx/docx/ppt/pptx/md/html 等，扩展为正式需求（兑现 mvp-design"版式扩展【B】"项）。
- 已确认范围（2026-10-06 决策）：**新格式五类 md/txt/csv/xlsx/pptx/html + 老格式 doc/ppt/xls 走 LibreOffice 自动转换**。

## 2. 设计决策与取舍

### 2.1 pdf 场景路由（已实施，F-1016）
文字版 pdf（前 3 页文字层 ≥200 字符）走 pypdf 秒级解析；扫描件才进 MinerU OCR。动机：CPU 版 MinerU 2-10 分钟/份且多份并发抢 CPU（实测 645% CPU、5 份卡 30 分钟+）；文字版两者内容等价。

### 2.2 五格式分块策略（F-1013）
| 格式 | 策略 | 依据 |
|------|------|------|
| md | #/##/### 标题层级切节，heading_path 天然 | 对 RAG 分块质量最好（title_path 前缀进向量） |
| txt/csv | 单节按句切；csv 行内空格连接 | 无结构可循 |
| xlsx | 每 sheet 一级标题+按行组装；**单元格行内空格连接** | 规避两个已知清洗坑：重复短行删除（repeated_short_lines ≥3 次）、纯数字整行删除 |
| pptx | 每幻灯片一节（page=幻灯片序号），标题进 heading_path，备注并入 | page 透传链已存在 |
| html | 内置 html.parser（无 lxml 实体风险）+ DOCTYPE/ENTITY 前置拒绝；script/style 剥离 | 满足 standards §6.3 XML 安全与 Mimosa 约束 |

红线影响评估：质量红线为文档级全文判定（非逐块），短碎文本格式不会被逐块拒；整簿清洗后 <100 字拒（合理）。

### 2.3 安全设计（F-1014，Mimosa 约束落实）
- **OOXML 判型**：三家族同为 PK 魔数无法区分——同步段读包内 `[Content_Types].xml` content-type 判 word/xl/ppt 家族与扩展名一致（修掉现状"改名混入"洞：现状改名 xlsx→docx 可过预检+包校验，最终由 python-docx 抛裸异常）。
- **宏拒收**：.xlsm/.docm/.pptm 扩展名 + 包内 vbaProject.bin 条目双闸。
- **XML 安全**：xmlguard 字节层 DOCTYPE/ENTITY 流式拒绝复用于全部 OOXML 部件（现状仅 docx 前缀白名单内生效）；html 用内置 html.parser + 前置实体拒绝。
- **zip 炸弹**：既有解压总量/条目数/嵌套拒收闸全覆盖新格式（zip_import 与单上传同一 check_sync）。

### 2.4 老格式转换（F-1015）
Gotenberg（内置 LibreOffice headless 的标准转换 API 容器）内网部署；doc→docx/ppt→pptx/xls→xlsx 后走既有解析。转换放**异步段开头**（转换 10-60s 不阻塞上传请求）；转换失败 → failed + 中文原因引导另存。取舍：自建 soffice 微服务（轻但并发/超时自管）vs Gotenberg（成熟、HTTP 标准、~1GB 镜像）——选 Gotenberg。

### 2.5 发散项登记（backlog，本轮不做）
URL 网页抓取入库（trafilatura）、eml/msg 邮件、MinerU 直解 Office（产物为单块无标题层级，分块质量差，不采用）、格式白名单配置化、OCR 图片直传（**状态更新 2026-10-06：部分兑现——文档内嵌图片 OCR 已立项 F-1017；图片文件直传入库仍属 backlog**）。

## 3. 待修改文件全清单（评审核心对象，实施逐项对照）

### 后端（10）
| # | 文件 | 变更 |
|---|------|------|
| 1 | app/rag/precheck.py | 白名单/MIME/魔数扩展；宏拒收；文本类"UTF-8 可解码+无 NUL"；OOXML 子类型判型（读 [Content_Types].xml） |
| 2 | app/rag/parsers_extra.py（**新**） | 五格式解析器（MarkdownParser/TextParser/XlsxParser/PptxParser/HtmlParser） |
| 3 | app/rag/parser.py | 仅 get_parser 加分派分支（**解析器主体零改动**，避开在途修改冲突） |
| 4 | app/rag/xmlguard.py | 错误文案泛化（去 docx 字样），拒绝规则不动 |
| 5 | app/services/ingest.py | _SYNC_REJECTS 文案扩展（宏/转换失败/判型不符） |
| 6 | app/services/convert.py（**新**） | Gotenberg 客户端（httpx，超时 120s、重试 1） |
| 7 | app/services/ingest_pipeline.py | 降级策略按格式收敛（MinerU 降级仅 pdf；新格式失败直接 failed 带原因） |
| 8 | app/core/config.py | convert_base_url / convert_timeout_s |
| 9 | docker-compose.yml | gotenberg 服务 + app 注入 CONVERT_BASE_URL |
| 10 | pyproject.toml | +python-pptx（依赖登记，openpyxl 已有） |

### 测试（4）
| # | 文件 | 变更 |
|---|------|------|
| 11 | tests/unit/test_parsers_extra.py（**新**） | 五格式解析器（现场构造 bytes→断言 ParsedDoc） |
| 12 | tests/api/test_precheck_formats.py（**新**） | 判型不符 422/宏拒收/文本类校验/改名混入 |
| 13 | tests/integration/test_format_expansion.py（**新**） | xlsx/md/html 上传→ready→检索命中；重复单元格行不被清洗误删 |
| 14 | tests/api/test_documents.py + test_zip_import.py | 回归断言补强（新格式 202 链路） |

### 前端（2）
| # | 文件 | 变更 |
|---|------|------|
| 15 | frontend/src/components/documents/UploadDialog.vue | accept/提示文案/宏与老格式引导 |
| 16 | frontend/src/components/documents/ZipImportDialog.vue | 条目说明文案 |

### 文档（阶段一已改 6 份，实施后按实际行为校对）
features.md（F-1013~F-1016）、standards.md（§10.1/§6.3/§10.2）、api.md、deploy-requirements.md、manual-test-guide.md（M2-T14~T16）、帮助页文案（frontend/src/help/admin.ts——**随阶段二实施同步**，避免功能未上线误导用户）。

## 4. 回归影响面分析（不影响原有功能的硬保障）

| 改动 | 对既有 pdf/docx 的影响 | 保障 |
|------|----------------------|------|
| precheck 常量扩展 | 白名单只增不改；pdf/docx 判定路径逐字节不变 | 既有 test_documents/test_zip_import 全量回归 |
| parser.py 加分派 | docx→DocxParser、pdf→路由 分派结果与现状一致 | test_parser 全量回归 |
| xmlguard 文案 | 规则零改动（仅消息文本） | test_parser 的包校验用例回归 |
| ingest 文案映射 | 只增条目 | test_documents 422 文案用例回归 |
| ingest_pipeline 降级收敛 | pdf 的 MinerU 降级路径不变；新格式才有新行为 | test_ingest/test_ingest_progress 回归 |
| Gotenberg 新服务 | 老格式才路由到转换；Gotenberg 停机仅影响老格式（failed+可重试），pdf/docx 不经过它 | 恢复演练：停 gotenberg 传 doc → failed 提示；pdf/docx 正常 |

**完成标准（阶段二）**：新格式用例全过 + 既有全量回归（test_documents/test_zip_import/test_parser/test_ingest*/services 层 + E2E DOC/SVC）零新增失败 + 五格式手动上传到就绪并检索命中。

## 5. 风险

| 风险 | 缓解 |
|------|------|
| 判型误拒（边缘 OOXML 变体） | 判型只比对 content-type 家族前缀，不比对完整 URI；判型失败文案给出"另存为"引导 |
| xlsx 大表解析内存 | openpyxl read_only 模式 + 既有 50MB/10 万条目闸 |
| python-pptx 新依赖 | 锁版本入 uv.lock；用途已登记 standards §10.2 |
| Gotenberg 不可用 | 老格式 failed+可重试（不阻塞主链路）；bootstrap 体检项提示 |
| 范围膨胀 | 发散项已登记 backlog（§2.5），本轮不做 |
