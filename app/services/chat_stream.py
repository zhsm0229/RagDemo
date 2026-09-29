r"""问答 SSE 事件流编排（Task 9，api 薄层拆出的 service 函数）：四事件产出与收尾落库。

事件顺序与 DTO 字段逐字按 standards §3.6：citations 恒先于 token（空列表也发）→ 按
计划四分支 → done/error 收尾。生成器自管短会话（本模块 SessionLocal，with 块每阶段
独立事务）——绝不复用请求级会话横跨整个流（连接占用 60s 红线）；测试经 monkeypatch
把本模块 SessionLocal 指向测试连接（tests/api/conftest.py chat_client）。

收尾语义：cache 计划整段下发、不再调 LLM 也不再 put 缓存（cache_hit=true 落库，
answer_type/model 取缓存条目原值）；refuse 逐字 REFUSAL_TEXT、不写缓存（F-1010 保真
拒答案频）；qa/rag 逐段下发，正常耗尽 finalize + 缓存 put（强制移交②：仅 qa/rag 且
流正常结束，条目契约见 services/chat.py）→ done。UpstreamError 与意外异常均
mark_interrupted（CHAT-12 只置 is_active=false 不重复落库）→ error 事件——响应头已发
FastAPI 兜底不可达，只能经事件表达；UpstreamError logger.warning、意外异常
logger.exception 均留堆栈（§16.4），error 文案对外脱敏不透传内部细节（§6.8）。
"""

import logging
from collections.abc import Iterator
from time import perf_counter

from app.core.config import Settings
from app.core.errors import ERR_INTERNAL, ERR_LLM_UNAVAILABLE
from app.core.llm import UpstreamError
from app.db.base import SessionLocal
from app.rag.prompt import REFUSAL_TEXT
from app.services.chat import ChatDeps, TurnPlan, finalize_turn, mark_interrupted

logger = logging.getLogger(__name__)

_STUB_MODEL = "stub"  # settings.llm_model 为空（test_mode Stub 无模型名）时的落库回退名
_MSG_LLM_UNAVAILABLE = "生成服务暂时不可用，请重试"  # §6.8 对外脱敏话术
_MSG_INTERRUPTED = "系统繁忙，请稍后重试"


def _elapsed_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def _model_name(settings: Settings) -> str:
    return settings.llm_model or _STUB_MODEL


def _done_payload(
    assistant_msg_id: int,
    *,
    refused: bool,
    cache_hit: bool,
    usage: tuple[int, int] | None,
    duration_ms: int,
) -> dict:
    """done 事件 DTO（§3.6 逐字）：无 usage（拒答/缓存命中/流断未耗尽）tokens 记 0。"""
    prompt_tokens, completion_tokens = usage if usage is not None else (0, 0)
    return {
        "messageId": assistant_msg_id,
        "refused": refused,
        "cacheHit": cache_hit,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "durationMs": duration_ms,
    }


def _error_payload(code: int, message: str) -> dict:
    """error 事件 DTO（§3.6）：code 取共享错误码枚举，message 中文脱敏；重试语义跟着
    错误码走——5001（上游故障）可重试，5000（兜底意外）不可重试，调用方不散落布尔。"""
    return {"code": code, "message": message, "retryable": code == ERR_LLM_UNAVAILABLE}


def _finalize(
    assistant_msg_id: int,
    *,
    content: str,
    citations: list[dict],
    answer_type: str,
    model: str,
    usage: tuple[int, int] | None,
    latency_ms: int,
    cache_hit: bool,
) -> None:
    """流式收尾（正常路径）：短会话 finalize_turn（纯 UPDATE）+ 独立事务提交。"""
    with SessionLocal() as db:
        finalize_turn(
            db,
            assistant_msg_id,
            content=content,
            citations=citations,
            answer_type=answer_type,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
        )
        db.commit()


def interrupt_turn(assistant_msg_id: int) -> None:
    """中断收尾（CHAT-12）：短会话 mark_interrupted（is_active=false）+ 独立提交；
    只置失败标记，不重复落库，重试由前端重新 POST 产生新消息对。流中断裂与 prepare
    阶段上游故障（api 层）共用本入口，保证两条路径的失败标记语义一致。"""
    with SessionLocal() as db:
        mark_interrupted(db, assistant_msg_id)
        db.commit()


def _stream_cache_hit(plan: TurnPlan, assistant_msg_id: int, start: float) -> Iterator[tuple]:
    """cache 分支：token 整段一次 → finalize（cache_hit=true）commit → done
    {refused:false, cacheHit:true, tokens 0}。不调 LLM、不 put 缓存（条目已在缓存）。"""
    entry = plan.cache_entry
    yield "token", {"messageId": assistant_msg_id, "content": entry["answer"]}
    duration_ms = _elapsed_ms(start)
    _finalize(
        assistant_msg_id,
        content=entry["answer"],
        citations=plan.citations,
        answer_type=entry["answer_type"],
        model=entry["model"],
        usage=None,
        latency_ms=duration_ms,
        cache_hit=True,
    )
    yield (
        "done",
        _done_payload(
            assistant_msg_id, refused=False, cache_hit=True, usage=None, duration_ms=duration_ms
        ),
    )


def _stream_refuse(settings: Settings, assistant_msg_id: int, start: float) -> Iterator[tuple]:
    """refuse 分支：token 逐字 REFUSAL_TEXT → finalize(answer_type='refuse') → done
    {refused:true, cacheHit:false, tokens 0}（拒答不写缓存）。"""
    yield "token", {"messageId": assistant_msg_id, "content": REFUSAL_TEXT}
    duration_ms = _elapsed_ms(start)
    _finalize(
        assistant_msg_id,
        content=REFUSAL_TEXT,
        citations=[],
        answer_type="refuse",
        model=_model_name(settings),
        usage=None,
        latency_ms=duration_ms,
        cache_hit=False,
    )
    yield (
        "done",
        _done_payload(
            assistant_msg_id, refused=True, cache_hit=False, usage=None, duration_ms=duration_ms
        ),
    )


def _finish_generated(
    deps: ChatDeps, plan: TurnPlan, assistant_msg_id: int, content: str, start: float
) -> dict:
    """生成正常耗尽收尾：finalize(commit) + 缓存 put（finalize 之后执行，强制移交②），
    返回 done DTO（usage 取 provider.last_usage，缺席记双 0）。"""
    duration_ms = _elapsed_ms(start)
    usage = getattr(deps.llm, "last_usage", None)
    model = _model_name(deps.settings)
    _finalize(
        assistant_msg_id,
        content=content,
        citations=plan.citations,
        answer_type=plan.kind,
        model=model,
        usage=usage,
        latency_ms=duration_ms,
        cache_hit=False,
    )
    deps.cache.put(
        plan.cache_key,
        {"answer": content, "citations": plan.citations, "answer_type": plan.kind, "model": model},
    )
    return _done_payload(
        assistant_msg_id, refused=False, cache_hit=False, usage=usage, duration_ms=duration_ms
    )


def _stream_generate(
    deps: ChatDeps, plan: TurnPlan, assistant_msg_id: int, start: float
) -> Iterator[tuple]:
    """qa/rag 分支：逐段 token → 正常耗尽走 _finish_generated；UpstreamError 与意外
    异常均 mark_interrupted 收尾后发 error 事件（都只置 is_active=false 不重复落库）。"""
    pieces: list[str] = []
    try:
        # 取迭代器与迭代包进同一异常边界（Task 4 交接：生成器形态首次 next() 才抛）
        for piece in deps.llm.chat_stream(plan.llm_messages, deps.settings.chat_max_tokens):
            pieces.append(piece)
            yield "token", {"messageId": assistant_msg_id, "content": piece}
    except UpstreamError:
        # 上游故障也要留观测痕（§16.4）：不上抛（响应头已发只能经 error 事件表达），
        # warning + 堆栈定位断流根因，与意外异常的 logger.exception 区分严重级。
        logger.warning(
            "流式生成中断（上游故障）assistant_message_id=%s", assistant_msg_id, exc_info=True
        )
        interrupt_turn(assistant_msg_id)
        yield "error", _error_payload(ERR_LLM_UNAVAILABLE, _MSG_LLM_UNAVAILABLE)
        return
    except Exception:
        logger.exception("问答流式生成意外异常 assistant_message_id=%s", assistant_msg_id)
        interrupt_turn(assistant_msg_id)
        yield "error", _error_payload(ERR_INTERNAL, _MSG_INTERRUPTED)
        return
    yield "done", _finish_generated(deps, plan, assistant_msg_id, "".join(pieces), start)


def event_stream(
    deps: ChatDeps, plan: TurnPlan, assistant_msg_id: int, start: float
) -> Iterator[tuple]:
    """SSE 事件编排入口：citations 恒先于 token（空列表也发），再按计划四分支收尾。

    产出 (事件名, payload) 二元组，SSE 线格式化归 api 层（presentation 关切）；start
    为请求阶段 perf_counter 起点（durationMs 口径：自请求处理起算到收尾完成）。
    """
    yield "citations", {"messageId": assistant_msg_id, "items": plan.citations}
    if plan.kind == "cache":
        yield from _stream_cache_hit(plan, assistant_msg_id, start)
    elif plan.kind == "refuse":
        yield from _stream_refuse(deps.settings, assistant_msg_id, start)
    else:  # qa / rag：逐段生成
        yield from _stream_generate(deps, plan, assistant_msg_id, start)
