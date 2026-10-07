r"""ReAct 文本兜底协议（specs §4.4：内网/小档模型可能无原生 tool-calling）。

自 app/core/agent_model.py 拆出（300 行红线 §1.4）：协议解析是无 IO 的纯文本关切，
与通道适配（BaseChatModel 包装）各自独立演进；B1 修复把"解析失败计一次 step error"
落成步级语义——不向上终止运行，而是合成一次对 react_parse_feedback 的工具调用，
由桥接层以 {"error": 解析失败摘要} 观察喂回 agent 循环（与工具失败路径同形）。

协议约定（绑定工具后，模型在正文表达工具调用）::

    <<TOOL:工具名|{"参数名": "值"}>>

单行标记一次调用，可多行连用多次；参数为 JSON 对象（空 ``{}`` 合法）；工具名必须
命中绑定集合（防幻觉工具）；解析为 AIMessage.tool_calls 后正文清空。仅在"绑定了
工具 + 未返回原生 tool_calls + react_fallback 开启"三条件同时成立时解析；无标记即
普通文本回答原样透传（协议标记只是普通文本，不误伤普通问答）。
"""

import json
import re
from typing import Any

# B1：解析失败的反馈通道工具名。适配层合成对它的调用，桥接层（services/agent_bridge）
# 提供同名执行体——core 只定协议常量，工具实现留在 services（跨层依赖单向）。
REACT_FEEDBACK_TOOL_NAME = "react_parse_feedback"

_TOOL_MARKER = re.compile(r"<<TOOL:([A-Za-z0-9_.-]+)\|(.*?)>>", re.DOTALL)


class ReactParseError(ValueError):
    """ReAct 文本协议解析失败（specs §4.4：计一次 step error）。模型适配层捕获后
    转为反馈观察喂回循环，不在适配层无限重试；本异常自身仍是协议层的严格契约。"""


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
