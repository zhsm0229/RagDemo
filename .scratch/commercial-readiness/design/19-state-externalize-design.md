# 工单 19 设计：进程内状态外置（协议化 + PG 可插拔实现）

> 版本 v1（评审基线）｜依赖：无｜下游：工单 20（多副本）、工单 21（令牌桶协议合流）
> 依据：.scratch/commercial-readiness/issues/19-state-externalize.md；六接口哲学的第七替换点

## 1. 现状锚点（已核实）

| 组件 | 现实现 | 公开方法 | 装配点 |
|---|---|---|---|
| ExactCache | app/services/chat_cache.py:32 | make_key(46)/get(60)/put(74)/clear_by_ref(80)/clear(105) | main.py:109 |
| DailyCounter | chat_cache.py:117 | try_consume(129) | main.py:110；agent 与 chat 共享（main.py:193） |
| HitCounter | chat_cache.py:146 | add(161)/flush(167)（快照扣减回收 200-206） | main.py:111；scheduler.py:257 注入 |
| SlidingWindowLimiter | app/services/auth.py:70 | hit(84)/reset(96)；模块级 default_limiter(102) | auth.login 内缺省解析(129-131) |
| JtiBlacklist | app/core/security.py:38 | revoke(53)/revoked(61)/reset(68)；default_blacklist(74) | decode_token 缺省解析(137-139) |

- ChatDeps 字段即契约：turn_plan.py:53-55 为具体类注解；eval_chat_deps（eval_engine.py:61-63）
  `replace(cache=新, counter=None)`，hit_counter 共享。
- flush 契约被 tests/unit/test_chat_cache.py:233-269 替身钉死；memory 内部字段被直接断言
  （:153/176/203/211 的 `_entries/_usage`）——只对 memory 实现保留。
- 测试隔离靠 reset()：tests/api/conftest.py:75-76、102。PG 无 on_conflict 先例（本工单首例）。

## 2. 裁决总览（详见 §8）

1. 协议与实现同层放置：五个 Protocol 就近放在现实现模块（不建独立 state_proto.py）——
   TurnPlan/ChatDeps 先例（turn_plan.py），core 层协议留 core（JtiBlacklistProto）。
2. ChatDeps 三字段注解改 Protocol；新增 `bucket` 字段归工单 21，本工单不动 ChatDeps 形状。
3. 后端开关 `Settings.state_backend: Literal["memory","pg"] = "memory"`（不用工单票面的
   SINGLE_INSTANCE 布尔：三值枚举为 A 阶段 Redis 留位）；`test_mode` 恒 memory。
4. PG 实现自建短会话（session_factory=SessionLocal 缺省），不借请求会话——调度任务体先例
   （scheduler.py:251）。
5. 装配收口 main._build_state → StateBundle，auth/security 模块级单例保留为 memory 缺省。

## 3. 协议面定义（方法签名与现实现逐字一致）

### 3.1 ChatCacheProto → 放 app/services/chat_cache.py（ExactCache 之前）
```python
class ChatCacheProto(Protocol):
    def make_key(self, dept_id: int, question_normalized: str,
                 user_suffix: str | None = None) -> str: ...
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, entry: dict) -> None: ...
    def clear_by_ref(self, ref_type: str, ref_id: int) -> int: ...
    def clear(self) -> None: ...
```
调用面核对：make_key（services/chat.py:124）、get/put（chat_stream）、clear_by_ref/clear
（admin_qa_pairs.py:150/163/178、document_admin.py:189/200/210/229、document_rebuild.py:53/69）。

### 3.2 DailyCounterProto → chat_cache.py（DailyCounter 之前）
```python
class DailyCounterProto(Protocol):
    def try_consume(self, user_id: int, limit: int) -> bool: ...
```

### 3.3 HitCounterProto → chat_cache.py（HitCounter 之前）
```python
class HitCounterProto(Protocol):
    def add(self, chunk_ids: list[int]) -> None: ...
    def flush(self, db: Session) -> int: ...
```

### 3.4 RateLimiterProto → app/services/auth.py（SlidingWindowLimiter 之前）
```python
class RateLimiterProto(Protocol):
    def hit(self, key: str) -> bool: ...
    def reset(self) -> None: ...
```
login 形参注解同步改为 `limiter: RateLimiterProto | None = None`（auth.py:115）。

### 3.5 JtiBlacklistProto → app/core/security.py（JtiBlacklist 之前）
```python
class JtiBlacklistProto(Protocol):
    def revoke(self, jti: str, exp: float) -> None: ...
    def revoked(self, jti: str) -> bool: ...
    def reset(self) -> None: ...
```
decode_token 形参注解同步（security.py:110）。五层纪律：core 的协议由 services 实现是向下
依赖，合法；services 的协议不进 core。

### 3.6 ChatDeps 注解改动（turn_plan.py:53-55）
```python
cache: ChatCacheProto
counter: DailyCounterProto
hit_counter: HitCounterProto
```
dataclass 无运行时类型检查，行为零变化；eval_engine.replace 与各测试 kwargs 构造不受影响。
import 行（turn_plan.py:14）保留具体类导入（memory 即缺省实现），另加 `from
app.services.chat_cache import ChatCacheProto, DailyCounterProto, HitCounterProto`。

## 4. PG 表设计（迁移 0020 草案；现 head=0018，0019 预留给并行父子块车道，
### 合入时以 alembic/versions 实际 head+1 为准，本稿按 0020 撰写）

三张表均为**易失状态、非业务实体**：无 is_active 软删（软删对缓存/计数无意义，偏差记录在
迁移注释）；每表每列带中文 COMMENT（standards 红线）。

```sql
-- ① 问答精确缓存（PgExactCache）
CREATE TABLE state_cache_entries (
  id        BIGSERIAL PRIMARY KEY,
  cache_key TEXT NOT NULL UNIQUE,      -- chat:{dept}[:u{uid}]:{question}，与 make_key 同构
  dept_id   BIGINT,                    -- 自键前缀解析，仅运维索引用，解析失败 NULL
  payload   JSONB NOT NULL,            -- 缓存条目（answer/citations/answerType 同内存版）
  expire_at TIMESTAMPTZ NOT NULL,      -- 过期时刻 = 装配时钟 now + ttl
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_state_cache_expire ON state_cache_entries (expire_at);

-- ② 通用计数器（PgDailyCounter / PgHitCounter / 工单 21 令牌桶预留）
CREATE TABLE state_counters (
  id      BIGSERIAL PRIMARY KEY,
  scope   TEXT NOT NULL,   -- daily=日配额 / hit=召回计数暂存 / bucket=令牌桶（21 预留）
  ckey    TEXT NOT NULL,   -- daily=user_id 文本 / hit=chunk_id 文本 / bucket={entry}:{user_id}
  n       BIGINT NOT NULL DEFAULT 0,  -- daily=当日已用 / hit=待刷增量 / bucket=剩余令牌(x1000 定点)
  day     DATE,            -- daily 专用：UTC 归属日；hit/bucket 为 NULL
  refill_at TIMESTAMPTZ,   -- bucket 专用：上次补充时刻（21 落地零迁移）；daily/hit 为 NULL
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (scope, ckey)
);

-- ③ 登录限流 + 登出黑名单（PgSlidingWindowLimiter / PgJtiBlacklist）
CREATE TABLE state_auth_guards (
  id        BIGSERIAL PRIMARY KEY,
  guard_type TEXT NOT NULL,  -- limiter=登录滑动窗口 / jti=登出黑名单
  guard_key  TEXT NOT NULL,  -- limiter="{ip}|{username}" / jti=令牌 jti
  event_at   TIMESTAMPTZ NOT NULL,  -- limiter=事件时刻 / jti=撤销时刻
  expire_at  TIMESTAMPTZ,    -- jti 专用=原令牌 exp；limiter 为 NULL
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (guard_type, guard_key, event_at)
);
CREATE INDEX idx_state_auth_expire ON state_auth_guards (expire_at) WHERE expire_at IS NOT NULL;
```
迁移 0020 要点：upgrade 建三表+COMMENT+索引；downgrade DROP 三表（可回滚）；仅追加不改历史。

## 5. 五个 PG 实现（app/services/state_pg.py 三件 + app/services/state_pg_auth.py 两件；
### 拆两模块防单文件触 300 行红线）

构造统一：`__init__(self, clock: Clock, session_factory=SessionLocal)`（cache 另收 ttl_s）。
所有时刻/日期用**注入时钟**算好再绑定（不用 SQL now()），FakeClock 等价性测试才可精确对齐。

### 5.1 PgExactCache
- get：`SELECT payload, expire_at FROM state_cache_entries WHERE cache_key=:key` →
  expire_at > now 返回 payload（JSON 解析即天然深拷贝）；已过期则执行
  `DELETE ... WHERE cache_key=:key AND expire_at<=:now`（惰性清除，对齐 chat_cache.py:67）。
- put（upsert）：`INSERT ... (cache_key,dept_id,payload,expire_at) VALUES (...) ON CONFLICT
  (cache_key) DO UPDATE SET payload=EXCLUDED.payload, expire_at=EXCLUDED.expire_at,
  updated_at=EXCLUDED.updated_at`；dept_id 由键前缀 `chat:{dept}:` 解析，失败置 NULL。
- clear：`DELETE FROM state_cache_entries`。
- clear_by_ref（对齐 _touches 的保守删除语义，chat_cache.py:94-103）：
```sql
DELETE FROM state_cache_entries WHERE
  payload->'citations' IS NULL OR jsonb_typeof(payload->'citations') <> 'array'
  OR jsonb_array_length(payload->'citations') = 0
  OR EXISTS (SELECT 1 FROM jsonb_array_elements(payload->'citations') c
             WHERE c->>'refType' = :ref_type AND c->>'refId' = :ref_id_text);
```
  refId 用文本比较（不 cast int）：畸形数据不抛错、走保守删除，语义与内存版一致。返回 rowcount。
- 容量：TTL 惰性清除 + get 顺手删过期键；无后台回收（与内存版同口径）。

### 5.2 PgDailyCounter（try_consume 裁决：单语句 ON CONFLICT ... WHERE ... RETURNING）
```sql
INSERT INTO state_counters (scope, ckey, n, day, updated_at)
VALUES ('daily', :user_id, 1, :today, :now)
ON CONFLICT (scope, ckey) DO UPDATE SET
    n = CASE WHEN state_counters.day IS DISTINCT FROM EXCLUDED.day THEN 1
             ELSE state_counters.n + 1 END,
    day = EXCLUDED.day, updated_at = EXCLUDED.updated_at
WHERE state_counters.day IS DISTINCT FROM EXCLUDED.day OR state_counters.n < :limit
RETURNING n;
```
有返回行 → True；空 → False（拒绝零写入，对齐"拒绝不计数" chat_cache.py:129-143）。
**理由**：单语句在 PK 行锁下天然原子（READ COMMITTED 足够），免 FOR UPDATE 的先读后写
两次往返，免 CTE 双语句的事务包装；跨日重置、达限拒写、恰限放行三语义全部收进一句。
对照否决：FOR UPDATE 版需 SELECT+UPDATE 两语句同事务（多一次往返+事务边界管理）；
CTE 版同语句快照在并发 INSERT 分支有 ON CONFLICT 竞态注释负担，收益不抵复杂度。

### 5.3 PgHitCounter（暂存-搬运两段式，聚合不丢）
- add：应用内先合并同 id（对齐内存版），再一条 upsert executemany：
  `INSERT (scope,ckey,n) VALUES ('hit',:cid,:k) ON CONFLICT (scope,ckey)
  DO UPDATE SET n=state_counters.n+EXCLUDED.n, updated_at=EXCLUDED.updated_at`；
  内部短会话自行提交（add 时机在 finalize 落库之后，与请求事务无纠缠）。
- flush(db)（签名不变，走传入会话）：
```sql
-- ① 锁定并快照全部暂存行（空则 return 0）
SELECT ckey, n FROM state_counters WHERE scope='hit' ORDER BY ckey FOR UPDATE;
-- ② 一条 UPDATE 搬运进 chunks（与内存版同一语句形态，chat_cache.py:184-193）
UPDATE chunks SET hit_count = hit_count + bindparam n, last_hit_at = :now
  WHERE id = bindparam chunk_id   -- executemany 按 id 升序
-- ③ 删除已消费暂存行（仅快照键）
DELETE FROM state_counters WHERE scope='hit' AND ckey = ANY(:keys);
-- db.commit()
```
  **不丢/滚入论证**：①的 FOR UPDATE 行锁使并发 add 的 upsert 阻塞到本事务提交；提交后其
  ON CONFLICT 目标行已被 ③ 删除 → 按新行插入，增量保留（PG 对"冲突行已被并发删除"按普通
  INSERT 继续）；②③同事务，任一失败整体回滚、快照未消费 → 滚入下一轮（对齐
  run_hit_flush 的吞异常重试，scheduler.py:243-254）。消失 chunk 的暂存行：②不命中、
  ③照删——与内存版"UPDATE executemany 静默跳过缺失 id 后照常回收"语义一致。
  偏差记录：PG 版空 flush 发一条 SELECT（内存版零 SQL），契约断言按实现分组（§7）。

### 5.4 PgSlidingWindowLimiter（hit 三语句事务 + 每键咨询锁）
```sql
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('limiter:' || :key));        -- 锁语句
DELETE FROM state_auth_guards WHERE guard_type='limiter'           -- ① 窗外清理（全局顺手扫，容量有界）
  AND event_at <= :floor;
SELECT count(*) FROM state_auth_guards                             -- ② 窗口计数
  WHERE guard_type='limiter' AND guard_key=:key AND event_at > :floor;
-- ② >= max_events → ROLLBACK return False（拒绝不落事件，对齐 auth.py:88-94）
INSERT INTO state_auth_guards (guard_type, guard_key, event_at)    -- ③ 仅放行时
VALUES ('limiter', :key, :now);
COMMIT;  -- return True
```
**理由**：READ COMMITTED 下②③之间并发插入会超放（check-then-act），per-key 事务级咨询锁
把同键串行化，行为与内存版逐字等价；登录窗口小（60s/10 次，auth.py:102）锁竞争可忽略。
否决 SERIALIZABLE（重试复杂度不成比例）。reset()：`DELETE WHERE guard_type='limiter'`。

### 5.5 PgJtiBlacklist
- revoke：两句同事务——`DELETE WHERE guard_type='jti' AND expire_at<=:now`（顺手过期治理，
  对齐 security.py:57）+ `INSERT (guard_type,guard_key,event_at,expire_at)
  VALUES ('jti',:jti,:now,:exp)`；exp<=now 直接跳过 INSERT（对齐 :58）。同 jti 重复撤销产生
  多行无害（uuid4 不复用），revoked 取"存在未过期行"。
- revoked：`SELECT EXISTS (SELECT 1 ... WHERE guard_type='jti' AND guard_key=:jti
  AND expire_at > :now)`。
- reset：`DELETE WHERE guard_type='jti'`。

## 6. 装配工厂（main.py 装配层收口）

```python
@dataclass
class StateBundle:            # main.py 新增（装配层内部载体）
    cache: ChatCacheProto
    counter: DailyCounterProto
    hit_counter: HitCounterProto
    limiter: RateLimiterProto
    blacklist: JtiBlacklistProto

def _build_state(settings: Settings) -> StateBundle:
    backend = "memory" if settings.test_mode else settings.state_backend
    clock = SystemClock()      # 进程级单例口径不变（main.py:87 注释）
    if backend == "pg":
        from app.services.state_pg import PgDailyCounter, PgExactCache, PgHitCounter
        from app.services.state_pg_auth import PgJtiBlacklist, PgSlidingWindowLimiter
        return StateBundle(PgExactCache(clock, settings.cache_ttl_s), PgDailyCounter(clock),
                           PgHitCounter(clock), PgSlidingWindowLimiter(60, 10, clock),
                           PgJtiBlacklist(clock))
    return StateBundle(ExactCache(clock, settings.cache_ttl_s), DailyCounter(clock),
                       HitCounter(clock), default_limiter, default_blacklist)

def _build_chat_deps(settings: Settings, state: StateBundle | None = None) -> ChatDeps:
    state = state or _build_state(settings)   # 缺省自建：scheduler.py:347 与 catchup 调用点零改动
```
lifespan（main.py:203-207）：`state = _build_state(settings)` → `app.state.state_bundle = state`
→ `_build_chat_deps(settings, state)`。接线三处显式传参（消除缺省解析的隐式全局）：
- app/api/auth.py:46 `login(..., limiter=request.app.state.state_bundle.limiter)`
- app/api/auth.py:65 `logout(token, blacklist=request.app.state.state_bundle.blacklist)`
- app/api/deps.py:44 `decode_token(token, secret, blacklist=request.app.state.state_bundle.blacklist)`

auth.py:102 default_limiter 与 security.py:74 default_blacklist **原样保留**（memory 缺省 +
测试 reset() 契约不变，conftest.py:75-76 零改动）；生产路由恒传 bundle 实例，模块级单例
在 prod 不再被触达。Settings 增：`state_backend: Literal["memory","pg"] = "memory"`。

## 7. 测试清单（先红后绿）

### 7.1 等价性参数化（tests/integration/test_state_pg_equivalence.py，真库 rag_test，
fixture 每Case先 TRUNCATE 三表——PG 实现自带 commit，回滚式 session 隔离不适用）
参数化 `backend in ("memory","pg")`，用例（中文标题）：
- make_key 四形态与内存版逐字节一致（键生成与后端无关，仅钉协议）
- put 后 get 读回 entry 全字段相等
- TTL 过期 get 返回 None 且行被惰性删除（读回断言 SELECT 计数）
- get 返回对象改写不污染缓存（再 get 读回原文）
- clear 整清后再 put 可用；clear_by_ref 四分支（命中删/多引用任一命中删/无 citations 保守删/
  无命中返回 0）
- try_consume 恰限放行、超限拒且不计数（读回 n）、跨日重置、按 user 隔离
- add 合并同 id；flush 读回 hit_count/last_hit_at（真 pg 读回）；flush 返回去重数；失败滚入
  （execute 抛错后聚合仍在：再 flush 带全量）
- limiter hit 窗口内恰 max 次放行、超窗重置、reset 清空
- jti revoke 后 revoked True、过期后 False、revoke 顺手清过期行、reset 清空
### 7.2 并发语义（tests/integration/test_state_pg_concurrency.py）
- 两 session 同 user 并发 try_consume 恰放行 limit 次（线程池 + 真库）
- flush 与 add 并发：commit 窗口内落下的 add 计数不丢（确定性：flush 会话内 execute 前后
  由另一连接 add，最终 hit_count 读回 == 总和）
### 7.3 装配测试（tests/unit/test_state_assembly.py，不触库）
- 缺省（memory）→ StateBundle 五件均 memory 类；state_backend="pg" → 五件均 Pg* 类
- test_mode=True 强制 memory（env 注 pg 也 memory）
- _build_chat_deps(settings, state) 复用传入 state（字段 is 同一实例断言）
- 路由接线：login/logout/get_current_user 走 bundle 实例（monkeypatch bundle.limiter 断言触达）
### 7.4 既有测试保护面（不动清单）
- tests/unit/test_chat_cache.py、test_auth_policy.py、test_jti_blacklist.py 原样保留（钉 memory
  契约与内部字段）；文件头补一行"memory 实现专属契约测试"注释
- tests/api/conftest.py:75-76/102 的 reset 逻辑不动（test_mode 恒 memory）
- tests/integration/test_hit_counter_flush.py 不动（内存 HitCounter+真库的既有读回）

## 8. 裁决点（拍板与理由）
1. **开关用 state_backend 枚举而非 SINGLE_INSTANCE 布尔**：三值枚举为 A 阶段 Redis 留位，
   避免 boolean 语义锁死（standards §25.4 Redis 禁提前建，此处只留演进位）。
2. **协议就近放实现模块，不建 state_proto.py**：协议面与缺省实现同文件，改动集中可评审；
   JtiBlacklistProto 必须在 core（core 不能反向 import services）。
3. **test_mode 恒 memory**：全套 reset()/FakeClock 测试基建建立在进程内语义上（conftest.py
   :75-76），PG 后端在 CI 引入跨用例残留风险，收益为零。
4. **DailyCounter 用 ON CONFLICT...RETURNING 单语句**：见 §5.2，原子性靠 PK 行锁，无额外
   事务编排，是三案中唯一"拒绝零写入"免费达成的写法。
5. **HitCounter 走"PG 暂存 + flush 搬运"而非 add 直写 chunks**：直写会把每轮问答的 N 条
   chunk 增量变成 N 次 chunks 行热写（行锁竞争集中在高频 chunk），暂存表把写压力摊散且
   flush 两语句保崩溃安全；内存版的"commit 窗口并发 add"契约由行锁+删后重插严格继承。
6. **auth/security 走 StateBundle 显式传参而非 getter 惰性单例**：装配唯一点仍是 main.py
   （standards §1.2），模块级单例保 reset() 契约、测试零改动。

## 9. 文档同步位
- docs/deploy-requirements.md §7 条目 1/2（:161-162）：state_backend=pg 的边界改写（工单 20
  收口时一并终稿，本工单先补"进程内状态可外置"事实）
- .env.example：STATE_BACKEND 键（含义+部署可选，缺省 memory）
- docs/api.md：无端点增减（标记行不动）；docs/runbook.md：备份范围补 state_* 三表随卷备份说明
- AGENTS.md"当前状态"节：工单收口时更新

## 10. 评审修订（2026-10-31，测试工程师 QC R1-R3）

- **R1 跨实例共享语义用例（补入 §7.2）**：新增"两个 StateBundle 实例（memory 两份 vs pg 两份）写读互见"用例——pg 两实例同 key put/get 互见、memory 两实例互不可见（钉死外置核心价值：共享语义随后端变化，20 多副本的前提）；memory 侧断言"互不可见"防误把 memory 当全局。
- **R2 clear_by_ref 异型 refId 钉死（补入 §7.1）**：refId 统一按 `c->>'refId'` 文本比较——补充用例钉死"put 条目 refId 为 int 7、按 7 失效命中；put 为字符串 '7'、按 7 同样命中；按 9 不命中"——JSONB 文本比较下 int/str 归一，两侧（memory 的 == 比较 int 7 vs '7' 不命中！）行为**发散**：裁决=memory 版 `_touches` 补 `str(c.get("refId")) == str(ref_id)` 归一，两侧一致（等价性测试覆盖）。
- **R3 flush/add 并发确定性（补入 §7.2）**：指名手段=两个独立 engine（各自 NullPool）+ threading.Barrier 对齐时序：flush 会话 FOR UPDATE 持锁窗口内由另一 engine add（确定性插队在 commit 前），commit 后读回 hit_count == 两段增量总和；复用 test_chat_cache.py:362-384 的"commit 窗口 add"契约语义，换真库实证。
