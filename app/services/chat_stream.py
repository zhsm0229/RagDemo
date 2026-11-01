r"""问答 SSE 事件流编排门面（Task 9，api 薄层拆出的 service 函数；工单 24 拆分后
保留既有导入路径）。

本模块只承载编排与守卫：event_stream 做"非空白 token 交付"判定并按工单 24 裁决
执行配额回补；分支实现（cache/refuse/qa·rag 生成器与 finalize/interrupt 收尾、
DTO helpers）在 chat_stream_branches.py（含生成器短会话 SessionLocal 与收尾语义
全文说明）。导入路径不变（chat.py 与测试直调 event_stream/interrupt_turn 均经本
门面 re-export）。
"""

from collections.abc import Iterator

from app.services.chat import ChatDeps, TurnPlan
from app.services.chat_stream_branches import (
    _stream_cache_hit,
    _stream_generate,
    _stream_refuse,
    interrupt_turn,
)

__all__ = ["event_stream", "interrupt_turn"]


def event_stream(
    deps: ChatDeps, plan: TurnPlan, assistant_msg_id: int, start: float, user_id: int
) -> Iterator[tuple]:
    """SSE 事件编排入口：citations 恒先于 token（空列表也发），再按计划四分支收尾。

    产出 (事件名, payload) 二元组，SSE 线格式化归 api 层（presentation 关切）；start
    为请求阶段 perf_counter 起点（durationMs 口径：自请求处理起算到收尾完成）。
    配额回补守卫（工单 24，口径已裁决）：至收尾仍未交付任何**非空白** token 内容
    （首 token 前断连 / 上游故障 / 空正文）→ release 回补该轮 1 次日配额——重试不双扣；
    已交付内容后中断照计。评测专用 deps 不经本入口，不受影响。
    """
    emitted = False
    released = False

    def _release_once() -> None:
        nonlocal released
        if not released and deps.counter is not None:
            released = True
            deps.counter.release(user_id)

    try:
        for event in _dispatch(deps, plan, assistant_msg_id, start):
            if event[0] == "token" and str(event[1].get("content", "")).strip():
                emitted = True
            yield event
    except GeneratorExit:
        if not emitted:
            _release_once()
        raise
    finally:
        if not emitted:  # 正常收尾但零内容交付（上游故障/空正文 error 收尾）同回补
            _release_once()


def _dispatch(
    deps: ChatDeps, plan: TurnPlan, assistant_msg_id: int, start: float
) -> Iterator[tuple]:
    yield "citations", {"messageId": assistant_msg_id, "items": plan.citations}
    if plan.kind == "cache":
        yield from _stream_cache_hit(plan, assistant_msg_id, start)
    elif plan.kind == "refuse":
        yield from _stream_refuse(deps.settings, plan.degraded, assistant_msg_id, start)
    else:  # qa / rag：逐段生成
        yield from _stream_generate(deps, plan, assistant_msg_id, start)
