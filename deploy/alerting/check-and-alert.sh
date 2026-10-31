#!/usr/bin/env bash
# 最小告警链拨测脚本（deploy/alerting/README.md 的参考实现，工单 09）。
# 四探针汇总：服务存活 / 容器健康与重启计数 / 磁盘水位 / 备份新鲜度。
# 输出单行 JSON {ts, fails, warns}；fails 非空且配置了 ALERT_WEBHOOK_URL 时 POST 告警。
# 用法：crontab 每 5 分钟；或手工 bash deploy/alerting/check-and-alert.sh
set -uo pipefail
cd "$(dirname "$0")/../.."

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FAILS=()
WARNS=()
BACKUP_DIR="${BACKUP_DIR:-backups}"
DISK_WARN="${DISK_WARN:-85}"
DISK_CRIT="${DISK_CRIT:-92}"
# 容器清单（dev 缺省；部署机经环境变量覆盖）
CONTAINERS="${ALERT_CONTAINERS:-ragdemo-app ragdemo-pg ragdemo-tei-embed ragdemo-tei-rerank ragdemo-mineru ragdemo-gotenberg}"

# 1. 服务存活
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:${APP_PORT:-8000}/healthz" 2>/dev/null || echo 000)
if [ "$HTTP_CODE" != "200" ]; then
  FAILS+=("healthz:$HTTP_CODE")
fi

# 2. 容器健康与重启计数
for c in $CONTAINERS; do
  STATE=$(docker inspect "$c" --format '{{.State.Health.Status}}|{{.RestartCount}}' 2>/dev/null || true)
  [ -z "$STATE" ] && STATE="missing|0"
  HEALTH="${STATE%%|*}"
  if [ "$HEALTH" != "healthy" ] && [ "$HEALTH" != "none" ]; then
    FAILS+=("container:$c:$HEALTH")
  fi
  COUNT=$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo 0)
  LAST=$(cat ".alert-restart-$c" 2>/dev/null || echo "$COUNT")
  if [ "$COUNT" -gt "$LAST" ]; then
    FAILS+=("container:$c:restarted($COUNT)")
  fi
  echo "$COUNT" > ".alert-restart-$c" 2>/dev/null || true
done

# 3. 磁盘水位（备份分区 + Docker 数据分区）
for mp in "$BACKUP_DIR" /var/lib/docker; do
  if [ -e "$mp" ]; then
    USE=$(df -P "$mp" | awk 'NR==2 {gsub("%",""); print $5}')
    if [ "$USE" -ge "$DISK_CRIT" ]; then
      FAILS+=("disk:$mp:${USE}%")
    elif [ "$USE" -ge "$DISK_WARN" ]; then
      WARNS+=("disk:$mp:${USE}%")
    fi
  fi
done

# 4. 备份新鲜度（与 bootstrap 体检项 #10 同口径：>25h 告警）
if [ -f "$BACKUP_DIR/last-backup.json" ]; then
  STALE=$(python3 - <<'PY'
import json
from datetime import datetime, timezone
try:
    data = json.load(open("backups/last-backup.json"))
    last = datetime.strptime(data["lastSuccessAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    print(f"{hours:.1f}")
except Exception:
    print("-1")
PY
)
  if [ "$(echo "$STALE > 25" | bc 2>/dev/null || echo 1)" = "1" ]; then
    FAILS+=("backup:stale:${STALE}h")
  fi
else
  FAILS+=("backup:missing-probe")
fi

# 引号数组序列化为 JSON 数组（空数组输出 []；set -u 下用 @+ 守卫展开）
join_quoted() {
  local out="" x
  for x in "$@"; do out="${out}\"${x}\","; done
  echo "${out%,}"
}

printf '{"ts":"%s","fails":[%s],"warns":[%s]}\n' "$TS" \
  "$(join_quoted ${FAILS[@]+"${FAILS[@]}"})" \
  "$(join_quoted ${WARNS[@]+"${WARNS[@]}"})"

# fails 非空且配置 webhook → POST（fire-and-forget，告警通道故障不阻断退出码）
if [ ${#FAILS[@]} -gt 0 ] 2>/dev/null && [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
  curl -s --max-time 5 -X POST "$ALERT_WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"[ragdemo] 告警 $(printf '%s ' "${FAILS[@]}")\"}}" \
    >/dev/null || true
fi
exit 0
