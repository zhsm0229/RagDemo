r"""合成流水周任务 + 运行配置集成测试（05 票，真 pg + FakeClock + Stub LLM）。

用例：
① 总闸关（缺省）→ run_synth_pipeline 直接返回零副作用：消息不采集、不出题、不留审计；
② 总闸开（app_settings 落行）→ 全流水跑通：拒答消息入池 → pending 种子合成变体 →
   审计 eval.autosynth（trigger=weekly）读回；
③ load/save 配置：save 落 app_settings 单行（updated_by 回填）+ 审计 eval.auto_config
   {before, after}；load 行缺失回落 Settings 缺省（enabled=False）。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.clock import FakeClock
from app.core.config import Settings
from app.db.models import AuditLog, Conversation, HardcaseSeed, Message, User
from app.services.eval_auto import run_synth_pipeline
from app.services.eval_auto_config import load_eval_auto_config, save_eval_auto_config

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
_CLOCK = FakeClock("2026-10-19T04:00:00+00:00")  # 周一：上周为完整周
_JSON_OK = '{"variants": ["变体问题"], "expected_answer": "块内答案"}'


class _StubLLM:
    def chat_stream(self, messages: Any, max_tokens: int) -> Any:
        yield _JSON_OK


def _admin_id(session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _make_refuse_pair(session) -> None:
    """直插一组拒答问答对（采集通道的输入素材）。"""
    dept_id = session.scalar(select(User.dept_id).where(User.username == "admin"))
    conv = Conversation(user_id=_admin_id(session), dept_id=dept_id, title="流水测试")
    session.add(conv)
    session.flush()
    session.add(
        Message(
            conversation_id=conv.id,
            user_id=conv.user_id,
            dept_id=dept_id,
            role="user",
            content="流水测试的拒答问题",
            created_at=_T0,
            updated_at=_T0,
        )
    )
    session.flush()
    session.add(
        Message(
            conversation_id=conv.id,
            user_id=conv.user_id,
            dept_id=dept_id,
            role="assistant",
            content="知识库中未找到相关内容",
            answer_type="refuse",
            created_at=_T0,
            updated_at=_T0,
        )
    )
    session.flush()


def test_总闸关零副作用(session) -> None:
    """① 缺省配置（enabled=False）：任务体直接返回零副作用——消息不采集、不出题。"""
    _make_refuse_pair(session)
    settings = Settings(jwt_secret="eval-auto-test")

    summary = run_synth_pipeline(session, _CLOCK, settings, llm=_StubLLM())

    assert summary["enabled"] is False
    assert session.scalars(select(HardcaseSeed)).all() == []
    assert session.scalars(select(AuditLog).where(AuditLog.action == "eval.autosynth")).all() == []


def test_总闸开跑通全流水(session) -> None:
    """② 总闸开：采集 → 合成全流水，摘要含 mined/synthesized，审计 trigger=weekly。"""
    _make_refuse_pair(session)
    settings = Settings(jwt_secret="eval-auto-test")
    save_eval_auto_config(
        session,
        _CLOCK,
        actor_id=_admin_id(session),
        enabled=True,
        synth_per_seed=2,
        doc_sample_per_dept=5,
        weekly_max_questions=50,
        regression_pp=5.0,
    )
    session.commit()

    summary = run_synth_pipeline(session, _CLOCK, settings, llm=_StubLLM())

    assert summary["enabled"] is True
    assert summary["mined"] == 1  # 拒答消息入池
    assert summary["noGround"] == 0  # 拒答种子采集时已直接落 no_ground，合成阶段无 pending
    assert summary["synthesized"] == 0
    seed = session.scalars(select(HardcaseSeed)).one()
    assert seed.status == "no_ground"
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "eval.autosynth")).one()
    assert audit.detail["trigger"] == "weekly"
    assert audit.detail["mined"] == 1


def test_配置读写与回落缺省(session) -> None:
    """③ load 行缺失回落 Settings 缺省（enabled=False）；save 落行 + 审计前后值。"""
    settings = Settings(jwt_secret="eval-auto-test")
    config = load_eval_auto_config(session, settings)
    assert config.enabled is False
    assert config.synth_per_seed == 2  # Settings 缺省

    save_eval_auto_config(
        session,
        _CLOCK,
        actor_id=_admin_id(session),
        enabled=True,
        synth_per_seed=3,
        doc_sample_per_dept=10,
        weekly_max_questions=80,
        regression_pp=3.5,
    )
    config = load_eval_auto_config(session, settings)
    assert config.enabled is True
    assert config.synth_per_seed == 3
    assert config.weekly_max_questions == 80
    assert config.regression_pp == 3.5
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "eval.auto_config")).one()
    assert audit.detail["before"]["enabled"] is False
    assert audit.detail["after"]["synthPerSeed"] == 3
