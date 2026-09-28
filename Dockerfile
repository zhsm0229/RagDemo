# 企业智能工作台应用镜像（Task 16 最小版）。
# CI build 阶段以「构建成功」为验收（standards §20.2-2，tag = 分支名 + 短 SHA）；
# 运行时依赖（pg / 嵌入服务 / MinerU / 对象存储档）由 docker-compose 与部署机提供。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/ragdemo

# 先拷元数据再装依赖：pyproject 未变的层可复用缓存
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
