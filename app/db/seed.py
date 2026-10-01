"""种子数据（specs/db-design.md §四，禁止空表交付）：三部门 + admin 用户。

- departments：客服(service) / 人力(hr) / 全员(all)，'all' 参与 dept_id IN (...) 过滤语义。
- users：1 个 admin（随机初始密码，bcrypt 哈希入库，明文仅打印一次；
  must_change_password=TRUE 首登强制改密）。
- 幂等：重复执行跳过已存在记录。调用方负责事务提交。

用法（对 dev 库）：.venv/Scripts/python -m app.db.seed
"""

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import Department, User

_SEED_DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("service", "客服"),
    ("hr", "人力"),
    ("all", "全员"),
)


def seed(session: Session, echo: bool = True) -> User:
    """插入三部门与 admin（已存在则跳过），返回 admin 用户；不 commit。"""
    depts: dict[str, Department] = {}
    for code, name in _SEED_DEPARTMENTS:
        dept = session.scalars(select(Department).where(Department.code == code)).first()
        if dept is None:
            dept = Department(code=code, name=name)
            session.add(dept)
            session.flush()  # 取自增 id 供 admin 外键
        depts[code] = dept

    admin = session.scalars(select(User).where(User.username == "admin")).first()
    if admin is not None:
        return admin

    initial_password = secrets.token_urlsafe(12)
    admin = User(
        username="admin",
        password_hash=hash_password(initial_password),
        dept_id=depts["all"].id,
        role="admin",
    )
    session.add(admin)
    session.flush()
    if echo:
        # §6.8"日志禁记密码"的登记豁免（规范审查裁决口径）：bootstrap 一次性凭据，
        # 不打印则无人能完成首次登录；密码只进部署终端 stdout 不落日志文件，且首登
        # 强制改密（must_change_password）后即失效。不得在 seed 之外复用此模式。
        print(f"[seed] admin 初始密码（仅此一次，首登强制改密）: {initial_password}")
    return admin


def main() -> None:
    from app.db.base import SessionLocal  # 延迟导入：避免仅需 seed 函数的场景连库

    with SessionLocal() as session, session.begin():
        seed(session)


if __name__ == "__main__":
    main()
