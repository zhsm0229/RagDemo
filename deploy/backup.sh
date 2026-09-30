#!/usr/bin/env bash
# ragdemo 每日备份脚本（standards §18 / runbook 第 3 节）
#
# 用途：①导出主库（容器 ragdemo-pg 内的 rag 库）pg_dump 全量并 gzip；②打包 uploads 上传目录；
#      ③按保留期清理旧备份。三步任一失败即非零退出（set -euo pipefail）。
# 调度：不在本脚本内写死——每日定时执行方式见 docs/runbook.md 第 3.1 节
#      （Windows 计划任务 schtasks / Linux crontab 两版示例）。
#
# 安全须知（§18.1）：
#   * 备份含全部问答与文档内容，访问权限同生产库——备份目录须独立目录/盘、权限 0700 + 专用系统用户，
#     禁止放入任何可公开访问的路径；
#   * .env（含密钥）不在备份内，副本托管密码管理器。
set -euo pipefail

# ---- 参数化区（按部署环境覆盖，勿在下方逻辑中硬编码）----
CONTAINER="ragdemo-pg"        # 主库容器名（docker-compose.yml pg 服务）
DB_USER="rag"                 # 数据库用户（POSTGRES_USER）
DB_NAME="rag"                 # 业务库名；注意 ragdemo-pg-test/rag_test 是另一实例，不在本脚本备份范围
STORAGE_DIR="./data/uploads"  # 上传目录（app/core/config.py STORAGE_DIR 默认值）
BACKUP_DIR="./backups"        # 备份输出目录（§18.1：独立目录/盘，权限 0700）
RETAIN_DAYS=7                 # 备份保留天数（§18.1）

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [backup] $*"
}
trap 'log "备份失败，退出码 $?（见上方步骤输出）"' ERR

TS="$(date +%Y%m%d-%H%M%S)"

# 前置：备份目录就绪并收敛权限（chmod 失败不阻断——Windows Git Bash 下为近似 no-op，Linux 部署机生效）
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || true

# 步骤 1/3：PostgreSQL 全量导出（gzip 在宿主侧管道完成，备份不落容器内盘）
log "1/3 pg_dump 开始：container=${CONTAINER} db=${DB_NAME}"
docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" "${DB_NAME}" | gzip > "${BACKUP_DIR}/rag-${TS}.sql.gz"
log "1/3 pg_dump 完成：${BACKUP_DIR}/rag-${TS}.sql.gz（大小 $(du -h "${BACKUP_DIR}/rag-${TS}.sql.gz" | cut -f1)）"

# 步骤 2/3：uploads 打包（目录不存在则警告跳过——首次部署可能尚未产生上传；tar 归档内保留 uploads/ 一级目录）
if [ -d "${STORAGE_DIR}" ]; then
    log "2/3 uploads 打包开始：${STORAGE_DIR}"
    tar -czf "${BACKUP_DIR}/uploads-${TS}.tar.gz" -C "$(dirname "${STORAGE_DIR}")" "$(basename "${STORAGE_DIR}")"
    log "2/3 uploads 打包完成：${BACKUP_DIR}/uploads-${TS}.tar.gz（大小 $(du -h "${BACKUP_DIR}/uploads-${TS}.tar.gz" | cut -f1)）"
else
    log "2/3 警告：上传目录 ${STORAGE_DIR} 不存在，跳过 uploads 备份（请核对 STORAGE_DIR 配置）"
fi

# 步骤 3/3：清理超过保留期的备份（仅匹配本目录 *.gz，不触碰运行日志等其他文件）
log "3/3 清理 ${RETAIN_DAYS} 天前的旧备份"
find "${BACKUP_DIR}" -name "*.gz" -mtime +"${RETAIN_DAYS}" -delete
log "全部完成：本轮备份 rag-${TS}.sql.gz$( [ -f "${BACKUP_DIR}/uploads-${TS}.tar.gz" ] && echo " + uploads-${TS}.tar.gz" )"
