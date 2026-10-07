r"""Agent 模型适配层单测（工单 #04）：三路径 + 协议边界 + 真实通道形状。

以 Stub 通道注入验证：① 原生 tool_calls 正常路径（不经文本解析）；② ReAct 文本协议
解析路径（正文含协议标记 → tool_calls）；③ 上游故障抛 UpstreamError（不吞不改）。
协议语义与失败边界（多调用/未绑定工具/非法 JSON/fallback 开关）见 app/core/agent_model.py
docstring；真实通道以 httpx.MockTransport 验证 payload 形状与重试纪律（tei 同款）。
"""

import json

import httpx
import pytest

from app.core.agent_model import (
    AgentChatModel,
    ChannelTurn,
    OpenAICompatToolChannel,
    _parse_channel_reply,
)
from app.core.agent_react import REACT_FEEDBACK_TOOL_NAME, ReactParseError, parse_react_tool_calls
from app.core.llm import UpstreamError

_TOOL = {
    "type": "function",
    "function": {
        "name": "knowledge_base_search",
        "description": "检索知识库",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

_NATIVE_TURN = ChannelTurn(
    content="",
    tool_calls=(
        {
            "name": "knowledge_base_search",
            "args": {"query": "退货时限"},
            "id": "call_1",
            "type": "tool_call",
        },
    ),
)


class StubChannel:
    """确定性替身：按脚本吐 ChannelTurn 或注入 fail_times 次 UpstreamError，并记录每次调用。"""

    def __init__(self, turns: list[ChannelTurn] | None = None, fail_times: int = 0) -> None:
        self.turns = turns if turns is not None else [ChannelTurn(content="普通文本回答")]
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def complete(self, messages, tools, max_tokens):
        self.calls.append(
            {"messages": list(messages), "tools": list(tools), "max_tokens": max_tokens}
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise UpstreamError("stub channel forced failure")
        return self.turns[min(len(self.calls), len(self.turns)) - 1]


def _model(channel: StubChannel, **kwargs) -> AgentChatModel:
    return AgentChatModel(channel=channel, **kwargs)


# ---------------------------------------------------------------------------
# 路径①：原生 tool_calls 正常路径
# ---------------------------------------------------------------------------


def test_原生tool_calls路径_不经文本解析直接透传() -> None:
    channel = StubChannel([_NATIVE_TURN])
    message = _model(channel).bind_tools([_TOOL]).invoke("客服退货时限是多久")

    assert message.content == ""
    assert message.tool_calls == [
        {
            "name": "knowledge_base_search",
            "args": {"query": "退货时限"},
            "id": "call_1",
            "type": "tool_call",
        }
    ]
    assert channel.calls[0]["tools"] == [_TOOL]  # 绑定工具原样下发通道（OpenAI 格式）
    assert channel.calls[0]["max_tokens"] == 4096  # 缺省生成上限
    assert channel.calls[0]["messages"] == [{"role": "user", "content": "客服退货时限是多久"}]


def test_多轮消息转换_工具结果消息带tool_call_id() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    channel = StubChannel([ChannelTurn(content="已收到工具结果")])
    messages = [
        HumanMessage("问"),
        AIMessage(
            "",
            tool_calls=[
                {
                    "name": "knowledge_base_search",
                    "args": {"query": "q"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage("工具产出", tool_call_id="call_1"),
    ]
    _model(channel).bind_tools([_TOOL]).invoke(messages)

    assert channel.calls[0]["messages"][0] == {"role": "user", "content": "问"}
    assert channel.calls[0]["messages"][1]["role"] == "assistant"
    assert (
        channel.calls[0]["messages"][1]["tool_calls"][0]["function"]["name"]
        == "knowledge_base_search"
    )
    assert channel.calls[0]["messages"][2] == {
        "role": "tool",
        "content": "工具产出",
        "tool_call_id": "call_1",
    }


# ---------------------------------------------------------------------------
# 路径②：ReAct 文本协议解析路径
# ---------------------------------------------------------------------------


def test_文本协议解析为tool_calls_正文清空() -> None:
    text = '<<TOOL:knowledge_base_search|{"query": "报销时限"}>>'
    channel = StubChannel([ChannelTurn(content=text)])
    message = _model(channel).bind_tools([_TOOL]).invoke("报销时限是多久")

    assert message.content == ""  # 协议行不进正文
    assert message.tool_calls[0]["name"] == "knowledge_base_search"
    assert message.tool_calls[0]["args"] == {"query": "报销时限"}
    assert message.tool_calls[0]["id"].startswith("react_")


def test_文本协议多行多次调用按序解析() -> None:
    text = '<<TOOL:knowledge_base_search|{"query": "a"}>>\n<<TOOL:knowledge_base_search|{"query": "b"}>>'
    channel = StubChannel([ChannelTurn(content=text)])
    message = _model(channel).bind_tools([_TOOL]).invoke("两个都要查")

    assert [call["args"]["query"] for call in message.tool_calls] == ["a", "b"]


def test_空参数对象_合法且解析为空args() -> None:
    channel = StubChannel([ChannelTurn(content="<<TOOL:knowledge_base_search|{}>>")])
    message = _model(channel).bind_tools([_TOOL]).invoke("q")

    assert message.tool_calls[0]["args"] == {}


def test_协议调用未绑定工具_解析层严格抛错() -> None:
    """协议层严格性保留（specs §4.4：宁可一次 step error 也不半解析）；模型层把它转成
    反馈调用（见下），不再直接终止 agent 循环。"""
    with pytest.raises(ReactParseError, match="未绑定工具"):
        parse_react_tool_calls("<<TOOL:web_search|{}>>", {"knowledge_base_search"})


def test_协议参数非法JSON_解析层严格抛错() -> None:
    with pytest.raises(ReactParseError, match="合法 JSON"):
        parse_react_tool_calls("<<TOOL:knowledge_base_search|{bad>>", {"knowledge_base_search"})


def test_原生tool_calls参数非法JSON_合成反馈调用_不终止运行() -> None:
    """实测 deepseek-flash 偶发输出非法参数 JSON（2026-10-07 模板生成成稿一步踩中，
    旧径 UpstreamError → RUN_ERROR"生成服务暂时不可用"）：与 B1 同款步级语义——
    合成反馈调用让模型自我修正，运行不终止。"""
    body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "save_draft",
                                "arguments": '{"title": "坏参数", "content": 55',
                            },
                        }
                    ],
                }
            }
        ]
    }

    turn = _parse_channel_reply(body)

    assert turn.content == ""
    assert turn.tool_calls[0]["name"] == REACT_FEEDBACK_TOOL_NAME
    assert "save_draft" in turn.tool_calls[0]["args"]["error"]
    assert "合法 JSON" in turn.tool_calls[0]["args"]["error"]
    assert turn.tool_calls[0]["id"].startswith("react_err_")  # 反馈调用自增 id


def test_解析失败合成反馈工具调用_不终止运行() -> None:
    """B1 步级语义：ReAct 解析失败不再向上抛（旧径落入 generic Exception → RUN_ERROR
    "系统繁忙"），模型层合成 react_parse_feedback 调用，失败摘要进 args——桥接层以
    {"error": ...} 观察喂回循环（与工具失败路径同形），模型据此修正输出。"""
    channel = StubChannel([ChannelTurn(content="<<TOOL:knowledge_base_search|{bad>>")])
    message = _model(channel).bind_tools([_TOOL]).invoke("q")

    assert message.content == ""
    assert message.tool_calls[0]["name"] == REACT_FEEDBACK_TOOL_NAME
    assert "合法 JSON" in message.tool_calls[0]["args"]["error"]
    assert message.tool_calls[0]["type"] == "tool_call"


def test_调用未绑定工具_同样合成反馈调用() -> None:
    channel = StubChannel([ChannelTurn(content="<<TOOL:web_search|{}>>")])
    message = _model(channel).bind_tools([_TOOL]).invoke("q")

    assert message.tool_calls[0]["name"] == REACT_FEEDBACK_TOOL_NAME
    assert "未绑定工具" in message.tool_calls[0]["args"]["error"]
    assert message.tool_calls[0]["id"].startswith("react_err_")  # 多次失败 id 可区分


def test_未绑定工具时_协议标记按普通文本透传() -> None:
    """无绑定工具即普通问答：标记不解析，避免误伤含特殊字符的普通回答。"""
    channel = StubChannel([ChannelTurn(content="<<TOOL:knowledge_base_search|{}>>")])
    message = _model(channel).invoke("你好")

    assert message.content == "<<TOOL:knowledge_base_search|{}>>"
    assert message.tool_calls == []


def test_react_fallback关闭时_文本协议不解析() -> None:
    channel = StubChannel([ChannelTurn(content="<<TOOL:knowledge_base_search|{}>>")])
    message = _model(channel, react_fallback=False).bind_tools([_TOOL]).invoke("q")

    assert message.content == "<<TOOL:knowledge_base_search|{}>>"
    assert message.tool_calls == []


def test_bind_tools返回副本_原模型未绑定() -> None:
    channel = StubChannel()
    model = _model(channel)

    bound = model.bind_tools([_TOOL])

    assert bound.bound_tools != []
    assert model.bound_tools == []  # 原实例不变：与 LangChain bind 语义一致


def test_流式产出与生成一致() -> None:
    channel = StubChannel([_NATIVE_TURN])
    chunks = list(_model(channel).bind_tools([_TOOL]).stream("q"))

    merged = chunks[0]
    assert merged.content == ""
    assert merged.tool_calls[0]["name"] == "knowledge_base_search"  # tool_call_chunks 合并还原


# ---------------------------------------------------------------------------
# 路径③：上游故障抛 UpstreamError（不吞不改）
# ---------------------------------------------------------------------------


def test_上游故障抛UpstreamError_不吞不改() -> None:
    channel = StubChannel(fail_times=1)
    model = _model(channel).bind_tools([_TOOL])

    with pytest.raises(UpstreamError, match="stub channel forced failure"):
        model.invoke("问题")
    assert channel.calls[0]["tools"] == [_TOOL]  # 故障发生在带工具的调用上


# ---------------------------------------------------------------------------
# 真实通道（OpenAICompatToolChannel）：payload 形状 + 重试纪律
# ---------------------------------------------------------------------------


def _mock_channel(handler) -> OpenAICompatToolChannel:
    return OpenAICompatToolChannel(
        base_url="http://stub",
        api_key="secret-key",
        model="deepseek-flash",
        transport=httpx.MockTransport(handler),
    )


def test_真实通道_Bearer头与payload形状_原生tool_calls解析() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {
                                        "name": "knowledge_base_search",
                                        "arguments": '{"query": "年假"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    turn = _mock_channel(handler).complete([{"role": "user", "content": "年假几天"}], [_TOOL], 512)

    assert seen["headers"]["authorization"] == "Bearer secret-key"  # api_key 非空才带头
    assert seen["payload"]["model"] == "deepseek-flash"
    assert seen["payload"]["tools"] == [_TOOL]
    assert seen["payload"]["tool_choice"] == "auto"
    assert seen["payload"]["max_tokens"] == 512
    assert turn.content == ""  # content=null 容忍为空串
    assert turn.tool_calls[0] == {
        "name": "knowledge_base_search",
        "args": {"query": "年假"},
        "id": "call_9",
        "type": "tool_call",
    }


def test_真实通道_api_key为空_不带鉴权头_无工具不下发tools() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "内网回答"}}]})

    channel = OpenAICompatToolChannel(
        base_url="http://stub", api_key="", model="m", transport=httpx.MockTransport(handler)
    )

    turn = channel.complete([{"role": "user", "content": "q"}], [], 8)

    assert "authorization" not in seen["headers"]  # 内网 vLLM 常无 key：不带头
    assert "tools" not in seen["payload"]  # 无工具：tools/tool_choice 不下发
    assert "tool_choice" not in seen["payload"]
    assert turn.content == "内网回答"


def test_真实通道_4xx不重试_抛UpstreamError() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(400, json={"error": "tools not supported"})

    with pytest.raises(UpstreamError, match="http 400"):
        _mock_channel(handler).complete([{"role": "user", "content": "q"}], [_TOOL], 64)
    assert counter["n"] == 1  # 4xx：请求本身有误，重试无意义


def test_真实通道_5xx重试一次后_抛UpstreamError() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(503)

    with pytest.raises(UpstreamError, match="http 503"):
        _mock_channel(handler).complete([{"role": "user", "content": "q"}], [], 64)
    assert counter["n"] == 2  # 5xx：恰重试 1 次（最多 2 次尝试）
