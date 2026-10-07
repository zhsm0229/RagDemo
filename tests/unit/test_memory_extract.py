r"""长期记忆自动抽取单测（F-210 §3.2，agent-evolution 工单 03）：解析 / 降级 / 查重 /
敏感丢弃 / 条数与长度硬约束 / 容量淘汰 / 开关冻结 / 任务体读回答案。

解析类为纯函数用例（不触库）；抽取类走 `session`（tests/conftest.py，测试事务整体
回滚）+ StubLLMProvider（chunks 注入 JSON 数组产出）+ 仓库 PromptPack——断言精确：
落库读回逐字段核对（§15.4 写库必读回），降级路径以"零入库 + 不抛异常 + LLM 调用
计数"锁定。容量淘汰经 monkeypatch memory.get_settings 收敛上限（tests/api/test_memory.py
同款）。召回 recall_recent 的注入窗口与开关退化也在此覆盖（注入消费方见 chat 流）。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.assembly import load_prompt_pack
from app.core.llm import StubLLMProvider
from app.core.security import hash_password
from app.db.models import AuditLog, Conversation, Department, Message, User, UserMemory
from app.services.memory import recall_recent
from app.services.memory_extract import (
    EXTRACT_MAX_TOKENS,
    ExtractionDeps,
    _parse_items,
    extract_and_store,
    extract_memory_task,
)

_QUESTION = "我平时喜欢先看结论再看依据，帮我查下年假政策"
_ANSWER = "年假按工龄五到十天 [1]"
_T0 = datetime(2026, 10, 1, tzinfo=UTC)


# ---------- 直插助手（tests/api/test_memory.py 同构，本文件本地化避免跨测试模块导入） ----------


def make_user(session: Session, username: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在全员部门、测试事务内。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password("right-one"),
        dept_id=dept.id,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def make_memory(
    session: Session, user_id: int, content: str, *, updated_at: datetime | None = None
) -> UserMemory:
    """直插记忆行：updated_at 可控（容量淘汰/召回窗口的最旧判定依赖它）。"""
    memory = UserMemory(user_id=user_id, content=content)
    if updated_at is not None:
        memory.created_at = updated_at
        memory.updated_at = updated_at
    session.add(memory)
    session.flush()
    return memory


def make_assistant_message(
    session: Session, user: User, content: str, *, active: bool = True
) -> Message:
    """直插 assistant 消息（任务体按 id 读回答案；会话归属该用户）。"""
    conversation = Conversation(user_id=user.id, dept_id=user.dept_id, title="任务体测试")
    session.add(conversation)
    session.flush()
    message = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content=content,
        is_active=active,
    )
    session.add(message)
    session.flush()
    return message


def _extract_deps(text: str) -> ExtractionDeps:
    """抽取替身依赖：Stub LLM（产出注入文本）+ 仓库提示词包（单一真相源）。"""
    return ExtractionDeps(llm=StubLLMProvider(chunks=(text,)), prompts=load_prompt_pack())


def _active_contents(session: Session, user_id: int) -> list[str]:
    session.expire_all()  # 抽取走独立会话提交，强制重查防身份映射旧值
    return list(
        session.scalars(
            select(UserMemory.content).where(
                UserMemory.user_id == user_id, UserMemory.is_active.is_(True)
            )
        ).all()
    )


# ---------- _parse_items：JSON 数组解析与容错 ----------


def test_parse_items_accepts_plain_array_and_code_fence() -> None:
    """合法数组直出与代码围栏（```json / ```）包裹两种形态都解析为字符串列表。"""
    assert _parse_items('["a", "b"]') == ["a", "b"]
    assert _parse_items('```json\n["倾向简洁回答"]\n```') == ["倾向简洁回答"]
    assert _parse_items('```\n["x"]\n```') == ["x"]
    assert _parse_items("[]") == []  # 空数组：合法产出（本轮无值得记的信息）


@pytest.mark.parametrize(
    "raw",
    ["不是 JSON", '{"a": 1}', '["a", 1]', '"单一字符串"', "[", ""],
)
def test_parse_items_rejects_malformed_or_wrong_shape(raw: str) -> None:
    """非法 JSON、非数组、含非字符串项：一律 None（静默降级的判定依据）。"""
    assert _parse_items(raw) is None


# ---------- extract_and_store：落库 / 降级 / 约束 ----------


def test_extract_and_store_persists_two_items_with_source_and_audit(session) -> None:
    """正常抽取：最多 2 条落库读回（content/source_message_id/is_active）+ 审计 source=auto。"""
    user = make_user(session, "extract-user")
    deps = _extract_deps('["倾向简洁回答，先给结论", "  负责华东区客服排班  ", "第三条被丢弃"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER, source_message_id=77)

    assert _active_contents(session, user.id) == ["倾向简洁回答，先给结论", "负责华东区客服排班"]
    rows = session.scalars(select(UserMemory).where(UserMemory.user_id == user.id)).all()
    assert all(row.source_message_id == 77 for row in rows)  # 溯源：来源消息 id
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "memory.add")).all()
    assert len(audits) == 2
    assert all(audit.detail["source"] == "auto" for audit in audits)
    assert deps.llm.stream_calls == 1
    assert deps.llm.last_usage is not None  # 流正常耗尽（抽取通道确实调过 LLM）


def test_extract_and_store_overlong_item_truncated_to_40_chars(session) -> None:
    """单条 ≤40 字硬约束：超长条目截断保留前 40 字落库（提示词第一道防线，此处兜底）。"""
    user = make_user(session, "extract-long")
    deps = _extract_deps(f'["{"长" * 60}"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    (content,) = _active_contents(session, user.id)
    assert content == "长" * 40
    assert len(content) == 40


def test_extract_and_store_upstream_failure_degrades_silently(session) -> None:
    """UpstreamError：静默降级零入库不抛异常（§3.2 抽取失败不影响问答主链路）。"""
    user = make_user(session, "extract-fail")
    deps = _extract_deps('["无所谓"]')
    deps.llm.fail_times = 1  # 首次调用即抛（未产出内容）

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)  # 不抛即通过

    assert _active_contents(session, user.id) == []
    assert session.scalars(select(AuditLog).where(AuditLog.action == "memory.add")).all() == []


def test_extract_and_store_unparseable_output_degrades_silently(session) -> None:
    """LLM 产出不是合法 JSON 数组（Stub 缺省正文）：零入库不抛异常。"""
    user = make_user(session, "extract-garbage")
    deps = ExtractionDeps(llm=StubLLMProvider(), prompts=load_prompt_pack())  # 缺省非 JSON

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    assert _active_contents(session, user.id) == []


def test_extract_and_store_dedups_against_existing_and_within_batch(session) -> None:
    """查重（strip 后精确比对）：与既有活跃记忆重复、本轮内重复均跳过；仅新信息落库。"""
    user = make_user(session, "extract-dedup")
    make_memory(session, user.id, "倾向简洁回答")
    deps = _extract_deps('["倾向简洁回答", " 倾向简洁回答 ", "负责排班", "负责排班"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    assert sorted(_active_contents(session, user.id)) == ["倾向简洁回答", "负责排班"]


def test_extract_and_store_drops_sensitive_items(session) -> None:
    """敏感信息排除（F-1006 口径）：手机号 / 身份证（过校验码）条目整条丢弃不落库。"""
    user = make_user(session, "extract-sensitive")
    deps = _extract_deps('["联系手机13812345678", "证件号11010519491231002X", "倾向简洁回答"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    assert _active_contents(session, user.id) == ["倾向简洁回答"]


def test_extract_and_store_capacity_evicts_oldest(session, monkeypatch) -> None:
    """容量上限：写满后再抽取，最旧（updated_at 早）软删淘汰（与手动添加共用写入口）。"""
    user = make_user(session, "extract-cap")
    monkeypatch.setattr(
        "app.services.memory.get_settings",
        lambda: SimpleNamespace(memory_max_per_user=2, memory_inject_limit=10),
    )
    make_memory(session, user.id, "旧画像", updated_at=_T0)
    make_memory(session, user.id, "较新画像", updated_at=_T0 + timedelta(minutes=1))
    deps = _extract_deps('["新画像一", "新画像二"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    rows = {
        row.content: row
        for row in session.scalars(select(UserMemory).where(UserMemory.user_id == user.id)).all()
    }
    assert rows["旧画像"].is_active is False and rows["较新画像"].is_active is False
    assert sorted(_active_contents(session, user.id)) == ["新画像一", "新画像二"]


def test_extract_and_store_skips_when_user_memory_disabled(session) -> None:
    """用户级开关关闭=冻结（§3.5）：不调 LLM 不落库，既有记忆保留不删除。"""
    user = make_user(session, "extract-off")
    make_memory(session, user.id, "既有画像")
    session.query(User).filter(User.id == user.id).update({"memory_enabled": False})
    session.flush()
    deps = _extract_deps('["新画像"]')

    extract_and_store(session, deps, user.id, _QUESTION, _ANSWER)

    assert deps.llm.stream_calls == 0  # 冻结：连 LLM 都不调
    assert _active_contents(session, user.id) == ["既有画像"]


# ---------- extract_memory_task：任务体读回答案 / 跳过中断轮 ----------


class _StubChatDeps:
    """BackgroundTasks 任务体的 deps 替身：任务体只读 prompts（_HasPrompts 结构面）。"""

    prompts = load_prompt_pack()


def test_extract_memory_task_reads_finalized_answer(session, monkeypatch) -> None:
    """任务体按 assistant_msg_id 读回收尾后的回答并抽取落库（端点调度时回答尚未生成，
    任务体自行读库拿最终正文；独立短会话提交对测试事务可见——commit 只释放 savepoint）。
    抽取通道 LLM 必须 monkeypatch（build_llm 直连 .env 真端点，测试严禁外呼）。"""
    user = make_user(session, "task-user")
    message = make_assistant_message(session, user, "回答正文")
    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    monkeypatch.setattr("app.services.memory_extract.SessionLocal", factory)
    monkeypatch.setattr(
        "app.services.memory_extract.build_llm",
        lambda settings: StubLLMProvider(chunks=('["任务体画像"]',)),
    )

    extract_memory_task(_StubChatDeps(), user.id, message.id, _QUESTION)

    contents = _active_contents(session, user.id)
    assert contents == ["任务体画像"]  # 抽取替身产出落库 = 卫语句放行（读到了非空回答）
    row = session.scalars(select(UserMemory).where(UserMemory.user_id == user.id)).one()
    assert row.source_message_id == message.id  # 溯源指向被读取的 assistant 消息


def test_extract_memory_task_skips_interrupted_or_empty_or_missing(session, monkeypatch) -> None:
    """中断轮（is_active=false）、空正文占位、不存在的消息 id：任务体卫语句直接跳过。"""
    user = make_user(session, "task-skip")
    interrupted = make_assistant_message(session, user, "半截", active=False)
    empty = make_assistant_message(session, user, "", active=True)
    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    monkeypatch.setattr("app.services.memory_extract.SessionLocal", factory)
    called: list[int] = []
    monkeypatch.setattr(
        "app.services.memory_extract.extract_and_store",
        lambda *args, **kwargs: called.append(args[2]),
    )

    extract_memory_task(_StubChatDeps(), user.id, interrupted.id, _QUESTION)
    extract_memory_task(_StubChatDeps(), user.id, empty.id, _QUESTION)
    extract_memory_task(_StubChatDeps(), user.id, 999999, _QUESTION)

    assert called == []  # 三种形态均未进入抽取


# ---------- recall_recent：注入召回窗口与开关 ----------


def test_recall_recent_returns_latest_active_within_limit(session, monkeypatch) -> None:
    """注入召回（§3.3）：created_at DESC 取最近 ≤N 条；软删与超出窗口的不返回。"""
    user = make_user(session, "recall-user")
    for i in range(12):
        make_memory(session, user.id, f"画像{i:02d}", updated_at=_T0 + timedelta(minutes=i))
    make_memory(
        session, user.id, "待软删", updated_at=_T0 + timedelta(minutes=99)
    ).is_active = False
    session.flush()
    monkeypatch.setattr(
        "app.services.memory.get_settings",
        lambda: SimpleNamespace(memory_max_per_user=30, memory_inject_limit=10),
    )

    recalled = recall_recent(session, user.id)

    assert len(recalled) == 10
    assert recalled[0] == "画像11"  # 最近发生在前（时间倒序）
    assert "画像00" not in recalled and "画像01" not in recalled  # 超出窗口不注入
    assert "待软删" not in recalled


def test_recall_recent_disabled_user_or_missing_returns_empty(session) -> None:
    """开关关闭/用户不存在：返回空列表（注入为空 → 链路退化为现状，§3.3）。"""
    user = make_user(session, "recall-off")
    make_memory(session, user.id, "画像")
    session.query(User).filter(User.id == user.id).update({"memory_enabled": False})
    session.flush()

    assert recall_recent(session, user.id) == []
    assert recall_recent(session, 999999) == []


def test_extract_max_tokens_constant_is_256() -> None:
    """抽取 max_tokens 模块常量锁值（rewrite.py 同款口径，256 封顶足够）。"""
    assert EXTRACT_MAX_TOKENS == 256
