"""全局 pytest fixtures：`pg`（session 级，对 5434 的 pg-test 执行 alembic upgrade head
并灌种子数据）与 `session`（每测试独立事务，测试结束回滚，保证用例互不污染）。

约定：测试库连接串默认指向 docker-compose 的 pg-test（localhost:5434, rag_test），
可用环境变量 TEST_DB_URL 覆盖——并行车道各自建隔离库（如 rag_test_b）互不踩踏。
必须在 pg fixture 函数体内读取 env，不能留模块 import 期：多进程并发收集时
import 期快照会钉死首个进程读到的值，函数体读取才保证每会话取实时环境。
连接串投递不走 env+缓存时序：pg fixture 显式 set_main_option 给 alembic，
并 cache_clear 后断言 get_settings().database_url == 测试库连接串——get_settings()
是 lru_cache，任何更早的调用都会把 dev 库 URL 钉进缓存，必须显式清除（见
app/core/config.py 的缓存契约注释）。
"""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
# 仅作缺省值供引用；实际连接串由 pg fixture 函数体内读 TEST_DB_URL 环境变量决定
TEST_DB_URL = "postgresql+psycopg://rag:rag@localhost:5434/rag_test"


@pytest.fixture(scope="session")
def pg():
    """session 级：对 pg-test 跑 alembic upgrade head + 种子，交付测试用引擎。

    步骤：函数体内读 TEST_DB_URL（env 可覆盖，并行车道各用隔离库）→ 清
    Settings 缓存并设 DATABASE_URL（供 app 层运行时代码解析）→ 显式把测试库
    连接串写进 alembic 配置（不依赖 env 时序）→ upgrade → 断言 get_settings()
    解析为测试库 → 灌种子。teardown 还原缓存与环境变量。
    """
    from app.core.config import get_settings

    test_db_url = os.environ.get("TEST_DB_URL", TEST_DB_URL)
    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = test_db_url

    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(alembic_cfg, "head")

    assert get_settings().database_url == test_db_url  # 防缓存时序把测试钉到 dev 库

    from app.db.seed import seed

    test_engine = create_engine(test_db_url)
    with Session(test_engine) as s, s.begin():
        seed(s, echo=False)  # 三部门 + admin；echo=False 避免测试日志泄随机口令
    yield test_engine
    test_engine.dispose()
    os.environ.pop("DATABASE_URL", None)
    get_settings.cache_clear()  # 还原：后续测试按 .env 重新解析（dev 库）


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


def login_as(client: TestClient, username: str, password: str) -> Response:
    """登录测试辅助（W5 自各 api 测试文件本地 login 定义上收至此）：POST
    /api/auth/login，cookie 由 client 的 cookie jar 保持，返回响应供调用侧
    `.status_code` 显式断言——故签名带返回值而非 brief 初稿的 None，否则
    机械替换无法做到断言零改动。各文件的 admin_login 因建号用户名/返回值
    （User / int / None）形态不一，保留文件内原样不上收。
    """
    return client.post("/api/auth/login", json={"username": username, "password": password})
