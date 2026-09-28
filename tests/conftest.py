"""全局 pytest fixtures：`pg`（session 级，对 5434 的 pg-test 执行 alembic upgrade head
并灌种子数据）与 `session`（每测试独立事务，测试结束回滚，保证用例互不污染）。

约定：测试库连接串硬编码指向 docker-compose 的 pg-test（localhost:5434, rag_test）；
conftest 通过 DATABASE_URL 环境变量驱动 alembic env.py 与 app.db.base 的 Settings。
"""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_URL = "postgresql+psycopg://rag:rag@localhost:5434/rag_test"


@pytest.fixture(scope="session")
def pg():
    """session 级：把 DATABASE_URL 指到 pg-test，跑 alembic upgrade head + 种子，交付测试用引擎。"""
    os.environ["DATABASE_URL"] = TEST_DB_URL

    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")

    from app.db.seed import seed

    test_engine = create_engine(TEST_DB_URL)
    with Session(test_engine) as s, s.begin():
        seed(s, echo=False)  # 三部门 + admin；echo=False 避免测试日志泄随机口令
    yield test_engine
    test_engine.dispose()


@pytest.fixture()
def session(pg) -> Session:
    """每测试独立事务：绑定到外层连接的事务上，测试结束整体回滚（truncate 的无痕替代）。"""
    connection = pg.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        transaction.rollback()
        connection.close()
