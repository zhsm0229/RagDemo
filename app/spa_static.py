r"""前端 SPA 静态托管（工单 26 自 main.py 拆分）：/assets immutable + 入口 no-cache。

STATIC_DIR 未设置（本机 dev 裸 uvicorn / CI test_mode）不挂载，行为与历史一致；必须
最后注册（catch-all 只兜未匹配路径，/docs、/api/** 先注册者优先）。缓存策略出处与
事故根因见 _mount_spa docstring。
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings


class _HashedAssets(StaticFiles):
    """/assets 响应追加 immutable 长缓存（vite hash 文件名，内容变则名变）。"""

    def file_response(self, *args: Any, **kwargs: Any) -> FileResponse:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _no_cache(response: FileResponse) -> FileResponse:
    """index.html 等无 hash 入口文件：no-cache = 可缓存但每次必须协商校验。"""
    response.headers["Cache-Control"] = "no-cache"
    return response


def _mount_spa(app: FastAPI) -> None:
    """前端 dist 托管（§12.2 生产仅 app 发布 80 端口的形态）。

    STATIC_DIR 未设置（本机 dev 裸 uvicorn、CI 的 test_mode API 测试）时不挂载，
    行为与历史完全一致；设置了且存在 index.html 才托管。必须最后注册：catch-all
    只兜未匹配路径（/docs、/api/** 等先注册者优先生效）。

    缓存策略：/assets 是 vite hash 文件名产物（内容变则名变）→ immutable 长缓存；
    index.html 无 hash，必须 no-cache 强制协商校验——否则浏览器启发式缓存旧壳，
    发新版后用户仍加载旧 JS（2026-10-02 zip 按钮旧文案事故的根因之一）。
    """
    # STATIC_DIR 走 Settings（评审 P2：os.getenv 不读 .env——宿主裸跑形态会把配置写
    # 进 .env 而静默不挂载；并入 pydantic 后一处读法，容器 ENV 注入形态不受影响）
    static_dir = get_settings().static_dir
    if not static_dir or not (Path(static_dir) / "index.html").is_file():
        return
    root = Path(static_dir)
    app.mount("/assets", _HashedAssets(directory=root / "assets"), name="spa-assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def _spa(spa_path: str) -> FileResponse:
        if spa_path.startswith("api/"):  # 未匹配的 API 路径保持 404 JSON，绝不回前端页
            raise HTTPException(status_code=404)
        candidate = (root / spa_path).resolve()
        if spa_path and candidate.is_relative_to(root.resolve()) and candidate.is_file():
            return _no_cache(FileResponse(candidate))  # vite 根部零散文件（favicon 等）
        return _no_cache(FileResponse(root / "index.html"))  # 客户端路由（/chat、/kb/**）fallback
