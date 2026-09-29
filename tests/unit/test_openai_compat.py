r"""OpenAI 兼容 LLM 流式真实现测试（task-4-brief Step 1 用例 ①-⑥）：MockTransport 注入。

断言表来源 task-4-brief：①两 chunk + usage 块 + data: [DONE] → 产出 ["Hello", "世界"]
且 last_usage==(12, 34)；②首块前 500 → UpstreamError 且 broken is False；③产出 1 块
后流截断（无 [DONE] 即结束）→ UpstreamError 且 broken is True、last_usage 保持 None；
④api_key 非空 → Authorization: Bearer <key>，api_key 空 → 无该头（内网 vLLM）；
⑤请求体恰为 model/max_tokens/stream/temperature/messages 原样（role+content）+
stream_options={"include_usage": true}；⑥max_tokens 只透传不钳制（钳制在调用方管道）。
另覆盖：读流中 httpx.ReadError → broken=True、连接失败 → broken=False、非法 JSON
数据块与 role-only 空内容块、bool 型 usage token（type is int 排除）、Protocol 兼容。
"""

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from app.core.llm import LLMProvider, UpstreamError
from app.core.openai_compat import OpenAICompatLLMProvider

BASE_URL = "http://llm.internal:8000/v1"
_MODEL = "glm-4-flash"
_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "介绍一下企业智能工作台"}]
_SSE_HEADERS = {"content-type": "text/event-stream"}


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    api_key: str = "sk-test-key",
    **kwargs: object,
) -> OpenAICompatLLMProvider:
    return OpenAICompatLLMProvider(
        base_url=BASE_URL,
        api_key=api_key,
        model=_MODEL,
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


def _sse(*events: str) -> bytes:
    """SSE 响应体：每个事件一行 `data: <event>` + 空行（与 vLLM/GLM 兼容层形状一致）。"""
    return "".join(f"data: {event}\n\n" for event in events).encode("utf-8")


def _chunk(content: str) -> str:
    """标准流式数据块：{"choices":[{"delta":{"content": ...}}]}。"""
    return json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)


def _usage_block(prompt: int, completion: int) -> str:
    """usage 块：stream_options.include_usage 时 vLLM/GLM 在 [DONE] 前追加的无 choices 块。"""
    return json.dumps(
        {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}, "choices": []}
    )


def _collect(iterator: Iterator[str], collected: list[str]) -> None:
    """持续取迭代器直至抛错；产出逐段追加进 collected（断裂前的部分内容仍可断言）。"""
    while True:
        collected.append(next(iterator))


# ---------------------------------------------------------------------------
# 用例 ①：两 chunk + usage 块 + data: [DONE] → 逐段产出，last_usage==(12, 34)
# ---------------------------------------------------------------------------


def test_two_chunks_usage_block_then_done_yields_and_sets_last_usage() -> None:
    body = _sse(_chunk("Hello"), _chunk("世界"), _usage_block(12, 34), "[DONE]")
    provider = _provider(lambda request: httpx.Response(200, content=body, headers=_SSE_HEADERS))

    pieces = list(provider.chat_stream(_MESSAGES, max_tokens=512))

    assert pieces == ["Hello", "世界"]  # delta.content 逐段拼接产出、顺序保持
    assert provider.last_usage == (12, 34)  # 正常耗尽（见 [DONE]）后为 usage 块的 (12, 34)


# ---------------------------------------------------------------------------
# 用例 ②：首块前非 200 → UpstreamError 且 broken is False（未产出内容，可整体重试）
# ---------------------------------------------------------------------------


def test_non_200_before_first_chunk_raises_not_broken() -> None:
    provider = _provider(lambda request: httpx.Response(500, text="internal error"))

    with pytest.raises(UpstreamError) as exc_info:
        list(provider.chat_stream(_MESSAGES, max_tokens=512))

    assert exc_info.value.broken is False  # 未产出任何内容前的失败
    assert provider.last_usage is None  # fail 路径不置值


# ---------------------------------------------------------------------------
# 用例 ③：产出 1 块后流截断（无 [DONE] 即结束）→ UpstreamError 且 broken is True
# ---------------------------------------------------------------------------


def test_stream_truncated_without_done_after_one_chunk_raises_broken() -> None:
    body = _sse(_chunk("Hello"))  # 有内容块但流意外结束，未见 [DONE]
    provider = _provider(lambda request: httpx.Response(200, content=body, headers=_SSE_HEADERS))

    iterator = provider.chat_stream(_MESSAGES, max_tokens=512)
    collected: list[str] = []
    with pytest.raises(UpstreamError) as exc_info:
        _collect(iterator, collected)

    assert collected == ["Hello"]  # 已产出 1 块部分内容
    assert exc_info.value.broken is True  # 流式中途断裂
    assert provider.last_usage is None  # broken 路径不置值（保持 None）


# ---------------------------------------------------------------------------
# 用例 ④：api_key 非空 → Authorization: Bearer <key>；api_key 空 → 无该头（内网 vLLM）
# ---------------------------------------------------------------------------


def test_authorization_header_only_when_api_key_non_empty() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=_sse("[DONE]"), headers=_SSE_HEADERS)

    with_key = _provider(handler, api_key="sk-secret-1")
    assert list(with_key.chat_stream(_MESSAGES, max_tokens=16)) == []
    assert captured["authorization"] == "Bearer sk-secret-1"  # api_key 非空才带 Bearer 头

    captured.clear()
    without_key = _provider(handler, api_key="")
    assert list(without_key.chat_stream(_MESSAGES, max_tokens=16)) == []
    assert captured["authorization"] is None  # api_key 空 → 不带 Authorization 头


# ---------------------------------------------------------------------------
# 用例 ⑤：请求体恰为 OpenAI 流式契约形状（model/max_tokens/stream/temperature/messages）
# ---------------------------------------------------------------------------


def test_request_body_shape_matches_openai_stream_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse("[DONE]"), headers=_SSE_HEADERS)

    list(_provider(handler).chat_stream(_MESSAGES, max_tokens=256))

    assert captured["url"] == f"{BASE_URL}/chat/completions"  # POST {base_url}/chat/completions
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "model": _MODEL,
        "max_tokens": 256,
        "stream": True,
        "temperature": 0.2,  # 构造器缺省温度
        "messages": [{"role": "user", "content": "介绍一下企业智能工作台"}],  # 原样（role+content）
        "stream_options": {"include_usage": True},
    }


# ---------------------------------------------------------------------------
# 用例 ⑥：max_tokens 只透传不钳制（钳制在调用方管道）
# ---------------------------------------------------------------------------


def test_max_tokens_is_passed_through_unclamped() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse("[DONE]"), headers=_SSE_HEADERS)

    list(_provider(handler).chat_stream(_MESSAGES, max_tokens=7))

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["max_tokens"] == 7  # 非常规小值原样透传：钳制职责在调用方（管道）
    assert body["temperature"] == 0.2  # 构造器 temperature 缺省值随请求体下发


# ---------------------------------------------------------------------------
# 断流语义补充：读流中 httpx.ReadError（已产出后）→ broken=True；连接失败 → broken=False
# ---------------------------------------------------------------------------


def test_read_error_mid_stream_after_content_raises_broken() -> None:
    def stream() -> Iterator[bytes]:
        yield _sse(_chunk("Hello"))
        raise httpx.ReadError("connection reset mid-stream")  # 读流中传输层断裂

    provider = _provider(
        lambda request: httpx.Response(200, content=stream(), headers=_SSE_HEADERS)
    )
    iterator = provider.chat_stream(_MESSAGES, max_tokens=512)
    collected: list[str] = []
    with pytest.raises(UpstreamError) as exc_info:
        _collect(iterator, collected)

    assert collected == ["Hello"]  # 断裂前已产出的部分内容仍在
    assert exc_info.value.broken is True


def test_connect_error_before_content_raises_not_broken() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider(handler)
    with pytest.raises(UpstreamError) as exc_info:
        list(provider.chat_stream(_MESSAGES, max_tokens=512))

    assert exc_info.value.broken is False  # 未产出任何内容前的连接失败
    assert provider.last_usage is None


# ---------------------------------------------------------------------------
# 解析守卫：非法 JSON 数据块、role-only 空内容块、bool 型 usage token（排除 bool）
# ---------------------------------------------------------------------------


def test_malformed_json_data_block_raises_upstream_error() -> None:
    provider = _provider(
        lambda request: httpx.Response(200, content=_sse("not-json"), headers=_SSE_HEADERS)
    )

    with pytest.raises(UpstreamError, match="JSON") as exc_info:
        list(provider.chat_stream(_MESSAGES, max_tokens=512))

    assert exc_info.value.broken is False  # 非法块出现在任何产出之前


def test_empty_content_chunks_skipped_and_done_without_usage_keeps_none() -> None:
    body = _sse(
        json.dumps(
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
        ),  # 首块 role-only
        _chunk("Hi"),
        "[DONE]",
    )
    provider = _provider(lambda request: httpx.Response(200, content=body, headers=_SSE_HEADERS))

    assert list(provider.chat_stream(_MESSAGES, max_tokens=512)) == ["Hi"]  # 空内容块跳过
    assert provider.last_usage is None  # 正常耗尽但上游未给 usage 块 → 保持 None


def test_bool_usage_tokens_are_ignored_not_coerced() -> None:
    body = _sse(
        json.dumps({"usage": {"prompt_tokens": True, "completion_tokens": 34}, "choices": []}),
        "[DONE]",
    )
    provider = _provider(lambda request: httpx.Response(200, content=body, headers=_SSE_HEADERS))

    list(provider.chat_stream(_MESSAGES, max_tokens=16))

    assert provider.last_usage is None  # bool 是 int 子类但不接受（type is int 显式排除）


# ---------------------------------------------------------------------------
# 接口兼容：OpenAICompatLLMProvider 与 LLMProvider Protocol 结构化兼容
# ---------------------------------------------------------------------------


def test_openai_compat_provider_satisfies_llm_protocol() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse("[DONE]"), headers=_SSE_HEADERS)

    assert isinstance(_provider(ok), LLMProvider)  # runtime_checkable 结构化兼容
