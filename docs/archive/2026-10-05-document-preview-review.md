# 文档原文件在线预览设计评审材料（2026-10-05）

> 评审对象：文档原文件**在线预览**能力（管理页 + 问答引用弹窗两入口），替代"新标签页强制下载"的唯一现状。
> 流程：本材料评审**通过并确认待修改文件清单**后，进入代码实施（同 F-305 文档→评审→实现流程）。
> 附带：评审中确认一处**现存缺陷**（引用弹窗拿 chunk_id 当文档 id，见 §1.2），修复随本功能一并实施。

---

## 1. 背景与现状

### 1.1 现状：只有下载，没有预览

- 管理页操作列"原文件"按钮（DocumentsView.vue:244-254）：`<a href="/api/documents/{id}/file" target="_blank">`，后端返回 `Content-Disposition: attachment`（documents.py:261-265）→ 浏览器新标签页**强制下载**，看完一篇文档要"下载→打开→关标签页"三步。
- 问答引用弹窗"查看原文件"（SourceDialog.vue:108-110）：同样的 `<a target="_blank">` 下载。
- 全前端无任何 iframe / embed / pdf.js 先例（已 grep 证实），属全新能力，无重复建设。

### 1.2 现存缺陷：引用弹窗拿 chunk_id 当文档 id

- `chat.py:165`：citation 的 `refId` = `hit.chunk_id`（**chunk 表主键**）；
- `retrieval.py:66`：`DocHit` 同时携带 `document_id`，但组装 citation 时被丢弃；
- 前端 SourceDialog.vue:39,45 却把 `refId` 直接拼进 `/documents/{refId}/file` 与 `/images/{refId}/{n}`，而后端按 **Document 主键**查（`db.get(Document, doc_id)`）→ chunk_id ≠ doc_id 时（绝大多数情况）**404 或错文档**。引用正文端点 `/api/citations/content` 用 chunk_id 是**正确**语义（按 chunk 取正文），仅前端两处 URL 拼接受影响。
- 修复：citation 增加 `docId` 字段（`_rag_plan` 一行），前端原文件/图片/预览全部改用 `docId`。

## 2. 交互方案对比

| 方案 | 说明 | 评价 |
|---|---|---|
| **A. 页内 el-dialog（宽 ~80%）+ iframe**【推荐】 | 点"预览"原地弹窗，iframe 加载 PDF；不离开当前页，问答页可边看原文边继续对话 | 与既有 UploadDialog/ZipImportDialog 组件模式同构；上下文不丢 |
| B. el-drawer 抽屉 | 右侧滑出 | 宽度受限，横版 PDF/pptx 阅读体验差 |
| C. 新标签页直开预览 URL | `window.open(previewUrl)`，浏览器原生 PDF 阅读器 | 实现最薄，但离开工作台上下文；与"下载"差异缩小 |

推荐 **A**：与现有弹窗组件族一致、上下文保留；弹窗头部放文档标题 + "下载原文件"常驻链接，预览失败时降级面板同样给下载链接兜底。

## 3. 分格式预览策略矩阵

| 格式 | 预览方式 | 说明 |
|---|---|---|
| pdf | 预览端点**直通原字节**，`Content-Disposition: inline` + `application/pdf`，iframe 原生渲染 | 零转换成本 |
| docx / doc / ppt / pptx / xls / xlsx | 预览端点调 **Gotenberg 转 PDF** 后回 PDF | 老格式（doc/ppt/xls）入库时原件已被覆盖为 OOXML（ingest.py:131-132），盘上只有转换后文件——预览即"OOXML→PDF"单段转换，无需保留原件副本 |
| md / txt / csv / html | **前端取文本 + 转义 `<pre>` 渲染**（fetch 原文件字节 → text） | 免转换、即时显示；html **绝不**服务端 inline 渲染（存储型 XSS 载体），一律按纯文本转义显示；表格化渲染登记 backlog |
| 转换失败 / 转换服务未配置 | 弹窗内**降级面板**："该文档暂不支持在线预览"+ "下载原文件"链接 | 文案与 convert.py:64 未配置提示同口径 |

## 4. 后端设计

| 决策点 | 方案 | 依据 |
|---|---|---|
| 端点 | `GET /api/documents/{doc_id}/preview`，**只服务 PDF 形态内容**（pdf 直通或转换结果），md/txt/csv/html 由前端走既有 `/file` 端点取文本，不进此端点 | 职责单一；与 /file（attachment）语义分离 |
| 鉴权与存活闸 | `CurrentUser` + 复用 `_viewable_document`（ready + is_active + 未软删 + 本部门/全员，否则 404） | 与 /file、/images 同口径，零新增闸逻辑 |
| 转换 | `DocumentConverter` 新增 `convert_to_pdf(filename, content) -> bytes`：同路由 `/forms/libreoffice/convert`、同超时（convert_timeout_s=120）与一次重试口径，失败抛 `UpstreamError` → ApiError 中文降级。**输出参数形态以已部署 Gotenberg 8 容器实测为准**（实施第一步先 curl 验证） | 与 convert_to_ooxml（convert.py:37-59）同构 |
| 缓存 | 落 `previews/{doc_id}/{content_hash}.pdf`（仿 ingest_pipeline.py:139 `images/{doc_id}/` 约定）；命中（文件存在）直回；未命中转换后 `open("xb")` 独占写（并发首预览防重复写，败者读已有文件）；**缓存键用 content_hash 而非 updated_at**（后者无 onupdate、靠服务层手动刷新，逐写路径确认成本高） | FileStorage 协议只有 save（storage.py:23-24），按既有 images/ 约定直接落盘 |
| 失效 | content_hash 寻址天然按内容失效：重试/重传产生新 hash → 新缓存文件；`delete_document` 顺带 best-effort 清理 `previews/{doc_id}/` 目录；孤儿文件体积小（≤50MB/个）可容忍 | 无需引入清理任务 |
| 未配置转换服务 | convert_base_url 为空时，office 类预览 → ApiError 中文降级文案；pdf 直通不受影响 | 与 convert.py:64 同口径 |
| Content-Disposition | `inline; filename*=UTF-8''{quote(文件名)}`；转换生成的 PDF 文件名 = 原名去扩展名 + `.pdf` | 复用 documents.py:261-265 的 RFC 5987 构造模式 |

## 5. 前端设计

| # | 文件 | 变更 |
|---|------|------|
| 1 | `frontend/src/components/documents/DocPreviewDialog.vue`（新） | props `visible` + `doc`（id/title/mime/originalFilename），emit `close`；按 mime 分支：pdf/office → `<iframe :src="docPreviewUrl(id)">`（加载 loading 态，转换可能数秒）；md/txt/csv/html → `fetchDocumentFile` 取文本转义 `<pre>`；错误 → 降级面板（含下载链接）；头部：标题 + "下载原文件"常驻链接 |
| 2 | `frontend/src/api/documents.ts` | 增 `docPreviewUrl(id)`、`fetchDocumentFile(id): Promise<Blob>`（独立 fetch + `credentials: 'include'`，模式抄 auditLogs.ts:72-97，不走 client.request 信封解包） |
| 3 | `frontend/src/views/DocumentsView.vue` | 操作列"原文件"旁增**预览**按钮（同 `v-if="row.status === 'ready'"`）；`previewDoc/previewVisible` ref；挂载 DocPreviewDialog（照 UploadDialog visible/close 模式，DocumentsView.vue:279-280） |
| 4 | `frontend/src/components/chat/SourceDialog.vue` | "查看原文件"旁增"在线预览"入口，复用 DocPreviewDialog；**cite-file/cite-image 全部改用 `citation.docId`**（修 §1.2 缺陷） |
| 5 | `frontend/src/api/types.ts` | `CitationItem` 增 `docId?: number`；修正 :45 过时注释（refId=chunk id） |

## 6. 后端待修改文件清单

| # | 文件 | 变更 |
|---|------|------|
| 7 | `app/services/convert.py` | `DocumentConverter.convert_to_pdf()`（同路由/超时/重试/UpstreamError 口径） |
| 8 | `app/api/documents.py` | `GET /{doc_id}/preview` 路由：`_viewable_document` 闸 → pdf 直通 / office 走 converter+缓存 / 文本类 422 明确提示 → FileResponse inline |
| 9 | `app/services/chat.py` | `_rag_plan` citation 增 `"docId": hit.document_id`（qa 引用无文档语义，不加） |
| 10 | `app/services/ingest.py` | `delete_document` 增 previews 目录 best-effort 清理 |
| 11 | 测试 | 见 §8 |

## 7. 安全分析（评审必读）

| 项 | 结论 |
|---|---|
| 部门隔离 / IDOR | 预览端点原样复用 `_viewable_document` 四重闸（软删/停用/非 ready/跨部门一律 404），与 /file /images 同口径，无新攻击面 |
| XSS | 服务端**只回 application/pdf**（浏览器内置阅读器不执行 PDF JS）；md/txt/csv/html 走前端转义文本渲染，用户上传的 html **永不**服务端 inline 渲染——存储型 XSS 路径封死 |
| SSRF | 无任何用户可控 URL：Gotenberg 调用是"文件字节上传到部署配置的内网服务"（convert_base_url 为部署配置项），Mimosa"服务端请求 URL 校验 host"约束核对为 **N/A**，已留此记录 |
| 上限 | 预览不新增上传面；缓存文件 ≤50MB（上传上限既有闸）；转换超时 120s 同既有 convert 口径 |
| 密钥/日志 | 转换失败技术细节只进服务端日志（parse_error_detail 同款双轨思路），响应只给中文用户文案 |

## 8. 测试计划

| 层 | 用例 |
|---|------|
| api（新 `tests/api/test_document_preview.py`，约 6 用例） | ① pdf 直通：inline 头 + application/pdf + 字节一致；② docx：stub converter 转 PDF + 缓存文件落盘读回 + **第二次请求 converter 只调一次**（缓存命中）；③ 401/404（不存在、软删后）；④ converter 未配置 → 中文降级 ApiError；⑤ 文本类格式（md）→ 明确 4xx 提示（前端走文本路径）；⑥ delete 后 previews 目录清理断言 |
| 单元 | `convert_to_pdf` 成功/超时重试一次/UpstreamError（模式抄既有 convert 测试） |
| 前端单测 | documents-view.spec：预览按钮存在 + 点击开弹窗；chat-feedback.spec：SourceDialog 预览入口 + **docId 断言**（钉缺陷修复）；documents-mock.ts / chat-mock.ts 的 routeFetch 补 `/documents/{id}/file` 字节流分支（现会 throw unexpected fetch） |
| E2E | documents-gov.spec 增 DOC-2x：上传 pdf fixture → ready → 点预览 → 弹窗内 pdf 容器可见；DocumentsPage.ts 增 `openPreview` helper。**现有 e2e 0 处断言"原文件"，零破坏**（已 grep 证实） |
| 文档 | api.md 增端点契约（47→48）；user-guide.md §1 增预览用法；manual-test-guide.md 增预览手测用例 |

## 9. 工作量与风险

工作量：后端约 0.5 天 + 前端约 0.5 天 + 测试约 0.5 天 + 文档同步 0.5 天。

| 风险 | 缓解 |
|------|------|
| Gotenberg 8 PDF 输出参数形态与预期不符 | 实施第一步先对已部署容器 curl 实测再写代码 |
| 大文件转换慢（50MB xlsx → 10s+） | 弹窗 loading 态；超时 120s 同既有口径；异步任务化登记 backlog（试点规模不做） |
| 并发首次预览同一文档重复转换 | `xb` 独占写，败者读已有文件；最坏容忍一次重复转换（幂等） |
| 缓存孤儿文件累积 | delete 时 best-effort 清理 + 单文件 ≤50MB 可容忍；物理清理归 retention（两段式既有语义） |
| 浏览器兼容 | Chrome/Edge/Firefox 内置 PDF 阅读器均支持 iframe inline；试点仅现代浏览器 |

## 10. 待评审确认点

1. **交互形态**：推荐 A（页内弹窗 + iframe）；B 抽屉 / C 新标签页如更合意请指出。
2. **csv 预览**：v1 按纯文本渲染，表格化渲染登记 backlog——是否可接受。
3. **引用侧 docId 缺陷修复**：随本功能一并实施（推荐）。

---

## 11. 实施附记（2026-10-05 批复"按推荐实施"后落档）

用户批复：方案 A + csv 纯文本 + docId 随修，三项均照此实施。实施中的两处偏离与裁定：

1. **F-1015 缺陷确认与修复（实施第一步容器实测发现）**：Gotenberg 8 libreoffice 路由
   **只输出 PDF、无保留原格式选项**（docx/txt 输入实测均返回 `%PDF-`；官方文档确认）。
   原设计"老格式盘上已是 OOXML，单段转换即可"不成立——`convert_to_ooxml` 实际把 PDF
   字节按 .docx 落盘，python-docx 解析必败，**老格式转换在真实容器上从未工作过**。
   裁定：转换目标改为 PDF（`DocumentConverter.convert_to_pdf`，含 `%PDF-` 魔数校验防
   上游行漂移），转换产物落盘即走 pdf 解析链（文字版 pypdf 快速通道/扫描件 MinerU），
   同一份 PDF 兼作预览直通来源——解析与预览共用一次转换，预览端对老格式退化为直通。
   已同步：api.md §2.10b/2.10d 附注、user-guide §M2-T18 口径、评审材料 §3 矩阵以此为准。
2. **预览弹窗改走 fetch→Blob→objectURL**（评审稿为 iframe 直指端点 URL）：转换失败/
   未配置时端点返回 JSON 错误体，iframe 直指会把裸 JSON 渲染在框内；fetch 路径可捕获
   错误信封渲染降级面板，体验一致且可控。文本类渲染、缓存、安全分析不变。

测试口径偏离说明：convert 客户端 HTTP 层未新增单测（原计划"convert 单测"）——其重试/
超时口径与 convert_to_ooxml 时代相同且不可注入 transport，改由 api 层转换桩用例 +
真实容器冒烟（DOC-26 e2e 与 M2-T20 手测）覆盖。
