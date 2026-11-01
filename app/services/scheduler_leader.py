r"""调度主锁（工单 27 自 scheduler.py 拆分达 ≤300 行红线；design/20 §3 语义原样迁移）。

会话级 advisory lock（pg_try_advisory_lock(hashtext('ragdemo:scheduler:leader'))）抢主，
持锁连接为独立 NullPool 引擎——池连接 recycle 会静默丢锁（最危险失败形态=双调度器），
NullPool 生命周期=进程；释放必须显式 conn.close()（NullPool.dispose() 是 no-op，实测源码），
进程退出 TCP 断开为兜底释放路径。锁键与排查 SQL 见 runbook §11。
"""

import logging

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings

logger = logging.getLogger(__name__)

_LEADER_KEY = "ragdemo:scheduler:leader"
_leader_engine = None  # 持锁连接的独立 NullPool 引擎（进程级，try_acquire/release 维护）
_leader_conn = None  # 持锁连接本体：锁随连接存活，显式 close 才释放


def try_acquire_leader(settings: Settings) -> bool:
    """进程级抢主：pg_try_advisory_lock(hashtext(key))，持锁连接随进程生命周期。

    为何不用 SessionLocal 池连接：池连接会被 recycle/归还，锁随连接回收静默丢失 →
    出现"第二个调度器"；NullPool 独立引擎的连接生命周期=进程，锁不可漂移（宁多一条
    常驻连接换确定性，design/20 §9.2）。失败路径连接即关、返回 False。"""
    global _leader_engine, _leader_conn
    _leader_engine = create_engine(settings.database_url, poolclass=NullPool)
    _leader_conn = _leader_engine.connect()
    got = _leader_conn.exec_driver_sql(
        "SELECT pg_try_advisory_lock(hashtext(%s))",  # 参数绑定，禁拼接（红线 2）
        (_LEADER_KEY,),
    ).scalar()
    if not got:
        _leader_conn.close()  # 会话级锁随连接关闭释放
        _leader_conn = None
        return False
    return True  # 连接保持打开：锁随连接存活；进程退出 TCP 断开 PG 自动释放


def release_leader() -> None:
    """释放调度主锁：关持锁连接并 dispose 引擎（lifespan finally 先 shutdown 后调用）。

    连接必须显式 close——NullPool.dispose() 是 no-op（实测源码），不关连接锁不释放
    （design/20 §3"关 NullPool 连接并 dispose 引擎"的字面落实）。异常吞掉留日志——
    进程退出时 TCP 断开为兜底释放路径；未抢锁过为 no-op（test_mode/开关关进程安全可调）。"""
    global _leader_engine, _leader_conn
    conn, _leader_conn = _leader_conn, None
    engine, _leader_engine = _leader_engine, None
    if conn is None and engine is None:
        return
    try:
        if conn is not None:
            conn.close()
        if engine is not None:
            engine.dispose()
    except Exception:
        logger.exception("调度主锁释放失败（进程退出兜底释放）")
