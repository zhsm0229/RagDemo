#!/usr/bin/env bash
# 发布脚本（standards §13.2 发布清单的机器执行部分）：在部署机 VM 上运行。
# 用法：./deploy.sh <ghcr.io/.../ragdemo@sha256:...>
# 前置：同目录 .env.prod 已配置（逐字段清单见 runbook §9）；TEI 模型已预热（download_models.py）。
# 起栈分档（deploy-requirements §4.4"严禁一次性全量 up"）：错峰 pg → tei-embed → tei-rerank
# →（mineru，仅内存足够时）→ gotenberg + app，每步等 healthy 再起下一个（模型互挤 OOM 事故
# 实证）；mineru 是否随栈常驻由 Docker 可分配内存决定，阈值与 bootstrap.sh MEM_FULL_MB 同源。
set -euo pipefail

IMAGE_REF="${1:?用法: $0 <镜像 digest 引用，如 ghcr.io/org/repo/ragdemo@sha256:...>}"
cd "$(dirname "$0")"

log() { echo "[deploy] $*"; }

await_health() { # $1=容器名 $2=strict（1=超时中止发布；0=仅告警不阻塞，如 mineru/gotenberg）
  local name="$1" strict="${2:-1}" i
  for i in $(seq 1 90); do
    if [ "$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ]; then
      log "  $name healthy"
      return 0
    fi
    sleep 10
  done
  if [ "$strict" = "1" ]; then
    log "  $name 15 分钟未 healthy，发布中止（日志：docker logs $name）"
    exit 1
  fi
  log "  $name 未就绪（非阻塞：不挡问答链路，稍后 docker inspect 复查，健康卡会如实暴露）"
}

COMPOSE="docker compose -f docker-compose.prod.yml"

# [0/6] 前置校验：内存分档 + mineru 镜像
# 内存分档（MB 精度比较防整数截断误报，与 bootstrap.sh 体检同源阈值）：≥22G 三巨头可常驻；
# <22G 时 mineru 延后拉起（pdf 解析前按需启停）。读不到限额（MemTotal=0）按低配档保守处理。
MEM_FULL_MB=22528
MEM_TOTAL=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
MEM_MB=$((MEM_TOTAL / 1024 / 1024))
WITH_MINERU=0
if [ "$MEM_MB" -ge "$MEM_FULL_MB" ]; then
  WITH_MINERU=1
  log "[0/6] 内存 $((MEM_MB / 1024))G ≥22G：mineru 随栈常驻"
else
  log "[0/6] 内存 $((MEM_MB / 1024))G <22G：mineru 不随栈拉起（pdf 解析前按需启停，见发布完成指引）"
  if [ "$MEM_MB" -eq 0 ]; then
    log "  警告：docker info 读不到内存限额（MemTotal=0），按低配档保守处理，请人工确认 ≥12G"
  fi
fi

# mineru 镜像校验（prod compose 引用 ${MINERU_IMAGE:-mineru-cpu:2.5.4}，该镜像不经 ghcr 拉取）：
# 常驻档缺失即中止（up 会在拉起 mineru 中途失败）；延后档缺失仅提示——本机不拉起，不挡发布
MINERU_REF="${MINERU_IMAGE:-mineru-cpu:2.5.4}"
if ! docker image inspect "$MINERU_REF" >/dev/null 2>&1; then
  if [ "$WITH_MINERU" = "1" ]; then
    log "mineru 镜像 $MINERU_REF 不在本地，发布中止。修复三选一："
    log "  1) 部署机自建：仓库根目录 docker build -t mineru-cpu:2.5.4 -f docker/mineru-cpu.Dockerfile .（版本见 runbook §6 模型版本表）"
    log "  2) 离线导入：docker load -i <镜像包> 后 docker tag 为 $MINERU_REF"
    log "  3) 引用覆盖：MINERU_IMAGE=<实际在位镜像引用> ./deploy.sh $IMAGE_REF"
    exit 1
  fi
  log "  提示：mineru 镜像 $MINERU_REF 不在本地——低配档本次不拉起、不影响发布；需要 pdf 解析时先按 runbook §6 补镜像"
else
  log "  mineru 镜像在位：$MINERU_REF"
fi

log "[1/6] 拉取镜像 $IMAGE_REF"
APP_IMAGE="$IMAGE_REF" $COMPOSE pull app

# [2/6] 错峰起栈（entrypoint 自动 alembic upgrade head，§12.2-3 迁移随容器）：每步等 healthy
log "[2/6] 错峰起栈：pg → tei-embed → tei-rerank$( [ "$WITH_MINERU" = "1" ] && echo " → mineru" ) → gotenberg → app"
$COMPOSE up -d pg
await_health ragdemo-pg

log "  tei-embed 加载中（BGE-M3，首启最长 10 分钟）"
$COMPOSE up -d tei-embed
await_health ragdemo-tei-embed

log "  tei-rerank 加载中（与 embed 错峰，避免互挤）"
$COMPOSE up -d tei-rerank
await_health ragdemo-tei-rerank

if [ "$WITH_MINERU" = "1" ]; then
  log "  mineru 加载中（最慢 15 分钟，非阻塞）"
  $COMPOSE up -d mineru
  await_health ragdemo-mineru 0
fi

log "  gotenberg（轻量）+ app"
$COMPOSE up -d gotenberg
await_health ragdemo-gotenberg 0 # 非阻塞：未就绪仅影响 doc/ppt/xls 老格式转换
$COMPOSE up -d app
await_health ragdemo-app

# [3/6] 探活/冒烟一律打宿主 80（prod compose 唯一对外端口 "80:8000"，8000 不发布到宿主）
log "[3/6] 等待 healthz（宿主 80，最长 5 分钟）"
ok=0
for _ in $(seq 1 60); do
  if curl -sf http://localhost/healthz >/dev/null; then ok=1; break; fi
  sleep 5
done
if [ "$ok" -ne 1 ]; then
  log "healthz 未就绪，发布中止（容器日志：$COMPOSE logs app）"
  exit 1
fi

log "[4/6] 发布冒烟（healthz / 首页 / 登录接口 / gotenberg 探活）"
curl -sf http://localhost/healthz >/dev/null
curl -sf -o /dev/null http://localhost/
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"__smoke__","password":"__smoke__"}')
if [ "$code" != "401" ]; then log "登录接口异常：HTTP $code"; exit 1; fi
# gotenberg 连通（老格式转换 F-1015；未部署时跳过——老格式功能降级知情即可）
if docker ps --format '{{.Names}}' | grep -q ragdemo-gotenberg; then
  docker exec ragdemo-gotenberg curl -sf http://localhost:3000/health >/dev/null \
    || { log "gotenberg 探活失败（老格式转换不可用）"; exit 1; }
  log "  gotenberg 连通正常"
fi

log "[5/6] 发布完成：$IMAGE_REF"
log "回滚：./rollback.sh <上一版镜像引用>（digest 清单见 deploy/releases.md）"
log "值守：发布后 30 分钟内观察错误日志（§13.3）：$COMPOSE logs -f app"
if [ "$WITH_MINERU" != "1" ]; then
  log "低配档备忘（mineru 按需启停，restart=unless-stopped 手动停后不会自启）："
  log "  pdf 解析前：$COMPOSE up -d mineru（确认内存余量 ≥6G，docker stats）"
  log "  解析完成后：docker stop ragdemo-mineru 释放内存；MINERU_BASE_URL 留空时全程走 pypdf 降级"
fi
