r"""多轮问题改写单元测试（Task 8 前置件，无 DB）：rewrite_question 消费 LLM 流并降级。

约定锚点（task-8 强制交接②）：真实现 chat_stream 是生成器函数，连接失败在首次
next() 才抛——rewrite 必须把"取迭代器 + 迭代收集"包进同一 UpstreamError 异常边界，
替身以生成器函数形态复刻该形态（raise 先于 yield），任一位置逃逸即测试红。
降级契约（CHAT-08）：上游故障或产物 strip 后为空，一律返回原问题继续编排；
system 提示词由调用方注入（prompts/rewrite_system.txt 单一真相源，§17.1），
max_tokens 为模块常量（改写产物是一句独立问题，不进 Settings 散落配置项）。
"""

from collections.abc import Iterator, Mapping, Sequence

from app.core.llm import StubLLMProvider, UpstreamError
from app.services.rewrite import REWRITE_MAX_TOKENS, rewrite_question

_HISTORY: list[tuple[str, str]] = [
    ("user", "公司的年假制度是什么"),
    ("assistant", "年假按工龄五到十天 [1]"),
]
_QUESTION = "那病假呢"
_SYSTEM = "你是问题改写器。"


class _RecordingLLM:
    """替身：记录 (messages, max_tokens) 后按注入片段产出，供消息形状断言。"""

    def __init__(self, pieces: Sequence[str]) -> None:
        self.pieces = tuple(pieces)
        self.calls: list[tuple[list[dict[str, str]], int]] = []

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        self.calls.append(([dict(message) for message in messages], max_tokens))
        yield from self.pieces


class _GeneratorFailureLLM:
    """生成器函数形态替身：调用不抛、首次 next() 才抛（复刻真实现连接失败形态）。"""

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        raise UpstreamError("连接失败")
        yield ""  # 不可达：仅为把本函数标记为生成器函数（真实现同构）


def test_rewrite_joins_stream_strips_and_sends_rewrite_messages() -> None:
    """流片段拼接 + strip 为改写结果；消息形状 = Task 6 组装（system 透传、问题收尾）。"""
    llm = _RecordingLLM(["  改写后", "的问题  "])

    assert rewrite_question(llm, _HISTORY, _QUESTION, system=_SYSTEM) == "改写后的问题"

    ((messages, max_tokens),) = llm.calls
    assert max_tokens == REWRITE_MAX_TOKENS
    assert messages == [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": "[对话]\n用户：公司的年假制度是什么\n助手：年假按工龄五到十天 [1]\n[当前问题]\n那病假呢",
        },
    ]


def test_rewrite_degrades_to_original_when_stub_fails_before_streaming() -> None:
    """Stub fail_times=1：调用即抛（未产出段），降级返回原问题（CHAT-08）且不外泄异常。"""
    llm = StubLLMProvider(chunks=("改写后的问题",), fail_times=1)

    assert rewrite_question(llm, _HISTORY, _QUESTION, system=_SYSTEM) == _QUESTION
    assert llm.stream_calls == 1  # 调用发生了，故障被异常边界吞掉


def test_rewrite_degrades_when_generator_function_fails_on_first_next() -> None:
    """生成器函数形态故障（首 next() 才抛）同样降级——锁定"异常边界包住整个迭代"。"""
    llm = _GeneratorFailureLLM()

    assert rewrite_question(llm, _HISTORY, _QUESTION, system=_SYSTEM) == _QUESTION


def test_rewrite_degrades_to_original_when_output_blank() -> None:
    """模型返回空白（strip 后为空）视同改写失败，返回原问题而非空串污染缓存键。"""
    llm = StubLLMProvider(chunks=("  ", "　"))

    assert rewrite_question(llm, _HISTORY, _QUESTION, system=_SYSTEM) == _QUESTION
