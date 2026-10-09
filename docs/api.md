# RagDemo 接口文档（W1 - W5）

> 定位：活文档｜对账：2026-10-07｜同步保障：端点总数标记行由 `tests/unit/test_api_contract.py` 守账（§19.4）
> 依据 `docs/standards.md` §3（API 接口规范）编写，随代码演进（§19.1 文档随代码）。
> 覆盖 `app/api/` 当前全部路由：auth 5 个、documents 13 个（documents 11 + rebuild 2）、文档分型 2 个（§2.10g/h）、chat 1 个、conversations 4 个、citations 1 个、messages（feedback）1 个、admin 管理端 31 个（问答对 6、评测 8、提问记录与 badcase 4、检索测试 1、概览与健康度 3（含零召回块下钻）、服务健康 1、用户管理 4、审计查询 2、知识缺口报告 2）、healthz 探针 1 个（`GET /healthz`，docker healthcheck 消费，不设业务章节）。
> **端点总数：66**（本行由 `tests/unit/test_api_contract.py` 从 FastAPI app 实际路由解析校验：新增/删除端点未同步本行即测试红，计数漂移清零，findings I10）。
> 最后更新：W5 Task 5 台账翻转（计数 44→47（W5）→48：2026-10-02 新增服务健康卡 1 端点（§4.5.4）；W5 新增知识缺口报告 2 端点，见 §4.9；零召回块下钻 `GET /api/admin/zero-recall-chunks` 1 端点，见 §4.5.3；§4.4.1 docs 项补 hitCount/lastHitAt 历史召回统计两键；2026-10-05 新增文档在线预览 1 端点 §2.10d，chat 引用 citation 增 docId 键 §3.1；2026-10-06 文档契约对账：头部计数改为"端点总数：56"标记行并交契约测试守护（I10）、§2.10c 删除成功响应体更正为 `{id, deleted: true}`（实现为准，I9）、§1.2 补"响应体以实现为准"统一注记；2026-10-06 jti 登出黑名单落地（findings I4）：新增 `POST /api/auth/logout` 1 端点 §2.4a，auth 4→5、总数 56→57；2026-10-06 草稿审核流（agent-evolution #05）：新增 `POST /api/documents/{id}/draft/approve` §2.10e 与 `/draft/reject` §2.10f 共 2 端点，documents 11→13、总数 57→59；2026-10-06 计数对齐实现现状 64：其余增量（memories ×4、agent runs ×1）由并行车道（记忆管理/agent 轨迹）随各自工单补章节登记）；2026-10-06 模板生成首任务（agent-evolution #06）：§5.1 taskType 白名单放开 `template_draft`、补 `save_draft` 工具与 `agent_steps` 逐步轨迹落库说明（无新增端点，总数不变）；2026-10-06 文档分型闭环（metrics-balance 工单 01）：新增 `PATCH /api/admin/documents/{id}/doc-type` §2.10g 与 `POST /api/admin/documents/backfill-doc-type` §2.10h 共 2 端点，文档列表项补 `docType` 键（§2.6），总数 64→66。

## 1. 通用约定

### 1.1 鉴权

- 除 `POST /api/auth/login`、`POST /api/auth/logout` 外全部接口需要登录：请求须携带登录时下发的 `jwt` Cookie
  （httpOnly、SameSite=Lax，有效期默认 8 小时）；logout 不设登录门（幂等清 Cookie，见 §2.4a）。
- 权限门两级：
  - **ReadyUser**（已登录且已完成首登改密）：问答 / 会话 / 反馈 / 引用正文全部端点；
  - **AdminUser**（admin 角色，链在改密门之后）：文档管理（含 zip 批量导入与元数据建议）+ 全部管理端点（问答对 / 评测 / 提问记录与 badcase 治理 / 检索测试 / 运营概览与语料健康度 / 用户管理 / 审计查询）。
- 首登未改密用户访问业务接口一律 `403/2004`；登录过期 / 令牌无效 / 账号禁用一律 `401/2003`。

### 1.2 统一响应体（standards §3-2）

```json
{"code": 0, "message": "ok", "data": ...}
```

`code=0` 成功；非 0 见错误码表（§1.4）。HTTP 状态码用标准语义（§3-3）。
JSON 键名统一 camelCase（§3-4）；列表响应统一 `{items, total, page, pageSize}`；
分页统一 `page`（≥1）+ `pageSize`（1-100，默认 20）（§3-5）。

> **响应体以实现为准**：当前全部端点均未声明 `response_model`（无 Pydantic 出参模型），
> 本文档"成功响应"描述与代码返回 dict 逐键对齐；若两者出现出入，以实现为准修文档
> （2026-10-06 §2.10c 删除响应体即属此类更正，findings I9），并列入 golden 契约回归核对项。

参数校验失败统一 `422/1003`，`data` 携带字段级错误数组：

```json
{"code": 1003, "message": "请求参数错误", "data": [{"field": "question", "message": "问题长度须在 1-2000 字之间"}]}
```

### 1.3 SSE 事件线格式

问答流（`POST /api/chat/stream`）为 `text/event-stream`，每事件两行：

```
event: {事件名}
data: {JSON payload，中文原样}

```

POST SSE 须用 fetch + ReadableStream 消费（无 EventSource 自动重连）；**重试 = 前端重新发起请求生成新消息**，被中断的 assistant 消息标记失败，不重复落库。

### 1.4 错误码表

| code | HTTP | 中文说明 | 典型端点 |
|------|------|---------|---------|
| 1001 | 422 | 文件格式不支持 / 未通过同步预检（宏格式提示"另存为无宏版本"；老格式转换失败提示"另存为对应新格式"；OOXML 判型不符提示"文件内容与扩展名不符"） | POST /api/documents、POST /api/documents/import-zip |
| 1002 | — | 语料解析失败（包体非法/超限/含危险声明；异步段落 failed 态，不经响应体返回） | 文档解析管道 |
| 1003 | 422 | 请求参数校验失败（缺字段/超限/格式非法，data 带字段级原因） | 全部端点 |
| 2001 | 401/400 | 用户名或密码错误（登录失败计数扣减提示；改密时"原密码不正确"同为 2001，HTTP 400） | login / change-password |
| 2002 | 401 | 账号已锁定：连续失败达上限触发 15 分钟持久锁，锁定窗口内正确密码同样拒绝 | login |
| 2003 | 401 | 未登录 / 令牌无效或过期 / 账号已禁用（不区分原因，不给探测空间） | 全部需登录端点 |
| 2004 | 403 | 已登录但无权限（非 admin 访问管理接口；首登未改密访问业务接口） | AdminUser/ReadyUser 端点 |
| 2005 | 429 | 登录请求过于频繁（进程内滑动窗口限流） | login |
| 3001 | 409 | 上传文件与库内已有文档完全重复。**一码双文案**：同步查重命中（F-1001）带已有文档 ID——"文件与已有文档完全重复（已有文档 ID N），如需更新请上传新版本"；并发撞唯一索引（DOC-06）为通用文案——"文件与已有文档重复，请勿重复上传" | POST /api/documents |
| 3002 | 429 | 单用户每日提问次数已达上限（问答端点开流前拒绝） | POST /api/chat/stream |
| 3003 | 404 | 会话/消息不存在或无权访问（归属校验 IDOR 防线，§21.8；不区分"不存在"与"无权"）。W3 管理端寻址失败同码：提问记录不存在 / 点踩记录不存在 / 评测任务不存在；documents 域寻址失败同码（I13，2026-10-06 统一）：文档不存在（retry/confirm/rebuild-index/file/preview/images/delete） | chat / conversations / feedback / documents / admin 治理与评测端点 |
| 3004 | 422 | 评测任务状态冲突：仅 status=done 收口的跑分可人工录分 / 阈值扫描（running 时后台正逐题写 detail，failed 明细不完整；扫描另有"无可重放题目"分支，见 4.2.8） | POST /api/admin/eval-runs/{id}/judgments、POST /api/admin/eval-runs/{id}/threshold-scan |
| 3005 | 409 | 用户名已存在（F-102 发放账号重名；并发撞 users_username_key 唯一索引同码兜底） | POST /api/admin/users |
| 5000 | 500 | 兜底系统错误（响应体只给通用提示"系统繁忙，请稍后重试"，堆栈进日志） | 全部端点 |
| 5001 | —（SSE） | 生成服务暂时不可用（仅经 SSE `error` 事件下发，`retryable=true`，话术对外脱敏） | POST /api/chat/stream |

---

## 2. W1 端点

### 2.1 POST /api/auth/login — 登录

| 项 | 说明 |
|----|------|
| 鉴权 | 无（匿名可访问） |
| 请求体 | `username`（1-64 字符）、`password`（1-200 字符；密码超 72 **字节**由服务层另拒 422/1003，bcrypt 上限） |
| 成功响应 | `200`，`data: {userId, username, role, mustChangePassword}`；`Set-Cookie: jwt=<token>; HttpOnly; SameSite=Lax; Max-Age=480*60`（`mustChangePassword=true` 时前端引导改密页） |
| 错误分支 | `422/1003` 参数校验失败、密码超 72 字节；`429/2005` 登录限流；`401/2001` 用户名或密码错误（带剩余尝试次数文案）；`401/2002` 账号已锁定（15 分钟） |

### 2.2 POST /api/auth/change-password — 修改密码（含首登强制）

| 项 | 说明 |
|----|------|
| 鉴权 | CurrentUser（已登录即可，含 `mustChangePassword=true` 的首登用户） |
| 请求体 | `old`（1-200 字符）、`new`（1-200 字符；服务层要求 ≥8 位且 ≤72 字节） |
| 成功响应 | `200`，`data` 为更新后用户信息 `{userId, username, role, mustChangePassword: false}`；改密成功同时清零失败计数并解除锁定 |
| 错误分支 | `422/1003` 新密码不足 8 位 / 超 72 字节；`400/2001` 原密码不正确 |

### 2.3 GET /api/auth/me — 当前用户信息

| 项 | 说明 |
|----|------|
| 鉴权 | CurrentUser（首登未改密也可访问：前端凭 `mustChangePassword` 决定是否引导改密页） |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {userId, username, role, mustChangePassword}` |
| 错误分支 | `401/2003` 未登录 / 令牌无效 |

### 2.4 GET /api/auth/admin/ping — admin 探针

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser（require_admin 依赖链验证锚点，AUTH-04/06） |
| 成功响应 | `200`，`data: {pong: true}` |
| 错误分支 | `401/2003`；`403/2004` 非 admin |

### 2.4a POST /api/auth/logout — 登出（jti 黑名单，AUTH-08 / findings I4）

| 项 | 说明 |
|----|------|
| 鉴权 | 无登录门（幂等：令牌缺失或已失效也 `200` 清 Cookie，不借登出探测令牌有效性） |
| 请求体 | 无 |
| 成功响应 | `200`，`data: null`；`Set-Cookie: jwt=""; Max-Age=0; HttpOnly; SameSite=Lax`（前端随之清掉本地 Cookie）；令牌有效时把其 jti 记入进程内黑名单（standards §5.3：条目 TTL=令牌剩余有效期，进程重启清空，残余风险书面接受；A 阶段迁 Redis §25.3-A） |
| 效果 | 登出后旧令牌**即时失效**：再带旧 jwt Cookie 请求任意需登录端点一律 `401/2003`（AUTH-08）；黑名单按 jti 生效，重新登录换发的新令牌不受牵连 |
| 错误分支 | 无（恒 `200`） |

### 2.5 POST /api/documents — 上传文档

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求 | multipart/form-data：`file`（文件本体，≤50MB；支持 pdf / docx / md / txt / csv / xlsx / pptx / html，doc / ppt / xls 自动转换；宏格式 xlsm/docm/pptm 拒收）；`title`（≥1 字符）；`dept`（部门 code：service / hr / all）；`owner`（≥1 字符）；`effectiveDate`（YYYY-MM-DD）；`expiryDate`（可选，YYYY-MM-DD）；`complianceConfirmed`（bool，须为 true） |
| 成功响应 | `202`，`data: {id, status}`（status=parsing；test_mode 下同步执行解析管道，响应即可断言终态 ready/failed） |
| 错误分支 | `422/1003` 合规未勾选、文件超 50MB、部门编码无效、元数据缺失、Pydantic 校验失败；`422/1001` 格式不支持 / 未通过同步预检；`409/3001` 精确查重命中（一码双文案见 §1.4） |

### 2.6 GET /api/documents — 文档列表

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 查询参数 | `dept`（code，可选）、`status`（pending/parsing/ready/failed，可选）、`keyword`（标题/原始文件名模糊，可选）、`sourceType`（upload/draft，可选）、`reviewStatus`（待审核/已通过/已驳回，可选）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items: [...], total, page, pageSize}`，created_at 降序（同刻 id 降序兜底）。items 项字段：`id, title, deptId, deptCode, originalFilename, mime, sizeBytes, version, effectiveDate, expiryDate, ownerName, status, parseEngine, parseError, parseErrorDetail(技术错误摘要，仅 failed 非空——管理端定位用，与用户文案双轨), parseStage(解析阶段文案，仅 parsing 非空), parseProgress(真实进度 0-100，仅 parsing 非空), degraded(ready 且带降级标注), sourceType(来源 upload/draft), reviewStatus(草稿审核状态，仅 draft 非空), rejectReason(驳回原因，仅已驳回非空), isActive, uploadedBy, createdAt, updatedAt` |
| 错误分支 | `422/1003` 分页越界、status/sourceType/reviewStatus 取值无效、部门编码无效 |

### 2.7 POST /api/documents/{id}/retry — 失败重试（DOC-11）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {id, status}`（非测试模式先落 parsing 再交后台接管；test_mode 同步执行） |
| 错误分支 | `404/3003` 文档不存在（I13 统一）；`422/1003` 仅 failed 状态可重试 |

### 2.8 POST /api/documents/{id}/confirm — 待确认文档激活

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {id, isActive: true}`（审计操作者为点击确认的管理员本人） |
| 错误分支 | `404/3003` 文档不存在（I13 统一） |

### 2.8a GET /api/documents/{id}/versions — 版本历史（W7 补齐，F-303）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 成功响应 | `200`，`data: {items: [{id, version, status, isActive, effectiveDate, sizeBytes, ownerName, uploadedBy, createdAt}]}`——同部门同主题全部代次按 `version` 降序（含当前行）；归档版本 `isActive: false` 在案可追溯 |
| 错误分支 | `404/3003` 文档不存在 |

### 2.8b POST /api/documents/{id}/rebuild-index — 文档级重建索引（W7 补齐，F-304）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 成功响应 | `202`，`data: {id, status}`（test_mode 同步执行响应即终态；生产 BackgroundTasks 逐文档独立会话） |
| 语义 | 按**现行分块设置**重走解析→红线→清洗→分块→嵌入，单事务内删旧块写新块（原子切换，无无索引窗口）；版本号不变（重建≠新版本）；审计 `document.rebuild {before.chunks, after.chunks, status, isActive}`；成功响应前整清问答精确缓存 |
| 错误分支 | `404/3003` 文档不存在（I13 统一）；`422/1003` 仅 ready 可重建（failed 走 §2.7 retry）或已删除 |
| 审计 | `document.rebuild`（actor=发起管理员；状态机自身另落 document.ready/document.failed） |

### 2.8c POST /api/documents/rebuild-index — 全库级重建索引（W7 补齐，F-304）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 成功响应 | `202`，`data: {accepted}`——受理的就绪文档数；后台逐文档独立事务重刷，单文档失败落 failed 不中断批次 |
| 错误分支 | `401/2003`；`403/2004` |
| 审计 | 逐文档 `document.rebuild` |

### 2.9 POST /api/documents/import-zip — zip 批量导入（W4 后端追加，F-1007）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求 | multipart/form-data：`file`（.zip 压缩包本体；无表单元数据字段） |
| 合规语义 | 视同已勾选内容合规确认（§7.4：zip 导入是管理端管理员的显式批量操作），不设 `complianceConfirmed` 字段 |
| 条目规则 | 文件名 `部门_主题_日期.ext`：部门段中文名（客服/人力/全员）与编码（service/hr/all）均收、归一为编码（与 §2.10 同源别名表）；日期 YYYYMMDD / YYYY-MM-DD（严格位数 + 真实日期），主题可含下划线；责任人=上传管理员；命名不符该条"元数据待补"不入库（skipped_metadata），不拖垮包内其余条目 |
| 安全上限（§6.3） | 先**安全预扫描**（流式计数实际解压字节，不信任 file_size 声明，不落盘不落库）：解压产物总量 ≤500MB、条目数 ≤10 万、嵌套 zip 与加密条目**整包拒收** 422；单条目 >50MB 仅该条 rejected、包内其余继续；任一整包超限读到即中止，无已写产物可残留 |
| 成功响应 | `202`，`data: {results, accepted, rejected, skipped}`。results 逐条目 `{filename, status, documentId?, reason?}`，status 四态枚举：`accepted`（入库并进解析队列，带 documentId）/ `duplicate`（SHA-256 与已有文档完全重复，带已有 documentId 与 reason）/ `rejected`（白名单 / 单条目超限等，带 reason）/ `skipped_metadata`（命名不符，带 reason）。`skipped` = duplicate + skipped_metadata；批量部分成功语义（逐条目独立事务），accepted 条目后台解析（test_mode 同步执行，响应即可断言终态） |
| 错误分支 | `422/1003` 非 zip 包 / 条目数超上限 / 解压总量超 500MB / 嵌套 zip / 加密条目（整包拒收，中文原因指名条目）；`401/2003`；`403/2004` |
| 审计 | `document.import_zip {total, accepted, rejected, skipped}` |

### 2.10a GET /api/documents/{id}/images/{index} — 解析图片回显（F-1017，2026-10-06 追加）

| 项 | 说明 |
|----|------|
| 权限 | 登录用户 + 部门隔离（仅本部门/全员文档）；未激活或非 ready → 404 |
| 成功响应 | `200`，图片字节（Content-Type 按存储 mime）；index 为"图片N："标记中的 N（1 起） |
| 错误分支 | `401/2003`；`404/3003`（文档不存在/未激活/图片序号不存在，I13 统一） |

### 2.10b GET /api/documents/{id}/file — 原文件下载/打开（F-1018，2026-10-06 追加）

| 项 | 说明 |
|----|------|
| 权限 | 登录用户 + 部门隔离（仅本部门/全员文档）；未激活或非 ready → 404 |
| 成功响应 | `200`，原上传文件字节（Content-Disposition 以 original_filename 命名，attachment）；doc/ppt/xls 返回的是入库转换产物（2026-10-05 起为 PDF，见 2.10d 附注） |
| 错误分支 | `401/2003`；`404/3003`（不存在/未激活，I13 统一） |


### 2.10c DELETE /api/documents/{id} — 文档删除（F-305，2026-10-06 立项实施）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member 一律 `403/2004`，未登录 `401/2003` |
| 语义 | 软删（deleted_at+is_active=false）+ 级联停用其 chunks（is_active=false，向量保留——重传同内容经查重拦截属预期）；document_images 行保留（回显随文档闸失效）；**parsing 态 422**（防中断任务竞态，等终态后再删）；审计 document.delete；预览缓存 previews/{doc_id}/ 随档 best-effort 清理 |
| 成功响应 | `200`，`data: {id, deleted: true}`（deleted 为语义标记；库内行保留原 status 并落软删位 deleted_at+is_active=false，前端按 deleted 消费） |
| 错误分支 | `401/2003`、`403/2004`、`404/3003`（不存在，I13 统一）；`422/1003` parsing 态删除（提示等解析终态） |

### 2.10d GET /api/documents/{id}/preview — 原文件在线预览（2026-10-05 追加）

| 项 | 说明 |
|----|------|
| 权限 | 登录用户 + 部门隔离（与 /file 同口径 `_viewable_document` 闸）；未激活/非 ready/软删 → 404 |
| 语义 | 统一以 **PDF inline** 返回（`Content-Disposition: inline`，文件名=原名去扩展名+.pdf），前端 iframe 内嵌渲染。pdf 与老格式（入库已转 PDF）直通；docx/pptx/xlsx 经 Gotenberg 转 PDF，结果按 `previews/{doc_id}/{content_hash}.pdf` 缓存（命中直回）；删除文档时缓存随档清理 |
| 错误分支 | `401/2003`；`404/3003`（不存在/未激活，I13 统一）；`422/1001` 文本类格式（md/txt/csv/html，前端走 /file 转义渲染）或转换服务未配置；`502/1002` 转换失败（技术细节进日志） |

> **2.10d 附注（2026-10-05 F-1015 裁定）**：Gotenberg 8 libreoffice 路由只输出 PDF（容器实测），
> 老格式（doc/ppt/xls）入库转换目标由 OOXML 更正为 PDF——同一份 PDF 落盘既作解析输入
> （走 pdf 链：文字版 pypdf 快速通道/扫描件 MinerU OCR）也作预览直通来源。

### 2.10e POST /api/documents/{id}/draft/approve — 草稿审核通过（agent-evolution #05，2026-10-06）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member `403/2004`，未登录 `401/2003` |
| 背景 | Agent 生成产物一律是草稿（documents.source_type='draft'，登记即 review_status='待审核'；draft 创建走 services/draft_review.create_draft——预检三闸/精确查重与上传同源，FileStorage 落盘，不触发解析）。红线（agent-evolution §五）：**无审核不落地**——草稿 status='pending' 且 is_active=False，检索侧"仅 ready 且 is_active"过滤天然不命中，审核通过前绝不可被检索（tests/integration/test_draft_review.py 读回断言） |
| 语义 | 待审核 → 已通过（审计 document.draft_approve，与状态变更同事务）→ 立即触发既有解析入库管道（编排同 retry：test_mode 同步，生产置 parsing 挂 BackgroundTasks；预检/查重/敏感扫描全走无旁路）；成功响应前整清问答精确缓存（通过改变可检索事实，W2 移交②） |
| 成功响应 | `200`，`data: {id, reviewStatus, status}`（test_mode 下 status 为管道收口终态；生产为 parsing） |
| 错误分支 | `404/3003`（不存在，I13 统一）；`422/1003` 非 draft 来源审核 / 重复审核（非法迁移 fail-fast） |
| 契约登记 | 迁移 0007（source_type/review_status/reject_reason，每列中文注释 + CHECK）；前端 DocumentsView「草稿审核」入口（draft+待审核快捷筛选，行内通过 confirm / 驳回 prompt） |

### 2.10f POST /api/documents/{id}/draft/reject — 草稿审核驳回（agent-evolution #05，2026-10-06）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member `403/2004`，未登录 `401/2003` |
| 请求体 | `{"reason": str}`（1-500 字必填；空串 `422/1003`） |
| 语义 | 待审核 → 已驳回 + 原因留档（reject_reason，审计 document.draft_reject）；永不触发解析与入库，status 停在 pending；不整清问答缓存（驳回不改变可检索事实） |
| 成功响应 | `200`，`data: {id, reviewStatus, rejectReason}` |
| 错误分支 | `404/3003`（不存在，I13 统一）；`422/1003` 非 draft 来源 / 非 待审核 状态 / 驳回原因为空 |

### 2.10g PATCH /api/admin/documents/{id}/doc-type — 文档分型人工修正（metrics-balance 工单 01，2026-10-06）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member `403/2004`，未登录 `401/2003` |
| 背景 | 文档自动分型（metrics-balance §3）：入库管道分块前按结构信号检测五档（policy_clause=制度条款 / table_report=表格报表 / notice=通知公告 / operation_manual=操作手册 / general=通用兜底），随 `documents.doc_type` 落库（迁移 0010，VARCHAR(32) DEFAULT 'general' + 具名 CHECK `documents_doc_type_check`）；检测纯函数在 `app/rag/doc_profile`，golden 镜像独立重算（spec §6） |
| 请求体 | `{"docType": str}`——仅此一字段（现无其他文档字段编辑端点）；取值限五档 code，枚举外 `422/1003`（"分型取值无效：…"） |
| 语义 | 覆写 doc_type 并审计 `document.set_doctype`（detail 记 before/after docType）；**编辑不限文档状态**（傻瓜式，改错可改回）；**不清问答精确缓存**（落锤 §3.4：docType 只影响查询期 top_n 档位与报告分组，不改可检索事实） |
| 成功响应 | `200`，`data: {id, docType}` |
| 错误分支 | `404/3003`（不存在，I13 统一）；`422/1003` 枚举外取值 |

### 2.10h POST /api/admin/documents/backfill-doc-type — 存量文档分型回填（metrics-balance 工单 01，2026-10-06）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member `403/2004`，未登录 `401/2003` |
| 语义 | 对全部 **is_active 且 ready** 文档从 chunks 重算分型（检测输入由 chunks 剥离 `title_path｜` 前缀重组，与入库期口径逐字一致——services/doc_type 适配器）；**覆盖现有值（含手工修正）**；ingest 进行中未 ready 的文档天然不参与；**不清问答缓存**（同 2.10g）；审计 `document.backfill_doctype`（detail 记 updated/byType） |
| 成功响应 | `200`，`data: {updated, byType}`——updated=分型发生变化的文档数；byType=其中按新分型的计数分布（如 `{"policy_clause": 3}`；无变化时 `{"updated": 0, "byType": {}}`） |
| 错误分支 | `403/2004`（member）；无业务性 4xx（空集即 0 变化） |

### 2.10 POST /api/documents/metadata-suggest — 上传元数据自动建议（W4 后端追加，F-1008）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求 | multipart/form-data：`file`（**仅解析文件名，不读文件内容**，纯函数无 IO） |
| 规则 | 与 zip 条目同源：`部门_主题_日期.ext`；部门中文名（客服/人力/全员）与编码（service/hr/all）均收、归一为编码；日期 YYYYMMDD / YYYY-MM-DD（严格位数 + 真实日期）；主题可含下划线 |
| 成功响应 | `200`，`data: {suggestedDeptCode, suggestedTitle, suggestedEffectiveDate}`；任一段不符返回空建议 `{}`（不报错、绝不产出错误建议），前端仅作表单预填 |
| 越权防线 | 建议部门仅限库内枚举——文件名声明的部门不得造成越权注入；建议仅预填，最终提交仍过上传端点部门校验（§2.5）；AdminUser 门即管辖闸（部门→ACL 演进时按上传人管辖部门收紧，§1.7） |
| 错误分支 | `401/2003`；`403/2004` |

---

## 3. W2 端点

### 3.1 POST /api/chat/stream — 问答流（SSE）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 请求体 | `conversationId`（int，可空；空=新建会话，首问标题取 question 前 20 字）、`question`（strip 后 1-2000 字，§21.5） |
| 成功响应 | `200`，`Content-Type: text/event-stream`，事件序 **citations → token → done**（citations 恒先于 token） |
| 开流前错误（普通 JSON） | `422/1003` question 长度非法（字段级）；`429/3002` 今日提问次数已达上限（"今日提问次数已达上限，请明天再试"）；`404/3003` 会话不存在或无权访问（含他人会话与已软删会话）；`500/5000` 检索/改写上游失败（prepare 失败时中断占位行被标记，不留幽灵行） |
| 流中错误 | SSE `error` 事件（生成段失败），不再重复落库 |

SSE 事件 DTO 字段表（**逐字引自 standards §3 第 6 条**）：

| 事件 | payload 字段 |
|------|-------------|
| `citations` | `messageId`, `items: [{n, refType: 'doc'\|'qa', refId, title, page, titlePath, score}]`（**先于 token 事件下发**） |
| `token` | `messageId`, `content`（增量文本片段） |
| `done` | `messageId`, `refused: bool`, `cacheHit: bool`, `promptTokens`, `completionTokens`, `durationMs`, `degraded: bool` |
| `error` | `code`（取自共享错误码枚举）, `message`（中文，已脱敏）, `retryable: bool` |

补充：`error.retryable` 仅 `code=5001`（生成服务暂时不可用）时为 true；`code=5000`（系统兜底）为 false。拒答分支 `refused=true` 且 token 事件逐字下发固定拒答话术、不写缓存；缓存命中分支 `cacheHit=true` 一次性下发全文。`degraded`（retrieval-policy §5）标记检索上游降级轮：embed 失败拒答或 rerank 故障向量序兜底为 true（拒答与"简化检索"语义并存），正常/qa/cache 轮恒 false 且字段不缺席（仅 done 事件携带，citations/token 不带；降级轮引用 score 为召回余弦口径，与 rerank sigmoid 分不互比）。`refId` 语义随 `refType`：`qa` → 问答对 id；`doc` → 命中片段的 chunk id（非文档 id）。检索范围恒为 `[用户部门, 全员]`（F-502）。

### 3.2 POST /api/conversations — 新建会话

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 请求体 | 无（title 走库默认"新会话"；问答首问以首问前 20 字命题——conversationId 为空（问答端点自建会话）直接命题，复用已有会话若仍为默认题（"新会话"/空）则随首轮问答补题，二轮起标题已命题不再改） |
| 成功响应 | `200`，`data: {id}` |
| 错误分支 | `401/2003`；`403/2004` 首登未改密 |

### 3.3 GET /api/conversations — 本人会话列表

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 查询参数 | `page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items: [{id, title, updatedAt}], total, page, pageSize}`，updated_at 降序（同刻 id 降序兜底）；仅本人活跃会话（软删不出现） |
| 错误分支 | `422/1003` 分页越界；`401/2003`；`403/2004` |

### 3.4 GET /api/conversations/{id}/messages — 会话历史消息

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser（归属本人，否则与不存在同判 404/3003） |
| 成功响应 | `200`，`data: {items: [...]}`，created_at 升序（同刻 id 升序兜底）。items 项字段：`id, role, content, citations, feedback, feedbackCategory, feedbackComment, answerType, cacheHit, degraded, createdAt`（citations 为 JSONB 原样，user 消息恒 null） |
| 过滤语义 | 中断遗留的 assistant 占位行（is_active=false）不进列表；user 消息全返回 |
| 错误分支 | `404/3003` 会话不存在或无权访问；`401/2003`；`403/2004` |

### 3.5 DELETE /api/conversations/{id} — 删除会话（软删）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser（归属本人；他人会话一律 404/3003） |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {id, isActive: false}`；不物理删；重复删除幂等 200 |
| 错误分支 | `404/3003` 会话不存在或无权访问；`401/2003`；`403/2004` |

### 3.6 POST /api/messages/{id}/feedback — 消息反馈（点赞/点踩，F-208）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser（仅本人 assistant 消息且 is_active=true 可反馈；user 消息与中断占位行不可） |
| 请求体 | `feedback`（枚举 `"up"` / `"down"`）；`category`（down 必填，枚举：答案错误 / 引用无关 / 拒答不当 / 其他）；`comment`（down 必填，strip 后 1-500 字）。up 时二者忽略置 null |
| 成功响应 | `200`，`data: {id, feedback, feedbackCategory, feedbackComment, badcaseStatus}`。down 落三列并把 badcase_status="待处理" 进治理队列（F-702；仅状态为空或"待处理"时置，admin 处理终态"已修语料"/"已忽略"不被用户改评重开）；改评语义：down→up 清空备注/类型但 badcase_status 不回退，重复 down 幂等更新备注/类型 |
| 错误分支 | `422/1003` 备注长度非法（field=comment）/ 类型不在枚举（field=category）；`404/3003` 消息不存在或无权访问（不存在 / 他人消息 / 非 assistant / 中断占位行同判）；`401/2003`；`403/2004` |

### 3.7 GET /api/citations/content — 引用正文（W4 后端追加，T8 评审裁决 Rider）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser（引用产自问答链路，读者即提问者） |
| 查询参数 | `refType`（枚举 `doc` / `qa`）、`refId`（正整数；语义随 refType：`doc` → chunk id，`qa` → 问答对 id，同 §3.1 citations 口径） |
| 隔离与存活闸 | 部门隔离 0 容忍（红线①）：`dept_id IN (用户部门, 全员部门)`，与检索 F-502 同口径；存活闸与检索面一致——chunk `is_active` / 问答对 `enabled`+`is_active` / 文档 `is_active`+`ready`（检索给出的引用必在面内，文档撤回后不得再经引用读出正文，W4 T4 评审裁定保留） |
| 成功响应 | `200`，`data` 六键：`{refType, refId, content, title, page, titlePath}`。doc 分支 content=块原文、title=文档规范标题；qa 分支 content=answer、title=question，`page` / `titlePath` 恒 null（问答对无页码与标题路径概念） |
| 错误分支 | `404/3003` 统一文案"引用不存在或无权访问"（隔离面外与不存在不区分，不给探测空间，同会话归属口径）；`422/1003` refType 枚举外 / refId 非正整数 |

---

## 3A. 记忆管理端点（agent-evolution F-210，2026-10-06 立项实施）

本节四个端点均为 **ReadyUser 门**，资源严格按 `user_id` 归属隔离（个人资源，user_id 校验强于部门过滤）；删除为软删（`is_active=false`），不存在与无权一律 `404/3003` 同话术"记忆不存在或无权访问"（IDOR 防线，不给探测空间）；响应体统一 `{code, message, data}`。

### 3.8a GET /api/memories — 本人记忆列表（F-210）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 查询参数 | `page`（≥1 默认 1）、`pageSize`（1-100 默认 20）、`keyword` 可选（内容模糊匹配） |
| 成功响应 | `200`，`data` 为 `{items, total, page, pageSize}`；item 键 `{id, content, origin, sourceMessageId, createdAt}`，`origin` ∈ `auto`（后台抽取）/ `manual`（手动添加），`is_active=false` 行不出现 |
| 说明 | 列表按 `created_at` 降序；用户记忆总容量 30 条（超限自动淘汰最旧，F-210 设计） |

### 3.8b POST /api/memories — 手动添加记忆（F-210）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 请求体 | `{content: string}`，strip 后 1-200 字（非法 422/1003 字段级中文报错） |
| 成功响应 | `201`，`data` 为新建记忆 `{id, content, origin: "manual", createdAt}` |
| 错误分支 | 容量满时自动淘汰最旧一条后写入（用户无感，F-210 设计）；内容查重不拦截手动添加 |

### 3.8c DELETE /api/memories/{memory_id} — 删除记忆（软删，F-210）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser（归属校验：非本人记忆与不存在同判） |
| 成功响应 | `200`，`data` 为 `{id, deleted: true}`；重复删除幂等 200 |
| 错误分支 | `404/3003` "记忆不存在或无权访问" |
| 副作用 | 审计留痕 `memory.delete`；删除后立即不参与回答注入 |

### 3.8d PUT /api/memories/settings — 记忆功能开关（F-210）

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 请求体 | `{enabled: boolean}` |
| 成功响应 | `200`，`data` 为 `{memoryEnabled: boolean}`；`GET /api/auth/me` 的 `memoryEnabled` 字段同步 |
| 副作用 | 关闭后立即停止后台记忆抽取与回答注入（有测试锁定）；审计留痕 `memory.settings` |

---

## 4. W3 端点（管理后台）

本节全部端点均为 **AdminUser 门**（依赖链：登录 → 首登改密 → admin 角色；`401/2003` 未登录 / 令牌无效、`403/2004` 已登录但无权限，下文各端点不再重复）。按域分七个 router：问答对管理（M4）、评测（M6）、提问记录与 badcase 治理（M7）、检索测试、运营概览与语料健康度，及 W4 追加的用户管理（§4.7，F-102）与审计查询（§4.8，F-802）。涉及列表的响应统一 `{items, total, page, pageSize}`、created_at 降序（同刻 id 降序兜底）；DTO 键 camelCase。

### 4.1 问答对管理（M4，/api/admin/qa-pairs）

#### 4.1.1 POST /api/admin/qa-pairs/import — Excel 整批导入

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求 | multipart/form-data：`file`（.xlsx，≤50MB；扩展名 / Content-Type / 魔数三重校验）；`deptCode`（表单字段，可选，缺省 `all`，枚举 service / hr / all——模板不含部门列，导入部门由此指定，保证检索 `dept_id IN (用户部门, all)` 语义可用） |
| 行级规则 | 问题非空 ≤2000 字、答案非空 ≤10000 字、状态列枚举 `enabled`/`disabled`（空缺省 enabled）；任何一行不合格整批退回零入库（QA-02/03/08）；单批上限 5000 行 |
| 成功响应 | `201`，`data: {imported, batchNo}`（batchNo 形如 `QA-YYYYMMDD-XXXX`）；只向量化问题列（答案不向量化，与检索同一 embedder）；成功即整清问答精确缓存 |
| 错误分支 | `422/1003` 文件超 50MB / 格式不支持或已损坏（三重白名单任一不过）；`422/1003` 行级错误整批退回——message 统一"导入文件存在 N 处错误，已整批退回，未入库"，`data` 为错误行数组 `[{row, field, reason}]`（row 为 Excel 物理行号，表头=1 数据从 2 起）；文件级单一错误（无数据 / 超 5000 行）reason 直接上 message；`422/1003` 部门编码无效 |

#### 4.1.2 GET /api/admin/qa-pairs/template — 模板下载

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，xlsx 字节流，`Content-Disposition: attachment`（文件名 `qa_pairs_template.xlsx`）；表头 问题 / 答案 / 状态 + 3 行示例 |

#### 4.1.3 GET /api/admin/qa-pairs — 管理列表

| 项 | 说明 |
|----|------|
| 查询参数 | `deptId`（可选）、`status`（enabled / disabled，可选）、`keyword`（问题/答案 ILIKE 模糊，`%`/`_`/`\` 已转义按字面匹配）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`。items 项字段：`id, deptId, deptCode, question, answer, status, batchNo, createdAt, isActive`（deptCode 为 join departments 取库内编码）；软删行不出现 |
| 错误分支 | `422/1003` status 取值无效、分页越界 |

#### 4.1.4 PATCH /api/admin/qa-pairs/{id}/status — 停用 / 启用

| 项 | 说明 |
|----|------|
| 请求体 | `status`（枚举 `enabled`=参与检索 / `disabled`=停用不参与） |
| 成功响应 | `200`，`data: {id, status}`；停用改变可检索事实，成功响应前整清问答精确缓存；审计 `qa.toggle {from, to}` |
| 错误分支 | `422/1003` status 枚举外；`404/1003` 问答对不存在或已软删 |

#### 4.1.5 PUT /api/admin/qa-pairs/{id} — 编辑

| 项 | 说明 |
|----|------|
| 请求体 | `question`、`answer`（全文；校验同导入规则：问题 1-2000、答案 1-10000 非空） |
| 成功响应 | `200`，`data: {id, question, answer}`；问题单条重嵌（与检索同一 embedder，同指纹空间）后整清缓存，新答案立即生效；审计 `qa.update {before, after 摘要}` |
| 错误分支 | `422/1003` 校验失败；`404/1003` 不存在或已软删 |

#### 4.1.6 DELETE /api/admin/qa-pairs/{id} — 删除（软删）

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，`data: {id, isActive: false}`；置 is_active=false 退出检索与列表（行保留可追溯），整清缓存；审计 `qa.delete` |
| 错误分支 | `404/1003` 不存在或已软删（已删行不可再管理） |

### 4.2 评测（M6，/api/admin/eval-questions + /api/admin/eval-runs）

#### 4.2.1 POST /api/admin/eval-questions/import — 题库 Excel 整批导入

| 项 | 说明 |
|----|------|
| 请求 | multipart/form-data：`file`（.xlsx，≤50MB，三重校验同 4.1.1；单批上限 5000 行） |
| 表头 | 部门 / 问题 / 期望答案 / 来源 / 题型：**部门在文件行内**用中文枚举（客服 / 人力 / 全员——与问答对导入的表单 deptCode 口径不同）；来源可空（空串落 NULL）；题型枚举 `factual` / `multi_hop` / `cross_reject`（空缺省 factual） |
| 拒答归一 | 期望答案填 `[拒答]` → 题型归一为 cross_reject；题型 cross_reject 而期望答案非 `[拒答]` → 期望答案归一为 `[拒答]`（双向静默归一，判定协议第 3 条） |
| 成功响应 | `201`，`data: {imported}`（表无批次号列，不回 batchNo）；评测题纯文本存库不向量化、不参与问答检索，导入不失效问答缓存 |
| 错误分支 | `422/1003` 行级错误整批退回（语义同 4.1.1；规则：部门中文枚举、问题非空 ≤2000、期望答案非空 ≤10000、题型枚举）；`422/1003` 部门未播种 |

#### 4.2.2 GET /api/admin/eval-questions/template — 模板下载

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，xlsx 字节流（`eval_questions_template.xlsx`）；表头 部门 / 问题 / 期望答案 / 来源 / 题型 + 3 行示例（事实型 / 多跳 / 串库拒答） |

#### 4.2.3 POST /api/admin/eval-runs — 发起跑分（异步）

| 项 | 说明 |
|----|------|
| 请求体 | `name`（strip 后非空 ≤100 字）、`perspectiveDeptCode`（视角部门 code；检索范围恒为 [视角部门, 全员]，题面部门只进报告口径不参与检索） |
| 成功响应 | `202`，`data: {id}`；创建即落 running 行并提交，跑分由后台任务执行（BackgroundTasks，逐题 commit 进度可查；单题异常记入明细不拖垮整体）；跑分走评测专用依赖副本：旁路问答精确缓存、不占每日提问限额，消息带 `is_eval=true` 不入运营统计 |
| 错误分支 | `422/1003` name 为空 / 超 100 字；`422/1003` 视角部门未播种 |

#### 4.2.4 GET /api/admin/eval-runs — 跑分任务列表

| 项 | 说明 |
|----|------|
| 查询参数 | `page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`。items 项字段：`id, name, status(running/done/failed), total, correct, accuracy, createdAt`——correct 为加权总分（NUMERIC 容半分）、accuracy=correct/total（pending 计 0，4 位小数） |

#### 4.2.5 GET /api/admin/eval-runs/{id} — 跑分详情（轮询用）

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，`data` = 4.2.4 列表项字段 + `config` + `detail`；`config` 为收口时 10 字段快照（model / promptVersion / qaThreshold / refuseThreshold / efSearch / topK / topN / temperature / manifestVersion / judgePromptVersion，锁定阈值、模型与提示词版本防跨跑对比被污染；running 期为 null） |
| detail 结构 | `{items: [逐题], pendingCount: n}`。逐题字段：`questionId, deptId, qtype, expected, sourceDocId, answerType, answer, citations, retrieval, metrics, autoJudgment, judgeError, error`——autoJudgment ∈ correct / wrong / pending；`sourceDocId` 为题目来源文档 id（拒答类/存量旧题 null，报告 byDocType 分组键）；`retrieval = {contextChunkIds, topKCandidates}` 为检索中间产物（拒答题 contextChunkIds 恒空、候选照记；缓存命中题候选空）；`metrics = {recall, citationsOk, contextPrecision, relevancy, correctness}` 四指标逐题值（拒答题/失败题 metrics=null，judge 失败时三判折算三键 null、机检两键照算）；`judgeError` 仅 judge 三判失败时非 null（整题 pending，绝不部分采纳）；error 仅单题生成失败时非 null（user 消息留痕、不落 assistant 行）；人工录分后逐题追加 `judgment` 字段。`pendingCount` = 人工与 auto 均未判（最终判定 pending）的题数。running 期逐题 commit 仅含 `items`；failed 态无 `pendingCount` 键（前端轮询需容忍键缺失） |
| 自动判分口径 | 拒答类（期望答案 `[拒答]` 或题型 cross_reject）按回答是否逐字等于固定拒答话术"知识库中未找到相关内容"自动判 correct / wrong，不调 LLM judge（metrics=null）；其余题型跑分后由 judge 三判合一次调用（temperature=0 评测专用实例）预填 correctness 三档，judge 输出任一字段非法 → 整题 pending + judgeError；人工评判以 judgment 覆写合并（判定协议第 1/3 条，metrics-balance §2/§4.3） |
| 错误分支 | `404/3003` 评测任务不存在 |

#### 4.2.6 POST /api/admin/eval-runs/{id}/judgments — 人工评判录分

| 项 | 说明 |
|----|------|
| 请求体 | `items`（非空数组）：`[{questionId, judgment}]`；judgment 三档枚举锁 API（判定协议第 1 条）：`correct` / `partial` / `wrong` |
| 折算口径 | correct=1 / partial=0.5 / wrong=0 / pending=0；每题最终判定 = 人工 judgment 优先，否则 autoJudgment（录分与报告共用同一合并口径）；correct/accuracy 即时重算 |
| 成功响应 | `200`，`data` = 收口后 summary（同 4.2.4 项字段）+ `pendingCount`（同步刷新，前端即时更新）；审计 `eval.judge {runId, judged}` |
| 错误分支 | `422/3004` 跑分未收口（仅 status=done 可录分，见错误码表）；`422/1003` questionId 不在本次明细（message 指名题号）、items 为空、judgment 枚举外；`404/3003` 任务不存在 |

#### 4.2.7 GET /api/admin/eval-runs/{id}/report — 分组报告

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，`data: {config, status, totals, byDept, byQtype, fourMetrics, byDocType}`：`totals: {total, correct, accuracy, pendingCount}`（从 detail 明细聚合而非直读列，与录分口径自洽）；`byDept: [{deptCode, total, accuracy}]`（按题面部门，字典序）；`byQtype: [{qtype, total, accuracy}]`（字典序）；组内 accuracy = 组内加权 correct / 组内 total。running / failed 也可出报告（部分明细 + 真实状态，不做状态闸） |
| fourMetrics | `{contextRecall, contextPrecision, answerRelevancy, answerCorrectness}`（metrics-balance §2/§4.4，顺序冻结），每项 `{value: number\|null, evaluatedCount}`：null 排除集均分，evaluatedCount=参算题数。口径：recall=期望出处文档是否进入 context（机检，拒答类与 source_doc_id 空的存量旧题记 null）；precision=context 逐块含作答所需信息的排名加权 `Σ rel_i/i ÷ Σ 1/i`（LLM judge）；relevancy=回答切题度三档 1/0.5/0（LLM judge）；correctness 走 final_judgment 合并口径（judge 预填 autoJudgment、人工评判优先，pending 与拒答类不计） |
| byDocType | `[{docType, questionCount, contextRecall, contextPrecision, answerRelevancy, answerCorrectness}]`（docType 字典序）：分组键=题目 source_doc_id 指向文档的分型，组内四指标同口径集均分；source_doc_id 为 NULL 的题入 `docType="unassigned"`（未标注）组——只显 questionCount、四指标为 null，但其 relevancy/precision/correctness 仍计入全局 fourMetrics（recall null 自然排除） |
| 错误分支 | `404/3003` 任务不存在 |

#### 4.2.8 POST /api/admin/eval-runs/{id}/threshold-scan — 阈值扫描向导（F-1012 简化版）

| 项 | 说明 |
|----|------|
| 请求体 | 无。对该 run 已收口的 detail 快照**离线重放**（不调 LLM、不触检索、不改线上阈值），纯内存毫秒级，同步返回（非后台任务） |
| 重放口径 | 仅重放明细中非 error 且最终判定非 pending（人工 judgment 优先否则 autoJudgment，同 report 口径）的题：refuse 题（citations 为空、无 doc 分）每档都判"拒答"；rag 题取 citations 中 doc 块最大 rerank 分对照档位，score < 阈值判"拒答"（与线上 refuse 分支同序）；qa 题不参与拒答曲线（scope.qaTotal 计数注明），仅参与 qa 曲线——refType=qa 的余弦分 ≥ 阈值即命中（与 match_qa 同序）。拒答档位 0.40-0.70 步长 0.05（7 档）、qa 档位 0.75-0.95 步长 0.05（5 档）。逐档 accuracy 折算：拒答期望题（`[拒答]`/cross_reject）判"拒答"=1、判"回答"=0；非拒答题判"拒答"=0、判"回答"=final_judgment 折算（人工优先，correct 1 / partial 0.5 / wrong 0） |
| 成功响应 | `200`，`data: {refuseCurve, qaCurve, recommended, scannedAt, scope}`。`refuseCurve: [{threshold, accuracy, refuseRate}]`×7（分母 = refuse/rag 参与题数）；`qaCurve: [{threshold, hitRate}]`×5——**仅 qa 题档位敏感度**：detail 快照未存 rag 题曾见的 qa 匹配分，不含"降档会把 rag 题误引入 qa 通道"的反证数据，局限在 `scope.note` 注明；`recommended: {refuseThreshold, qaThreshold}`——accuracy（qa 侧为 hitRate）最高的档、**并列取更低阈值**（同等得分下更倾向回答/更宽容），该侧无参与者为 null；**拒答侧推荐守卫（T5 修复轮裁决）：`refuseRate=1.0` 的"全拒答"档不参与推荐**——该档可凭严格最高 accuracy 把推荐推向"全部拒答"（如仅剩漏判串库题在该档被拒），恰是本功能要解决的场景，故排除；仅当所有档均 refuseRate=1.0（无可回答档）时守卫回退、全档参与（此时 accuracy 全档同值，并列取最低档）；qa 侧无拒答率概念、不设对称守卫；`scannedAt` ISO 时刻；`scope: {refuseTotal, qaTotal, pendingSkipped, errorSkipped, note}` 分区计数与口径局限说明。结果同时存入该 run 的 `detail.thresholdScan`（F-1012 可追溯，重复扫描覆盖），审计 `eval.threshold_scan {runId}` 与写入同事务 |
| 错误分支 | `404/3003` 任务不存在；`422/3004` 非 done 状态（message"评测任务未完成，暂不能扫描阈值"）、或明细全部待人工评判/执行失败（message 指名"无可重放题目"） |

### 4.3 提问记录与 badcase 治理（M7，/api/admin/messages、/api/admin/badcases）

两查询恒滤 `is_eval=true`（W3 约束：评测数据不污染运营查询）；提问记录以 user 消息行为单位，badcase 队列以点踩回答为单位。口径说明：提问记录与运营概览包含已删除会话的消息（管理审计面，删除仅前端隐藏）。

#### 4.3.1 GET /api/admin/messages — 提问记录列表（F-701）

| 项 | 说明 |
|----|------|
| 查询参数 | `deptId`（≥1，可选）、`dateFrom` / `dateTo`（YYYY-MM-DD，含首尾日，可选）、`hasFeedback`（bool，true=回答被点踩，可选）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`。items 项字段：`id, conversationId, createdAt, username, deptCode, questionPreview（截 50 字）, answerType, hasCitations, feedback`——后三项取自配对回答；无回答 / 中断的问题记 None / false |

#### 4.3.2 GET /api/admin/messages/{id} — 提问详情

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，`data: {user, assistant}`。user 段：`{id, conversationId, createdAt, username, deptCode, content（提问原文，区别于列表 50 字摘要）}`；assistant 段为 null（该问无有效回答，如中断）或 `{id, content, citations（JSONB 原样，含 id——badcase 处理入口）, answerType, model, promptTokens, completionTokens, latencyMs, cacheHit, feedback, feedbackCategory, feedbackComment}` |
| 错误分支 | `404/3003` 提问记录不存在（非 user 行 / 评测消息 / 不存在 id 同文案，不给探测空间） |

#### 4.3.3 GET /api/admin/badcases — badcase 队列（F-702）

| 项 | 说明 |
|----|------|
| 查询参数 | `deptId`（≥1，可选）、`badcaseStatus`（可选，缺省不过滤——"待处理"由前端显式传参，默认视图由前端带参）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 队列口径 | feedback='down' 且非评测的 assistant 消息（软删中断行不入），一条回答一条记录不重复；created_at 降序 |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`。items 项字段：`messageId, createdAt, deptCode, questionPreview（截 50 字）, category, comment, badcaseStatus` |

#### 4.3.4 PATCH /api/admin/badcases/{messageId} — badcase 处置

| 项 | 说明 |
|----|------|
| 请求体 | `badcaseStatus`（枚举 `"已修语料"` / `"已忽略"`——只收 admin 终态，"待处理"仅由 W2 点踩入口产生，不提供重开回退）；`note`（处理理由，随冻结基线 ADM-03：`已忽略` 必填 strip 后 1-500 字、`已修语料` 可选给了则同样 1-500；只入审计不入 messages） |
| 成功响应 | `200`，`data: {messageId, badcaseStatus}`；审计 `badcase.update {messageId, from, to, note}` 与状态更新同事务落库 |
| 错误分支 | `422/1003` 枚举外（字段级指名 badcaseStatus）/ note 长度非法；`404/3003` 点踩记录不存在（不存在 / 非 down / 评测点踩同文案） |

### 4.4 检索测试（/api/admin/retrieval-test）

#### 4.4.1 POST /api/admin/retrieval-test — 检索调参工具（只读）

| 项 | 说明 |
|----|------|
| 请求体 | `question`（strip 后 1-2000 字）、`deptCode`（枚举 service / hr / all，检索视角；admin 任意视角，但检索 SQL 恒带部门过滤） |
| 成功响应 | `200`，`data: {qa, docs, thresholds}`——重放检索管道（normalize → embed → 问答对匹配 → 向量检索 → 重排折叠），qa 与 docs 两路结果并呈互不短路 |
| qa 字段 | `{id, question, similarity, thresholdPassed}` 或 null——similarity 为 **embedding 余弦**（qa 口径，对照当前 qa 阈值；低于阈值即返回 null，命中时 thresholdPassed 恒 true）。**两套分数口径不互比**：qa 的余弦与 docs 的 rerank 分分属不同空间（standards §4.4） |
| docs 项字段 | `{chunkId, documentTitle, page, titlePath, rerankScore, recallCosine, thresholdPassed, folded, hitCount, lastHitAt}`——**rerankScore 为 rerank sigmoid 分、recallCosine 为召回阶段余弦**，两套分数分字段呈列；folded=true 标记进 top_n 但被折叠丢弃的块（调参需可见）；thresholdPassed 对照当前 Settings 阈值逐块标记；hitCount/lastHitAt 为该块历史召回统计（F-1011 读现列回显：lastHitAt ISO8601，null=从未召回；本端点只读不写计数，不污染 §4.5 零召回榜） |
| thresholds | `{qa, refuse}` 当前生效阈值（config 缺省 0.85 / 0.55） |
| 副作用 | 无：不调 LLM、不写召回计数（不污染零召回榜）、只读不落审计 |
| 错误分支 | `422/1003` question 长度非法（field 级）、deptCode 枚举外 |

### 4.5 运营概览与语料健康度

口径说明：运营概览与提问记录一样，包含已删除会话的消息（管理审计面，删除仅前端隐藏）。

#### 4.5.1 GET /api/admin/overview — 运营概览（F-704）

| 项 | 说明 |
|----|------|
| 查询参数 | `days`——仅 `1` 或 `7` 两档（缺省 7；窗口 created_at >= now-days） |
| 成功响应 | `200`，`data: {totals, byDept}`。两组同构：`{questions, hitRate, refuseRate, downRate, tokens}`——questions=窗口内 assistant **非评测**回答行数（含中断行）；分母恒为 questions（各比率同口径可横向对比）：hitRate=qa+rag 回答占比、refuseRate=拒答占比、downRate=点踩占比；比率统一 0-1 四位小数；tokens=prompt+completion 之和。byDept 按 deptCode 升序且只含有数据的部门 |
| 错误分支 | `422/1003` days 取值非法（窗口外） |

#### 4.5.2 GET /api/admin/health — 语料健康度（F-1009）

| 项 | 说明 |
|----|------|
| 成功响应 | `200`，`data: {totalDocs, readyCount, readyRate, pendingConfirm, zeroRecall, expiringSoon}`——`zeroRecall` 零召回文档榜：is_active+ready 且建档严格早于 30 天前、且近 30 天无任一 chunk 被命中（文档级反连接，恰在 30 天线的命中即算近期）；项 `{id, title, deptCode, createdAt}` 按建档升序。`expiringSoon` 待过期清单：expiry_date ≤ 今天+7 天且未删除（**含已到期未下线**）；项 `{id, title, expiryDate}` |

#### 4.5.3 GET /api/admin/zero-recall-chunks — 零召回块清单（W5 后端追加，F-1011）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member 一律 `403/2004`，未登录 `401/2003`（§1.1 全端点口径） |
| 查询参数 | `days`（1-365，缺省 30，与 §4.5.2 文档级零召回榜同值，可调宽供运营排查）、`page`（≥1，缺省 1）、`pageSize`（1-100，缺省 20）（§1.2 分页口径） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`（§1.2 列表口径）；items 按 createdAt 降序（同刻 id 降序兜底），total 为 WHERE 全量计数 |
| items 项字段 | `{chunkId, documentTitle, deptCode, titlePath, contentPreview, createdAt}`——contentPreview 服务端截断 50 字（预览列不整段拖表）；createdAt 为块建档时刻（ISO8601） |
| 入列口径 | §4.5.2 `zeroRecall` 零召回文档榜的块级下钻（同一口径的文档级/块级两面）：块 `lastHitAt` 为 null（从未召回）或早于 now−days 即入列，恰压 days 线算近期不入列；文档闸与文档级逐字一致（is_active+ready），块侧另加块级 is_active（停用块同样不可被召回） |
| 副作用 | 无：只读、不写召回计数（不污染零召回榜）、不落审计 |
| 错误分支 | `422/1003` 查询参数越界（days/page/pageSize field 级） |

#### 4.5.4 GET /api/admin/services/health — 服务健康卡（2026-10-02 试点反馈新增）

| 项 | 说明 |
|----|------|
| 权限 | 仅 admin（AdminUser）；member 一律 `403/2004`，未登录 `401/2003`（§1.1 全端点口径） |
| 用途 | 运营概览"服务健康"卡：对问答链路各依赖做轻量探活只读展示（app 容器 healthy 但 TEI/LLM 挂掉时管理页此前不可见）。**只读不做启停**——服务生命周期归 compose/deploy/recover.sh（web 触达容器编排=攻击面质变，2026-10-02 评估结论） |
| 探活方式 | pg=注入连接 SELECT 1；tei-embed/rerank=GET {base}/health；llm=GET {base}/models（Bearer key，5s 超时）；mineru=GET {base}/docs；gotenberg=GET {base}/health（convert_base_url 空为 not_configured）。各 2.5s 超时（llm 5s），HTTP 探活并发（总耗时=最慢者） |
| TEST_MODE | embed/rerank/llm 走 Stub 无网络依赖 → 直接 up 且 detail 注明"TEST_MODE 桩，未探活"（CI 无 TEI/外网也全绿）；pg 与 mineru 照常真实探活 |
| 成功响应 | `200`，`data: {overall, rerankCircuit, services: [{key, label, status, latencyMs, detail}]}`。key ∈ pg/embed/rerank/llm/mineru/gotenberg；status ∈ up/down/not_configured（mineru_base_url 空=pypdf 降级路径；convert_base_url 空=老格式拒收，均非故障）；overall 三态：down=问答链路（pg/embed/rerank/llm）任一不可用，degraded=仅 mineru/gotenberg 不可用（问答正常、解析或老格式转换降级），up=全绿。`rerankCircuit`（retrieval-policy §3）=rerank 熔断器三态 closed/open/half_open，与探活相互独立（探活 up 不代表熔断没开），总闸（RERANK_BREAKER_ENABLED）关闭恒 closed——open/half_open 表示问答已走向量序降级 |
| 副作用 | 无：只读、不写库、不落审计（探活请求发往依赖服务自身，不触用户数据） |
| 错误分支 | `401/2003`、`403/2004`；探活异常不报 500——一律转为对应服务 status=down + detail 原因摘要 |

### 4.6 提问记录保留策略（非端点，后台任务）

提问记录两段式清理（F-803 / §4.6，无管理端点，随进程调度执行）：

- **第一阶段（归档停用）**：created_at 早于 now − `message_retention_days`（默认 90）的 messages 置 is_active=false，幂等空跑不审计；命中时写批量审计 `message.archive`（detail 记 archived 条数与 cutoff 边界）。
- **冷却期**：`message_cleanup_cooldown_days`（默认 30）。
- **第二阶段（物理清理）**：created_at 早于 now −（保留 + 冷却）的消息物理删除——messages 是全库**唯一物理删除点**，分批 ≤1000 行/批、批间独立提交；purge 阶段不再逐条审计（audit_logs 只增不删，被清理记录靠归档批量审计可追溯）。
- **调度**：每日 03:00 UTC（APScheduler cron，单机单实例语义）；保留 / 冷却天数实时读 Settings，改配置无需重启调度器。

### 4.7 用户管理（W4 后端追加，F-102，/api/admin/users）

#### 4.7.1 GET /api/admin/users — 用户列表

| 项 | 说明 |
|----|------|
| 查询参数 | `keyword`（username ILIKE 模糊，`%`/`_`/`\` 已转义按字面匹配，可选）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`，created_at 降序（同刻 id 降序兜底）。items 项字段：`id, username, name（同 username，库无姓名列）, deptCode, role（admin/member）, status（"正常"/"已禁用"）, locked（按当下时刻判定，locked_until 已过期不算锁着）, mustChangePassword, lastLoginAt（null=从未登录）, createdAt` |

#### 4.7.2 POST /api/admin/users — 发放账号

| 项 | 说明 |
|----|------|
| 请求体 | `username`（2-32 位中文/字母/数字，§21.5）、`deptCode`（枚举 service / hr / all）、`role`（枚举 admin / member） |
| 一次性密码语义 | 初始密码 16 位密码学安全随机（secrets.choice，无歧义字符集去 0/O/1/l/I，人工抄写不混淆），**仅在本次响应体出现一次**：库里只有 bcrypt 哈希，明文不落库、不进日志与审计（红线 4）；新账号置 `mustChangePassword=true` 首登强制改密 |
| 成功响应 | `201`，`data: {id, initialPassword}`；审计 `user.create {username, deptCode, role}`（不含明文密码） |
| 错误分支 | `422/1003` 用户名口径 / 角色枚举外 / 部门编码无效；`409/3005` 用户名已存在（并发撞 users_username_key 唯一索引同码兜底，见错误码表） |

#### 4.7.3 PATCH /api/admin/users/{id}/status — 启用 / 禁用

| 项 | 说明 |
|----|------|
| 请求体 | `isActive`（bool：false=禁用 / true=启用） |
| 成功响应 | `200`，`data: {id, isActive}`；禁用即时失效（§5.6：下次请求 `401/2003`）；审计 `user.toggle {from, to}` |
| 错误分支 | `422/1003` 禁用自己（防自锁，"不能禁用当前登录账号"）；`404/1003` 用户不存在 |

#### 4.7.4 POST /api/admin/users/{id}/reset-password — 重置密码

| 项 | 说明 |
|----|------|
| 一次性密码语义 | 新初始密码同 4.7.2 口径（16 位无歧义随机、仅响应体一次展示）；重置同时清零失败计数并解除锁定、置 `mustChangePassword=true` 强制首登改密 |
| 成功响应 | `200`，`data: {id, initialPassword}`；审计 `user.reset_password`（不含明文） |
| 错误分支 | `404/1003` 用户不存在 |

### 4.8 审计日志查询（W4 后端追加，F-802，/api/admin/audit-logs）

audit_logs 只增不改（F-801），两端点均只读、不留审计；筛选经共享依赖解析、两端口径一致且全参数绑定。

#### 4.8.1 GET /api/admin/audit-logs — 审计列表

| 项 | 说明 |
|----|------|
| 查询参数 | `actorId`（≥1，可选）、`action`（精确匹配，可选）、`objectType`（精确匹配，可选）、`dateFrom` / `dateTo`（YYYY-MM-DD，UTC 整日边界，dateTo 含当日，可选）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items, total, page, pageSize}`，created_at 降序（同刻 id 降序兜底）。items 项字段：`id, actorUsername, action, objectType, objectId, detail（JSONB 原样，键序不保证）, ip, createdAt` |

#### 4.8.2 GET /api/admin/audit-logs/export — CSV 导出

| 项 | 说明 |
|----|------|
| 查询参数 | 同 4.8.1 筛选条件（无分页） |
| 成功响应 | `200`，`text/csv` 流式响应（StreamingResponse 逐行推送，不整表载内存）：UTF-8 BOM 适配 Excel、中文列头（时间/操作人/动作/对象类型/对象ID/IP/详情，detail 为紧凑 JSON 字符串列）；`Content-Disposition: attachment; filename="audit-yyyymmdd.csv"`；行数超 10 万截断并以 `X-Truncated: true` 响应头标注（内存放大保护，截断判定用同 WHERE 计数） |


### 4.9 知识缺口报告（W5 后端追加，F-1010，/api/admin/knowledge-gap）

拒答问题按周聚合榜（前端 P10"知识缺口 TOP"卡片数据源）。口径：messages 中
role='user' AND is_eval=false 的用户问题，LATERAL 配对其同会话中 id 大于问题行且
最小的 assistant 行（最小 id 配对同 4.3 提问记录；assistant 侧同样滤 is_eval），
配对回答活跃且 answer_type='refuse' 即记一次缺口（中断/无回答不算拒答）；按问题
原文分组 count 降序。周界按 UTC 周一 00:00（weekOffset=0 为本周，负数回看），
过滤 [weekStart, weekEnd) 左闭右开。两端点均只读、不留审计，筛选口径一致。

#### 4.9.1 GET /api/admin/knowledge-gap — 知识缺口周榜

| 项 | 说明 |
|----|------|
| 查询参数 | `weekOffset`（0 与负整数，-11..0：0=本周、-1=上周……默认 0，越界 422/1003） |
| 成功响应 | `200`，`data: {items, total, weekStart, weekEnd}`。items 至多 50 项按 count 降序（同数按问题文本升序兜底），项字段：`question`（用户问题原文）、`count`（窗口内该问题拒答次数）；`total` 为窗口内去重问题总数（可大于 items 长度）；`weekStart` / `weekEnd` 为聚合窗口 ISO 字符串（左闭右开） |

#### 4.9.2 GET /api/admin/knowledge-gap/export — CSV 导出

| 项 | 说明 |
|----|------|
| 查询参数 | 同 4.9.1 `weekOffset` |
| 成功响应 | `200`，`text/csv` 流式响应（逐行推送，不整表载内存）：UTF-8 BOM 适配 Excel、中文列头（问题/出现次数）；聚合口径同 4.9.1 但不限前 50；问题列首字符为 = + - @ 等公式触发符时前置制表符强制按文本解释（CSV 公式注入防御）；`Content-Disposition: attachment; filename="knowledge-gap-yyyymmdd.csv"`（取注入时钟当日）；行数超 10 万截断并以 `X-Truncated: true` 响应头标注（内存放大保护，截断判定用同窗口计数） |

---

## 5. Agent 演进端点（2026-10-06 立项，specs/agent-evolution.md）

### 5.1 POST /api/agent/runs — agent 任务流（SSE，AG-UI 协议，F-212 双轨新轨）

DeepAgents 骨架的 agent 任务流式端点。SSE 线格式同 §1.3（`event:` + `data:` 两行），
但事件名与载荷键名**说 AG-UI 标准协议**（ag-ui-protocol 官方事件类型，载荷键为协议
本身的 snake_case，如 `thread_id`/`tool_call_id`——不套本仓 camelCase 惯例，改名即
破坏互操作）。旧 `POST /api/chat/stream` 四事件契约冻结（§3.1），两轨互不污染；
收缩条件见 specs/agent-evolution.md §6.3。

| 项 | 说明 |
|----|------|
| 鉴权 | ReadyUser |
| 请求体 | `taskType`（白名单：`kb_qa` 知识库问答 / `template_draft` 模板草稿生成（agent-evolution #06，2026-10-06），未知 422/1003）、`input`（任务输入，strip 后 1-2000 字，§21.5） |
| 成功响应 | `200`，`text/event-stream`。事件序 **RUN_STARTED → (STEP_STARTED → TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END → STEP_FINISHED)×N → TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT → TEXT_MESSAGE_END → RUN_FINISHED**；失败以 RUN_ERROR 收尾 |
| 开流前错误（普通 JSON） | `422/1003` taskType 不在白名单（现支持 `kb_qa` / `template_draft`）/ input 长度非法（字段级）；`429/3002` 今日额度用尽（与问答共享同一计数器，换端点不绕限）；`429/2005` 同一用户已有 2 个进行中任务（并发上限，完成后再发起） |
| 流中错误 | SSE `RUN_ERROR` 事件（`message` 对外脱敏、`code` 为字符串型共享错误码：5001 上游故障 / 5000 兜底意外）；agent_runs 行同步置 failed，不留 running 幽灵行；客户端断连由服务端兜底置 failed |

关键事件载荷（AG-UI 官方键名）：`RUN_STARTED`/`RUN_FINISHED` 带 `thread_id`、`run_id`（RUN_FINISHED 另带 `result.text` 成稿）；`STEP_*` 带 `step_name`（step-N，一次模型迭代一步）；`TOOL_CALL_*` 带 `tool_call_id`、`tool_call_name`（ARGS 的 `delta` 为参数 JSON）；`TEXT_MESSAGE_*` 带 `message_id`、`delta`。检索工具 `knowledge_base_search` 部门范围恒为 `[本人部门, 全员]`（F-502，调用者上下文强制继承，参数携带的部门字段一律无效）。

任务说明（任务档案 `services/agent_tasks.TASK_PROFILES`，白名单校验与编排同源消费）：

- `kb_qa`：知识库问答；工具集 `knowledge_base_search`。
- `template_draft`（#06）：HR 制度/客服话术模板生成——系统提示要求先检索知识库内既有制度/话术做 few-shot 参照，再产出结构化 Markdown 模板；工具集 `knowledge_base_search` + `save_draft`（参数 `title`/`content`，包装 `services/draft_review.create_draft`：草稿按调用者身份落 `documents.source_type='draft'`/`review_status='待审核'`，无审核不落地——审核走 §2.10e/§2.10f，通过后既有管道收口 ready 方可被检索）。检索持续空手而归时任务以"知识库中未找到相关内容"收尾，不输出编造成稿。

落库：每次开流写一行 `agent_runs`（归属 user_id/dept_id、run_type、status、input 快照），成功回填 `output={text}` 与耗时，失败回填脱敏 `error`；每次工具调用另落一行 `agent_steps`（迁移 0009：`seq` 与 TOOL_CALL 事件序一致，args/观察截断存储、工具失败原因落 `error`）——前端看到的事件序 = 库内轨迹（审核回放依据，specs §五"轨迹与草稿一体保存"）。
