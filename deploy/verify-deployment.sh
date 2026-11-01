#!/usr/bin/env bash
# ragdemo 部署机一键验收（工单 32）：把工单 13 验收检查单从 markdown 变可执行物。
#
# 用法：
#   bash deploy/verify-deployment.sh [--only crossview|golden|backup|arm64] [--dry-run]
#
# 四子检查（任一 FAIL 非零退出；结果 JSON 归档 deploy/verify-results/<ts>.json）：
#   crossview  串库复测前置核对：治理清单文档下线状态（客服版 v1.1/doc=1、全员旧版
#              v1.1/doc=2）——治理漏项会在真实 embedding 复测里漏拒，先机检堵门；
#              真实链路复测本体需 TEI 独占窗口，脚本末尾打印触发口径（runbook §5.5）。
#   golden     黄金样本解析回归（pytest tests/integration/test_golden.py；MINERU_BASE_URL
#              在位时 mineru 样本真跑，缺席自动 skip——skip 计入 WARN 不算 FAIL）。
#   backup     备份调度实测：真实执行 backup.sh 一次 + last-backup.json 新鲜度（<25h）。
#   arm64      arm64 TEI 推理冒烟（工单 29 部署机复测项）：起 ragdemo-tei-cpu-arm64
#              容器挂模型卷 → 等 /health → /embed 真实一问断言数值向量 → teardown。
#              镜像缺失给构建指引（docker/tei-arm64.Dockerfile）。
#
# 日志口径同 backup.sh：每步 [verify] 时间戳行；任何一步失败即非零退出。

set -uo pipefail

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-deploy/verify-results}"
RESULT_JSON="${RESULT_DIR}/verify-${TS}.json"
BACKUP_DIR="${BACKUP_DIR:-backups}"
PG_CONTAINER="${PG_CONTAINER:-ragdemo-pg}"
PG_DB="${PG_DB:-rag}"
ARM64_IMAGE="${ARM64_IMAGE:-ragdemo-tei-cpu-arm64:cpu-1.9.2}"
ARM64_PORT="${ARM64_PORT:-8191}"
ARM64_MODELS="${ARM64_MODELS:-$(pwd)/data/tei-models}"
FRESH_HOURS=25 # 与 bootstrap.sh 第 10 步同口径

ONLY=""; DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
done

mkdir -p "$RESULT_DIR"
declare -a STEP_NAMES=() STEP_STATUS=() STEP_DETAIL=()

log() { echo "[verify] $(date -u +%FT%TZ) $*"; }
want() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }
record() { STEP_NAMES+=("$1"); STEP_STATUS+=("$2"); STEP_DETAIL+=("$3"); }
run_or_dry() {
  # dry-run 只打印不执行（返回 0 占位）
  if [[ $DRY_RUN -eq 1 ]]; then log "(dry-run) $*"; return 0; fi
  "$@"
}

# ---------- ① crossview：串库复测前置核对 ----------
step_crossview() {
  log "① 串库复测前置核对（治理清单文档下线状态）"
  if ! docker exec "$PG_CONTAINER" psql -U rag -d "$PG_DB" -tAc "SELECT 1" > /dev/null 2>&1; then
    record "crossview" "SKIP" "容器 ${PG_CONTAINER} 不可达（部署机核对口径：docker exec ${PG_CONTAINER} psql）"
    return 0
  fi
  local leak=""
  # 治理动作（W6 T2）：id=1 客服版 v1.1、id=2 全员旧版 v1.1 均应已软删（is_active=false）
  for id in 1 2; do
    local row
    row="$(docker exec "$PG_CONTAINER" psql -U rag -d "$PG_DB" -tAc \
      "SELECT coalesce(title||'|active='||is_active,'MISSING') FROM documents WHERE id=${id}" 2>/dev/null | tr -d ' ')"
    if [[ "$row" == *"active=true"* ]]; then
      leak+="doc id=${id}(${row%%|*}) 仍 active；"
    fi
    log "  doc id=${id}: ${row}"
  done
  if [[ -n "$leak" ]]; then
    record "crossview" "FAIL" "治理漏项：${leak}——真实 embedding 复测前必须先下线（runbook §5.5）"
    return 1
  fi
  record "crossview" "PASS" "治理清单旧版均已下线；真实链路复测按 runbook §5.5 在 TEI 独占窗口发起"
}

# ---------- ② golden：黄金样本解析回归 ----------
step_golden() {
  log "② 黄金样本解析回归（pytest test_golden；MINERU_BASE_URL 在位时 mineru 样本真跑）"
  if [[ $DRY_RUN -eq 1 ]]; then
    record "golden" "SKIP" "(dry-run) pytest tests/integration/test_golden.py -q"
    return 0
  fi
  local out
  if out="$(.venv/bin/python -m pytest tests/integration/test_golden.py -q 2>&1)"; then
    local skipped
    skipped="$(grep -oE "[0-9]+ skipped" <<< "$out" | grep -oE "[0-9]+" || echo 0)"
    if [[ "${skipped:-0}" -gt 0 ]]; then
      record "golden" "WARN" "通过但有 ${skipped} 个样本 skip（细节见输出；mineru 未部署或样本 pending）——${out##*$'\n'}"
    else
      record "golden" "PASS" "全部通过——${out##*$'\n'}"
    fi
  else
    record "golden" "FAIL" "黄金样本回归有失败——${out##*$'\n'}"
    return 1
  fi
}

# ---------- ③ backup：备份调度实测 ----------
step_backup() {
  log "③ 备份调度实测（真实执行 backup.sh 一次 + 新鲜度核对）"
  if ! run_or_dry bash deploy/backup.sh; then
    record "backup" "FAIL" "backup.sh 非零退出（见上方 [backup] 日志）"
    return 1
  fi
  local marker="${BACKUP_DIR}/last-backup.json"
  if [[ ! -f "$marker" ]]; then
    record "backup" "WARN" "last-backup.json 不存在——探针未随本次备份落盘（backup.sh 步骤 4/3 是否启用）"
    return 0
  fi
  local age_h last_ts
  last_ts="$(python3 -c "import json;print(json.load(open('$marker'))['lastSuccessAt'])" 2> /dev/null || echo "")"
  # 跨平台 age 计算（BSD date -j / GNU date -d 语法分叉，统一 python 解析 ISO8601）
  age_h="$(python3 -c "
from datetime import datetime, timezone
try:
    t = datetime.fromisoformat('${last_ts}'.replace('Z', '+00:00'))
    print(int((datetime.now(timezone.utc) - t).total_seconds() // 3600))
except Exception:
    print(999999)
" 2> /dev/null || echo 999999)"
  if (( age_h > FRESH_HOURS )); then
    record "backup" "FAIL" "备份新鲜度 ${age_h}h > ${FRESH_HOURS}h（lastSuccessAt=${last_ts}）"
    return 1
  fi
  record "backup" "PASS" "备份成功且新鲜（age=${age_h}h）"
}

# ---------- ④ arm64：arm64 TEI 推理冒烟（工单 29 部署机复测项） ----------
step_arm64() {
  log "④ arm64 TEI 推理冒烟（镜像 ${ARM64_IMAGE}）"
  if ! docker image inspect "$ARM64_IMAGE" > /dev/null 2>&1; then
    record "arm64" "FAIL" "镜像缺失——本机构建：git clone TEI v1.9.2 后 docker buildx build --platform linux/arm64 -f docker/tei-arm64.Dockerfile（见文件头用法）"
    return 1
  fi
  if [[ $DRY_RUN -eq 1 ]]; then log "(dry-run) 起容器→等 health→/embed 断言→teardown"; return 0; fi
  local cid="verify-arm64-${TS}"
  docker rm -f "$cid" > /dev/null 2>&1 || true
  if ! docker run -d --name "$cid" --platform linux/arm64 -p "${ARM64_PORT}:80" \
    -v "${ARM64_MODELS}/bge-m3:/data/bge-m3:ro" \
    "$ARM64_IMAGE" --model-id /data/bge-m3 > /dev/null 2>&1; then
    record "arm64" "FAIL" "容器起动失败（模型卷路径 ${ARM64_MODELS} 或平台参数核对）"
    return 1
  fi
  local ok=0
  for _ in $(seq 1 60); do
    if curl -sf --max-time 2 "http://localhost:${ARM64_PORT}/health" > /dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if (( ok == 0 )); then
    record "arm64" "FAIL" "120s 内 health 未就绪（容器日志：docker logs ${cid}）"
    docker rm -f "$cid" > /dev/null 2>&1
    return 1
  fi
  local emb
  emb="$(curl -sf --max-time 60 http://localhost:${ARM64_PORT}/embed -X POST \
    -H 'Content-Type: application/json' -d '{"inputs":"年假有几天"}' 2> /dev/null || echo "")"
  docker rm -f "$cid" > /dev/null 2>&1
  local dim
  dim="$(python3 -c "import json,sys;v=json.loads(sys.stdin.read())[0];print(len(v) if isinstance(v,list) and all(isinstance(x,(int,float)) for x in v) else 'BAD')" <<< "$emb" 2> /dev/null || echo BAD)"
  if [[ "$dim" == "1024" ]]; then
    record "arm64" "PASS" "arm64 原生推理通：bge-m3 向量维度 1024（真实一问）"
  else
    record "arm64" "FAIL" "embed 响应异常（dim=${dim}，期望 1024）——响应头 200 字节：${emb:0:200}"
    return 1
  fi
}

# ---------- 执行与归档 ----------
FAILED=0
for step in crossview golden backup arm64; do
  if want "$step"; then
    "step_${step}" || FAILED=1
  else
    record "$step" "SKIP" "--only ${ONLY} 未选中"
  fi
done

# 结果 JSON（bash 数组 → JSON；detail 内引号转义后逐行 printf）
if [[ $DRY_RUN -eq 0 ]]; then
  {
    echo "{"
    echo "  \"timestamp\": \"${TS}\","
    echo "  \"only\": \"${ONLY}\","
    echo "  \"arch\": \"$(uname -m)\","
    echo "  \"steps\": ["
    for i in "${!STEP_NAMES[@]}"; do
      [[ $i -gt 0 ]] && echo ","
      printf '    {"name": "%s", "status": "%s", "detail": "%s"}' \
        "${STEP_NAMES[$i]}" "${STEP_STATUS[$i]}" "$(sed 's/"/\\"/g' <<< "${STEP_DETAIL[$i]}")"
    done
    echo ""
    echo "  ]"
    echo "}"
  } > "$RESULT_JSON"
  log "结果已归档：${RESULT_JSON}"
fi

# 汇总表
echo ""
log "=========== 验收汇总 ==========="
for i in "${!STEP_NAMES[@]}"; do
  log "  [${STEP_STATUS[$i]}] ${STEP_NAMES[$i]} — ${STEP_DETAIL[$i]}"
done
if (( FAILED )); then
  log "结论：FAIL（任一子检查失败即非零退出）"
  exit 1
fi
log "结论：全部选中子检查通过（FAIL=0；SKIP/WARN 见上）"
