r"""用户长期记忆管理服务（F-210 管理面）：手动添加 / 列表 / 软删 / 用户级开关 / 注入召回。

设计基线 docs/specs/agent-evolution.md §三：记忆是用户画像结论，按**用户**隔离
（非部门维度，不存在串库面），本模块全部函数只操作 user_id 本人数据；删除一律
软删（is_active=false，行保留可追溯，不物理删）。容量上限 memory_max_per_user
（§3.2 每用户 30 条）在写入路径统一维护：超限淘汰最旧——自动抽取（memory_extract.py，
行数红线 §1.4 自本模块拆出）与手动添加共用 _insert_memory 本入口，约束不会旁路。
变更类动作写 audit_logs（§3.5 同 F-801 口径）：memory.add / memory.delete /
memory.settings，审计与业务写入同事务 commit（record_audit 只 flush，commit 归
调用方，同 qa_pairs 约定）。

归属裁决（IDOR §21.8）：软删按 id 取行后比对 user_id，不符与不存在一律 404 同
话术"记忆不存在或无权访问"，不区分两种情形、不给探测空间。
"""

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import get_settings
from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ApiError
from app.db.models import User, UserMemory
from app.services.audit import record_audit

_CONTENT_MIN: int = 1
_CONTENT_MAX: int = 200  # 与 user_memories_content_check 同源（自动抽取单条 ≤40 字在抽取侧约束）
_MSG_MEMORY_NOT_FOUND = "记忆不存在或无权访问"
_ESCAPES = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))  # §22：ILIKE 通配符按字面匹配
_SUMMARY_MAX: int = 50  # 审计 detail 摘要截断宽度（内容上限 200 字，防审计行膨胀）


def add_manual(
    db: Session, user_id: int, content: str, *, clock: Clock | None = None
) -> UserMemory:
    """手动添加一条记忆：strip 后 1-200 字（422/1003）→ 容量淘汰 → 落库 + 审计。

    source_message_id 留空（NULL=手动添加）；自动抽取路径（memory_extract.py）带
    来源消息 id 复用 _insert_memory 落库。commit 归本函数（审计同事务）。
    """
    text_value = _validated_content(content)
    memory = _insert_memory(
        db,
        user_id,
        text_value,
        source_message_id=None,
        audit_detail={"source": "manual"},
        clock=clock,
    )
    db.commit()
    return memory


def recall_recent(db: Session, user_id: int) -> list[str]:
    """注入召回（§3.3）：本人 memory_enabled 且 is_active 记忆按 created_at DESC 取
    ≤ memory_inject_limit 条的正文（strip，空串剔除）。

    时间倒序语义是"最近发生的画像"而非"最相关的画像"（无向量列，§3.1）；同刻以
    id 降序兜底（load_recent_history 同款）。开关关闭或无记忆返回空列表——注入为
    空，链路退化为现状（缓存键不含 user 维度，§3.4）。
    """
    user = db.get(User, user_id)
    if user is None or not user.memory_enabled:
        return []
    rows = db.scalars(
        select(UserMemory.content)
        .where(and_(UserMemory.user_id == user_id, UserMemory.is_active.is_(True)))
        .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
        .limit(get_settings().memory_inject_limit)
    ).all()
    return [content.strip() for content in rows if content and content.strip()]


def list_for_user(
    db: Session,
    user_id: int,
    *,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """本人记忆列表（管理面）：软删行不出现，updated_at 降序（同刻 id 兜底），分页。

    keyword 对内容 ILIKE 且通配符先转义（§22）；返回 {items, total, page, pageSize}，
    items 为 camelCase DTO（standards §3.4）。注入路径（工单 03）另走精简查询，
    不经本函数（列表 DTO 无需注入语义）。
    """
    conditions = [UserMemory.user_id == user_id, UserMemory.is_active.is_(True)]
    if keyword and keyword.strip():
        escaped = _escape_like(keyword.strip())
        conditions.append(UserMemory.content.ilike(f"%{escaped}%"))
    where = and_(*conditions)
    total = db.scalar(select(func.count()).select_from(UserMemory).where(where)) or 0
    rows = db.scalars(
        select(UserMemory)
        .where(where)
        .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [item_payload(m) for m in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def soft_delete(
    db: Session, user_id: int, memory_id: int, *, clock: Clock | None = None
) -> UserMemory:
    """软删本人记忆（行保留可追溯）：归属不符与不存在一律 404 同话术；审计 memory.delete。"""
    memory = _get_own_active(db, user_id, memory_id)
    memory.is_active = False
    record_audit(
        db,
        user_id,
        "memory.delete",
        "user_memory",
        memory.id,
        {"content": _summary(memory.content)},
        clock=clock,
    )
    db.commit()
    return memory


def set_enabled(db: Session, user: User, enabled: bool, *, clock: Clock | None = None) -> User:
    """用户级记忆开关（§3.5）：false=冻结（不注入不提炼），已有记忆保留不删除；
    审计 memory.settings {enabled}。幂等写（同值重放为无净变化的留痕，同 qa.toggle 口径）。"""
    user.memory_enabled = enabled
    record_audit(db, user.id, "memory.settings", "user", user.id, {"enabled": enabled}, clock=clock)
    db.commit()
    return user


def _validated_content(content: str) -> str:
    """内容校验（手动添加与自动抽取共用口径）：strip 后 1-200 字，越界 422/1003。"""
    text_value = (content or "").strip()
    if not _CONTENT_MIN <= len(text_value) <= _CONTENT_MAX:
        raise ApiError(ERR_PARAM_INVALID, f"记忆内容须在 {_CONTENT_MIN}-{_CONTENT_MAX} 字之间", 422)
    return text_value


def _insert_memory(
    db: Session,
    user_id: int,
    text_value: str,
    *,
    source_message_id: int | None,
    audit_detail: dict,
    clock: Clock | None = None,
) -> UserMemory:
    """唯一写入入口（容量约束不旁路）：容量淘汰 → INSERT → 审计；只 flush 不 commit，
    事务边界归调用方（手动添加逐条 commit，自动抽取整批一次 commit）。"""
    _enforce_capacity(db, user_id)
    memory = UserMemory(user_id=user_id, content=text_value, source_message_id=source_message_id)
    db.add(memory)
    db.flush()  # 取 id 供审计 object_id
    record_audit(db, user_id, "memory.add", "user_memory", memory.id, audit_detail, clock=clock)
    return memory


def _enforce_capacity(db: Session, user_id: int) -> None:
    """写入前容量裁决（§3.2）：有效记忆数将超 memory_max_per_user 时软删最旧腾位。

    最旧按 updated_at 升序（同刻 id 升序兜底）；只置 is_active=false，不物理删。
    """
    max_per_user = get_settings().memory_max_per_user
    active_cond = and_(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
    active_count = db.scalar(select(func.count()).select_from(UserMemory).where(active_cond)) or 0
    overflow = active_count + 1 - max_per_user  # 本次写入后的超出量
    if overflow <= 0:
        return
    oldest = db.scalars(
        select(UserMemory)
        .where(active_cond)
        .order_by(UserMemory.updated_at.asc(), UserMemory.id.asc())
        .limit(overflow)
    ).all()
    for memory in oldest:
        memory.is_active = False
    db.flush()


def _get_own_active(db: Session, user_id: int, memory_id: int) -> UserMemory:
    """按 id 取本人未软删记忆：不存在、已软删或归属他人一律 404 同话术（IDOR §21.8）。"""
    memory = db.scalars(
        select(UserMemory).where(UserMemory.id == memory_id, UserMemory.is_active.is_(True))
    ).first()
    if memory is None or memory.user_id != user_id:
        raise ApiError(ERR_NOT_FOUND, _MSG_MEMORY_NOT_FOUND, 404)
    return memory


def item_payload(memory: UserMemory) -> dict:
    """列表/读回 DTO（camelCase，standards §3.4）：数据库行不直接返回；API 层
    POST 的单条响应用同一映射（列表与新建形状零漂移）。

    来源由 source_message_id 推导：非空=自动提炼（带溯源消息 id），空=手动添加。
    """
    return {
        "id": memory.id,
        "content": memory.content,
        "source": "auto" if memory.source_message_id is not None else "manual",
        "sourceMessageId": memory.source_message_id,
        "createdAt": memory.created_at.isoformat(),
    }


def _escape_like(keyword: str) -> str:
    """§22：`%`/`_`/`\\` 逐一转义，用户关键字按字面匹配不展开为 ILIKE 通配。"""
    for raw, safe in _ESCAPES:
        keyword = keyword.replace(raw, safe)
    return keyword


def _summary(text: str) -> str:
    """审计 detail 摘要：超宽截断（审计快照够定位即可，不复制全文）。"""
    return text if len(text) <= _SUMMARY_MAX else text[:_SUMMARY_MAX]
