r"""模板生成首任务端到端测试（agent-evolution #06）：运行 → 草稿 → 审核 → 入库 → 可检索。

演示路径全链路（TEST_MODE Stub 零网络）：POST /api/agent/runs（taskType=template_draft，
脚本化通道按"检索 few-shot → save_draft 落草稿 → 成稿说明"剧本走）→ AG-UI 事件序精确 +
agent_runs succeeded → agent_steps 轨迹与 TOOL_CALL 事件一一对应（读回逐行断言）→
documents 出现 source_type='draft'/review_status='待审核' 行 → admin 审核通过（test_mode
同步走既有入库管道）收口 ready → 检索可命中（#05 手法：StubEmbedder 同算法同向量）。
红线读回断言一并在案：审核通过前草稿不可被检索命中。

跨部门隔离 e2e：service 部门用户运行任务，语料只挂 hr 部门——检索工具强制继承
调用者 dept_ids（F-502），轨迹观察里不得出现 hr 语料（SQL 过滤零命中 → 拒答话术），
草稿归属运行者部门而非语料部门。工具失败路径：save_draft 参数非法 → fail 观察喂模型、
error 落 agent_steps、documents 零落行。

消费方式同 test_agent_runs（agent_client 本地构造）：services.agent_runs 与
services.agent_steps 两处 SessionLocal 指向测试连接（短会话与用例读回同处测试事务）；
剧本通道逐用例注入（app.state.agent_deps.channel，故障通道测试同款手法）。
"""

import hashlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.agent_model import ChannelTurn
from app.core.llm import StubEmbedder
from app.db.models import AgentRun, AgentStep, Chunk, Department, Document, User
from app.main import ScriptedAgentChannel, app
from app.services.retrieval import search_chunks
from tests.conftest import login_as

_DRAFT_TITLE = "HR_考勤制度模板"
# 草稿正文：无真实人名/电话/证件号（敏感扫描在审核后的入库管道执行，命中会落 failed）
_DRAFT_CONTENT = "\n".join(
    f"考勤制度模板第{i}条 说明文字{i}：本条为模板示例，供起草人参照改写后提交审核。"
    for i in range(1, 11)
)
_FINAL_TEXT = "已生成草稿《HR_考勤制度模板》并提交审核，请等待管理员审核通过。"
_CORPUS_TITLE = "全员_考勤制度"
_CORPUS_TEXT = "年假按工龄五到十天"

# 剧本（ScriptedAgentChannel 逐回合吐）：检索 few-shot → save_draft 落草稿 → 成稿说明
_TEMPLATE_TURNS = (
    ChannelTurn(
        content="",
        tool_calls=(
            {
                "name": "knowledge_base_search",
                "args": {"query": "考勤制度", "top_n": 3},
                "id": "call_1",
                "type": "tool_call",
            },
        ),
    ),
    ChannelTurn(
        content="",
        tool_calls=(
            {
                "name": "save_draft",
                "args": {"title": _DRAFT_TITLE, "content": _DRAFT_CONTENT},
                "id": "call_2",
                "type": "tool_call",
            },
        ),
    ),
    ChannelTurn(content=_FINAL_TEXT),
)

_EXPECTED_ORDER = [
    "RUN_STARTED",
    "STEP_STARTED",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "STEP_FINISHED",
    "STEP_STARTED",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "STEP_FINISHED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "RUN_FINISHED",
]


# ---------- 构造与消费辅助（口径同 tests/api/test_agent_runs.py） ----------


def make_user(
    session: Session, username: str, password: str, *, role: str = "member", dept_code: str = "all"
) -> User:
    """直插测试用户（bcrypt 哈希），部门/角色可选（隔离用例需 service/hr 部门）。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == dept_code)).one()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def seed_corpus(session: Session, dept_id: int, title: str, content: str) -> Chunk:
    """直插 ready 文档 + 单 chunk（StubEmbedder 确定性向量与检索链 Stub 同算法同向量）：
    证明工具调用真的穿过部门过滤 SQL（隔离被打破会以命中泄漏暴露）。"""
    embedder = StubEmbedder()
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=1,
    )
    session.add(doc)
    session.flush()
    chunk = Chunk(
        document_id=doc.id,
        dept_id=dept_id,
        title_path=title,
        content=content,
        content_chars=len(content),
        position=0,
        embedding=embedder.embed([content])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """响应体按空行切事件，逐块解析 event/data 行（data 为 JSON，中文原样）。"""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        payload: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        events.append((name, payload))
    return events


def _run_row(session: Session) -> AgentRun:
    session.expire_all()  # 短会话的写入/收尾不经过本 session 身份映射，强制重载
    return session.scalars(select(AgentRun)).one()


def _steps_of(session: Session, run_id: int) -> list[AgentStep]:
    return list(
        session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)
        ).all()
    )


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存（口径同 test_document_draft_review）：
    save_draft 落盘与审核后的解析管道都从该根取件。"""
    import os

    from app.core.config import get_settings

    saved = os.environ.get("STORAGE_DIR")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved)
    get_settings.cache_clear()


@pytest.fixture()
def agent_client(session, fake_clock, test_mode, sandbox_storage, monkeypatch):
    """模板生成任务测试客户端：三处短会话工厂指向测试连接（agent_runs 生成器/收尾、
    agent_bridge 工具调用、agent_steps 轨迹落库）；剧本通道逐用例经 _use_turns 注入
    （fresh 实例保 _calls 归零）。"""
    from app.api.deps import get_db
    from app.services import agent_bridge, agent_runs, agent_steps
    from app.services.auth import default_limiter

    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    monkeypatch.setattr(agent_runs, "SessionLocal", factory)
    monkeypatch.setattr(agent_bridge, "SessionLocal", factory)
    monkeypatch.setattr(agent_steps, "SessionLocal", factory)
    default_limiter.reset()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http
    app.dependency_overrides.pop(get_db, None)


def _use_turns(monkeypatch, turns: tuple[ChannelTurn, ...]) -> None:
    """把 app.state.agent_deps.channel 换成给定剧本（故障通道测试同款换缝手法）。"""
    monkeypatch.setattr(app.state.agent_deps, "channel", ScriptedAgentChannel(list(turns)))


# ---------- ① 端到端演示路径：运行 → 草稿 → 审核 → 入库 → 可检索 ----------


def test_template_draft_e2e_run_draft_approve_ready_searchable(
    agent_client, session, monkeypatch
) -> None:
    """全链路：template_draft 一次运行（RUN 事件序 + 轨迹一一对应）→ save_draft 落草稿
    （draft/待审核读回 + 未审核不可检索）→ admin 审核通过 → 既有管道收口 ready → 检索命中。"""
    user = make_user(session, "tpluser", "right-one")
    dept_id = user.dept_id
    seed_corpus(session, dept_id, _CORPUS_TITLE, _CORPUS_TEXT)
    _use_turns(monkeypatch, _TEMPLATE_TURNS)
    assert login_as(agent_client, "tpluser", "right-one").status_code == 200

    resp = agent_client.post(
        "/api/agent/runs", json={"taskType": "template_draft", "input": "帮我起草一份考勤制度模板"}
    )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    # 事件序精确：两次工具步（检索 → 存草稿）+ 最终文本（剧本三回合全数走完）
    assert [name for name, _ in events] == _EXPECTED_ORDER
    run = _run_row(session)
    assert events[0][1]["thread_id"] == f"agent-run-{run.id}"
    assert events[2][1]["tool_call_name"] == "knowledge_base_search"
    assert json.loads(events[3][1]["delta"]) == {"query": "考勤制度", "top_n": 3}
    assert events[7][1]["tool_call_name"] == "save_draft"
    assert json.loads(events[8][1]["delta"]) == {"title": _DRAFT_TITLE, "content": _DRAFT_CONTENT}
    assert events[12][1]["delta"] == _FINAL_TEXT
    assert events[-1][1]["result"] == {"text": _FINAL_TEXT}
    # run 读回：succeeded + 归属提问者
    assert run.status == "succeeded"
    assert run.run_type == "template_draft"
    assert run.output == {"text": _FINAL_TEXT}
    assert (run.user_id, run.dept_id) == (user.id, dept_id)

    # 轨迹与工具调用一一对应：TOOL_CALL 事件序 == agent_steps 工具序，args/观察逐行读回
    tool_events = [p["tool_call_name"] for name, p in events if name == "TOOL_CALL_START"]
    steps = _steps_of(session, run.id)
    assert (
        [s.tool_name for s in steps]
        == tool_events
        == [
            "knowledge_base_search",
            "save_draft",
        ]
    )
    assert [s.seq for s in steps] == [1, 2]
    assert all(s.run_id == run.id and s.step_type == "tool" and s.error is None for s in steps)
    assert steps[0].args == {"query": "考勤制度", "top_n": 3}
    assert _CORPUS_TITLE in steps[0].observation  # few-shot 参照真的来自部门语料
    assert steps[1].args["title"] == _DRAFT_TITLE
    assert "documentId" in steps[1].observation  # save_draft 产物快照（草稿 id 与审核状态）

    # 草稿读回：source_type=draft + 待审核 + 归属运行者（status/is_active 由 #05 状态机落定）
    draft = session.scalars(select(Document).where(Document.title == _DRAFT_TITLE)).one()
    assert draft.source_type == "draft"
    assert draft.review_status == "待审核"
    assert draft.status == "pending"
    assert draft.is_active is False
    assert draft.dept_id == dept_id
    assert draft.uploaded_by == user.id

    # 红线读回：待审核草稿不可被检索命中（检索侧仅 ready 且 is_active，无审核不落地）
    embedder = StubEmbedder()
    query_vec = embedder.embed(["考勤制度模板"])[0]

    def _draft_hits() -> list:
        hits = search_chunks(session, query_vec, [dept_id], top_k=5, ef_search=40)
        return [hit for hit in hits if hit.document_id == draft.id]

    assert _draft_hits() == [], "待审核草稿被检索命中：违反无审核不落地红线"

    # admin 审核通过：test_mode 同步走既有入库管道 → ready + is_active
    make_user(session, "tpladmin", "Admin#2026", role="admin")
    assert login_as(agent_client, "tpladmin", "Admin#2026").status_code == 200
    approve = agent_client.post(f"/api/documents/{draft.id}/draft/approve")
    assert approve.status_code == 200
    assert approve.json()["data"]["reviewStatus"] == "已通过"
    assert approve.json()["data"]["status"] == "ready"

    session.expire_all()
    row = session.get(Document, draft.id)
    assert row.status == "ready"
    assert row.is_active is True

    # 收口后检索可命中：草稿 chunk 进入检索库（与种子语料并存，按 document_id 精确判）
    hits = _draft_hits()
    assert len(hits) == 1
    assert hits[0].document_id == draft.id


# ---------- ② 跨部门隔离 e2e：A 部门运行检索不到 B 部门语料 ----------


def test_cross_dept_isolation_in_trace(agent_client, session, monkeypatch) -> None:
    """service 用户运行 template_draft，语料只挂 hr 部门：检索工具强制继承调用者
    dept_ids=[service, 全员]（F-502），轨迹观察零命中且不泄漏 hr 语料；草稿归属
    service 部门——隔离证据直接取自 agent_steps 观察（轨迹即审计面）。"""
    service_user = make_user(session, "svcuser", "right-one", dept_code="service")
    service_dept_id = service_user.dept_id
    hr_dept = session.scalars(select(Department).where(Department.code == "hr")).one()
    seed_corpus(session, hr_dept.id, "hr_薪酬保密制度", "薪酬保密：全员薪酬数据仅限总监查阅。")
    turns = (
        _TEMPLATE_TURNS[0],
        ChannelTurn(
            content="",
            tool_calls=(
                {
                    "name": "save_draft",
                    "args": {"title": "客服_话术模板", "content": _DRAFT_CONTENT},
                    "id": "call_2",
                    "type": "tool_call",
                },
            ),
        ),
        _TEMPLATE_TURNS[2],
    )
    _use_turns(monkeypatch, turns)
    assert login_as(agent_client, "svcuser", "right-one").status_code == 200

    resp = agent_client.post(
        "/api/agent/runs", json={"taskType": "template_draft", "input": "起草客服话术模板"}
    )

    assert resp.status_code == 200
    run = _run_row(session)
    assert run.status == "succeeded"
    assert run.dept_id == service_dept_id
    steps = _steps_of(session, run.id)
    assert [s.tool_name for s in steps] == ["knowledge_base_search", "save_draft"]
    # 检索观察：零命中走拒答话术，hr 语料（标题与正文）绝不出现——隔离证据在轨迹里
    assert "知识库中未找到相关内容" in steps[0].observation
    assert "薪酬保密" not in steps[0].observation
    assert "hr_薪酬保密制度" not in steps[0].observation
    # 草稿归属运行者部门（service），绝非语料部门（hr）
    draft = session.scalars(select(Document).where(Document.title == "客服_话术模板")).one()
    assert draft.dept_id == service_dept_id
    assert draft.dept_id != hr_dept.id
    assert draft.review_status == "待审核"


# ---------- ③ 工具失败路径：fail 观察喂模型 + error 落轨迹 + 零落行 ----------


def test_save_draft_invalid_args_recorded_in_trace(agent_client, session, monkeypatch) -> None:
    """save_draft 空标题：业务规则失败以 {"error": 中文原因} 观察喂模型（不向 agent 循环
    抛异常），error 落 agent_steps，documents 零落行——run 仍 succeeded（agent 循环自收尾）。"""
    make_user(session, "badtpl", "right-one")
    _use_turns(
        monkeypatch,
        (
            ChannelTurn(
                content="",
                tool_calls=(
                    {
                        "name": "save_draft",
                        "args": {"title": "  ", "content": "正文占位"},
                        "id": "call_1",
                        "type": "tool_call",
                    },
                ),
            ),
            ChannelTurn("标题缺失，无法保存草稿。"),
        ),
    )
    assert login_as(agent_client, "badtpl", "right-one").status_code == 200

    resp = agent_client.post(
        "/api/agent/runs", json={"taskType": "template_draft", "input": "起草模板"}
    )

    assert resp.status_code == 200
    run = _run_row(session)
    assert run.status == "succeeded"
    assert session.scalars(select(Document)).all() == []  # 非法调用零落行
    steps = _steps_of(session, run.id)
    assert len(steps) == 1
    assert steps[0].tool_name == "save_draft"
    assert steps[0].error == "title 不能为空"
    assert '"error"' in steps[0].observation  # 观察即喂给模型的失败形态
