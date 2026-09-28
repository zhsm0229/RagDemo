# 企业智能工作台应用镜像（Task 16 最小版）。
# CI build 阶段以「构建成功」为验收（standards §20.2-2，tag = 分支名 + 短 SHA）；
# 运行时依赖（pg / 嵌入服务 / MinerU / 对象存储档）由 docker-compose 与部署机提供。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 非 root 运行（§6 安全规范）：固定 uid/gid 10001（数字写法不依赖 /etc/passwd 可解析）
RUN useradd -r -u 10001 app

WORKDIR /srv/ragdemo

# 先拷元数据再装依赖：pyproject 未变的层可复用缓存；应用目录归 app 用户所有
COPY pyproject.toml ./
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 alembic ./alembic

# pip install 仍以 root 执行（装 site-packages 后收紧）；预建运行时上传目录并交权
RUN pip install --no-cache-dir . \
    && mkdir -p /srv/ragdemo/data \
    && chown -R app:app /srv/ragdemo

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
