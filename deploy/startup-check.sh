#!/usr/bin/env bash
# 试点栈启动检查（2026-10-02 OOM 事故后新增）：错峰拉起 + 等健康 + 冒烟提问。
# 背景：8G Docker 下 tei-embed(3g)/tei-rerank(4.5g)/mineru(3.15g) 模型同时加载必 OOM 互挤
# （137），且 app 对 TEI 是有意软依赖（不阻塞全栈启动）——必须按序等 healthy 再起下一个。
# 用法：bash deploy/startup-check.sh（Git Bash；.cmd 不适用）
set -euo pipefail
cd "$(dirname "$0")/.."

# 缺省冒烟账号与 app/db/e2e_seed.py 种子对齐（e2e-admin / 显式口令）；
# 覆盖口令时经第 4 步 E2E_PASSWORD 透传给种子，保证登录与落库口令同源
SMOKE_USER="${SMOKE_USER:-e2e-admin}"
# 口令必填（2026-10-31 商用就绪工单 04 去演示化：公开缺省口令是交付级 P0 后门——
# 旧缺省 E2ePass#2026 已移除；e2e_seed 对非测试库同时要求 PILOT_CONFIRM=1）。
# 仅限试点（pilot）装配链路：账号由本脚本种子、prod 发布走 deploy.sh 不经本脚本。
SMOKE_PASS="${SMOKE_PASS:?请显式设置 SMOKE_PASS（试点冒烟种子口令，勿用公开缺省值）}"

log() { echo "[startup-check] $*"; }

await_health() {
  local name="$1" i strict="${2:-1}"
  for i in $(seq 1 90); do
    if [ "$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ]; then
      log "  $name healthy"
      return 0
    fi
    sleep 10
  done
  if [ "$strict" = "1" ]; then
    log "  $name 15 分钟未 healthy，中止"; exit 1
  fi
  log "  $name 未就绪（非阻塞，稍后可单独 docker start）"
}

log "1/4 拉起 pg + tei-embed（embed 先独占内存窗口加载模型）"
docker compose up -d pg tei-embed
await_health ragdemo-pg
await_health ragdemo-tei-embed

log "2/4 拉起 tei-rerank（此时加载，避免与 embed 互挤）"
docker compose up -d tei-rerank
await_health ragdemo-tei-rerank

# mineru 仅文档解析需要（3.15g 大户）；默认不随栈拉起（8G 内存按需启停；是否常驻由
# WITH_MINERU 决定——≥22G 全常驻，阈值唯一常量 MEM_FULL_MB=22528，与 bootstrap.sh/deploy.sh 同源）
if [ "${WITH_MINERU:-0}" = "1" ]; then
  log "  WITH_MINERU=1：拉起 mineru"
  docker compose up -d mineru
  await_health ragdemo-mineru 0
fi

log "3/4 拉起 gotenberg + app"
docker compose up -d gotenberg
await_health ragdemo-gotenberg 0  # 非阻塞：gotenberg 未就绪仅影响老格式转换
docker compose up -d app
await_health ragdemo-app

log "4/4 冒烟提问（embed→检索→rerank→LLM 全链，CPU rerank 慢，最长等 3 分钟）"
# —— pilot-only 守卫：本段会向目标库播 E2E 冒烟账号并跑冒烟问答，仅限试点链路执行 ——
# 生产标志（任一命中即视为生产/业务环境）：① deploy/.env.prod 存在；② 当前 compose 渲染
# 的 app 服务为镜像直拉、无 build 段（prod compose 引 ghcr 镜像，pilot 基础 compose 自建）。
# 命中且未显式 PILOT_CONFIRM=1 → 拒绝执行种子并中止；显式放行 = 放行人自行接受
# e2e-* 测试账号与冒烟问答写入该库。
PROD_FLAG=0
if [ -f deploy/.env.prod ]; then PROD_FLAG=1; fi
if [ "$PROD_FLAG" -eq 0 ]; then
  APP_RENDER=$(docker compose config 2>/dev/null | sed -n '/^  app:/,/^  [a-z][a-z-]*:/p' || true)
  if [ -n "$APP_RENDER" ] && ! echo "$APP_RENDER" | grep -q "build:"; then PROD_FLAG=1; fi
fi
if [ "$PROD_FLAG" = "1" ] && [ "${PILOT_CONFIRM:-0}" != "1" ]; then
  log "检测到生产环境标志（deploy/.env.prod 或 prod compose 形态），拒绝在业务库播 E2E 冒烟账号，中止。"
  log "确为试点链路且接受测试账号入库时，显式 PILOT_CONFIRM=1 重新执行本脚本；生产发布走 deploy.sh，不经本脚本。"
  exit 1
fi
if [ "$PROD_FLAG" = "1" ]; then
  log "PILOT_CONFIRM=1：已确认试点链路，继续执行（生产机请勿放行）。"
fi
# 冒烟前种子账号（全新机无账号登录必 401；e2e_seed 为幂等 upsert，重复装配安全）。
# 种子只在试点装配链路执行：e2e 测试账号不进生产（prod 发布走 deploy.sh，不经本段）。
# PILOT_CONFIRM=1 与口令经 stdin 传递（工单 04：非隔离库种子防呆 + 口令不落命令行）
log "  种子冒烟账号（app.db.e2e_seed，幂等；口令经 stdin 对齐 SMOKE_PASS）"
printf '%s\n' "$SMOKE_PASS" | docker compose exec -T app python -m app.db.e2e_seed --password-stdin
TOKEN_COOKIE=$(mktemp)
curl -sf -c "$TOKEN_COOKIE" -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$SMOKE_USER\",\"password\":\"$SMOKE_PASS\"}" > /dev/null
SMOKE_BODY=$(mktemp)
printf '{"question":"LLM是什么"}' > "$SMOKE_BODY" # 中文走文件体，Git Bash 直传会 GBK 乱码
# 重建后 TEI 首次真实推理常冷超时（embed 60s / rerank 120s），第二问起正常——失败重试一次
for attempt in 1 2; do
  RESULT=$(curl -sf -N --max-time 180 -b "$TOKEN_COOKIE" -X POST http://localhost:8000/api/chat/stream \
    -H "Content-Type: application/json" --data-binary "$SMOKE_BODY" | tail -c 400) && break
  [ "$attempt" = "2" ] && { log "冒烟两次均失败，回答链路仍异常"; exit 1; }
  log "  第 $attempt 次冒烟失败（TEI 冷启动超时常见），20s 后重试"; sleep 20
done
echo "$RESULT" | grep -q '"refused"' && log "冒烟通过：done 事件已返回（refused/cacheHit 见上）" \
  || { log "冒烟未拿到 done 事件，回答链路仍异常：$RESULT"; exit 1; }
log "全栈就绪。注意：仅跑 E2E 时 e2e-visible 会把 TEST_MODE 拉回 true，用完记得 up -d app 切回。"
