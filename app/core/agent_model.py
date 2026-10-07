r"""Agent 模型适配层（F-211，工单 #04）：OpenAI 兼容通道 → LangChain 聊天模型。

DeepAgents 只消费 LangChain BaseChatModel 面。本模块把现有 OpenAI 兼容通道包装为最小
BaseChatModel 实现（bind_tools + _generate + _stream），通道构造口径与 openai_compat 同款
（base_url/api_key/model/timeout/temperature/effort，api_key 非空才带 Bearer 头；密钥来源一律
Settings，不复制密钥加载逻辑），档位留空复用主问答通道的决策在 Settings 注释与工单 #06 装配。
不进 core/llm：core 保持无 agent 语义（specs/agent-evolution.md §4.4）。

ReAct 文本兜底协议（specs §4.4：内网/小档模型可能无原生 tool-calling）::

    <<TOOL:工具名|{"参数名": "值"}>>

约定：绑定工具后，模型在正文用上面的单行标记表达一次工具调用，可多行连用表达多次；参数为
JSON 对象（空 ``{}`` 合法）；工具名必须命中绑定集合（防幻觉工具）；解析为 AIMessage.tool_calls
交给 agent 循环，正文清空。仅在"绑定了工具 + 未返回原生 tool_calls + react_fallback 开启"
三条件同时成立时解析；含标记但参数非法 / 工具未绑定 → ReactParseError（计一次 step error，由循环计数，
不在适配层重试）；无标记即普通文本回答原样透传。

上游故障语义：通道一切失败统一 UpstreamError（broken=False，非流式无半成品），不吞不改；一次重试收口在
真实通道内部（tei 同款纪律：传输错误/超时/5xx 重试 1 次，4xx 不重试）。测试以 Stub 通道注入；真实通道
注入 httpx.MockTransport（同 openai_compat 测试缝）。
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from app.core.llm import UpstreamError

_TOOL_MARKER = re.compile(r"<<TOOL:([A-Za-z0-9_.-]+)\|(.*?)>>", re.DOTALL)
_MAX_ATTEMPTS: int = 2  # 一次重试：最多 2 次尝试（每次独立新请求，tei 同款）


class ReactParseError(ValueError):
    """ReAct 文本协议解析失败（specs §4.4：计一次 step error，不无限重试）。"""


def parse_react_tool_calls(text: str, allowed: set[str]) -> list[dict[str, Any]]:
    """按模块 docstring 协议解析正文为 tool_calls：无标记返回 []，有标记即严格解析。

    参数必须为 JSON 对象（空 ``{}`` 合法）、工具名必须已绑定；任一不满足抛 ReactParseError
    ——宁可一次 step error，也不把半解析结果喂给 agent 循环。
    """
    matches = _TOOL_MARKER.findall(text)
    if not matches:
        return []
    calls: list[dict[str, Any]] = []
    for index, (name, raw_args) in enumerate(matches):
        if name not in allowed:
            raise ReactParseError(f"react 协议调用了未绑定工具: {name}")
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError as exc:  # 含 JSONDecodeError；同族收口不裸抛
            raise ReactParseError(f"react 协议参数不是合法 JSON: {exc}") from exc
        if not isinstance(args, dict):
            raise ReactParseError("react 协议参数必须是 JSON 对象")
        calls.append({"name": name, "args": args, "id": f"react_{index}", "type": "tool_call"})
    return calls


@dataclass(frozen=True)
class ChannelTurn:
    """上游一次成型回复：文本正文 + 原生 tool_calls（OpenAI 语义的 name/args/id）。"""

    content: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()


class AgentModelChannel(Protocol):
    """上游会话通道最小面：OpenAI 格式 messages + 工具清单 → 一次成型回复。

    独立成协议的意义：单测以 Stub 注入（三路径可验证），真实通道只一个实现，替换缝与六接口同哲学。
    """

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> ChannelTurn: ...


_ROLE_BY_TYPE = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


def _to_openai_message(message: BaseMessage) -> dict[str, Any]:
    """BaseMessage → OpenAI 请求体消息：role 映射；ai 带回 tool_calls，tool 带 tool_call_id。"""
    role = _ROLE_BY_TYPE.get(message.type, "user")
    payload: dict[str, Any] = {"role": role, "content": message.content}
    if message.type == "ai" and getattr(message, "tool_calls", None):
        payload["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["args"], ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.type == "tool":
        payload["tool_call_id"] = getattr(message, "tool_call_id", "")
    return payload


class OpenAICompatToolChannel:
    """OpenAI 兼容非流式通道（带原生 tools 下发）：DeepSeek/内网 vLLM 共用。连接构造与
    openai_compat 同款（构造参数即其超集，多 tools 能力），区别仅在请求体：非流式 +
    tools/tool_choice。失败语义：传输错误/超时/5xx 重试 1 次，4xx 不重试（如端点不支持
    tools）；一切失败统一 UpstreamError(broken=False)。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        temperature: float = 0.2,
        effort: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url: str = base_url
        self.api_key: str = api_key
        self.model: str = model
        self.timeout_s: float = timeout_s
        self.temperature: float = temperature
        self.effort: str | None = effort
        self.transport: httpx.BaseTransport | None = transport

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        max_tokens: int,
    ) -> ChannelTurn:
        """一次成型调用：payload 组装（tools 缺席不下发）+ 一次重试 + message 解析。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,  # 只透传：钳制语义在调用方（与主通道同口径）
            "temperature": self.temperature,
            "messages": [dict(message) for message in messages],
        }
        if self.effort:
            payload["effort"] = self.effort
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        return _parse_channel_reply(self._post(payload))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_error: UpstreamError | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                # 每次尝试独立新请求：with 短连接用毕即关（openai_compat/tei 同款）
                with httpx.Client(
                    base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
                ) as client:
                    response = client.post("/chat/completions", headers=headers, json=payload)
            except httpx.HTTPError as exc:  # 连接错误/超时等传输层故障：可重试
                last_error = UpstreamError(f"agent llm 传输失败: {type(exc).__name__}: {exc}")
                continue
            if response.status_code == 200:
                return self._parse_json(response)
            last_error = UpstreamError(f"agent llm http {response.status_code}")
            if response.status_code < 500:
                raise last_error  # 4xx：请求本身有误，重试无意义
        assert last_error is not None  # 循环至少执行一次，两次皆败路径必已赋值
        raise last_error

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:  # 含 UnicodeDecodeError（同族收口，openai_compat 同款）
            raise UpstreamError(f"agent llm 响应不是合法 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise UpstreamError("agent llm 响应不是 JSON 对象")
        return body


def _parse_channel_reply(body: dict[str, Any]) -> ChannelTurn:
    """choices[0].message → ChannelTurn；形状不符 / arguments 非 JSON 归 UpstreamError。"""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise UpstreamError("agent llm 响应缺少 choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise UpstreamError("agent llm 响应缺少 message")
    calls: list[dict[str, Any]] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for index, raw in enumerate(raw_calls):
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise UpstreamError("agent llm tool_calls 形状不符")
            function = raw["function"]
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError as exc:
                raise UpstreamError(f"agent llm tool arguments 不是合法 JSON: {exc}") from exc
            if not isinstance(args, dict):
                raise UpstreamError("agent llm tool arguments 必须是 JSON 对象")
            entry = {
                "name": function.get("name", ""),
                "args": args,
                "id": raw.get("id") or f"call_{index}",
                "type": "tool_call",
            }
            calls.append(entry)
    content = message.get("content")
    return ChannelTurn(content=content if isinstance(content, str) else "", tool_calls=tuple(calls))


class AgentChatModel(BaseChatModel):
    """DeepAgents 可消费的 BaseChatModel 最小面：bind_tools + _generate + _stream。

    react_fallback=True 时：绑定工具且上游未返回原生 tool_calls，正文含协议标记即解析为
    tool_calls（协议与失败语义见模块 docstring）；未绑定或 fallback 关闭时正文原样透传
    （协议标记只是普通文本，不误伤普通问答）。上游故障抛 UpstreamError，不吞不改。
    """

    channel: Any  # AgentModelChannel（Protocol 不便 pydantic 校验，Any 承载；必填无缺省）
    max_tokens: int = 4096  # 生成上限：服务端钳制语义与主通道一致（§17.5）
    react_fallback: bool = True
    # exclude：绑定态是实现细节，不进序列化/回调
    bound_tools: list[dict[str, Any]] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "agent-openai-compat"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable:
        """绑定工具清单（convert_to_openai_tool 归一，dict/pydantic/@tool 均可）；返回模型
        副本（原实例不变，与 LangChain bind 语义一致），副本共享 channel。"""
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        return self.model_copy(update={"bound_tools": formatted, **kwargs})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """生成一次回复：通道调用（故障上抛）→ 原生 tool_calls 优先 → ReAct 文本兜底。"""
        turn = self.channel.complete(
            [_to_openai_message(message) for message in messages], self.bound_tools, self.max_tokens
        )
        content = turn.content
        tool_calls = list(turn.tool_calls)
        if not tool_calls and self.bound_tools and self.react_fallback:
            allowed = {
                tool["function"]["name"]
                for tool in self.bound_tools
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
            parsed = parse_react_tool_calls(content, allowed)
            if parsed:
                content = ""  # 协议行不进正文：工具调用语义由 tool_calls 承载
                tool_calls = parsed
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls))]
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """最小流式面：一次成型结果整体出块（DeepAgents 的流式观感由 langgraph 层组织）。"""
        result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        assert isinstance(message, AIMessage)
        chunks = [
            {
                "name": call["name"],
                "args": json.dumps(call["args"], ensure_ascii=False),
                "id": call["id"],
                "index": index,
                "type": "tool_call_chunk",
            }
            for index, call in enumerate(message.tool_calls)
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_call_chunks=chunks)
        )
