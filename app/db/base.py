"""SQLAlchemy 同步基础设施：Engine / SessionLocal / DeclarativeBase。

- 连接串唯一来源是 app/core/config.py 的 Settings.database_url（.env / DATABASE_URL 环境变量）。
- Engine 创建不触发连接（惰性），pool_pre_ping 防止 PG 重启后的僵死连接。
- 全项目同步栈（psycopg 3）；建表由 Alembic 迁移负责，本模块不做 create_all。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """全项目 ORM 基类；target_metadata 供 Alembic autogenerate 参考。"""


def autogenerate_include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """autogenerate 的 include_object 过滤：忽略 messages 分区子表。

    messages_default / messages_YYYY_MM 由迁移与 ensure_month_partitions() 管理、
    不进 metadata；不过滤会被 autogenerate 误判为多余表而生成 DROP TABLE。
    其余对象一律放行。env.py 与 tests/integration 的零差异断言共用本策略。
    """
    is_unmanaged_partition = (
        type_ == "table"
        and reflected
        and compare_to is None
        and bool(name)
        and name.startswith("messages_")
    )
    return not is_unmanaged_partition


# 连接池按部署形态可调（评审 P2：缺省 5+10 低于同步端点线程池 40 上限，高并发排队；
# 生产按并发档位经 .env 调大，压测口径见工单 15 NFR 取证）
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=get_settings().db_pool_size,
    max_overflow=get_settings().db_max_overflow,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
