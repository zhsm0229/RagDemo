r"""长期记忆自动抽取（F-210 §3.2，memory.py 行数红线 §1.4 自其拆出，conversations_store 同款）。

时序：问答回合结束后由 BackgroundTasks 静默执行（api/chat 对 qa/rag/refuse 计划挂
任务——A7：拒答轮用户问题同样可能携带画像信息；cache 命中不抽取）——
extract_memory_task 为任务体：先建独立 LLM 客户端（build_llm 每任务一实例，不触问答
主链路 deps.llm 的调用计数/usage），再自管 SessionLocal 短会话（chat_stream._finalize
同款）读回收尾后的回答正文，中断轮（is_active=false / 空正文）直接跳过。

硬约束（全部服务层执行，§3.2）：每轮最多 2 条；单条 ≤40 字（超长截断保留前 40 字，
画像结论多为短句，截断不破坏语义）；与该用户既有活跃记忆 strip 后精确查重（重复
跳过）；敏感信息复用 F-1006 扫描口径（身份证/手机号/银行卡），命中即丢弃该条。
LLM 空产物（空串/非法 JSON）重试 1 次（A1，tei/convert 同 _MAX_ATTEMPTS 纪律）；
落库前复查用户级开关（B4，LLM 窗口内可能被关闭）与 per-user advisory lock（A5，
与 add_manual 共用写入互斥）；任何异常（UpstreamError / 解析失败 / 落库失败）静默
log 不抛——抽取失败不影响问答主链路（§3.2 降级纪律）。

依赖收窄（rewrite.py 同款）：ExtractionDeps 只声明 llm + prompts 两字段，与
services.chat.ChatDeps 结构兼容（端点直接传 deps，本模块不反向依赖 chat 模块）。
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.assembly import PromptPack, build_llm
from app.core.clock import Clock
from app.core.config import get_settings
from app.core.llm import LLMProvider, UpstreamError
from app.db.base import SessionLocal
from app.db.models import Message, User, UserMemory
from app.rag.precheck import scan_sensitive
from app.rag.prompt import build_memory_extract_messages
from app.services.memory import _acquire_user_lock, _insert_memory

logger = logging.getLogger(__name__)

# 抽取产物是 ≤2 条 × ≤40 字的 JSON 数组，256 token 封顶足够；不进 Settings（避免
# 散落配置项，rewrite.REWRITE_MAX_TOKENS 同款口径）
EXTRACT_MAX_TOKENS: int = 256
_EXTRACT_MAX_ITEMS: int = 2  # §3.2：每轮最多 2 条（提示词第一道防线，此处硬约束）
_EXTRACT_ITEM_MAX_CHARS: int = 40  # §3.2：单条 ≤40 字
_MAX_ATTEMPTS: int = 2  # tei/convert/ocr 同口径：失败重试 1 次（共 2 次尝试）

# A7 拒答轮的抽取回答段替代文字：refuse 轮助手正文是逐字 REFUSAL_TEXT（无知识内容），
# 直接喂给模型会诱导它从拒答话术里"提炼"；以声明文字替换 answer 段、指引只看用户
# 问题。声明属调用侧组装、不进 prompts/memory_extract.txt 本体——避免触 §17.3
# 提示词全量重评（该文件 v1 保持逐字节不动）。
_REFUSED_EXTRACT_ANSWER: str = (
    "[本轮助手未给出知识回答（拒答），请仅从用户问题中提炼关于用户的稳定信息]"
)


class _HasPrompts(Protocol):
    """任务体所需的最小依赖面：ChatDeps 结构兼容（只读 prompts，不触其 llm 计数）。"""

    prompts: PromptPack


@dataclass(frozen=True)
class ExtractionDeps:
    """抽取运行时依赖（收窄面）：LLM 流接口 + 提示词包（memory_extract 单一真相源）。"""

    llm: LLMProvider
    prompts: PromptPack


def extract_memory_task(
    deps: _HasPrompts,
    user_id: int,
    assistant_msg_id: int,
    question: str,
    *,
    refused: bool = False,
) -> None:
    """BackgroundTasks 任务体：读回收尾后的回答正文并抽取落库（异常静默，见模块注）。

    回答正文在端点调度时尚未生成，故任务体按 assistant_msg_id 自行读库——响应发完
    后 finalize 已提交，读到的即最终回答；中断轮被开头卫语句拦下，天然"跳过中断轮"。
    refused=True（A7）：本轮为拒答（端点按 plan.kind 判定传入），回答正文是逐字
    REFUSAL_TEXT 无抽取价值，抽取提示的回答段以声明文字替代、只从用户问题提炼。
    LLM 用独立实例（build_llm），共享 deps.llm 的 stream_calls/last_usage 不被污染；
    测试经 monkeypatch 本模块 build_llm / SessionLocal 替换抽取通道（conftest 模式）。
    """
    with SessionLocal() as db:
        # messages 为分区表、复合主键 (id, created_at)：按 id 的 select 取行（get 需全键）
        row = db.scalars(select(Message).where(Message.id == assistant_msg_id)).first()
        if row is None or not row.is_active or not row.content.strip():
            return  # 中断/空正文轮：无可抽取的回答，静默跳过（连 LLM 客户端都不建）
        llm = build_llm(get_settings())
        extract_and_store(
            db,
            ExtractionDeps(llm=llm, prompts=deps.prompts),
            user_id,
            question,
            row.content,
            source_message_id=assistant_msg_id,
            refused=refused,
        )


def extract_and_store(
    db: Session,
    deps: ExtractionDeps,
    user_id: int,
    question: str,
    answer: str,
    *,
    source_message_id: int | None = None,
    refused: bool = False,
    clock: Clock | None = None,
) -> None:
    """抽取一轮问答的稳定画像并落库：LLM 提炼 → 解析（空/非法产出重试 1 次）→ 归一化
    查重 → 敏感过滤 → 容量淘汰 → 插入（整批一次 commit，审计同事务）。

    refused=True（A7 拒答轮）：answer 参数被声明文字替代（模块注 _REFUSED_EXTRACT_ANSWER），
    仅从用户问题提炼。任何异常静默 log 不抛（§3.2 抽取失败不影响问答主链路）；回滚后
    返回，调用方（任务体短会话）with 块正常关闭。
    """
    try:
        _extract_and_store(
            db, deps, user_id, question, answer, source_message_id, clock, refused=refused
        )
    except Exception:
        db.rollback()  # 半途插入（如容量淘汰已 flush）一并撤销，不留半批脏数据
        logger.exception(
            "长期记忆抽取失败，静默降级 user_id=%s source_message_id=%s", user_id, source_message_id
        )


def _extract_and_store(
    db: Session,
    deps: ExtractionDeps,
    user_id: int,
    question: str,
    answer: str,
    source_message_id: int | None,
    clock: Clock | None,
    *,
    refused: bool,
) -> None:
    """抽取内联实现（异常边界归 extract_and_store）。"""
    user = db.get(User, user_id)
    if user is None or not user.memory_enabled:
        return  # 用户级开关关闭=冻结（不提炼不注入，§3.5），已有记忆保留不删除
    existing = {content.strip() for content in _active_contents(db, user_id)}
    messages = build_memory_extract_messages(
        deps.prompts.memory_extract,
        sorted(existing),
        question,
        _REFUSED_EXTRACT_ANSWER if refused else answer,
    )
    items = _extract_items(deps, user_id, messages)
    if items is None:
        return  # 两次尝试均无有效产出（_extract_items 已逐次留痕），静默降级
    if not items:
        return  # 合法空数组：本轮无值得记的信息，零新增不提交
    _acquire_user_lock(db, user_id)  # A5：写入临界区起点（LLM 调用留锁外，不长时间持锁）
    if not _memory_enabled_now(db, user_id):
        # B4 落库前复查（LLM 调用耗时窗口内开关可能被关闭——并发关闭形态）：
        # 已关闭则静默放弃，不报错不落库（§3.5 冻结语义覆盖到"最后一刻"）
        logger.info("长期记忆抽取落库前发现开关已关闭，放弃 user_id=%s", user_id)
        return
    # A5：锁内刷新查重全集——并发方在 LLM 窗口内新增的记忆同样参与精确查重
    fresh = {content.strip() for content in _active_contents(db, user_id)}
    if _store_valid_items(db, user_id, items, fresh, source_message_id, clock):
        db.commit()  # 整批一次提交（容量淘汰与插入同事务）；无新增不空转提交


def _store_valid_items(
    db: Session,
    user_id: int,
    items: list[str],
    existing: set[str],
    source_message_id: int | None,
    clock: Clock | None,
) -> int:
    """归一化逐条入库（调用方须持 user 锁）：查重 / 敏感过滤 / 条数与长度硬约束，
    返回实际落库条数（0=零新增，调用方不空转提交）。"""
    seen: set[str] = set()
    for raw in items:
        if len(seen) >= _EXTRACT_MAX_ITEMS:
            break  # §3.2 每轮最多 2 条（按"跳过"语义先查重后计额，重复不占名额）
        text = raw.strip()[:_EXTRACT_ITEM_MAX_CHARS].strip()
        if not text or text in existing or text in seen:
            continue  # 空/已有（精确查重）/本轮内重复：跳过
        if scan_sensitive(text):
            logger.info("长期记忆抽取命中敏感信息，丢弃该条 user_id=%s", user_id)
            continue  # §3.2：命中即丢弃该条不落库（F-1006 口径），同样不占名额
        seen.add(text)
        _insert_memory(
            db,
            user_id,
            text,
            source_message_id=source_message_id,
            audit_detail={"source": "auto"},
            clock=clock,
        )
    return len(seen)


def _extract_items(
    deps: ExtractionDeps, user_id: int, messages: list[dict[str, str]]
) -> list[str] | None:
    """LLM 调用 + 解析（A1 空产物重试）：原始输出为空串或 _parse_items 返回 None 时
    重试 1 次（_MAX_ATTEMPTS=2，tei/convert 同纪律）；模型明确输出合法 []（空数组，
    本轮无值得记的信息）是有效产出，不重试。两次尝试均失败返回 None（调用方静默
    降级）；UpstreamError 是通道故障而非产出质量问题，不在重试范围（沿用原降级语义）。"""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            # 迭代在推导式内、仍在异常边界里：生成器形态首次 next() 才可能抛（rewrite 同款）
            raw = "".join([piece for piece in deps.llm.chat_stream(messages, EXTRACT_MAX_TOKENS)])
        except UpstreamError:
            logger.warning(
                "长期记忆抽取 LLM 调用失败（降级跳过）user_id=%s", user_id, exc_info=True
            )
            return None
        items = _parse_items(raw)
        if raw.strip() and items is not None:
            return items  # 有效产出（合法 [] 亦是——调用方按零新增处理，不重试）
        logger.warning(
            "长期记忆抽取产出为空或非合法数组（attempt=%s/%s，重试判定）user_id=%s",
            attempt + 1,
            _MAX_ATTEMPTS,
            user_id,
        )
    return None


def _memory_enabled_now(db: Session, user_id: int) -> bool:
    """B4：落库前复查用户级开关（§3.5 冻结语义延伸到 INSERT 前）。

    列查询直读数据库——绕过身份映射缓存（开头 db.get 装载的 User 对象不回流），
    READ COMMITTED 下可见 LLM 调用窗口内其他事务对 users.memory_enabled 的最新提交。"""
    return bool(db.scalar(select(User.memory_enabled).where(User.id == user_id)))


def _active_contents(db: Session, user_id: int) -> list[str]:
    """该用户全部活跃记忆正文（查重全集——不限于注入窗口的最近 N 条）。"""
    return list(
        db.scalars(
            select(UserMemory.content).where(
                and_(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
            )
        ).all()
    )


def _parse_items(raw: str) -> list[str] | None:
    """解析抽取产出为字符串数组；代码围栏容错剥离（规则已禁止，模型偶发兜底）；
    非法 JSON / 形状不符（非数组 / 含非字符串项）返回 None（静默降级）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text[3:].lstrip()
        if text[:4].lower() == "json":
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed
