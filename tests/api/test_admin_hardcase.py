r"""难例池管理端点 API 测试（03 票，评测集自动更新）。

断言：GET 列表（status/dept 筛选、camelCase 响应、total）；PATCH 置 ignored（终态）
读回 + 审计 hardcase.ignore；POST synthesize（monkeypatch build_llm 为 Stub）——
出题计数返回、审计 eval.autosynth、LLM 未配置/解析失败不 500（返回 0 题摘要）；
member 403。unknown id 404/3003，非法 status 422/1003。
"""

import hashlib
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND
from app.db.models import AuditLog, Chunk, Department, Document, HardcaseSeed, User
from tests.conftest import login_as

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
_CHUNK_VEC = [0.0] * 1024
_JSON_OK = '{"variants": ["变体问题一"], "expected_answer": "块内答案"}'


def make_user(session, username: str, password: str, role: str):
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    from app.db.models import User as UserModel

    user = UserModel(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def admin_login(client, session) -> None:
    make_user(session, "hc_admin", "right-one", role="admin")
    assert login_as(client, "hc_admin", "right-one").status_code == 200


def _doc_and_chunk(session: Session) -> Chunk:
    dept_id = session.scalar(select(Department.id).where(Department.code == "service"))
    admin_id = session.scalar(select(User.id).where(User.username == "hc_admin"))
    doc = Document(
        title="报销制度",
        dept_id=dept_id,
        original_filename="报销制度.docx",
        file_path=f"hc/{hashlib.sha256(b'hc-doc').hexdigest()}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(b"hc-doc").hexdigest(),
        effective_date=date(2026, 10, 1),
        owner_name="责任人",
        status="ready",
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    chunk = Chunk(
        document_id=doc.id,
        dept_id=dept_id,
        title_path=doc.title,
        content="报销标准：每人每月 200 元。",
        content_chars=20,
        position=0,
        embedding=_CHUNK_VEC,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _make_seed(
    session: Session,
    question: str,
    *,
    status: str = "pending",
    chunk_id: int | None = None,
    dept_code: str = "service",
) -> HardcaseSeed:
    dept_id = session.scalar(select(Department.id).where(Department.code == dept_code))
    seed = HardcaseSeed(
        message_id=1,
        message_created_at=_T0,
        source_type="feedback_down",
        dept_id=dept_id,
        question_text=question,
        normalized_question=question.strip(),
        answer_snapshot="回答原文",
        citations_snapshot=(
            [{"n": 1, "refType": "doc", "refId": chunk_id}] if chunk_id is not None else None
        ),
        status=status,
        created_at=_T0,
        updated_at=_T0,
    )
    session.add(seed)
    session.flush()
    return seed


# ---------- 列表 ----------


def test_难例池列表与筛选(client, session) -> None:
    admin_login(client, session)
    pending = _make_seed(session, "待合成问题", chunk_id=None)
    done = _make_seed(session, "已合成问题", status="synthesized")

    resp = client.get("/api/admin/hardcase-seeds")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    first = data["items"][0]
    assert {
        "id",
        "sourceType",
        "status",
        "questionText",
        "deptCode",
        "synthCount",
        "createdAt",
    } <= set(first)

    resp = client.get("/api/admin/hardcase-seeds?status=synthesized")
    assert [item["id"] for item in resp.json()["data"]["items"]] == [done.id]

    resp = client.get("/api/admin/hardcase-seeds?status=pending")
    assert [item["id"] for item in resp.json()["data"]["items"]] == [pending.id]


def test_难例池置忽略并留审计(client, session) -> None:
    admin_login(client, session)
    seed = _make_seed(session, "要忽略的问题")

    resp = client.patch(f"/api/admin/hardcase-seeds/{seed.id}", json={"status": "ignored"})
    assert resp.status_code == 200
    session.expire_all()
    session.refresh(seed)
    assert seed.status == "ignored"
    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "hardcase.ignore", AuditLog.object_id == seed.id)
    ).one()
    assert audit.object_type == "hardcase_seed"

    resp = client.patch(f"/api/admin/hardcase-seeds/{seed.id}", json={"status": "nope"})
    assert resp.status_code == 422
    resp = client.patch("/api/admin/hardcase-seeds/999999", json={"status": "ignored"})
    assert resp.status_code == 404
    assert resp.json()["code"] == ERR_NOT_FOUND


# ---------- 手动合成 ----------


def test_手动合成出题并留审计(client, session, monkeypatch) -> None:
    admin_login(client, session)
    chunk = _doc_and_chunk(session)
    _make_seed(session, "报销标准是什么？", chunk_id=chunk.id)

    class _Stub:
        def chat_stream(self, messages, max_tokens):
            yield _JSON_OK

    from app.api import admin_hardcase

    monkeypatch.setattr(admin_hardcase, "build_llm", lambda settings, temperature: _Stub())

    resp = client.post("/api/admin/hardcase-seeds/synthesize")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["synthesized"] == 1
    seed = session.scalars(select(HardcaseSeed)).one()
    session.expire_all()
    session.refresh(seed)
    assert seed.status == "synthesized"
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "eval.autosynth")).one()
    assert audit.detail["synthesized"] == 1


def test_手动合成预算走app_settings在线配置且审计带trigger键(
    client, session, monkeypatch
) -> None:
    """手动合成与周任务统一配置源（评审 P2：设置页预算对手动按钮无效的口径分叉）：
    app_settings 配 synthPerSeed=1 / weeklyMaxQuestions=1 → 手动合成最多产 1 题；
    审计 detail 带 trigger="manual"（与周任务 trigger="weekly" 口径对齐，消费方
    可统一区分触发来源）。修复前端点读 env 缺省、审计无 trigger 键。"""
    from app.core.clock import get_clock
    from app.services.eval_auto_config import save_eval_auto_config

    admin_login(client, session)
    save_eval_auto_config(
        session,
        get_clock(),
        actor_id=session.scalar(select(User.id).where(User.username == "admin")),
        enabled=False,
        synth_per_seed=1,
        doc_sample_per_dept=5,
        weekly_max_questions=1,  # 预算闸收紧到 1：验证手动路径真读 app_settings
        regression_pp=5.0,
    )
    chunk = _doc_and_chunk(session)
    _make_seed(session, "预算分叉的问题", chunk_id=chunk.id)
    _make_seed(session, "第二个种子不出题", chunk_id=chunk.id)  # 预算 1 → 只出一题

    class _Stub:
        def chat_stream(self, messages, max_tokens):
            yield _JSON_OK

    from app.api import admin_hardcase

    monkeypatch.setattr(admin_hardcase, "build_llm", lambda settings, temperature: _Stub())

    resp = client.post("/api/admin/hardcase-seeds/synthesize")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["synthesized"] == 1  # 修复前：env 缺省预算 100 → 两种子各出题（=2）
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "eval.autosynth")).one()
    assert audit.detail["trigger"] == "manual"  # 修复前：无 trigger 键


def test_合成解析失败不炸接口(client, session, monkeypatch) -> None:
    admin_login(client, session)
    chunk = _doc_and_chunk(session)
    _make_seed(session, "解析会失败的问题", chunk_id=chunk.id)

    class _Bad:
        def chat_stream(self, messages, max_tokens):
            yield "不是 JSON"

    from app.api import admin_hardcase

    monkeypatch.setattr(admin_hardcase, "build_llm", lambda settings, temperature: _Bad())

    resp = client.post("/api/admin/hardcase-seeds/synthesize")
    assert resp.status_code == 200
    assert resp.json()["data"]["synthesized"] == 0
    seed = session.scalars(select(HardcaseSeed)).one()
    session.expire_all()
    session.refresh(seed)
    assert seed.status == "pending"  # 可重试


def test_成员无权难例池(client, session) -> None:
    make_user(session, "hc_member", "right-one", role="member")
    assert login_as(client, "hc_member", "right-one").status_code == 200
    assert client.get("/api/admin/hardcase-seeds").status_code == 403
    assert client.post("/api/admin/hardcase-seeds/synthesize").status_code == 403
    assert (
        client.patch("/api/admin/hardcase-seeds/1", json={"status": "ignored"}).status_code == 403
    )
    assert client.get("/api/admin/hardcase-seeds").json()["code"] == ERR_FORBIDDEN


def test_重扫API_审计读回与member拒(client, session) -> None:
    """POST /admin/hardcase-seeds/rescan（工单 16）：admin 可调、审计 hardcase.rescan
    记转回条数（只转有引用且 skip 累计型）；member 403。"""
    make_user(session, "rescan_admin", "right-one", role="admin")
    dept_id = session.scalar(select(Department.id).where(Department.code == "service"))
    session.add(
        HardcaseSeed(
            message_id=1,
            message_created_at=datetime.now(UTC),
            source_type="feedback_down",
            dept_id=dept_id,
            question_text="重扫目标种子",
            normalized_question="重扫目标种子",
            answer_snapshot="答案",
            citations_snapshot=[{"refType": "doc", "refId": 1}],
            status="no_ground",
            skip_count=3,
        )
    )
    session.commit()
    assert login_as(client, "rescan_admin", "right-one").status_code == 200

    resp = client.post("/api/admin/hardcase-seeds/rescan")
    assert resp.status_code == 200
    assert resp.json()["data"]["rescanned"] == 1
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "hardcase.rescan"))
    assert audit is not None and audit.detail["rescanned"] == 1

    make_user(session, "rescan_member", "right-two", role="member")
    assert login_as(client, "rescan_member", "right-two").status_code == 200
    assert client.post("/api/admin/hardcase-seeds/rescan").status_code == 403
