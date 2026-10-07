r"""agent_steps 逐步落库（F-211 审计面，工单 #06）：每次工具调用一行，随行一次写入。

红线（specs §四.5"逐工具落实而非事后补" + §6.1"前端事件序 = 库内轨迹"）：落库时机
= 工具观察（ToolMessage）到达、该步收口事件发出之前——DB 真相先于线通知；行只增
不改（无 updated_at），故不做"先占位后回填"。截断上限（db-design §六.17"防膨胀"）：
args 超 _ARGS_MAX_CHARS 存 preview 形态、observation 超 _OBS_MAX_CHARS 截断并标注，
两上限都远大于单工具观察的真实需要、小于上下文炸裂的量级。落库失败不吞也不拖垮
任务：记日志后继续（轨迹缺一行可从服务端日志补查，agent 主链路不该因审计面抖动
而失败——与 memory_extract 静默降级同哲学）。

连接红线（chat_stream/agent_runs 同款）：每行独立 SessionLocal 短会话提交，绝不
横跨整流占住池中连接。测试经 monkeypatch 把本模块 SessionLocal 指向测试连接
（tests/api/test_agent_template_draft.py 等）。
"""

import json
import logging
from typing import Any

from app.core.clock import get_clock
from app.db.base import SessionLocal
from app.db.models import AgentStep

logger = logging.getLogger(__name__)

_ARGS_MAX_CHARS = 2000  # 工具参数快照上限（超限存 preview 形态，JSONB 内仍合法）
_OBS_MAX_CHARS = 2000  # 工具观察快照上限（截断处加标注，回放可辨）
_TRUNCATED_SUFFIX = "…[已截断]"
_STEP_TYPE_TOOL = "tool"  # 一期只写 tool；llm/planning 预留（0009 CHECK 口径）


def record_tool_step(
    run_id: int,
    seq: int,
    tool_name: str,
    args: dict[str, Any],
    observation: str,
    error: str | None,
    latency_ms: int | None,
) -> None:
    """写一行工具轨迹：独立短会话提交；失败记日志不拖垮 agent 主链路（模块 docstring）。"""
    now = get_clock().now()
    try:
        with SessionLocal() as db:
            db.add(
                AgentStep(
                    run_id=run_id,
                    seq=seq,
                    step_type=_STEP_TYPE_TOOL,
                    tool_name=tool_name,
                    args=_condense_args(args),
                    observation=_truncate(observation, _OBS_MAX_CHARS),
                    error=error,
                    latency_ms=latency_ms,
                    created_at=now,
                )
            )
            db.commit()
    except Exception:
        logger.exception("agent_steps 落库失败 run_id=%s seq=%s tool=%s", run_id, seq, tool_name)


def extract_tool_error(observation: str, status: str | None) -> str | None:
    """从工具观察提取失败原因：优先 LangChain ToolMessage.status='error'（工具抛异常），
    其次解析桥接层约定的 {"error": 中文原因} 形态（ToolResult.fail 的观察）；都不是即成功。
    提取出的原因即落库值（桥接层已脱敏，不向库内引入堆栈细节）。"""
    if status == "error":
        return _truncate(observation, _OBS_MAX_CHARS)
    try:
        payload = json.loads(observation)
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


def _truncate(text: str, limit: int) -> str:
    """超长截断并标注（回放可辨"此处非全文"），不静默丢尾。"""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED_SUFFIX


def _condense_args(args: dict[str, Any]) -> dict[str, Any]:
    """args 快照：JSON 序列化不超限原样存；超限存 preview 形态（JSONB 内仍合法可查）。"""
    dumped = json.dumps(args, ensure_ascii=False)
    if len(dumped) <= _ARGS_MAX_CHARS:
        return args
    return {"truncated": True, "preview": dumped[:_ARGS_MAX_CHARS]}
