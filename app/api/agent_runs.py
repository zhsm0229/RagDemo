r"""AG-UI agent 流式端点（F-212，工单 #07）：POST /api/agent/runs —— 薄控制器 + 请求阶段落库时序。

时序（对齐 /api/chat/stream 先例）：①参数校验（taskType 白名单 + input strip 后 1-2000，
422/1003 字段级）→ ②并发上限（同用户 running≥2 拒新，A3，429 未开流普通 JSON）→
③每日限额（复用问答 DailyCounter 同实例同额度，429/3002 未开流普通 JSON）→ ④事务：
agent_runs 占位行（status=running、input 快照）flush 发号 + commit 后开流 →
⑤StreamingResponse（text/event-stream）。

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
from sqlalchemy import func, select

from app.api.deps import DbSession, ReadyUser
from app.core.errors import ERR_DAILY_LIMITED, ERR_RATE_LIMITED, ApiError
from app.core.tools import RunContext
from app.db.models import AgentRun
from app.services.agent_runs import AgentDeps, event_stream
from app.services.agent_tasks import TaskType
from app.services.departments import all_dept_id

router = APIRouter(prefix="/agent", tags=["agent"])

_INPUT_MAX_CHARS = 2000  # §21.5：任务输入 strip 后 1-2000 字（与问答问题同口径）
_MSG_DAILY_LIMITED = "今日提问次数已达上限，请明天再试"
# A3 并发上限：同用户同时至多 2 个 running run（占用生成通道与配额的长任务，放任并发
# 会挤占问答主链路）。错误码取共享枚举限流语义最近的 ERR_RATE_LIMITED（429 族；
# ERR_DAILY_LIMITED 专指每日额度，语义不合），消息直述等待/停止二选一的自助路径。
_MAX_CONCURRENT_RUNS = 2
_MSG_CONCURRENT_LIMITED = "已有生成任务进行中，请等待完成或停止后再试"


class AgentRunRequest(BaseModel):
    """POST /api/agent/runs 请求体（camelCase，§3.4）：taskType 任务类型 + input 任务输入。

    taskType 用 TaskType Literal（B5）：白名单即评审点（档案见 services/agent_tasks
    .TASK_PROFILES，校验与编排同源动态派生，OpenAPI 自动暴露 enum），未知类型 422 且
    话术自然列出合法值；注意与旧 str+validator 版本的差别是不再 strip——空白包裹的
    类型名按非法拒绝（类型名是机器标识，宽松无益）。
    """

    taskType: TaskType
    input: str

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
    """agent 任务流式端点：请求阶段完成并发上限、限额与占位落库（brief 第 1-3 步），
    生成器只管流与收尾。"""
    deps: AgentDeps = request.app.state.agent_deps
    running = db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.user_id == user.id, AgentRun.status == "running")
    )
    # A3：占位行落库前判并发（请求会话内单语句计数，占位行自身尚未插入不存在自计）；
    # 置于每日限额消费之前——被拒请求未消费任何服务，不烧额度。
    if (running or 0) >= _MAX_CONCURRENT_RUNS:
        raise ApiError(ERR_RATE_LIMITED, _MSG_CONCURRENT_LIMITED, 429)
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
