"""审计服务集成测试：record_audit 写库 → 同事务 SELECT 读回逐字段断言（§15.4）。

fixtures 见 tests/conftest.py：`session`（每测试独立事务，结束回滚）。
audit_logs.actor_user_id 有 FK → 复用种子 admin（pg fixture 已提交，本事务可见）。
detail 为 JSONB：读回用 detail::text 取 JSON 文本再 json.loads——psycopg 对 jsonb 列
默认直接反序列化成 dict，拿不到原文；::text 才能证明"列值可 json 解析"且与原 dict
逐字段相等（jsonb 会重排键序，dict 相等性不受影响）。created_at 属于复合主键
(id, created_at)，由注入 clock 显式赋值，读回断言逐条一致。
"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.services.audit import record_audit

# 统一读回 SQL：detail 以 ::text 取原文供 json.loads 验证
_READ_BACK = text(
    "SELECT actor_user_id, action, object_type, object_id, detail::text AS detail, "
    "ip, created_at FROM audit_logs"
)


def _admin_id(session: Session) -> int:
    return session.execute(text("SELECT id FROM users WHERE username = 'admin'")).scalar_one()


def test_record_audit_roundtrip_all_fields(session: Session) -> None:
    """核心用例：写一条 → 同 session SELECT 读回，7 字段逐字段断言。"""
    admin_id = _admin_id(session)
    detail: dict = {"before": None, "after": {"role": "member", "username": "客服01"}}
    before = datetime.now(tz=UTC)
    record_audit(
        session,
        actor_user_id=admin_id,
        action="user.create",
        object_type="user",
        object_id=admin_id,
        detail=detail,
        ip="10.0.0.8",
    )

    rows = session.execute(_READ_BACK).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == admin_id
    assert row.action == "user.create"
    assert row.object_type == "user"
    assert row.object_id == admin_id
    assert row.ip == "10.0.0.8"
    loaded = json.loads(row.detail)
    assert isinstance(loaded, dict)
    assert loaded == detail  # json.loads 逐字段一致（含 before=None / after 嵌套）
    # 默认 SystemClock（UTC 微秒归零）：created_at 落库为当前时刻，±60s 容忍执行耗时
    assert row.created_at.tzinfo is not None
    assert abs(row.created_at - before) < timedelta(seconds=60)


def test_detail_chinese_and_nested_roundtrip(session: Session) -> None:
    """detail 含中文键值与深嵌套结构：JSONB 往返保真，中文以明文可读落库。"""
    detail: dict = {
        "标题": "客服部_报销规范_v2",
        "变更": {"旧": {"status": "pending"}, "新": {"status": "ready"}},
        "标签": ["制度", "财务"],
        "深嵌套": {"a": {"b": [1, 2, {"c": "内容"}]}},
    }
    record_audit(
        session,
        actor_user_id=_admin_id(session),
        action="document.update",
        object_type="document",
        object_id=42,
        detail=detail,
        ip="192.168.1.10",
    )

    raw = session.execute(text("SELECT detail::text FROM audit_logs")).scalar_one()
    assert isinstance(raw, str)
    assert json.loads(raw) == detail
    assert "报销规范" in raw  # jsonb 文本输出为 UTF-8 明文，非 \\uXXXX 转义


def test_ip_defaults_to_none(session: Session) -> None:
    """ip 缺省 None：落库为 NULL（非空串），读回断言。"""
    record_audit(
        session,
        actor_user_id=_admin_id(session),
        action="user.login",
        object_type="user",
        object_id=_admin_id(session),
        detail={"result": "成功"},
    )
    row = session.execute(_READ_BACK).one()
    assert row.ip is None


def test_created_at_from_injected_clock(session: Session) -> None:
    """created_at 逐条取注入 clock.now()：两条记录间 advance 的差值精确落库。"""
    clock = FakeClock("2026-10-01T08:00:00+00:00")
    admin_id = _admin_id(session)
    record_audit(
        session,
        actor_user_id=admin_id,
        action="document.upload",
        object_type="document",
        object_id=1,
        detail={"n": 1},
        clock=clock,
    )
    clock.advance(hours=3, minutes=30)
    record_audit(
        session,
        actor_user_id=admin_id,
        action="document.upload",
        object_type="document",
        object_id=2,
        detail={"n": 2},
        clock=clock,
    )

    times = [r[0] for r in session.execute(text("SELECT created_at FROM audit_logs ORDER BY id"))]
    assert times == [
        datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 11, 30, tzinfo=UTC),
    ]


def test_commit_left_to_caller(session: Session, pg) -> None:
    """commit 由调用方控制：record_audit 内部不 commit，调用方 commit 后才落库。

    用引擎直连 Session（等价生产拓扑 sessionmaker(bind=engine)）：record_audit
    返回时行只在未提交事务里，独立探针连接不可见 → 调用方 commit 后可见。若实现
    误在内部 commit，第一条探针断言失败。（写真实 pg-test 库，finally 清除本行。）
    注：对 conftest 的 `session`（挂在连接事务上，conditional_savepoint 语义）
    探测 commit 与否无意义——commit 只会释放 savepoint，够不到真库。
    """
    writer = Session(pg)
    try:
        record_audit(
            writer,
            actor_user_id=_admin_id(session),
            action="user.lock",
            object_type="user",
            object_id=1,
            detail={"reason": "连续失败"},
            ip=None,
        )
        with pg.connect() as probe:
            committed = probe.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'user.lock'")
            ).scalar_one()
        assert committed == 0  # 未 commit：其他连接不可见（内部 commit 则此处 = 1）

        writer.commit()
        with pg.connect() as probe:
            committed = probe.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'user.lock'")
            ).scalar_one()
        assert committed == 1  # 调用方 commit：落库生效
    finally:
        writer.execute(text("DELETE FROM audit_logs WHERE action = 'user.lock'"))
        writer.commit()
        writer.close()
