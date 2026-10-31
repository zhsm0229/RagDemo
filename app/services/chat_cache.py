r"""问答进程内三件套（Task 7，Task 8 编排直接消费）：精确缓存 / 每日限额 / 召回计数聚合。

- ExactCache（F-205/§17.5）：键 ``chat:{dept_id}:{question_normalized}`` →
  (entry, expire_at)。仅进程内、精确匹配——语义缓存（相似度命中）属禁建清单
  （standards §25.4）；过期惰性清除（get 时删，无后台回收线程）；拒答结果是否
  入缓存由调用方（Task 8）裁决，本类不审查 entry 内容；get 返回深拷贝，调用方
  原地改写不污染缓存。多 worker 部署各进程各持一份、不做跨进程同步（W4 部署
  关切，与 build_scheduler 单实例语义同口径）。
- DailyCounter（每日提问限额）：{user_id: (当日, 已用次数)}，跨日以
  clock.now().date() 比较（UTC 日期口径，北京时区为已登记遗留）；try_consume
  满 limit 拒绝且不计数。
- HitCounter（F-1011）：召回命中 chunk_id 进程内聚合；flush 一条 UPDATE
  （executemany 参数绑定、单事务）原子累加 hit_count 并统一置 last_hit_at；
  聚合仅在提交成功后回收，失败轮次滚入下一周期（scheduler.run_hit_flush
  logger.exception 吞掉后自然重试）。
- TokenBucketLimiter（工单 21）：用户×入口的令牌桶平滑限速（三层限流第 2 层），
  懒补充、拒绝不扣水位；pg 实现随工单 19 预留列落地（本工单只做 memory）。
- 线程安全：各组件均为进程内单例，被 SSE 并发请求线程 + APScheduler flush 线程
  结构性跨线程触达，与 auth.SlidingWindowLimiter 同先例以 threading.Lock 保护
  （FastAPI 线程池并发）；锁只护内存结构，DB 提交不持锁。
"""

import copy
import threading
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import Integer, bindparam, update
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.db.models import Chunk


class ChatCacheProto(Protocol):
    """问答精确缓存协议面（工单 19）：方法签名与 ExactCache 逐字一致，装配层据
    Settings.state_backend 选择 memory/pg 实现（state_pg.PgExactCache），编排层
    （chat/eval/admin）只依赖本协议，不感知后端形态。"""

    def make_key(
        self, dept_id: int, question_normalized: str, user_suffix: str | None = None
    ) -> str: ...

    def get(self, key: str) -> dict | None: ...

    def put(self, key: str, entry: dict) -> None: ...

    def clear_by_ref(self, ref_type: str, ref_id: int) -> int: ...

    def clear(self) -> None: ...


class DailyCounterProto(Protocol):
    """每日限额协议面（工单 19）：try_consume 语义见 DailyCounter（恰限放行、
    超限拒绝且不计数、跨日重置、按 user 隔离），pg 实现由 state_pg.PgDailyCounter 提供。"""

    def try_consume(self, user_id: int, limit: int) -> bool: ...


class HitCounterProto(Protocol):
    """召回计数聚合协议面（工单 19）：add 攒批 / flush 刷库语义见 HitCounter
    （聚合不丢、失败滚入），pg 实现由 state_pg.PgHitCounter 提供。"""

    def add(self, chunk_ids: list[int]) -> None: ...

    def flush(self, db: Session) -> int: ...


class ExactCache:
    """进程内精确问答缓存：键 → (entry, 过期时刻)，TTL 到期惰性清除。

    线程安全（FastAPI 线程池并发问答）：put/get 共用一把粗锁——惰性清除路径若
    不加锁，双线程同时 del 同一键会抛未捕获 KeyError（请求侧 500，并发用例实测
    复现），简洁正确优先不做分桶细锁。
    """

    def __init__(self, clock: Clock, ttl_s: int) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._entries: dict[str, tuple[dict, datetime]] = {}
        self._lock = threading.Lock()

    def make_key(
        self, dept_id: int, question_normalized: str, user_suffix: str | None = None
    ) -> str:
        """缓存键（brief 规格）：部门进键，同问题不同部门绝不互串。

        user_suffix 非空（调用方传 "u{user_id}"）时插在部门与问题之间——长期记忆
        注入非空的回合（agent-evolution §3.4）：带不同画像的两人对同一问题不得互串
        答案；缺省 None 时键与历史形态逐字节相同（无记忆用户继续共享部门级缓存，
        注入面不影响缓存命中率）。键形态变化收敛在本方法一处。
        """
        if user_suffix:
            return f"chat:{dept_id}:{user_suffix}:{question_normalized}"
        return f"chat:{dept_id}:{question_normalized}"

    def get(self, key: str) -> dict | None:
        """命中返回 entry 深拷贝；过期返回 None 并惰性清除（无后台回收线程）。"""
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                return None
            entry, expire_at = found
            if self._clock.now() >= expire_at:  # 到期即失效（含恰等边界的秒级口径）
                del self._entries[key]
                return None
            # 深拷贝出借：Task 8 会原地加工 citations，浅引用挡不住嵌套列表改写；
            # 条目极小（≤10k 字回答 + ≤5 条引用），拷贝开销可忽略，粗锁内完成。
            return copy.deepcopy(entry)

    def put(self, key: str, entry: dict) -> None:
        """写入缓存条目，过期时刻 = 注入时钟 now + ttl（内容裁决在调用方 Task 8）。"""
        expire_at = self._clock.now() + timedelta(seconds=self._ttl_s)
        with self._lock:
            self._entries[key] = (entry, expire_at)

    def clear_by_ref(self, ref_type: str, ref_id: int) -> int:
        """按引用精准失效（工单 23）：条目 citations 任一元素 (refType, refId) 命中
        即删；citations 缺席/空的条目保守删除（fail-safe——宁可多失效不留旧答案）。
        返回删除条数。文档/QA 变更点按资源 id 调用，替代整清以保住无关条目命中率。"""
        with self._lock:
            stale = [
                key
                for key, (entry, _) in self._entries.items()
                if self._touches(entry, ref_type, ref_id)
            ]
            for key in stale:
                del self._entries[key]
            return len(stale)

    @staticmethod
    def _touches(entry: dict, ref_type: str, ref_id: int) -> bool:
        """条目是否引用目标资源；citations 形态漂移（缺席/空/非 list）按保守删除。

        refId 统一按文本比较（工单 19 修订 R2）：PG 侧 JSONB 经 c->>'refId' 文本
        归一（int 7 与 "7" 同命中），内存版同步 str() 归一——两后端失效语义一致，
        否则同一份缓存按后端不同而漏删/多删。
        """
        citations = entry.get("citations") if isinstance(entry, dict) else None
        if not isinstance(citations, list) or not citations:
            return True
        return any(
            isinstance(c, dict)
            and c.get("refType") == ref_type
            and str(c.get("refId")) == str(ref_id)
            for c in citations
        )

    def clear(self) -> None:
        """整清全部条目（W3 Task 1）：文档 retry/confirm 改变可检索事实，陈旧答案
        不能等 TTL 自然过期，成功响应前接线调用本方法（app/api/documents.py）。

        并发契约：与 put/get 共用同一把粗锁，锁内 ``_entries.clear()``——失效线程与
        并发问答线程交错时字典结构不被并发改写（类 docstring 的锁契约同样覆盖本方法，
        不做逐键精细删除的细锁）。清空不可逆且刻意从宽：宁可多一次生成，不留旧答案。
        """
        with self._lock:
            self._entries.clear()


class DailyCounter:
    """单用户每日提问限额：{user_id: (当日, 已用次数)}，跨日自动重置。

    线程安全（同一用户可并发提问）：try_consume 的 check-then-act 整体在锁内，
    同用户并发调用放行总数恰为 limit，不超限也不丢计数。
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._usage: dict[int, tuple[date, int]] = {}
        self._lock = threading.Lock()

    def try_consume(self, user_id: int, limit: int) -> bool:
        """第 N 次消费（N ≤ limit）允许并计数；第 limit+1 次拒绝且不计数。

        跨日重置以 clock.now().date() 与记录日比较（UTC 日期口径，已登记遗留）；
        拒绝路径不写回——不会因连续拒绝把已用次数越推越满。
        """
        today = self._clock.now().date()
        with self._lock:
            used_date, count = self._usage.get(user_id, (today, 0))
            if used_date != today:
                count = 0
            if count >= limit:
                return False
            self._usage[user_id] = (today, count + 1)
            return True


class HitCounter:
    """召回命中计数进程内聚合器：add 攒批，flush 一条 UPDATE 原子刷库。

    线程安全（请求线程 add 与 APScheduler flush 线程并发）：add 与 flush 的
    快照/回收共用一把锁；DB execute/commit 在锁外执行（不拿锁碰 IO）。commit
    窗口内新落的 add 以「快照扣减」回收（对快照里的 id 逐个减 n，减到 0 删键），
    而非整表 clear——窗口内的并发 add 计数保留、绝不静默丢弃（并发用例实测：
    clear 版会丢）。并发 flush 语义未定义（调度单线程 + 请求侧只 add 不 flush）。
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._counts: dict[int, int] = {}
        self._lock = threading.Lock()

    def add(self, chunk_ids: list[int]) -> None:
        """累加本轮召回命中的 chunk（同 id 多次出现按出现次数累加）。"""
        with self._lock:
            for chunk_id in chunk_ids:
                self._counts[chunk_id] = self._counts.get(chunk_id, 0) + 1

    def flush(self, db: Session) -> int:
        """一条 UPDATE（executemany、参数绑定）原子累加 hit_count 并统一置 last_hit_at。

        返回本次覆盖的去重 chunk 数；空聚合直接返回 0、不发 SQL。参数组按 id 升序
        （断言与读回可复现）；now 取注入时钟（读库断言可精确钉值）。聚合在锁内
        快照、commit 后锁内按快照扣减回收——execute/commit 失败原样上抛（吞异常
        是调度任务体 scheduler.run_hit_flush 的职责），快照未回收、计数保留滚入
        下一周期。
        """
        with self._lock:
            if not self._counts:
                return 0
            now = self._clock.now()
            rows = [
                {"chunk_id": chunk_id, "n": count, "now": now}
                for chunk_id, count in sorted(self._counts.items())
            ]
        stmt = (
            update(Chunk)
            .where(Chunk.id == bindparam("chunk_id"))
            # 参数名避开列名：UPDATE 语句里 bindparam 叫列名 "id" 是 SET 保留名
            # （实测 CompileError），故以 chunk_id 命名。
            .values(
                hit_count=Chunk.hit_count + bindparam("n", type_=Integer),
                last_hit_at=bindparam("now"),
            )
        )
        # 走 Core 通道（会话当前连接/事务）而非 Session.execute：ORM 批量 UPDATE
        # 按主键路径要求以映射键 "id" 供参（实测 InvalidRequestError bupq），与
        # 上述保留名限制互相锁死；Core executemany 正是 brief 要的「一条 UPDATE
        # 批量原子累加」，随 db.commit() 与置时同事务生效。
        db.connection().execute(stmt, rows)
        db.commit()  # flush 由调度独立会话调用、无外层提交者，原子性随本提交生效
        with self._lock:  # 快照扣减：commit 窗口内的并发 add 计数保留（见类 docstring）
            for row in rows:
                remaining = self._counts[row["chunk_id"]] - row["n"]
                if remaining:
                    self._counts[row["chunk_id"]] = remaining
                else:
                    del self._counts[row["chunk_id"]]
        return len(rows)


class RateBucketProto(Protocol):
    """令牌桶限流协议面（工单 21）：try_acquire 语义见 TokenBucketLimiter（突发放行/
    平滑补充、拒绝不扣水位、scope×key 隔离）。参数由调用方按 Settings 分档传入——
    实现不读配置（协议纯函数化，memory/PG 等价性测试无需动 Settings）；pg 实现随
    工单 19 预留列（state_counters scope='bucket'）落地，本工单只做 memory。"""

    def try_acquire(self, scope: str, key: str, rate_per_min: int, burst: int) -> bool: ...

    def reset(self) -> None: ...


class TokenBucketLimiter:
    """令牌桶平滑限速：{(scope, key): (水位令牌, 上次补充时刻)}，懒补充（工单 21）。

    线程纪律与同模块三组件一致（threading.Lock 护内存结构，无 IO）；时间经可注入
    Clock（测试 FakeClock 精确推演），生产 SystemClock。拒绝不扣水位（只补不耗）；
    每次调用（含拒绝）都推进 last——数学上与"固定原点"等价（tokens 已含截至 last
    的补充），并防时钟回拨重补与浮点回算误差；进程重启窗口清空（同
    SlidingWindowLimiter 语义）。
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, str], tuple[float, datetime]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, scope: str, key: str, rate_per_min: int, burst: int) -> bool:
        """首见键满水位放行（突发额度即 burst）；水位 + 按流逝时长线性补充、封顶
        burst；水位 <1 拒绝且不扣（只推进 last）。"""
        now = (self._clock if self._clock is not None else get_clock()).now()
        with self._lock:
            tokens, last = self._buckets.get((scope, key), (float(burst), now))
            elapsed = (now - last).total_seconds()
            if elapsed > 0:
                tokens = min(float(burst), tokens + elapsed * rate_per_min / 60.0)
            if tokens < 1.0:
                self._buckets[(scope, key)] = (tokens, now)  # 拒绝：只推进 last 防回拨重补
                return False
            self._buckets[(scope, key)] = (tokens - 1.0, now)
            return True

    def reset(self) -> None:
        """清空全部桶（测试隔离用；生产进程重启等效）。"""
        with self._lock:
            self._buckets.clear()
