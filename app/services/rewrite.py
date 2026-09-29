r"""多轮问题改写（Task 8 编排的前置件）：把追问改写为可独立理解的完整问题。

- 流消费约定（task-8 强制交接②）：真实现的 ``chat_stream`` 是生成器函数，连接失败
  在首次 ``next()`` 才抛——本函数把"取迭代器 + 迭代收集"整体包进同一个
  ``UpstreamError`` 异常边界，任一时刻失败都降级返回原问题（CHAT-08），绝不打断
  问答编排；调用方拿到的返回值恒为非空字符串。
- system 提示词由调用方注入（deps.prompts.rewrite_system，prompts/ 目录单一真相源，
  §17.1/§17.4），本模块不读文件、不内嵌提示词副本；历史轮次条数由调用方（编排层）
  截断后再传入，本函数只做 Task 6 消息组装与流消费。
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from app.core.llm import UpstreamError
from app.rag.prompt import build_rewrite_messages

# 改写产物是一句独立完整的问题，256 token 封顶足够；不进 Settings（避免散落配置项，
# 答案生成的上限仍由生成侧按 settings.chat_max_tokens clamp）
REWRITE_MAX_TOKENS: int = 256


class _ChatStream(Protocol):
    """rewrite 所需的最小 LLM 面（与 core.llm.LLMProvider 同构，收窄依赖）。"""

    def chat_stream(
        self, messages: Sequence[Mapping[str, str]], max_tokens: int
    ) -> Iterator[str]: ...


def rewrite_question(
    llm: _ChatStream, history: list[tuple[str, str]], question: str, *, system: str
) -> str:
    """把追问改写成独立完整问题；上游故障或空产物一律降级返回原问题（CHAT-08）。

    system 为必填关键字参数：单一真相源在 prompts/rewrite_system.txt，经 deps 传入，
    本模块绝不内嵌副本。history 为 (role, content) 按时间升序、已由调用方截断。
    """
    messages = build_rewrite_messages(system, history, question)
    try:
        # 迭代在推导式内完成、仍在异常边界里：首次 next() 才可能抛（生成器形态）
        pieces: list[str] = [piece for piece in llm.chat_stream(messages, REWRITE_MAX_TOKENS)]
    except UpstreamError:
        return question
    rewritten = "".join(pieces).strip()
    return rewritten if rewritten else question
