#!/usr/bin/env bash
# 发布脚本（standards §13.2 发布清单的机器执行部分）：在部署机 VM 上运行。
# 用法：./deploy.sh <ghcr.io/.../ragdemo@sha256:...>
# 前置：同目录 .env.prod 已配置（字段清单见 runbook）；TEI 模型已预热（download_models.py）。
set -euo pipefail

IMAGE_REF="${1:?用法: $0 <镜像 digest 引用，如 ghcr.io/org/repo/ragdemo@sha256:...>}"
cd "$(dirname "$0")"

echo "[1/5] 拉取镜像 $IMAGE_REF"
APP_IMAGE="$IMAGE_REF" docker compose -f docker-compose.prod.yml pull app

echo "[2/5] 起栈（entrypoint 自动 alembic upgrade head，§12.2-3 迁移随容器）"
APP_IMAGE="$IMAGE_REF" docker compose -f docker-compose.prod.yml up -d

echo "[3/5] 等待 healthz（最长 5 分钟）"
ok=0
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8000/healthz >/dev/null; then ok=1; break; fi
  sleep 5
done
if [ "$ok" -ne 1 ]; then
  echo "healthz 未就绪，发布中止（容器日志：docker compose -f docker-compose.prod.yml logs app）"
  exit 1
fi

echo "[4/5] 发布冒烟（healthz / 首页 / 登录接口探活）"
curl -sf http://localhost:8000/healthz >/dev/null
curl -sf -o /dev/null http://localhost:8000/
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"__smoke__","password":"__smoke__"}')
[ "$code" = "401" ] || { echo "登录接口异常：HTTP $code"; exit 1; }

echo "[5/5] 发布完成：$IMAGE_REF"
echo "回滚：./rollback.sh <上一版镜像引用>（digest 清单见 deploy/releases.md）"
echo "值守：发布后 30 分钟内观察错误日志（§13.3）：docker compose -f docker-compose.prod.yml logs -f app"
