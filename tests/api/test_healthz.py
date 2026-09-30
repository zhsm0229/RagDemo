r"""存活探针：无鉴权、零业务语义，供 docker healthcheck 与发布脚本消费（§12.2-3）。"""

from fastapi.testclient import TestClient


def test_healthz_ok_without_auth(client):
    """探针无鉴权可达且返回存活态（未登录 cookie 也必须 200——探针不是业务接口）。"""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_reports_unhealthy_when_db_down(client, monkeypatch):
    """DB 不可达必须 500（fail-closed）：docker healthcheck 据此标记容器 unhealthy。

    raise_server_exceptions=False 与生产 uvicorn 同路径：异常走全局兜底处理器
    （errors.unhandled_error_handler）转 500 通用错误，而非向调用方裸抛堆栈。"""
    from app.api.deps import get_db

    def broken_db() -> None:
        raise RuntimeError("db gone")

    monkeypatch.setitem(client.app.dependency_overrides, get_db, broken_db)
    prod_like = TestClient(client.app, raise_server_exceptions=False)
    resp = prod_like.get("/healthz")
    assert resp.status_code == 500
    assert resp.json()["code"] == 5000
