r"""memory.py 服务级并发测试（A5 并发容量竞态，agent-evolution 修复批）。

与 tests/api/test_memory.py（API 面）分工：本文件只覆盖多线程真实并发——需要**已
提交**数据跨连接可见（session fixture 的 savepoint 隔离不适用），故直用 pg 引擎建
独立会话并在 finally 清理（不留残渣）。advisory lock 行为断言：
- 两线程同内容并发 add_manual：查重与容量裁决在 per-user 事务级 advisory lock
  临界区内串行化——最终不超容量上限、零重复（一成功一 409/3001）。
竞态钉死：spy 包裹 _duplicate_exists，第一个完成查重的线程在其后逗留 30ms（模拟
真实世界"慢提交者"——正确性不得依赖提交速度），另一线程的查重必然落进该窗口：
未修复时双查重通过双双插入（红），修复后后到者在锁内重查到先到者已提交的行（绿）。
"""

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.core.errors import ApiError
from app.core.security import hash_password
from app.db.models import AuditLog, Department, User, UserMemory
from app.services import memory as memory_service

_USERNAME = "conc-add-user"
_CONTENT = "并发写入的画像"
_FIRST_COMMITTER_LINGER_S = 0.03  # 首个查重者逗留窗口：远大于另一线程一次查重往返


@pytest.fixture()
def conc_user(pg):
    """并发用例数据：真实提交（跨连接可见），测试结束清理（含审计行，不留残渣）。"""
    monkey_settings = SimpleNamespace(memory_max_per_user=2, memory_inject_limit=10)

    factory = sessionmaker(bind=pg, expire_on_commit=False)
    with factory() as setup:
        # 防御性预清理：上次运行异常中断的残渣（用户名唯一约束不挡路）
        stale = setup.scalars(select(User).where(User.username == _USERNAME)).all()
        for stale_user in stale:
            setup.execute(delete(UserMemory).where(UserMemory.user_id == stale_user.id))
            setup.execute(delete(AuditLog).where(AuditLog.actor_user_id == stale_user.id))
            setup.delete(stale_user)
        setup.commit()

        dept = setup.scalars(select(Department).where(Department.code == "all")).first()
        user = User(
            username=_USERNAME,
            password_hash=hash_password("right-one"),
            dept_id=dept.id,
            must_change_password=False,
        )
        setup.add(user)
        setup.flush()  # 先发号 user.id，seed 记忆行引用归属
        seed = UserMemory(user_id=user.id, content="既有画像")  # 与新写入合计将触容量边界
        setup.add(seed)
        setup.commit()
        user_id = user.id
    yield user_id, monkey_settings
    with factory() as cleanup:
        cleanup.execute(delete(UserMemory).where(UserMemory.user_id == user_id))
        cleanup.execute(delete(AuditLog).where(AuditLog.actor_user_id == user_id))
        cleanup.execute(delete(User).where(User.id == user_id))
        cleanup.commit()


def test_concurrent_add_respects_capacity_and_no_duplicates(pg, conc_user, monkeypatch) -> None:
    """A5：两线程各持 SessionLocal 同时 add 同一内容——advisory lock 把"查重/容量/插入"
    读改写临界区按用户串行化：恰一成功一 409，最终活跃行数不超容量上限且零重复。"""
    user_id, settings_stub = conc_user
    monkeypatch.setattr("app.services.memory.get_settings", lambda: settings_stub)
    # 竞态钉死 spy：记录查重到达序号，第一名在查重后逗留（见模块 docstring）——
    # 未修复时该逗留制造"双查重通过"窗口（红），修复后逗留者持锁，后到者锁内重查（绿）
    real_duplicate_exists = memory_service._duplicate_exists
    seq_lock = threading.Lock()
    arrivals = [0]

    def spy_duplicate_exists(db, uid: int, text_value: str) -> bool:
        with seq_lock:
            arrivals[0] += 1
            turn = arrivals[0]
        result = real_duplicate_exists(db, uid, text_value)
        if turn == 1 and not result:
            time.sleep(_FIRST_COMMITTER_LINGER_S)  # 慢提交者：查重通过后不立刻提交
        return result

    monkeypatch.setattr(memory_service, "_duplicate_exists", spy_duplicate_exists)
    factory = sessionmaker(bind=pg, expire_on_commit=False)
    outcomes: list[object] = []

    def worker() -> None:
        with factory() as db:
            try:
                memory_service.add_manual(db, user_id, _CONTENT)
                outcomes.append("ok")
            except ApiError as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with factory() as verify:
        actives = verify.scalars(
            select(UserMemory).where(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
        ).all()
        contents = [row.content for row in actives]

    assert arrivals[0] == 2  # 两线程都执行过查重
    assert len(contents) <= 2  # 容量上限（memory_max_per_user=2）不被并发击穿
    assert len(contents) == len(set(contents))  # 零重复
    assert set(contents) == {"既有画像", _CONTENT}
    assert sorted(outcomes, key=str) == [3001, "ok"]  # 恰一成功一查重 409（ERR_DUPLICATE_DOC）
