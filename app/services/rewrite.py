r"""多轮问题改写（Task 8 编排的前置件）：把追问改写为可独立理解的完整问题。

- 流消费约定（task-8 强制交接②）：真实现的 ``chat_stream`` 是生成器函数，连接失败
  在首次 ``next()`` 才抛——本函数把"取迭代器 + 迭代收集"整体包进同一个
  ``UpstreamError`` 异常边界，任一时刻失败都降级返回原问题（CHAT-08），绝不打断
  问答编排；调用方拿到的返回值恒为非空字符串。
- system 提示词由调用方注入（deps.prompts.rewrite_system，prompts/ 目录单一真相源，
  §17.1/§17.4），本模块不读文件、不内嵌提示词副本；历史轮次条数由调用方（编排层）
  截断后再传入，本函数只做 Task 6 消息组装与流消费。
- 三产物合并调用（retrieval-policy §4，工单 06）：multi_query 门控命中时改写升级为
  一次调用产出改写问题 + L1 意图标签 + 子查询（不加往返）；解析失败/缺产物/上游故障
  一律降级单路（原问题、无子查询），与单产物路径同一降级纪律；仅当调用方
  （services/chat）确认 policy.multi_query=True 才进入该模式，False 走
  rewrite_question 既有路径逐字不变。
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.llm import UpstreamError
from app.rag.prompt import build_rewrite_messages
from app.rag.retrieval_policy import INTENT_MULTI_HOP, INTENT_SINGLE_HOP

# 改写产物是一句独立完整的问题，256 token 封顶足够；不进 Settings（避免散落配置项，
# 答案生成的上限仍由生成侧按 settings.chat_max_tokens clamp）
REWRITE_MAX_TOKENS: int = 256
# 三产物整包输出钳制（spec §4：子查询 ≤150 token，经 max_tokens 参数生效）——截断的
# JSON 解析失败即降级单路，宁缺毋滥（门控本就以 L0 多跳信号为前提）
THREE_PRODUCT_MAX_TOKENS: int = 150
_MAX_SUB_QUERIES: int = 3  # 子查询条数钳制（spec §4 冻结：≤3）
_VALID_INTENTS: tuple[str, str] = (INTENT_SINGLE_HOP, INTENT_MULTI_HOP)


class _ChatStream(Protocol):
    """rewrite 所需的最小 LLM 面（与 core.llm.LLMProvider 同构，收窄依赖）。"""

    def chat_stream(
        self, messages: Sequence[Mapping[str, str]], max_tokens: int
    ) -> Iterator[str]: ...


@dataclass(frozen=True)
class RewriteOutcome:
    """三产物调用解析结果：rewritten 恒非空（降级时=原问题）；intent 为 L1 标签
    （single_hop/multi_hop，激活=B 类门槛，本票仅解析校验不参与路由）或降级 None；
    sub_queries 已钳制 ≤3 且剔除空白条目。"""

    rewritten: str
    intent: str | None
    sub_queries: list[str]


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


def rewrite_question_multi(
    llm: _ChatStream,
    history: list[tuple[str, str]],
    question: str,
    *,
    system: str,
    multi_system: str,
) -> RewriteOutcome:
    """三产物合并调用（multi_query 门控命中时的改写升级，spec §4）：一次 LLM 调用产出
    改写问题 + L1 意图标签 + 子查询（整包 max_tokens=150 钳制）；解析失败、缺产物、
    字段非法或上游故障一律降级 RewriteOutcome(question, None, [])——与 rewrite_question
    同一降级纪律（失败静默回退，绝不打断问答编排）。multi_system 单一真相源在
    prompts/rewrite_multi_system.txt（版本头 §17.4），经 deps 传入不内嵌副本。
    """
    messages = build_rewrite_messages(multi_system, history, question)
    try:
        pieces = [piece for piece in llm.chat_stream(messages, THREE_PRODUCT_MAX_TOKENS)]
    except UpstreamError:
        return RewriteOutcome(question, None, [])
    return _parse_three_product("".join(pieces), question)


def _parse_three_product(raw: str, question: str) -> RewriteOutcome:
    """严格解析三产物 JSON：rewritten 非空字符串、intent 属冻结枚举、subQueries 为
    字符串列表——任一不满足即降级单路（原问题、无子查询）。子查询先按产出序钳制
    前三条、再剔除空白条目（不足不回补）。"""
    degraded = RewriteOutcome(question, None, [])
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError:
        return degraded
    if not isinstance(payload, dict):
        return degraded
    rewritten = payload.get("rewritten")
    intent = payload.get("intent")
    sub_queries = payload.get("subQueries")
    if not isinstance(rewritten, str) or not rewritten.strip():
        return degraded
    if intent not in _VALID_INTENTS:
        return degraded
    if not isinstance(sub_queries, list) or any(not isinstance(item, str) for item in sub_queries):
        return degraded
    cleaned = [item.strip() for item in sub_queries[:_MAX_SUB_QUERIES]]
    return RewriteOutcome(rewritten.strip(), intent, [item for item in cleaned if item])
