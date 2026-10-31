# 工单 21 设计：令牌桶分级限流（memory 实现 + 两入口接线）

> 版本 v1（评审基线）｜依赖：工单 19（协议面与 StateBundle 装配已就位；PG 桶实现随 19
> 的 state_counters scope='bucket' 预留列落地，本工单只做 memory）
> 依据：商用就绪阶段三评审；三层限流架构见 §6

## 1. 现状锚点（已核实）
- 现有限流：SlidingWindowLimiter（登录 60s/10 次，auth.py:102）+ DailyCounter（日配额
  200 问/人，config.py:36；agent 与 chat 共享同实例 main.py:193）。无秒级平滑限速、无分档。
- 两接线点：chat.py:139-141（`deps.counter.try_consume` → 429/3002 未开流普通 JSON）；
  agent_runs.py:85-90（并发上限 429 在前、日配额 429 在后）。
- ChatDeps 字段即契约（turn_plan.py:45-58）；AgentDeps 共享 counter（main.py:188-194）。
- 错误码：ERR_RATE_LIMITED 已存在（app/core/errors，auth.py:24 import，登录限流 429 在用）；
  ApiError 支持 data 载荷（auth.py:139 lockedUntil 先例）。

## 2. 协议与 memory 实现
### 2.1 RateBucketProto（放 app/services/chat_cache.py，与三协议同层）
```python
class RateBucketProto(Protocol):
    def try_acquire(self, scope: str, key: str, rate_per_min: int, burst: int) -> bool: ...
    def reset(self) -> None: ...
```
scope：入口域（"chat"/"agent"）；key：用户 id 文本。参数由调用方按 Settings 分档传入——
桶实现不读配置（协议纯函数化，等价性测试无需改配置）。

### 2.2 TokenBucketLimiter（chat_cache.py 追加，≈45 行）
```python
class TokenBucketLimiter:
    """令牌桶平滑限速：{(scope,key): (水位令牌, 上次补充时刻)}，懒补充。

    线程纪律与同模块三组件一致（threading.Lock 护内存结构，无 IO）；时间经可注入
    Clock（测试 FakeClock 精确推演），生产 SystemClock。拒绝不扣水位（只补不耗）。
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, str], tuple[float, datetime]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, scope, key, rate_per_min, burst) -> bool:
        now = (self._clock if self._clock is not None else get_clock()).now()
        with self._lock:
            tokens, last = self._buckets.get((scope, key), (float(burst), now))
            elapsed = (now - last).total_seconds()
            if elapsed > 0:
                tokens = min(float(burst), tokens + elapsed * rate_per_min / 60.0)
            if tokens < 1.0:
                self._buckets[(scope, key)] = (tokens, now)   # 拒绝：只推进 last（防时钟回拨重补）
                return False
            self._buckets[(scope, key)] = (tokens - 1.0, now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
```
- 懒补充算法：水位 + 上次补充时刻，按注入 clock.now() 差值线性补充，封顶 burst；首见键
  满水位放行（突发额度即 burst）。
- 每次调用（含拒绝）都推进 last：数学上与"固定原点"等价（tokens 已含截至 last 的补充），
  推进可防 elapsed 累积漂移与浮点回算误差，实现最简。
- 进程重启窗口清空（同 SlidingWindowLimiter 语义，§5.4 已登记口径）。

## 3. 与工单 19 协议合流
- ChatDeps 追加字段（turn_plan.py:58 后）：`bucket: RateBucketProto | None = None`——
  默认 None：既有 kwargs 构造（测试/eval_engine.replace）零改动；开关关时装配 None 亦可。
- AgentDeps 追加 `bucket: RateBucketProto | None = None`，装配取共享实例
  （main.py:188-194 处 `bucket=chat_deps.bucket`，counter 共享先例同款）。
- eval_chat_deps（eval_engine.py:61-63）继承 bucket——评测路径不经 API 接线，无影响。
- PG 实现（随 19 预留，不在本工单实施）：state_counters scope='bucket'，ckey=
  {scope}:{user_id}，n=水位（x1000 定点），refill_at=上次补充；try_acquire 单语句
  `INSERT ... ON CONFLICT (scope,ckey) DO UPDATE SET n=..., refill_at=... WHERE <水位够>
  RETURNING n`（与 19 §5.2 同款裁决）。19 的迁移 0020 已含 refill_at 列，落地零迁移。
- 装配：_build_state（19 §6）memory 分支补 `bucket=TokenBucketLimiter(clock)`，StateBundle
  加字段；PG 分支留 TODO 注释（本工单结束前 PG 桶未实现——scale 拓扑 + 桶同时启用需
  PG 桶，缺省 rate_limiter_enabled=false 不阻塞）。

## 4. 分级配置与两处接线
### 4.1 Settings 四键 + 总闸（config.py，全部代码缺省、不进 .env.example——纯内部阈值，
### 同 agent_model_* 口径，调整走评审）
```python
rate_limiter_enabled: bool = False   # 总闸缺省关=行为与现状一致（灰度由部署决策）
rate_chat_per_min: int = 30          # 问答平滑速率（次/分钟/用户）
rate_chat_burst: int = 10            # 问答突发容量（令牌桶深度）
rate_agent_per_min: int = 10         # agent 平滑速率
rate_agent_burst: int = 4            # agent 突发容量（长任务入口更紧）
```
### 4.2 接线一：app/api/chat.py（插在 :139 取 deps 之后、:140 日配额之前）
```python
if deps.settings.rate_limiter_enabled and deps.bucket is not None and not deps.bucket.try_acquire(
    "chat", str(user.id), deps.settings.rate_chat_per_min, deps.settings.rate_chat_burst
):
    raise ApiError(ERR_RATE_LIMITED, _MSG_RATE_LIMITED, 429, data={"retryAfterSec": _retry_after(deps.settings.rate_chat_per_min)})
```
`_retry_after(rate) = max(1, ceil(60 / rate))`——补足一个令牌的最小时长，作保守提示；
**协议保持 bool 纯函数**（不为此膨胀返回结构），retryAfterSec 是配置推导值而非精确水位
倒推（裁决 §7.3）。`_MSG_RATE_LIMITED = "操作过于频繁，请稍后重试"`。
### 4.3 接线二：app/api/agent_runs.py（插在 :88 并发上限之后、:89 日配额之前）
同款，scope="agent"、读 settings.rate_agent_*；`deps` 为 AgentDeps（bucket 字段 §3）。
次序裁决：并发检查（读查询、零成本）→ 桶 → 日配额，被拒请求均未落占位行、未烧额度
（agent_runs.py:86 既有注释口径延续）。

## 5. 测试清单（先红）
### 5.1 单元 tests/unit/test_token_bucket.py（FakeClock，全中文标题）
- "突发容量内连发全放行"：burst=3 首见键连发 3 次 True
- "超突发即拒且拒绝不扣水位"：第 4 次 False；前拨恰补 1 令牌时长后恰 1 次 True
- "懒补充速率精确"：rate=60/min（1/s）耗尽后每前拨 1s 恰放行 1 次；前拨 0.5s 放行为 False
- "水位封顶 burst 不超储"：闲置远超 burst 时长后仍至多连发 burst 次
- "scope 隔离"：chat 耗尽不影响 agent 同 key 首发放行；同 scope 不同 user 隔离
- "reset 清空后恢复满水位"
- "并发恰放行 burst 次"（Barrier + tiny_switch_interval，同 test_chat_cache.py 竞态先例）
- "关闭总闸装配 None 时接线短路"：deps.bucket=None + enabled=True 不抛（字段缺省兜底）
### 5.2 API tests/api/test_chat_rate_limit.py
- "桶拒绝返回未开流 429/ERR_RATE_LIMITED 且 data.retryAfterSec>=1"（chat_client SSE 前置 JSON）
- "桶拒绝不烧日配额"：拒绝后 DailyCounter 未计数（memory 实例 _usage 断言，memory 专属组）
- "agent 入口同款 429 + retryAfterSec"（test_agent_runs.py 追加）
- "总闸关闭行为不变"：enabled=false 同负载不 429、日配额仍生效（回归）

## 6. 三层限流叠加关系（明示架构）
```
第 1 层 登录滑窗 SlidingWindowLimiter（auth.py:102）：IP+用户名，60s/10 次——防撞库
第 2 层 令牌桶 TokenBucketLimiter（本工单）：用户×入口，秒级平滑——防脚本化高频
第 3 层 日配额 DailyCounter（chat_cache.py:117）：用户全局 200 问/日——防总量滥用
```
- 三层**独立状态、独立判定**，请求被任一层拒即 429 终止；层间无回写：
  桶拒绝不烧日配额（§5.2 断言钉死）、日配额拒绝不消耗桶、登录滑窗与后两层无耦合。
- 错误码分档：滑窗/桶=ERR_RATE_LIMITED（429，"操作频繁"类文案）；日配额=ERR_DAILY_LIMITED
  （429/3002，"今日额度"文案）——用户可区分"稍后再试"与"明天再来"。
- 顺序：chat 入口=桶→日配额（chat.py:140 前）；agent 入口=并发→桶→日配额。

## 7. 裁决点
1. **令牌桶而非漏桶**：漏桶=恒定速率排队（需队列/延迟语义），令牌桶=速率+突发容量，
   与"burst 放行、平滑补充"的产品诉求同构且实现是无队列纯函数；工单票面"漏桶式
   （令牌桶）"两者混称，本设计钉死令牌桶语义。
2. **桶参数进协议签名而非实现读配置**：try_acquire 纯函数化后，memory/PG 等价性测试
   无需动 Settings，且分档逻辑（哪层入口用哪档）收敛在接线处可评审。
3. **retryAfterSec 用配置推导而非协议返回**：bool 协议最简（PG 单语句 RETURNING 恰好
   匹配 bool）；精确 retry-after 需返回剩余秒数，等 PG 桶实现时一并评估（触发线）。
4. **总闸缺省关**：与 eval_auto_enabled 同款灰度口径（config.py:100-103 先例），缺省行为
   与现状逐字节一致，部署稳定后再开。
5. **AgentDeps 也加 bucket 字段**：与 counter 共享先例（main.py:193）同构——共享同一桶
   实例保证同用户跨入口的水位一致视图；避免 agent_runs 反向取 chat_deps 的跨依赖包耦合。

## 8. 文档同步位
- docs/api.md：chat/stream 与 agent/runs 的 429 说明补"限流 429（ERR_RATE_LIMITED）+
  retryAfterSec 载荷"一行（无端点增减，标记行不动）。
- docs/user-guide.md 与 frontend/src/help/：问答/agent 常见问题补"操作过于频繁"文案含义
  （界面原文同步义务，AGENTS.md 红线 7；前端展示后端 message，无组件改动）。
- docs/deploy-requirements.md §7：开启限流后的容量口径（30/min/用户）与关闭缺省说明。
- .env.example：不加键（§4.1 口径），指针写在 config.py 注释。
- AGENTS.md"当前状态"节：收口时更新。

## 9. 评审修订（2026-10-31，测试工程师 QC R7）

- **R7 agent 入口对称断言（补入 §5.2）**：补用例"agent 桶拒绝不烧日配额且不落占位行"——断言拒绝后 agent_runs 表无新行、DailyCounter 未计数（与 chat 侧 §5.2 第 2 条对称）；同时钉死次序：并发闸→桶→日配额，桶拒绝发生在并发检查之后（429 归因明确）。
- 工单票已补登（issues/21-rate-bucket-limiter.md，2026-10-31 PM 评审发现缺票；本设计即对照物）。
