r"""召回命中计数聚合器（F-1011，工单 24 自 chat_cache 拆分达 ≤300 行红线）。

add 攒批 / flush 一条 UPDATE（executemany、参数绑定、单事务）原子累加 hit_count
并统一置 last_hit_at；聚合仅在提交成功后回收，失败轮次滚入下一周期
（scheduler.run_hit_flush logger.exception 吞掉后自然重试）。协议面 HitCounterProto
（工单 19）由本模块承载；导入路径经 chat_cache 门面 re-export 保持不变。
"""

import threading
from typing import Protocol

from sqlalchemy import Integer, bindparam, update
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.models import Chunk


class HitCounterProto(Protocol):
    """召回计数聚合协议面（工单 19）：add 攒批 / flush 刷库语义见 HitCounter
    （聚合不丢、失败滚入），pg 实现由 state_pg.PgHitCounter 提供。"""

    def add(self, chunk_ids: list[int]) -> None: ...

    def flush(self, db: Session) -> int: ...


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
