r"""前端 dist 托管（STATIC_DIR）：SPA fallback 与 API 404 语义的边界测试。

未设 STATIC_DIR 时不挂载（行为与历史一致）；设置后客户端路由回 index.html、
vite 根部静态文件直出、未匹配的 /api/** 保持 404 JSON 绝不回前端页。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return dist


def test_spa_fallback_serves_index_for_client_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("STATIC_DIR", str(_make_dist(tmp_path)))
    client = TestClient(create_app())
    for path in ("/", "/chat", "/kb/documents", "/admin/eval"):
        resp = client.get(path)
        assert resp.status_code == 200 and "SPA" in resp.text, path


def test_spa_serves_root_static_files_and_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("STATIC_DIR", str(_make_dist(tmp_path)))
    client = TestClient(create_app())
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/favicon.ico").content == b"\x00\x00\x01\x00"


def test_unmatched_api_paths_stay_json_404(tmp_path, monkeypatch):
    """未匹配的 /api/** 必须 404 JSON——绝不把前端 index.html 回给 API 调用方。"""
    monkeypatch.setenv("STATIC_DIR", str(_make_dist(tmp_path)))
    client = TestClient(create_app())
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404
    assert "SPA" not in resp.text


def test_without_static_dir_behavior_unchanged(monkeypatch):
    """未设 STATIC_DIR（本机 dev / CI test_mode）：不挂载，/ 返回 404 与历史一致。"""
    monkeypatch.delenv("STATIC_DIR", raising=False)
    client = TestClient(create_app())
    assert client.get("/").status_code == 404
