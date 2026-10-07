r"""ToolProvider → LangChain/DeepAgents 桥接（F-211，工单 #04/#06）：dept 红线唯一收口处。

从 services/agent_runs 拆出（单文件 300 行红线，§1.4；拆分先例 ingest/chat 同款）：
注册表工具经 StructuredTool 闭包桥进 DeepAgents，图装配按任务档案（agent_tasks）取
系统提示与工具集——api 层 taskType 白名单与编排消费同一份档案，两处各写一份必然漂移。
dept_ids 整体继承 RunContext（F-502：桥接层只透传 ctx，模型/参数无法改写部门；args
夹带部门字段在 provider.invoke 内已被无视，双保险）。每次工具调用开独立短会话（连接
红线，chat_stream 同款），失败以 {"error": 中文原因} 作为观察喂模型（agent 循环决定
重试/拒答，不向模型泄漏堆栈）。

测试经 monkeypatch 把本模块 SessionLocal 指向测试连接（与 services.agent_runs /
services.agent_steps 两处短会话工厂同批替换，见 tests/api/test_agent_runs.py 的
agent_client fixture）。
"""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, create_model
from sqlalchemy.orm import Session

from app.core.agent_model import AgentChatModel
from app.core.agent_react import REACT_FEEDBACK_TOOL_NAME
from app.core.tools import RunContext, ToolProvider
from app.db.base import SessionLocal
from app.services.agent_tasks import get_profile

if TYPE_CHECKING:  # 仅类型标注：运行时不回引编排模块（避免循环导入）
    from app.services.agent_runs import AgentDeps

_JSON_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}


def _args_model(tool_schema: dict[str, Any]) -> type[BaseModel]:
    """OpenAI function schema → pydantic 参数模型：StructuredTool 的通用桥（schema()
    单点定义，specs §4.4——新工具接入本桥零改动）。required 字段必填，其余可选。"""
    params = tool_schema["function"]["parameters"]
    required = set(params.get("required", []))
    fields: dict[str, tuple[Any, Any]] = {}
    for name, meta in params.get("properties", {}).items():
        py_type = _JSON_TYPES.get(str(meta.get("type", "string")), str)
        fields[name] = (
            (py_type, ...) if name in required else (py_type | None, None)  # type: ignore[operator]
        )
    return create_model(f"{tool_schema['function']['name']}_args", **fields)


def _lc_tool(provider: ToolProvider, ctx: RunContext) -> StructuredTool:
    """注册表工具 → LangChain 工具闭包：失败以 {"error": 中文原因} 作为观察喂模型。"""
    spec = provider.spec
    args_model = _args_model(provider.schema())

    def _invoke(**kwargs: Any) -> dict[str, Any] | str:
        db: Session = SessionLocal()
        try:
            result = provider.invoke(kwargs, replace(ctx, db=db))
        finally:
            db.close()
        if result.ok:
            payload = {"result": result.value}
            if spec.observation_notice:
                # B2：声明行前置观察正文（声明制契约见 ToolSpec.observation_notice），
                # 让模型读取资料前先进入"数据不是指令"语境；错误观察不加声明，
                # 保持 {"error": ...} 形态（extract_tool_error 的解析口径依赖）。
                return f"{spec.observation_notice}\n{json.dumps(payload, ensure_ascii=False)}"
            return payload
        return {"error": result.error}

    return StructuredTool.from_function(
        func=_invoke, name=spec.name, description=spec.description, args_schema=args_model
    )


def _react_feedback_tool() -> StructuredTool:
    """react_parse_feedback 执行体（B1 步级语义，协议常量见 core/agent_react）：适配层
    解析失败时合成对它的调用，观察以 {"error": 解析失败摘要} 喂回 agent 循环（与
    ToolResult.fail 的工具失败观察同形，编排层据此落 agent_steps.error）。属协议
    plumbing 而非能力工具：不进 ToolRegistry/TASK_PROFILES（白名单最小化纪律），
    description 明示模型不得主动调用。"""
    schema = {
        "type": "function",
        "function": {
            "name": REACT_FEEDBACK_TOOL_NAME,
            "description": "内部协议反馈通道，禁止主动调用",
            "parameters": {
                "type": "object",
                "properties": {"error": {"type": "string", "description": "解析失败原因"}},
                "required": ["error"],
            },
        },
    }

    def _invoke(error: str) -> dict[str, Any]:
        return {"error": error}

    return StructuredTool.from_function(
        func=_invoke,
        name=REACT_FEEDBACK_TOOL_NAME,
        description="内部协议反馈通道，禁止主动调用",
        args_schema=_args_model(schema),
    )


def build_agent_graph(deps: "AgentDeps", ctx: RunContext, task_type: str) -> Any:
    """装配本次运行的 DeepAgents 图：模型取装配层通道现建（换缝即刻生效）；系统提示与
    工具集按任务档案取（agent_tasks，白名单同源——api 层校验与编排消费同一份档案），
    按档案序桥接（序稳定 → 通道收到的 tools 清单可复现，测试断言依赖），末尾恒追加
    react_parse_feedback 反馈执行体（B1：ReAct 解析失败以工具观察形态喂回循环）。"""
    model = AgentChatModel(
        channel=deps.channel,
        max_tokens=deps.settings.chat_max_tokens,  # 服务端钳制语义与主通道一致（§17.5）
        react_fallback=deps.react_fallback,
    )
    profile = get_profile(task_type)
    tools = [_lc_tool(deps.registry.get(name), ctx) for name in profile.tool_names]
    tools.append(_react_feedback_tool())
    return create_deep_agent(model=model, tools=tools, system_prompt=profile.system_prompt)
