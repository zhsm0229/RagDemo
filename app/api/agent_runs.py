r"""AG-UI agent 流式端点（F-212，工单 #07）：POST /api/agent/runs —— 薄控制器 + 请求阶段落库时序。

时序（对齐 /api/chat/stream 先例）：①参数校验（taskType 白名单 + input strip 后 1-2000，
422/1003 字段级）→ ②每日限额（复用问答 DailyCounter 同实例同额度，429/3002 未开流普通
JSON）→ ③事务：agent_runs 占位行（status=running、input 快照）flush 发号 + commit 后开流
→ ④StreamingResponse（text/event-stream）。

SSE 线格式照搬 chat 先例（`event: <事件名>\ndata: <JSON>\n\n`，事件名 = AG-UI EventType，
载荷键名随 ag-ui 官方 snake_case，见 services/agent_runs 模块说明）；Cache-Control: no-cache。
归属说明：本端点无按 ID 寻址的既有资源（run 恒归属当前调用者，user_id/dept_id 取自鉴权
身份），无 IDOR 面；部门上下文 dept_ids=[本人部门, 全员部门] 由 RunContext 整体继承
（services/agent_runs 桥接层强制）。事件编排与收尾落库在 services/agent_runs（生成器自管
短会话，绝不横跨整流），本模块只做线格式化（presentation 关切）。
"""

import json
from collections.abc import Iterator
from time import perf_counter

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import DbSession, ReadyUser
from app.core.errors import ERR_DAILY_LIMITED, ApiError
from app.core.tools import RunContext
from app.db.models import AgentRun
from app.services.agent_runs import AgentDeps, event_stream
from app.services.departments import all_dept_id

router = APIRouter(prefix="/agent", tags=["agent"])

_INPUT_MAX_CHARS = 2000  # §21.5：任务输入 strip 后 1-2000 字（与问答问题同口径）
_MSG_DAILY_LIMITED = "今日提问次数已达上限，请明天再试"
# 任务白名单即评审点（档案见 services/agent_tasks.TASK_PROFILES，校验与编排同源）：
# kb_qa=#07 知识库问答；template_draft=#06 模板草稿生成（检索 few-shot → 起草 → save_draft
# 落草稿待审核）。未放开 taskType 一律 422 拒绝，防"任意类型都跑同一个最小 agent"的口径漂移。
_TASK_TYPES = ("kb_qa", "template_draft")


class AgentRunRequest(BaseModel):
    """POST /api/agent/runs 请求体（camelCase，§3.4）：taskType 任务类型 + input 任务输入。"""

    taskType: str
    input: str

    @field_validator("taskType")
    @classmethod
    def _check_task_type(cls, value: str) -> str:
        """白名单校验（strip 后精确匹配）：未知类型拒绝，不给"任意任务同一骨架"的误用空间。"""
        stripped = value.strip()
        if stripped not in _TASK_TYPES:
            raise ValueError("不支持的任务类型")
        return stripped

    @field_validator("input")
    @classmethod
    def _strip_and_check_length(cls, value: str) -> str:
        """strip 后 1-2000 字（§21.5 字段级 422/1003）：全空白等价空输入一并拒绝。"""
        stripped = value.strip()
        if not 1 <= len(stripped) <= _INPUT_MAX_CHARS:
            raise ValueError("任务输入长度须在 1-2000 字之间")
        return stripped


def _format_stream(events: Iterator[tuple]) -> Iterator[str]:
    """SSE 事件线格式（chat 同款）：ensure_ascii=False 保中文原样，事件间空行分隔。"""
    for name, payload in events:
        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/runs")
def create_agent_run(
    body: AgentRunRequest, user: ReadyUser, db: DbSession, request: Request
) -> StreamingResponse:
    """agent 任务流式端点：请求阶段完成限额与占位落库（brief 第 1-3 步），生成器只管流与收尾。"""
    deps: AgentDeps = request.app.state.agent_deps
    if not deps.counter.try_consume(user.id, deps.settings.daily_question_limit):  # 每请求恰一次
        raise ApiError(ERR_DAILY_LIMITED, _MSG_DAILY_LIMITED, 429)
    ctx = RunContext(
        user_id=user.id,
        username=user.username,
        dept_ids=(user.dept_id, all_dept_id(db)),  # 部门红线：恒 [本人部门, 全员部门]（F-502）
    )
    run = AgentRun(
        user_id=user.id,
        dept_id=user.dept_id,
        run_type=body.taskType,
        input={"taskType": body.taskType, "input": body.input},
    )
    db.add(run)
    db.commit()  # flush 取发号 id；提交后占位行（running）对生成器短会话可见
    return StreamingResponse(
        _format_stream(event_stream(deps, run.id, ctx, body.taskType, body.input, perf_counter())),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
