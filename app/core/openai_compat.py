r"""OpenAI 兼容云 LLM 真实现骨架（chat completions 流式）：W2 激活，当前仅保留接口形状。

激活口径：POST {base_url}/chat/completions（stream=true），逐 SSE chunk 拼
delta.content 产出；上游异常/提前断流统一抛 app.core.llm.UpstreamError
（broken=True 表示已产出部分内容），与 Stub 故障注入语义一致，管道测试可直接换替身。
"""

from collections.abc import Iterator, Mapping, Sequence


class OpenAICompatLLMProvider:
    """OpenAI 兼容 chat 流式真实现：W2 激活。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 60.0) -> None:
        self.base_url: str = base_url
        self.api_key: str = api_key
        self.model: str = model
        self.timeout_s: float = timeout_s

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        """逐段 yield 回答片段，总量受 max_tokens 约束；中途断裂抛 UpstreamError(broken=True)（W2 激活）。"""
        raise NotImplementedError("OpenAI 兼容 chat_stream W2 激活")
