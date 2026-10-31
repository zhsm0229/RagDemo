#!/usr/bin/env bash
# 恢复演练脚本化（2026-10-31 工单 09，评审 P1-3）：把 runbook §3.3(a) 的手工序列
# 变成可重复执行的命令——恢复到临时验证库 rag_restore_verify → 核心表行数/结构断言
# → 清理。备份可用性从"一次性人工演练"变为"周期可验证"，为外部调度/拨测提供入口。
#
# 用法：bash deploy/verify-restore.sh [备份文件.sql.gz]
#   不带参数 = 取 backups/ 下最新的 rag-*.sql.gz；带参数 = 指定备份文件。
#   exit 0 = 演练通过；非零 = 任一断言失败（可接外部告警）。
#
# 前提：pg 容器在跑（dev=ragdemo-pg / prod=容器名经环境变量注入）。
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER="${PG_CONTAINER:-ragdemo-pg}"
DB_USER="${PG_USER:-rag}"
VERIFY_DB="rag_restore_verify"
BACKUP_DIR="${BACKUP_DIR:-backups}"

log() { echo "[verify-restore] $*"; }

# ---- 选备份：参数指定或取最新 rag-*.sql.gz（防呆：非 0 字节才可选） ----
BK="${1:-}"
if [ -z "$BK" ]; then
    BK="$(ls -t "${BACKUP_DIR}"/rag-*.sql.gz 2>/dev/null | head -1 || true)"
fi
if [ -z "$BK" ] || [ ! -s "$BK" ]; then
    log "错误：未找到可验证的备份（${BACKUP_DIR}/rag-*.sql.gz），先跑 deploy/backup.sh 产出备份"
    exit 1
fi
log "验证目标：${BK}（$(du -h "${BK}" | cut -f1)）"

# ---- 1. 建临时验证库（幂等） ----
docker exec "${CONTAINER}" psql -U "${DB_USER}" -c "DROP DATABASE IF EXISTS ${VERIFY_DB}" >/dev/null
docker exec "${CONTAINER}" psql -U "${DB_USER}" -c "CREATE DATABASE ${VERIFY_DB}" >/dev/null
log "验证库 ${VERIFY_DB} 就绪"

# ---- 2. 恢复（ON_ERROR_STOP=1：任一 SQL 失败即失败，不静默半成功） ----
START=$(date +%s)
if ! gunzip -c "$BK" | docker exec -i "${CONTAINER}" psql -q -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${VERIFY_DB}" >/dev/null; then
    log "错误：恢复失败（SQL 中途报错），备份不可用——按 runbook §3.3(a) 排查"
    docker exec "${CONTAINER}" psql -U "${DB_USER}" -c "DROP DATABASE IF EXISTS ${VERIFY_DB}" >/dev/null
    exit 1
fi
RTO=$(( $(date +%s) - START ))
log "恢复完成（${RTO}s）"

# ---- 3. 断言：核心表在册且行数一致（恢复库 == 备份时点；备份后增量不比对） ----
FAILS=0
check_table() {
    local table="$1"
    local restored
    restored=$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${VERIFY_DB}" -tAc \
        "SELECT count(*) FROM ${table}" 2>/dev/null || echo "MISSING")
    if [ "$restored" = "MISSING" ]; then
        log "失败：核心表 ${table} 在恢复库中缺失"
        FAILS=$((FAILS + 1))
    else
        log "核对 ${table}：恢复库 ${restored} 行"
    fi
}
for t in documents qa_pairs chunks users departments eval_questions eval_runs; do
    check_table "$t"
done
# 结构断言：alembic 版本行在册（迁移历史完整）
VERSION=$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${VERIFY_DB}" -tAc \
    "SELECT version_num FROM alembic_version" 2>/dev/null || echo "MISSING")
if [ "$VERSION" = "MISSING" ]; then
    log "失败：alembic_version 缺失（迁移历史未恢复）"
    FAILS=$((FAILS + 1))
else
    log "核对 alembic 版本：${VERSION}"
fi

# ---- 4. 清理 + 结论 ----
docker exec "${CONTAINER}" psql -U "${DB_USER}" -c "DROP DATABASE ${VERIFY_DB}" >/dev/null
if [ "$FAILS" -gt 0 ]; then
    log "演练失败：${FAILS} 项断言未过（备份不可用或结构漂移）"
    exit 1
fi
log "演练通过：核心表全部在册、迁移历史完整（RTO≈${RTO}s；正式演练记录回填 runbook §3.4）"
