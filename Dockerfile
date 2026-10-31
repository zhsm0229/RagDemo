# 企业智能工作台应用镜像：多阶段构建（前端 dist + Python 运行时）。
# 形态依据 §12.2「仅 app 对外发布 80 端口」：dist 由 FastAPI 静态托管（STATIC_DIR），
# API 走 /api 前缀，容器清单保持五容器不增（无独立 nginx）。
# ---- 构建段：vue-tsc 类型检查 + vite build（类型失败即镜像失败，质量门前置）----
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

# ---- 运行时段：非 root（§6 安全规范），固定 uid/gid 10001 ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATIC_DIR=/srv/ragdemo/static

RUN useradd -r -u 10001 app

WORKDIR /srv/ragdemo

# 先拷元数据再装依赖：pyproject 未变的层可复用缓存；应用目录归 app 用户所有
COPY pyproject.toml ./
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 prompts ./prompts
# 前端产物与入口（dist 归 app 用户只读即可，但统一 chown 简化权限）
COPY --from=web --chown=10001:10001 /web/dist ./static
COPY --chown=10001:10001 docker/entrypoint.sh ./entrypoint.sh

# pip install 仍以 root 执行（装 site-packages 后收紧）；预建运行时上传目录并交权
RUN pip install --no-cache-dir . \
    && mkdir -p /srv/ragdemo/data \
    && chown -R app:app /srv/ragdemo \
    && chmod +x /srv/ragdemo/entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
# gunicorn 多 worker（工单 20，shell 形式：env 展开需经 shell）：WEB_CONCURRENCY 缺省 1
# ≈ 现单进程 uvicorn 形态（worker 类同为 uvicorn）。--timeout 0：gunicorn watchdog 对
# 长 SSE 流有误杀面，async worker 自有心跳，0 关闭并由 compose healthcheck 兜底挂死检出。
CMD gunicorn app.main:app --workers ${WEB_CONCURRENCY:-1} \
    --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 \
    --timeout 0 --graceful-timeout 60 --access-logfile -
