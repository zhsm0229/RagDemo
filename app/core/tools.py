r"""第七个可替换点 ToolProvider（F-211，工单 #04）：Agent 工具统一抽象与注册表。

与六接口同哲学（specs/agent-evolution.md §4.3）：注册制 + 声明制——每个工具以 ToolSpec
声明部门过滤语义与审计语义，两者都是评审强制检查项（§4.5 审计面）；超时/重试/审计落库/
权限闸统一在 ToolProvider.invoke 收口，工具实现自身不得绕过。RunContext 携带调用者身份
与部门上下文（含请求作用域 db 会话），由调用方（agent 循环，工单 #06 装配）构造。
本模块只承载协议与注册表；具体工具在 services 层实现（跨模块取数走 service 函数，§1.6），
不在装配层（assembly.py）接线之前被 main 消费。
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据：name/description 供模型与 schema 复用；两项语义声明是声明制契约。

    dept_scoped=True 表示工具强制继承调用者 dept_ids（RunContext），args 携带的任何
    部门字段一律无效——工具层不得成为绕过 F-502 的后门；audit_note 声明审计落点与
    记录内容（agent_steps，迁移 0008），随工具接入评审逐条核对。
    """

    name: str  # 工具名（英文域词，模型可见，如 knowledge_base_search）
    description: str  # 用途描述（中文，模型可见，进 schema）
    dept_scoped: bool  # 部门过滤语义声明：True=强制继承调用者部门上下文
    audit_note: str  # 审计语义声明：审计落点与记录内容


@dataclass(frozen=True)
class RunContext:
    """一次 agent 运行的调用者上下文：身份 + 部门 + 请求作用域会话。

    dept_ids 用 tuple（不可变）：工具只能整体继承（[用户部门, 全员部门]，F-502 过滤
    语义与问答链路 prepare_turn 同口径），不能改写往里塞别的部门。
    """

    user_id: int
    username: str
    dept_ids: tuple[int, ...]
    db: Any = None  # SQLAlchemy Session（请求作用域；协议不与 ORM 耦合，core 不引 db 层类型）


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果：ok=True 时 value 承载结构化产出（喂给模型），ok=False 时 error
    带中文原因（agent 循环据此决定重试/拒答，不向模型泄漏内部细节由循环层脱敏）。"""

    ok: bool
    value: Any = None
    error: str | None = None

    @classmethod
    def succeed(cls, value: Any) -> "ToolResult":
        return cls(ok=True, value=value)

    @classmethod
    def fail(cls, error: str) -> "ToolResult":
        return cls(ok=False, error=error)


@runtime_checkable
class ToolProvider(Protocol):
    """Agent 工具统一抽象（第七个可替换点）：schema 供模型（原生 tool-calling 与
    ReAct 文本协议共用同一份定义，specs §4.4 单点对接），invoke 收口横切关切。"""

    @property
    def spec(self) -> ToolSpec: ...

    def schema(self) -> dict[str, Any]:
        """JSON Schema（OpenAI function 格式），交给模型两种协议共用。"""
        ...

    def invoke(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        """执行工具：超时+重试、审计落 agent_steps、部门权限闸都在实现层收口。"""
        ...


class ToolRegistry:
    """工具注册表：装配层（工单 #06）按 Settings 注册，agent 循环按名取用。

    注册即过审：每注册一个工具 = 部门过滤声明 + 审计声明各过一遍评审（specs §4.3），
    重名拒绝 fail-fast，防静默覆盖改变语义。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolProvider] = {}

    def register(self, tool: ToolProvider) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"工具重名注册: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> ToolProvider:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未注册的工具: {name}") from exc

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]
