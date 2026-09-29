# RagDemo 接口文档（W1 + W2）

> 依据 `docs/standards.md` §3（API 接口规范）编写，随代码演进（§19.1 文档随代码）。
> 覆盖 `app/api/` 当前全部路由：auth 4 个、documents 4 个、chat 1 个、conversations 4 个、messages（feedback）1 个，共 14 个端点。
> 最后更新：W2 收口（Task 12）。

## 1. 通用约定

### 1.1 鉴权

- 除 `POST /api/auth/login` 外全部接口需要登录：请求须携带登录时下发的 `jwt` Cookie
  （httpOnly、SameSite=Lax，有效期默认 8 小时）。
- 权限门两级：
  - **ReadyUser**（已登录且已完成首登改密）：问答 / 会话 / 反馈全部端点；
  - **AdminUser**（admin 角色，链在改密门之后）：文档管理全部端点。
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
| 3003 | 404 | 会话/消息不存在或无权访问（归属校验 IDOR 防线，§21.8；不区分"不存在"与"无权"） | chat / conversations / feedback |
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
| 请求体 | 无（title 走库默认"新会话"；仅 conversationId 为空（问答端点自建会话）时以首问前 20 字命题，复用已有会话时标题保持不变（"新会话"）） |
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
| 成功响应 | `200`，`data: {id, feedback, feedbackCategory, feedbackComment, badcaseStatus}`。down 落 badcase_status="待处理" 进治理队列（F-702）；改评语义：down→up 清空备注/类型但 badcase_status 不回退，重复 down 幂等更新为最新值 |
| 错误分支 | `422/1003` 备注长度非法（field=comment）/ 类型不在枚举（field=category）；`404/3003` 消息不存在或无权访问（不存在 / 他人消息 / 非 assistant / 中断占位行同判）；`401/2003`；`403/2004` |
