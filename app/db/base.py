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


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
