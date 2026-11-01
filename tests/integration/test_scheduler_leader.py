r"""调度器 advisory lock 抢主集成测试（工单 20 设计 §7，真库 pg-test）。

pg_try_advisory_lock(hashtext('ragdemo:scheduler:leader')) 的会话级语义：两独立
NullPool 引擎模拟两进程恰一抢到；持锁连接关闭后锁释放、第二进程可再抢；
build_scheduler 抢锁失败不 start 且 is_leader=False、抢锁成功则启动（lifespan 消费
契约）。try_acquire_leader 的失败路径会把模块级 _leader_engine 覆写为失败方引擎——
测试经 _restore_and_release 恢复持锁方引用再释放，模拟真实进程退出并保证锁全程
清理不跨用例遗留（生产每进程恰调用一次，无此覆写面）。
"""

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

import app.services.scheduler_leader as scheduler_module
from app.core.config import get_settings
from app.services.scheduler_leader import _LEADER_KEY
from app.services.scheduler import (
    build_scheduler,
    release_leader,
    try_acquire_leader,
)


def _settings():
    """pg fixture 已把 DATABASE_URL 钉到测试库并断言 get_settings 读数（conftest 契约）。"""
    return get_settings()


def _restore_and_release(engine, conn) -> None:
    """把全局引擎/连接引用恢复到持锁方再释放（失败路径覆写全局的测试侧补救）。"""
    scheduler_module._leader_engine = engine
    scheduler_module._leader_conn = conn
    release_leader()


# ---------- §7：两进程先后 try_acquire_leader，恰一 True 一 False ----------


def test_两进程先后抢主恰一成功一失败(pg) -> None:
    """两独立 NullPool 引擎模拟两进程：先者 True（连接保持打开持锁）、后者 False。"""
    assert try_acquire_leader(_settings()) is True
    leader_engine = scheduler_module._leader_engine
    leader_conn = scheduler_module._leader_conn
    try:
        assert try_acquire_leader(_settings()) is False
    finally:
        _restore_and_release(leader_engine, leader_conn)  # 模拟 A 进程退出释放


# ---------- §7：持锁连接关闭后锁释放，第二进程可再抢 ----------


def test_持锁连接关闭后锁释放_第二进程可再抢(pg) -> None:
    """A 释放（release_leader 关连接并 dispose 引擎）后，B 抢锁成功。"""
    assert try_acquire_leader(_settings()) is True
    release_leader()

    assert try_acquire_leader(_settings()) is True
    release_leader()


# ---------- §7：build_scheduler 抢锁失败不 start 且 is_leader=False ----------


def test_抢锁失败不start且is_leader_false(pg) -> None:
    """外部持锁方在场（独立 NullPool 连接）：build_scheduler 抢锁失败 → 不启动、
    is_leader=False（info 日志路径），未启动的 flush 任务永不触发。"""
    url = get_settings().database_url
    holder = create_engine(url, poolclass=NullPool)
    conn = holder.connect()
    got = conn.exec_driver_sql("SELECT pg_try_advisory_lock(hashtext(%s))", (_LEADER_KEY,)).scalar()
    assert got is True
    try:
        scheduler = build_scheduler(_settings())
        assert scheduler.running is False
        assert scheduler.is_leader is False
    finally:
        conn.close()  # 会话级锁随连接关闭释放
        holder.dispose()


def test_抢锁成功则start且is_leader_true(pg) -> None:
    """锁空闲时 build_scheduler 抢锁成功：启动且 is_leader=True（lifespan 读该属性
    决定 catchup 归属的契约面）；收尾 shutdown + release_leader 全程清理。"""
    scheduler = build_scheduler(_settings())
    try:
        assert scheduler.running is True
        assert scheduler.is_leader is True
    finally:
        if scheduler.running:  # 断言失败时避免 SchedulerNotRunningError 掩盖根因
            scheduler.shutdown(wait=False)
        release_leader()
