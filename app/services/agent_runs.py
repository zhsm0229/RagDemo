r"""Agent 运行服务（F-212，工单 #07）：POST /api/agent/runs 的 AG-UI 事件流编排与收尾落库。

事件协议（specs §6.1"不另造事件方言"）：事件对象一律取 ag-ui-protocol 官方类型构造，
线载荷为其 model_dump（键名随 AG-UI 官方 snake_case，**不套本仓 camelCase 惯例**——
协议键名是方言本身，改名即破坏互操作）；序列产组：RUN_STARTED →（STEP_STARTED →
TOOL_CALL_START/ARGS/END → STEP_FINISHED）×N → TEXT_MESSAGE_START/CONTENT/END →
RUN_FINISHED，失败走 RUN_ERROR。STATE_* 一期不做（specs §6.1 以 SNAPSHOT 为主、
禁清单纪律到线再评审）。

DeepAgents 骨架：create_deep_agent（系统提示与工具集按任务档案取 agent_tasks，工单 #06
任务白名单同源）+ 注册表工具经 LangChain 闭包桥接（services/agent_bridge，dept 红线
收口处）；RunContext 整体继承调用者身份与 dept_ids=[本人部门, 全员部门]。stream_mode=
"updates" 的节点增量即映射源：model 节点 AIMessage.tool_calls → 工具事件组（一步可含
多次调用），tools 节点 ToolMessage → 逐调用落 agent_steps（services/agent_steps，迁移
0009；DB 真相先于 STEP_FINISHED 收口事件）并收步，无 tool_calls 的 AIMessage 正文 →
文本事件组。

连接红线（chat_stream 同款）：生成器自管短会话——工具每次 invoke 与收尾落库各自 with
SessionLocal 独立事务，绝不横跨整流占住池中连接。收尾语义与 chat 对齐：终态先落库
commit 再发收尾事件（DB 真相先于线通知）；上游故障与意外异常 → _fail_run（独立短会话
置 failed + 脱敏中文摘要）→ RUN_ERROR；客户端断连（GeneratorExit）不可再发事件，finally
兜底 _fail_run——任何路径不留 status=running 幽灵行。空正文按上游失败收尾（2026-10-01
事故纪律：推理模型烧空预算，严禁空串当正常产物落库）。

测试经 monkeypatch 把本模块 SessionLocal 指向测试连接（tests/api/test_agent_runs.py
agent_client），使短会话与用例读回同处测试事务；AgentDeps.channel 是可替换缝，测试注入
故障通道走 RUN_ERROR 路径。
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from ag_ui.core.events import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import update

from app.core.clock import get_clock
from app.core.config import Settings
from app.core.errors import ERR_INTERNAL, ERR_LLM_UNAVAILABLE
from app.core.llm import UpstreamError
from app.core.tools import RunContext, ToolRegistry
from app.db.base import SessionLocal
from app.db.models import AgentRun
from app.services.agent_bridge import build_agent_graph
from app.services.agent_steps import extract_tool_error, record_tool_step
from app.services.chat_cache import DailyCounter, RateBucketProto

logger = logging.getLogger(__name__)

_STUB_MODEL = "stub"  # settings 模型名为空（test_mode Stub）时的落库回退名
_MSG_LLM_UNAVAILABLE = "生成服务暂时不可用，请重试"  # §6.8 对外脱敏话术（与问答同口径）
_MSG_INTERRUPTED = "系统繁忙，请稍后重试"


@dataclass
class AgentDeps:
    """Agent 链路依赖包（ChatDeps 同位）：实现选择全在装配层，本模块只消费。

    channel 是可替换缝（AgentModelChannel 协议）：模型按 run 现建，测试换缝即刻生效；
    counter 与问答共用同一 DailyCounter 实例——agent 与问答共享每日额度，换端点不绕限。
    """

    settings: Settings
    registry: ToolRegistry
    channel: Any  # AgentModelChannel（Protocol 不便标注具体类型，Any 承载）
    react_fallback: bool
    counter: DailyCounter
    # bucket（工单 21）：与问答共享同一桶实例（counter 共享先例同款，同用户跨入口
    # 水位一致视图）；缺省 None 兜底接线守卫
    bucket: RateBucketProto | None = None


def _model_name(settings: Settings) -> str:
    return settings.agent_model_model or settings.llm_model or _STUB_MODEL


def _payload(event: BaseEvent) -> tuple[str, dict]:
    """ag-ui 事件 → (事件名, 线载荷)：mode="json" 展开枚举，exclude_none 掉缺席字段。"""
    return event.type.value, event.model_dump(exclude_none=True, mode="json")


# DeepAgents 增量 → AG-UI 事件映射


def _stream_agent_updates(
    deps: AgentDeps,
    ctx: RunContext,
    task_type: str,
    input_text: str,
    thread_id: str,
    run_id: int,
    collected: list[str],
) -> Iterator[tuple[str, dict]]:
    """跑 DeepAgents 并映射增量：一步 = 模型发起一轮工具调用（STEP + TOOL_CALL 组），
    每次工具观察（ToolMessage）先落一行 agent_steps（step_type='tool'，与 TOOL_CALL_*
    事件一一对应，specs §6.1"事件序 = 库内轨迹"）再收口该步；无 tool_calls 的正文 =
    最终回答（TEXT_MESSAGE 组）。收集器 collected 追加正文（生成器不能 return 值，
    列表代传）；空正文由外层守卫。"""
    graph = build_agent_graph(deps, ctx, task_type)
    step_no = 0
    seq_no = 0
    open_step: int | None = None
    open_calls: list[str] = []  # 当前步已发起、未收到观察的 tool_call_id（保序，全收齐才收步）
    pending: dict[str, tuple[str, dict, float]] = {}  # call_id → (工具名, 参数, 发起时刻)
    message_seq = 0
    for node_update in graph.stream(
        {"messages": [("user", input_text)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="updates",
    ):
        for node_payload in node_update.values():
            if not isinstance(node_payload, dict):
                continue  # 中间件占位（值为 None 等）不携带消息
            for message in node_payload.get("messages", []):
                if isinstance(message, ToolMessage):
                    call_id = str(message.tool_call_id)
                    name, args, started = pending.pop(call_id, ("", {}, perf_counter()))
                    open_calls = [cid for cid in open_calls if cid != call_id]
                    seq_no += 1
                    raw = message.content
                    observation = (
                        raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    )
                    # 轨迹先于收口事件（DB 真相先于线通知，收尾同口径）；落库失败由
                    # record_tool_step 记日志降级，不拖垮 agent 主链路
                    record_tool_step(
                        run_id=run_id,
                        seq=seq_no,
                        tool_name=name or str(getattr(message, "name", "") or ""),
                        args=args,
                        observation=observation,
                        error=extract_tool_error(observation, getattr(message, "status", None)),
                        latency_ms=int((perf_counter() - started) * 1000),
                    )
                    if open_step is not None and not open_calls:  # 该步观察全部落定：收口
                        yield _payload(StepFinishedEvent(step_name=f"step-{open_step}"))
                        open_step = None
                elif isinstance(message, AIMessage) and message.tool_calls:
                    step_no += 1
                    open_step = step_no
                    open_calls = [str(call["id"]) for call in message.tool_calls]
                    yield _payload(StepStartedEvent(step_name=f"step-{step_no}"))
                    for call in message.tool_calls:
                        call_id = str(call["id"])
                        pending[call_id] = (str(call["name"]), dict(call["args"]), perf_counter())
                        yield _payload(
                            ToolCallStartEvent(
                                tool_call_id=call_id, tool_call_name=str(call["name"])
                            )
                        )
                        yield _payload(
                            ToolCallArgsEvent(
                                tool_call_id=call_id,
                                delta=json.dumps(call["args"], ensure_ascii=False),
                            )
                        )
                        yield _payload(ToolCallEndEvent(tool_call_id=call_id))
                elif isinstance(message, AIMessage) and isinstance(message.content, str):
                    if not message.content.strip():
                        continue  # 无正文中间帧（如纯空回复）：不发文本组
                    message_seq += 1
                    message_id = f"msg-{thread_id}-{message_seq}"
                    collected.append(message.content)
                    yield _payload(TextMessageStartEvent(message_id=message_id, role="assistant"))
                    yield _payload(
                        TextMessageContentEvent(message_id=message_id, delta=message.content)
                    )
                    yield _payload(TextMessageEndEvent(message_id=message_id))
    if open_step is not None:  # 流自然耗尽仍有未收步（异常路径外 Rare 兜底）：收口
        yield _payload(StepFinishedEvent(step_name=f"step-{open_step}"))


# 收尾落库（各自独立短会话，commit 后才发对应收尾事件——DB 真相先于线通知）


def _finalize_run(deps: AgentDeps, run_id: int, answer: str, start: float) -> None:
    """正常收尾：status=succeeded + 产物/模型/耗时回填（纯 UPDATE + 独立提交）。"""
    now = get_clock().now()
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                status="succeeded",
                output={"text": answer},
                model=_model_name(deps.settings),
                latency_ms=int((perf_counter() - start) * 1000),
                finished_at=now,
                updated_at=now,
            )
        )
        db.commit()


def _fail_run(run_id: int, summary: str) -> None:
    """失败/中断收尾（mark_interrupted 等价物）：置 failed + 脱敏中文摘要（技术细节只进
    服务端日志）；独立短会话提交，上游故障/意外异常/客户端断连三条路共用，保证不留
    running 幽灵行。重试语义 = 前端重新 POST 产生新 run。"""
    now = get_clock().now()
    with SessionLocal() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(status="failed", error=summary, finished_at=now, updated_at=now)
        )
        db.commit()


def _run_error_event(code: int, message: str) -> tuple[str, dict]:
    """RUN_ERROR 载荷：code 取共享错误码枚举（AG-UI 为字符串型），message 对外脱敏。"""
    return _payload(RunErrorEvent(message=message, code=str(code)))


# 事件流编排入口（api 层只做 SSE 线格式化，presentation 关切）


def event_stream(
    deps: AgentDeps, run_id: int, ctx: RunContext, task_type: str, input_text: str, start: float
) -> Iterator[tuple[str, dict]]:
    """AG-UI 事件编排入口：RUN_STARTED → agent 步骤/工具/文本事件 → RUN_FINISHED；
    系统提示与工具集按任务档案取（agent_tasks，taskType 白名单同源）；
    上游故障与意外异常 → _fail_run 后 RUN_ERROR（可重试性跟错误码，chat 同口径）；
    finally 兜底断连清扫——finalized 未置即有未达终态的退出（GeneratorExit 等），
    此时已不可再发事件，只补 failed 不留幽灵。"""
    thread_id = f"agent-run-{run_id}"
    collected: list[str] = []
    finalized = False
    try:
        yield _payload(RunStartedEvent(thread_id=thread_id, run_id=str(run_id)))
        try:
            yield from _stream_agent_updates(
                deps, ctx, task_type, input_text, thread_id, run_id, collected
            )
        except UpstreamError:
            logger.warning("agent 运行中断（上游故障）run_id=%s", run_id, exc_info=True)
            _fail_run(run_id, _MSG_LLM_UNAVAILABLE)
            finalized = True
            yield _run_error_event(ERR_LLM_UNAVAILABLE, _MSG_LLM_UNAVAILABLE)
            return
        except Exception:
            logger.exception("agent 运行意外异常 run_id=%s", run_id)
            _fail_run(run_id, _MSG_INTERRUPTED)
            finalized = True
            yield _run_error_event(ERR_INTERNAL, _MSG_INTERRUPTED)
            return
        answer = "".join(collected).strip()
        if not answer:
            # 空正文防护（2026-10-01 事故纪律）：流"正常耗尽"但零正文按上游失败收尾，
            # 严禁把空串当正常产物落库（拒答红线与落库正确性双前提）。
            logger.warning("agent 运行正常耗尽但零正文 run_id=%s", run_id)
            _fail_run(run_id, _MSG_LLM_UNAVAILABLE)
            finalized = True
            yield _run_error_event(ERR_LLM_UNAVAILABLE, _MSG_LLM_UNAVAILABLE)
            return
        _finalize_run(deps, run_id, answer, start)
        finalized = True
        yield _payload(
            RunFinishedEvent(thread_id=thread_id, run_id=str(run_id), result={"text": answer})
        )
    finally:
        if not finalized:
            try:
                _fail_run(run_id, _MSG_INTERRUPTED)
            except Exception:  # 清扫失败不吞观测痕，也不掩盖 GeneratorExit
                logger.exception("agent 运行断连清扫失败 run_id=%s", run_id)
