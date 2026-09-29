r"""OpenAI 兼容云 LLM 流式真实现：chat completions（云 GLM-4 与内网 vLLM 共用此客户端）。

激活口径：POST {base_url}/chat/completions（stream=true，stream_options.include_usage），
逐 SSE 数据块取 delta.content 产出；usage 块（[DONE] 前无 choices 的数据块）填实例属性
last_usage=(prompt_tokens, completion_tokens)，仅在流正常耗尽（见 data: [DONE]）后置值，
fail/broken 路径保持 None。断流语义与 Stub 一致（管道层靠它区分重试策略）：
未产出任何内容前的失败（连接失败/超时/非 200）→ UpstreamError(broken=False)；
已产出至少一个片段后的中断（读流 httpx.HTTPError、流意外结束未见 [DONE]）→ broken=True。
api_key 非空才带 Authorization: Bearer 头（内网 vLLM 常无 key）；max_tokens 钳制在
调用方（管道），本类只透传。异常纪律：只捕 httpx.HTTPError 族与 JSON/解码解析的
ValueError（含 UnicodeDecodeError），禁 except Exception。测试以构造参数注入
httpx.MockTransport 替身（transport 缺省 None → httpx 默认传输）；模块不加全局 client，
方法内 with 短连接。合规双轨：装配层 app/core/assembly.py 按 Settings 指向云 GLM-4
或内网 vLLM 端点，本客户端对两者透明（仅 base_url/api_key 不同）。
"""

import json
from collections.abc import Iterator, Mapping, Sequence

import httpx

from app.core.llm import UpstreamError

_DONE = "[DONE]"
_DATA_PREFIX = "data:"


def _iter_sse_data(response: httpx.Response) -> Iterator[str]:
    """逐行解析 SSE：yield 每个 data: 载荷（去前缀与首尾空白）；空载荷/非 data 行跳过。"""
    for line in response.iter_lines():
        text = line.strip()
        if not text.startswith(_DATA_PREFIX):
            continue
        payload = text[len(_DATA_PREFIX) :].strip()
        if payload:  # 空载荷（keep-alive 注释行）跳过
            yield payload


def _parse_event(
    data: str, usage: tuple[int, int] | None, broken: bool
) -> tuple[str | None, tuple[int, int] | None]:
    """解析单个 data 载荷 → (产出片段, 累计 usage)；非法 JSON/形状抛 UpstreamError(broken)。

    usage 块形如 {"usage": {...}, "choices": []}，token 数以 type is int 校验（显式排除
    bool 子类，Task 3 评审教训）；delta.content 为非空 str 才产出（首块 role-only 跳过）。
    """
    try:
        chunk = json.loads(data)
    except ValueError as exc:  # 含 JSONDecodeError；同族收口不裸抛
        raise UpstreamError(f"llm sse 数据块不是合法 JSON: {exc}", broken=broken) from exc
    if not isinstance(chunk, dict):
        raise UpstreamError("llm sse 数据块不是 JSON 对象", broken=broken)
    if isinstance(chunk.get("usage"), dict):
        prompt = chunk["usage"].get("prompt_tokens")
        completion = chunk["usage"].get("completion_tokens")
        if type(prompt) is int and type(completion) is int:  # type is int：排除 bool
            usage = (prompt, completion)
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, usage
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    if isinstance(content, str) and content:
        return content, usage
    return None, usage


class OpenAICompatLLMProvider:
    """OpenAI 兼容 chat 流式真实现：云 GLM-4 与内网 vLLM 共用（合规双轨切换点）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        temperature: float = 0.2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url: str = base_url
        self.api_key: str = api_key
        self.model: str = model
        self.timeout_s: float = timeout_s
        self.temperature: float = temperature
        self.transport: httpx.BaseTransport | None = transport
        self.last_usage: tuple[int, int] | None = None

    def _headers(self) -> dict[str, str]:
        """请求头：api_key 非空才带 Authorization: Bearer（内网 vLLM 常无 key）。"""
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    def _payload(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> dict[str, object]:
        """请求体：model/max_tokens/stream/temperature/messages 原样 + include_usage。"""
        return {
            "model": self.model,
            "max_tokens": max_tokens,  # 只透传：钳制在调用方（管道）
            "stream": True,
            "temperature": self.temperature,
            "messages": [dict(message) for message in messages],
            "stream_options": {"include_usage": True},
        }

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        """逐段 yield 回答片段；正常耗尽（见 data: [DONE]）后填 last_usage，否则保持 None。

        断流语义：未产出内容前的失败（连接失败/超时/非 200）→ broken=False；
        已产出片段后的中断（读流 httpx.HTTPError、流意外结束未见 [DONE]）→ broken=True。
        """
        self.last_usage = None
        produced = 0
        usage: tuple[int, int] | None = None
        try:
            with (
                httpx.Client(
                    base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
                ) as client,
                client.stream(
                    "POST",
                    "/chat/completions",
                    headers=self._headers(),
                    json=self._payload(messages, max_tokens),
                ) as response,
            ):
                if response.status_code != 200:
                    raise UpstreamError(f"llm http {response.status_code}")
                for data in _iter_sse_data(response):
                    if data == _DONE:
                        self.last_usage = usage
                        return
                    content, usage = _parse_event(data, usage, broken=produced > 0)
                    if content:
                        produced += 1
                        yield content
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"llm 流式传输失败: {type(exc).__name__}: {exc}", broken=produced > 0
            ) from exc
        except ValueError as exc:  # 流体解码失败（UnicodeDecodeError ⊂ ValueError，同族收口）
            raise UpstreamError(f"llm 流式数据解码失败: {exc}", broken=produced > 0) from exc
        raise UpstreamError("llm 流意外结束（未见 data: [DONE]）", broken=produced > 0)
