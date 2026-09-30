r"""用户管理服务（F-102，前端 P6 用户管理页后端）：列表 / 发放 / 启停 / 重置密码。

初始密码（F-102 验收）：secrets.choice（密码学安全随机，禁 random）在无歧义
字符集（字母数字去 0O1lI，人工抄写/口头传达不混淆）上取 16 位，仅在发放/重置
的响应体展示一次——库里只有 bcrypt 哈希，明文不落库、不进日志与审计（红线 4）。
禁用即时失效（§5.6 下次请求 401）由 get_current_user 的 is_active 校验天然
实现，本模块只翻 is_active 位并写审计；禁用自己 → 422（防自锁）。用户名口径
（standards §21.5）：2-32 位中文/字母/数字（W3 账本发现 db-design 写 3-32 系
笔误，一律按 standards 执行）；重复 → 409/3005。列表 keyword 对 username
ILIKE 且通配符先转义（§22，同 qa_pairs 口径）。
"""

import re
import string
from datetime import datetime
from secrets import choice

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.errors import ERR_DUPLICATE_USER, ERR_PARAM_INVALID, ApiError
from app.core.security import hash_password
from app.db.models import Department, User
from app.services.audit import record_audit

_USERNAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9]{2,32}$")  # standards §21.5
_ROLES: frozenset[str] = frozenset({"admin", "member"})
_PASSWORD_LENGTH: int = 16
# 无歧义字符集：字母数字去掉 0/O/1/l/I（发放的初始密码要人抄去登录，肉眼可辨）
_PASSWORD_ALPHABET: str = "".join(
    ch for ch in string.ascii_letters + string.digits if ch not in "0O1lI"
)
_ESCAPES = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))  # §22：ILIKE 通配符按字面匹配
_MSG_NOT_FOUND = "用户不存在"


def generate_initial_password(length: int = _PASSWORD_LENGTH) -> str:
    """随机初始密码：secrets.choice 逐位取无歧义字符集（F-102 验收口径）。"""
    return "".join(choice(_PASSWORD_ALPHABET) for _ in range(length))


def list_users(
    db: Session, *, keyword: str | None = None, page: int = 1, page_size: int = 20
) -> dict:
    """管理列表（F-102）：keyword 对 username ILIKE（通配符先转义），created_at
    降序（同刻 id 兜底）。返回 {items, total, page, pageSize}，camelCase DTO。"""
    stmt = select(User, Department.code).join(Department, User.dept_id == Department.id)
    if keyword:
        like = f"%{_escape_like(keyword)}%"
        stmt = stmt.where(User.username.ilike(like, escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    now = get_clock().now()
    return {
        "items": [_item_payload(user, code, now) for user, code in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def create_user(
    db: Session,
    *,
    username: str,
    dept_code: str,
    role: str,
    actor_user_id: int,
    clock: Clock | None = None,
) -> dict:
    """发放账号：用户名/部门/角色校验 → 随机初始密码 bcrypt 落库 → 审计
    user.create → 提交。返回 {id, initialPassword}，明文只出现这一次（二次不可见）。"""
    effective_clock = clock if clock is not None else get_clock()
    _validate_username(username)
    if role not in _ROLES:
        raise ApiError(ERR_PARAM_INVALID, f"角色取值无效：{role}（可选 admin/member）", 422)
    dept_id = _resolve_dept(db, dept_code)
    dup = db.scalars(select(User).where(User.username == username)).first()
    if dup is not None:  # users_username_key 兜底防并发，此处先行给 409 中文提示
        raise ApiError(ERR_DUPLICATE_USER, f"用户名已存在：{username}", 409)
    initial_password = generate_initial_password()
    user = User(
        username=username,
        password_hash=hash_password(initial_password),
        dept_id=dept_id,
        role=role,
        must_change_password=True,  # 发放的号首登强制改密（§21.5）
    )
    db.add(user)
    db.flush()  # 取自增 id 供审计 object_id
    record_audit(
        db,
        actor_user_id,
        "user.create",
        "user",
        user.id,
        {"username": username, "deptCode": dept_code, "role": role},  # 不含明文密码
        clock=effective_clock,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        # 并发抢注同名兜底（DOC-06 同款）：预查重只挡提交前可见的重复，查重通过后、
        # 提交前的并发半边在此撞 users_username_key，回滚并转 409；只捕
        # IntegrityError 一族，其余异常绝不吞。回滚即丢弃半程写入（用户行+审计）。
        db.rollback()
        raise ApiError(ERR_DUPLICATE_USER, f"用户名已存在：{username}", 409) from exc
    return {"id": user.id, "initialPassword": initial_password}


def toggle_user_status(
    db: Session,
    *,
    target_user_id: int,
    is_active: bool,
    actor_user_id: int,
    clock: Clock | None = None,
) -> User:
    """启用/禁用：禁用自己 → 422（防自锁，管理员把自己禁了将无人可管）；审计
    user.toggle {from,to} 后提交。禁用即时失效由 get_current_user 校验 is_active。"""
    if not is_active and target_user_id == actor_user_id:
        raise ApiError(ERR_PARAM_INVALID, "不能禁用当前登录账号", 422)
    user = _get_user(db, target_user_id)
    old_active = user.is_active
    user.is_active = is_active
    user.updated_at = (clock if clock is not None else get_clock()).now()
    record_audit(
        db,
        actor_user_id,
        "user.toggle",
        "user",
        user.id,
        {"from": old_active, "to": is_active},
        clock=clock,
    )
    db.commit()
    return user


def reset_user_password(
    db: Session, *, target_user_id: int, actor_user_id: int, clock: Clock | None = None
) -> dict:
    """重置密码：新随机初始密码一次展示 + must_change=true 强制首登改密；同时清
    失败计数与锁定（管理员重置即恢复可用，与 change_password 口径一致）；审计
    user.reset_password 不含明文。返回 {id, initialPassword}。"""
    effective_clock = clock if clock is not None else get_clock()
    user = _get_user(db, target_user_id)
    initial_password = generate_initial_password()
    user.password_hash = hash_password(initial_password)
    user.must_change_password = True
    user.failed_attempts = 0
    user.locked_until = None
    user.updated_at = effective_clock.now()
    record_audit(
        db,
        actor_user_id,
        "user.reset_password",
        "user",
        user.id,
        {"mustChangePassword": True},
        clock=effective_clock,
    )
    db.commit()
    return {"id": user.id, "initialPassword": initial_password}


def _validate_username(username: str) -> None:
    """用户名口径（standards §21.5）：2-32 位中文/字母/数字；违规 422/1003。"""
    if _USERNAME_RE.fullmatch(username) is None:
        raise ApiError(ERR_PARAM_INVALID, "用户名需为 2-32 位中文、字母或数字", 422)


def _resolve_dept(db: Session, code: str) -> int:
    """deptCode → departments.id：枚举外（或未播种）422，与 documents 同文案口径。"""
    dept = db.scalars(select(Department).where(Department.code == code)).first()
    if dept is None:
        raise ApiError(ERR_PARAM_INVALID, f"部门编码无效：{code}（可选 service/hr/all）", 422)
    return dept.id


def _get_user(db: Session, user_id: int) -> User:
    """按 id 取用户：不存在一律 404 同文案（users 无软删列，不做二次区分）。"""
    user = db.get(User, user_id)
    if user is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    return user


def _escape_like(keyword: str) -> str:
    """§22：`%`/`_`/`\\` 逐一转义，用户关键字按字面匹配不展开为 ILIKE 通配。"""
    for raw, safe in _ESCAPES:
        keyword = keyword.replace(raw, safe)
    return keyword


def _item_payload(user: User, dept_code: str, now: datetime) -> dict:
    """列表 DTO（camelCase，standards §3.4）：无 name 列则 name 即 username；
    locked 按当下时刻判定（locked_until 已过期不算锁着）；数据库行不直接返回。"""
    return {
        "id": user.id,
        "username": user.username,
        "name": user.username,
        "deptCode": dept_code,
        "role": user.role,
        "status": "正常" if user.is_active else "已禁用",
        "locked": user.locked_until is not None and user.locked_until > now,
        "mustChangePassword": user.must_change_password,
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
        "createdAt": user.created_at.isoformat(),
    }
