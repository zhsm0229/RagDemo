"""服务健康卡端点测试（GET /api/admin/services/health，2026-10-02 试点反馈新增）。

覆盖：test_mode 下核心链路短路全绿（CI 无 TEI/外网可用）、探活失败标记 down 且
overall=down、仅 mineru 失败为 degraded（问答不受影响）、member 403 / 未登录 401。
探活不发真实网络请求：非 test_mode 场景 monkeypatch 模块级 _probe_http；
用户自建（种子账号密码会被各轮 E2E seed 轮换，不作为测试依赖）。
"""

import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Department, User
from app.services import services_health
from tests.conftest import login_as


@pytest.fixture()
def real_mode(monkeypatch):
    """关掉 test_mode 并清 settings 缓存：走真实探活分支（探活函数被 monkeypatch）。"""
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "false"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（口径同 test_zip_import.make_user），挂在测试事务内。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def login_admin(client: TestClient, session: Session) -> None:
    make_user(session, "svchealth-admin", "Svchealth#2026", role="admin")
    assert login_as(client, "svchealth-admin", "Svchealth#2026").status_code == 200


def test_services_health_test_mode_core_up_and_mineru_not_configured(
    test_mode, client, session, monkeypatch
):
    """test_mode：embed/rerank/llm 走桩标 up（不发网络请求）；mineru 未配置为
    not_configured 不计入故障；pg 真实探活 up；overall=up。"""
    monkeypatch.setenv("MINERU_BASE_URL", "")  # 钉死未配置场景，不随本机 .env 漂移
    monkeypatch.setenv("CONVERT_BASE_URL", "")  # gotenberg 未配置 → not_configured
    get_settings.cache_clear()
    login_admin(client, session)
    resp = client.get("/api/admin/services/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_key = {s["key"]: s for s in data["services"]}
    assert set(by_key) == {"pg", "embed", "rerank", "llm", "mineru", "gotenberg"}
    assert by_key["pg"]["status"] == "up"
    for key in ("embed", "rerank", "llm"):
        assert by_key[key]["status"] == "up", key
        assert "TEST_MODE" in by_key[key]["detail"]
    assert by_key["mineru"]["status"] == "not_configured"
    assert by_key["gotenberg"]["status"] == "not_configured"
    assert data["overall"] == "up"


def test_services_health_probe_failure_marks_down(real_mode, client, session, monkeypatch):
    """非 test_mode：核心依赖探活失败 → 该行 down（detail 含原因摘要）且 overall=down。"""

    async def fake_probe(key, label, base_url, path, headers=None, timeout_s=None, **kwargs):
        if key == "rerank":
            return services_health.ServiceHealth(key, label, "down", 2500, "ReadTimeout")
        return services_health.ServiceHealth(key, label, "up", 5, "")

    monkeypatch.setattr(services_health, "_probe_http", fake_probe)
    login_admin(client, session)
    resp = client.get("/api/admin/services/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_key = {s["key"]: s for s in data["services"]}
    assert by_key["rerank"]["status"] == "down"
    assert by_key["rerank"]["detail"] == "ReadTimeout"
    assert data["overall"] == "down"


def test_services_health_mineru_down_alone_is_degraded(real_mode, client, session, monkeypatch):
    """仅 mineru down：问答链路（pg/embed/rerank/llm）不受影响 → overall=degraded
    而非 down（红/黄语义：down=问答不可用，degraded=仅文档解析不可用）。"""

    async def fake_probe(key, label, base_url, path, headers=None, timeout_s=None, **kwargs):
        if key == "mineru":
            return services_health.ServiceHealth(key, label, "down", 2500, "ConnectError")
        return services_health.ServiceHealth(key, label, "up", 5, "")

    monkeypatch.setattr(services_health, "_probe_http", fake_probe)
    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru.invalid")
    monkeypatch.setenv("CONVERT_BASE_URL", "http://gotenberg.invalid")
    get_settings.cache_clear()
    login_admin(client, session)
    data = client.get("/api/admin/services/health").json()["data"]
    assert data["overall"] == "degraded"
    assert {s["status"] for s in data["services"] if s["key"] != "mineru"} == {"up"}


def test_services_health_gotenberg_down_alone_is_degraded(real_mode, client, session, monkeypatch):
    """仅 gotenberg down：问答链路不受影响 → overall=degraded（与 mineru 同语义，
    down=问答不可用 / degraded=辅助能力不可用——gotenberg 只影响老格式上传）。"""

    async def fake_probe(key, label, base_url, path, headers=None, timeout_s=None, **kwargs):
        if key == "gotenberg":
            return services_health.ServiceHealth(key, label, "down", 2500, "ConnectError")
        return services_health.ServiceHealth(key, label, "up", 5, "")

    monkeypatch.setattr(services_health, "_probe_http", fake_probe)
    monkeypatch.setenv("CONVERT_BASE_URL", "http://gotenberg.invalid")
    get_settings.cache_clear()
    login_admin(client, session)
    data = client.get("/api/admin/services/health").json()["data"]
    assert data["overall"] == "degraded"
    by_key = {s["key"]: s for s in data["services"]}
    assert by_key["gotenberg"]["status"] == "down"


def test_services_health_forbidden_for_member(client, session):
    """member 访问 → 403/2004（管理端点权限矩阵）。"""
    make_user(session, "svchealth-member", "Svchealth#2026", role="member")
    assert login_as(client, "svchealth-member", "Svchealth#2026").status_code == 200
    resp = client.get("/api/admin/services/health")
    assert resp.status_code == 403
    assert resp.json()["code"] == 2004


def test_services_health_unauthenticated_401(client):
    """未登录 → 401/2003。"""
    resp = client.get("/api/admin/services/health")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003


# ---------------------------------------------------------------------------
# I15：mineru 探活与 FastAPI /docs 开关解耦
# MinerU 2.5.4 容器实测（2026-10-06）无自有 /health（/health、/ 均为 404），唯一路由
# POST /file_parse，GET 得 405=服务在位且路由已挂载 → 探针打 /file_parse、405 视为 up。
# ---------------------------------------------------------------------------


def test_probe_http_mineru_up_even_when_docs_disabled() -> None:
    """健康卡对 mineru docs 开关不敏感：模拟生产加固关闭 /docs（404）的容器，
    探针 GET /file_parse 得 405 仍判 up（extra_ok 白名单），detail 为空。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(405, json={"detail": "Method Not Allowed"})

    report = asyncio.run(
        services_health._probe_http(
            "mineru",
            "文档解析 (MinerU)",
            "http://mineru.invalid",
            "/file_parse",
            extra_ok=frozenset({405}),
            transport=httpx.MockTransport(handler),
        )
    )
    assert report.status == "up"  # docs 关掉不再误报 down→degraded
    assert report.detail == ""


def test_probe_http_three_states() -> None:
    """_probe_http 直测三态（MockTransport 注入）：2xx=up；5xx=down（HTTP 前缀摘要）；
    连接错误=down（异常类型+消息摘要）。"""

    ok = asyncio.run(
        services_health._probe_http(
            "k",
            "标签",
            "http://x.invalid",
            "/health",
            transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        )
    )
    assert (ok.status, ok.detail) == ("up", "")

    server_err = asyncio.run(
        services_health._probe_http(
            "k",
            "标签",
            "http://x.invalid",
            "/health",
            transport=httpx.MockTransport(lambda request: httpx.Response(503, text="busy")),
        )
    )
    assert (server_err.status, server_err.detail) == ("down", "HTTP 503")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    conn_err = asyncio.run(
        services_health._probe_http(
            "k",
            "标签",
            "http://x.invalid",
            "/health",
            transport=httpx.MockTransport(broken),
        )
    )
    assert conn_err.status == "down"
    assert conn_err.detail.startswith("ConnectError:")


def test_services_health_mineru_probe_uses_stable_file_parse_path(
    real_mode, client, session, monkeypatch
):
    """mineru 探活打稳定路径 GET /file_parse（405 视为 up），不再依赖 FastAPI /docs
    开关——生产关闭 docs 时健康卡不再误报 degraded（I15 主断言）。"""
    captured: dict[str, object] = {}

    async def fake_probe(key, label, base_url, path, headers=None, timeout_s=None, **kwargs):
        captured["path"] = path
        captured["extra_ok"] = kwargs.get("extra_ok")
        return services_health.ServiceHealth(key, label, "up", 5, "")

    monkeypatch.setattr(services_health, "_probe_http", fake_probe)
    monkeypatch.setenv("MINERU_BASE_URL", "http://mineru.invalid")
    monkeypatch.setenv("CONVERT_BASE_URL", "")  # gotenberg 未配置分支，聚焦 mineru 断言
    get_settings.cache_clear()
    login_admin(client, session)
    data = client.get("/api/admin/services/health").json()["data"]
    assert data["overall"] == "up"
    assert captured["path"] == "/file_parse"  # 稳定探针路径（原为与 docs 开关耦合的 /docs）
    assert captured["extra_ok"] == frozenset({405})  # GET 打 POST-only 路由 → 405=up
