#!/usr/bin/env bash
# 试点栈按组件诊断与恢复（2026-10-02 OOM 事故后新增，配套 startup-check.sh）。
# 用法：
#   bash deploy/recover.sh            # 全量诊断（只读，不改状态）
#   bash deploy/recover.sh embed      # 拉起 tei-embed 并等健康
#   bash deploy/recover.sh rerank     # 拉起 tei-rerank 并等健康（OOM 后常见）
#   bash deploy/recover.sh mineru     # 拉起 mineru（解析文档前）
#   bash deploy/recover.sh gotenberg  # 拉起 gotenberg（老格式转换前）
#   bash deploy/recover.sh app        # 重建 app（切 TEST_MODE 后使环境变量生效）
#   bash deploy/recover.sh all        # 全栈错峰拉起 + 冒烟（等同 startup-check.sh）
set -euo pipefail
cd "$(dirname "$0")/.."

log() { echo "[recover] $*"; }

state() { docker inspect "$1" --format '{{.State.Status}}/{{.State.Health.Status}}' 2>/dev/null || echo "missing/-"; }

await_health() {
  local name="$1" i
  for i in $(seq 1 90); do
    [ "$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && { log "  $name healthy"; return 0; }
    sleep 10
  done
  log "  $name 15 分钟未 healthy，中止"; exit 1
}

# 退出原因核对：137=OOM/SIGKILL（8G 内存下与另两个模型服务互挤），提示内存取舍而非盲目重启
check_exit_reason() {
  local name="$1" code
  code=$(docker inspect "$name" --format '{{.State.ExitCode}}' 2>/dev/null || echo "?")
  if [ "$code" = "137" ]; then
    log "  注意：$name 上次退出码 137（OOM 被杀）。若反复出现：停 mineru（docker stop ragdemo-mineru）"
    log "  或按 .wslconfig 提升内存（需重启 Docker Desktop 生效）后再试。"
  fi
}

diag() {
  log "—— 容器状态（status/health）——"
  for c in ragdemo-pg ragdemo-tei-embed ragdemo-tei-rerank ragdemo-mineru ragdemo-gotenberg ragdemo-app; do
    echo "  $c: $(state "$c")"
  done
  log "—— 应用探活 ——"
  curl -sf -o /dev/null -w "  app /healthz: %{http_code}\n" --max-time 5 http://localhost:8000/healthz \
    || echo "  app /healthz: 不可达"
  log "—— app 近 30 分钟错误摘要 ——"
  docker logs ragdemo-app --since 30m 2>&1 | grep -iE "error|UpstreamError|refused|timed out" | tail -5 || echo "  （无错误日志）"
  log "处置指引：rerank/embed 挂 → recover.sh rerank|embed；切 TEST_MODE 后 → recover.sh app；"
  log "全栈从停机拉起 → bash deploy/startup-check.sh"
}

case "${1:-diag}" in
  diag) diag ;;
  embed)
    check_exit_reason ragdemo-tei-embed
    docker compose up -d tei-embed; await_health ragdemo-tei-embed ;;
  rerank)
    check_exit_reason ragdemo-tei-rerank
    docker compose up -d tei-rerank; await_health ragdemo-tei-rerank ;;
  mineru)
    check_exit_reason ragdemo-mineru
    docker compose up -d mineru; await_health ragdemo-mineru ;;
  gotenberg)
    check_exit_reason ragdemo-gotenberg
    docker compose up -d gotenberg; await_health ragdemo-gotenberg ;;
  all) exec bash "$(dirname "$0")/startup-check.sh" ;;
  app)
    docker compose up -d --force-recreate app; await_health ragdemo-app
    log "  环境变量（如 TEST_MODE）已按 .env 重新生效" ;;
  all) exec bash "$(dirname "$0")/startup-check.sh" ;;
  *) log "未知组件：$1（可用：diag/embed/rerank/mineru/app/all）"; exit 1 ;;
esac
