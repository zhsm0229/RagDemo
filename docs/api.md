# RagDemo 接口文档（W1 - W4）

> 依据 `docs/standards.md` §3（API 接口规范）编写，随代码演进（§19.1 文档随代码）。
> 覆盖 `app/api/` 当前全部路由：auth 4 个、documents 6 个、chat 1 个、conversations 4 个、citations 1 个、messages（feedback）1 个、admin 管理端 27 个（问答对 6、评测 8、提问记录与 badcase 4、检索测试 1、概览与健康度 2、用户管理 4、审计查询 2），共 44 个端点。
> 最后更新：W5 Task 2（qa-pairs 列表 DTO 补 deptCode 成 9 键；§4.3/§4.5 补"包含已删除会话的消息"口径句；§2.9 部门段双收中文，与 §2.10 同源别名表）。

## 1. 通用约定

### 1.1 鉴权

- 除 `POST /api/auth/login` 外全部接口需要登录：请求须携带登录时下发的 `jwt` Cookie
  （httpOnly、SameSite=Lax，有效期默认 8 小时）。
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
| 1001 | 422 | 文件格式不支持 / 未通过同步预检（.doc 请另存为 docx 后上传） | POST /api/documents |
| 1002 | — | 语料解析失败（包体非法/超限/含危险声明；异步段落 failed 态，不经响应体返回） | 文档解析管道 |
| 1003 | 422 | 请求参数校验失败（缺字段/超限/格式非法，data 带字段级原因） | 全部端点 |
| 2001 | 401/400 | 用户名或密码错误（登录失败计数扣减提示；改密时"原密码不正确"同为 2001，HTTP 400） | login / change-password |
| 2002 | 401 | 账号已锁定：连续失败达上限触发 15 分钟持久锁，锁定窗口内正确密码同样拒绝 | login |
| 2003 | 401 | 未登录 / 令牌无效或过期 / 账号已禁用（不区分原因，不给探测空间） | 全部需登录端点 |
| 2004 | 403 | 已登录但无权限（非 admin 访问管理接口；首登未改密访问业务接口） | AdminUser/ReadyUser 端点 |
| 2005 | 429 | 登录请求过于频繁（进程内滑动窗口限流） | login |
| 3001 | 409 | 上传文件与库内已有文档完全重复。**一码双文案**：同步查重命中（F-1001）带已有文档 ID——"文件与已有文档完全重复（已有文档 ID N），如需更新请上传新版本"；并发撞唯一索引（DOC-06）为通用文案——"文件与已有文档重复，请勿重复上传" | POST /api/documents |
| 3002 | 429 | 单用户每日提问次数已达上限（问答端点开流前拒绝） | POST /api/chat/stream |
| 3003 | 404 | 会话/消息不存在或无权访问（归属校验 IDOR 防线，§21.8；不区分"不存在"与"无权"）。W3 管理端寻址失败同码：提问记录不存在 / 点踩记录不存在 / 评测任务不存在 | chat / conversations / feedback / admin 治理与评测端点 |
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

### 2.5 POST /api/documents — 上传文档

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求 | multipart/form-data：`file`（文件本体，≤50MB，白名单格式）；`title`（≥1 字符）；`dept`（部门 code：service / hr / all）；`owner`（≥1 字符）；`effectiveDate`（YYYY-MM-DD）；`expiryDate`（可选，YYYY-MM-DD）；`complianceConfirmed`（bool，须为 true） |
| 成功响应 | `202`，`data: {id, status}`（status=parsing；test_mode 下同步执行解析管道，响应即可断言终态 ready/failed） |
| 错误分支 | `422/1003` 合规未勾选、文件超 50MB、部门编码无效、元数据缺失、Pydantic 校验失败；`422/1001` 格式不支持 / 未通过同步预检；`409/3001` 精确查重命中（一码双文案见 §1.4） |

### 2.6 GET /api/documents — 文档列表

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 查询参数 | `dept`（code，可选）、`status`（pending/parsing/ready/failed，可选）、`keyword`（标题/原始文件名模糊，可选）、`page`（≥1，默认 1）、`pageSize`（1-100，默认 20） |
| 成功响应 | `200`，`data: {items: [...], total, page, pageSize}`，created_at 降序（同刻 id 降序兜底）。items 项字段：`id, title, deptId, deptCode, originalFilename, mime, sizeBytes, version, effectiveDate, expiryDate, ownerName, status, parseEngine, parseError, isActive, uploadedBy, createdAt, updatedAt` |
| 错误分支 | `422/1003` 分页越界、status 取值无效、部门编码无效 |

### 2.7 POST /api/documents/{id}/retry — 失败重试（DOC-11）

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {id, status}`（非测试模式先落 parsing 再交后台接管；test_mode 同步执行） |
| 错误分支 | `404/1003` 文档不存在；`422/1003` 仅 failed 状态可重试 |

### 2.8 POST /api/documents/{id}/confirm — 待确认文档激活

| 项 | 说明 |
|----|------|
| 鉴权 | AdminUser |
| 请求体 | 无 |
| 成功响应 | `200`，`data: {id, isActive: true}`（审计操作者为点击确认的管理员本人） |
| 错误分支 | `404/1003` 文档不存在 |

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
| `done` | `messageId`, `refused: bool`, `cacheHit: bool`, `promptTokens`, `completionTokens`, `durationMs` |
| `error` | `code`（取自共享错误码枚举）, `message`（中文，已脱敏）, `retryable: bool` |

补充：`error.retryable` 仅 `code=5001`（生成服务暂时不可用）时为 true；`code=5000`（系统兜底）为 false。拒答分支 `refused=true` 且 token 事件逐字下发固定拒答话术、不写缓存；缓存命中分支 `cacheHit=true` 一次性下发全文。`refId` 语义随 `refType`：`qa` → 问答对 id；`doc` → 命中片段的 chunk id（非文档 id）。检索范围恒为 `[用户部门, 全员]`（F-502）。

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
| 成功响应 | `200`，`data: {items: [...]}`，created_at 升序（同刻 id 升序兜底）。items 项字段：`id, role, content, citations, feedback, feedbackCategory, feedbackComment, answerType, cacheHit, createdAt`（citations 为 JSONB 原样，user 消息恒 null） |
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
| 成功响应 | `200`，`data` = 4.2.4 列表项字段 + `config` + `detail`；`config` 为收口时 9 字段快照（model / promptVersion / qaThreshold / refuseThreshold / efSearch / topK / topN / temperature / manifestVersion，锁定阈值与模型防跨跑对比被污染；running 期为 null） |
| detail 结构 | `{items: [逐题], pendingCount: n}`。逐题字段：`questionId, deptId, qtype, expected, answerType, answer, citations, autoJudgment, error`——autoJudgment ∈ correct / wrong / pending；error 仅单题失败时非 null（user 消息留痕、不落 assistant 行）；人工录分后逐题追加 `judgment` 字段。`pendingCount` = 人工与 auto 均未判（最终判定 pending）的题数。running 期逐题 commit 仅含 `items`；failed 态无 `pendingCount` 键（前端轮询需容忍键缺失） |
| 自动判分口径 | 拒答类（期望答案 `[拒答]` 或题型 cross_reject）按回答是否逐字等于固定拒答话术"知识库中未找到相关内容"自动判 correct / wrong；其余题型 autoJudgment=pending 留人工评判（判定协议第 3 条） |
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
| 成功响应 | `200`，`data: {config, status, totals, byDept, byQtype}`：`totals: {total, correct, accuracy, pendingCount}`（从 detail 明细聚合而非直读列，与录分口径自洽）；`byDept: [{deptCode, total, accuracy}]`（按题面部门，字典序）；`byQtype: [{qtype, total, accuracy}]`（字典序）；组内 accuracy = 组内加权 correct / 组内 total。running / failed 也可出报告（部分明细 + 真实状态，不做状态闸） |
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
| docs 项字段 | `{chunkId, documentTitle, page, titlePath, rerankScore, recallCosine, thresholdPassed, folded}`——**rerankScore 为 rerank sigmoid 分、recallCosine 为召回阶段余弦**，两套分数分字段呈列；folded=true 标记进 top_n 但被折叠丢弃的块（调参需可见）；thresholdPassed 对照当前 Settings 阈值逐块标记 |
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

