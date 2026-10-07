#!/usr/bin/env bash
# ragdemo 每日备份脚本（standards §18 / runbook 第 3 节）
#
# 用途：①导出主库（容器 ragdemo-pg 内的 rag 库）pg_dump 全量并 gzip；②打包 uploads 上传目录；
#      ③按保留期清理旧备份。三步任一失败即非零退出（set -euo pipefail）。
# 产物防残片（cross-review I24）：先写 *.part 临时名 → 完整性校验（gzip -t / tar -tzf）→
#      同目录原子改名（rename(2)）→ 落 *.sha256 边车。中断/磁盘满只可能留下 *.part（保留期
#      一并清理），截断残片永远不会顶着正式备份名冒充可用备份；*.sha256 供速检真伪
#      （cd backups && sha256sum -c rag-*.sql.gz.sha256）。
# uploads 双模式（cross-review I26）：UPLOADS_VOLUME 非空=prod named volume（临时容器只读挂卷
#      打包，部署机口径见 runbook §3.6）；空=宿主目录（dev 缺省 ./data/uploads）。
# 干跑：DRY_RUN=1 只打印将执行的动作与产物路径（不碰 docker、不写任何文件），用于变更前
#      核对参数推演（runbook §3.2）。
# 调度：不在本脚本内写死——每日定时执行方式见 docs/runbook.md 第 3.1 节
#      （Windows 计划任务 schtasks / Linux crontab 两版示例）。
#
# 安全须知（§18.1）：
#   * 备份含全部问答与文档内容，访问权限同生产库——备份目录须独立目录/盘、权限 0700 + 专用系统用户，
#     禁止放入任何可公开访问的路径；
#   * .env（含密钥）不在备份内，副本托管密码管理器。
set -euo pipefail

# ---- 参数化区（按部署环境覆盖，勿在下方逻辑中硬编码）----
CONTAINER="ragdemo-pg"        # 主库容器名（docker-compose.yml pg 服务 / prod compose 同名）
DB_USER="rag"                 # 数据库用户（POSTGRES_USER）
DB_NAME="rag"                 # 业务库名；注意 ragdemo-pg-test/rag_test 是另一实例，不在本脚本备份范围
STORAGE_DIR="./data/uploads"  # 宿主目录模式的上传目录（app/core/config.py STORAGE_DIR 默认值；
                              # dev 嵌套布局说明见 docker-compose.yml app.volumes 注释）
UPLOADS_VOLUME="${UPLOADS_VOLUME:-}" # prod uploads 卷名（prod compose 项目名 deploy → deploy_uploads；
                              # 发现命令：docker volume ls | grep uploads）；非空即走卷模式
TAR_IMAGE="pgvector/pgvector:pg15"   # 卷打包临时容器镜像（部署机必有，免额外拉 alpine；
                              # 非 root --user 读卷：app 以 uid 10001 写文件且 0644 可读）
BACKUP_DIR="./backups"        # 备份输出目录（§18.1：独立目录/盘，权限 0700）
RETAIN_DAYS=7                 # 备份保留天数（§18.1）
DRY_RUN="${DRY_RUN:-0}"       # 1=干跑（只打印动作与路径，不执行不落盘）

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [backup] $*"
}
trap 'log "备份失败，退出码 $?（正式产物不受影响；残留 *.part 可待保留期清理或手工删除）"' ERR

TS="$(date +%Y%m%d-%H%M%S)"

# 干跑开关：run() 与 finalize() 在干跑下只打印，不做副作用（变更验证口径见 runbook §3.2）
if [ "$DRY_RUN" = "1" ]; then
    run() { log "干跑：$*"; }
else
    run() { "$@"; }
fi

# 收尾：.part → 非空校验 → 完整性校验（checker=gzip|tar）→ 原子改名 → sha256 边车
# $1=.part 路径 $2=正式名 $3=校验器；mv 为同目录 rename(2)，原子生效、无中间态
finalize() {
    local part="$1" final="$2" checker="$3"
    if [ "$DRY_RUN" = "1" ]; then
        log "干跑：校验（$checker -t）+ 原子改名 $part → $final（+ ${final}.sha256）"
        return 0
    fi
    if [ ! -s "$part" ]; then
        log "错误：$part 为 0 字节，备份无效（保留 .part 供排查，不产出正式名）"
        exit 1
    fi
    # 显式 if ! 判定、不依赖 errexit（函数被 if 条件等上下文调用时 errexit 会被挂起，漏判即残片放行）
    if [ "$checker" = "tar" ]; then
        if ! tar -tzf "$part" >/dev/null; then
            log "错误：$part tar 完整性校验失败（截断或损坏），保留 .part 供排查，不产出正式名"
            exit 1
        fi
    else
        if ! gzip -t "$part"; then
            log "错误：$part gzip 完整性校验失败（截断或损坏），保留 .part 供排查，不产出正式名"
            exit 1
        fi
    fi
    mv -f "$part" "$final"
    sha256sum "$final" > "${final}.sha256"
    log "产物就绪：$final（$(du -h "$final" | cut -f1)，sha256=$(cut -d' ' -f1 "${final}.sha256")）"
}

# 前置：备份目录就绪并收敛权限（chmod 失败不阻断——Windows Git Bash 下为近似 no-op，Linux 部署机生效）
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || true

# 步骤 1/3：PostgreSQL 全量导出（gzip 在宿主侧管道完成，备份不落容器内盘；pipefail 保证
# pg_dump 中途失败整条管道非零退出，不会产出截断残片冒充成功）
log "1/3 pg_dump 开始：container=${CONTAINER} db=${DB_NAME}"
if [ "$DRY_RUN" = "1" ]; then
    log "干跑：docker exec ${CONTAINER} pg_dump -U ${DB_USER} ${DB_NAME} | gzip > ${BACKUP_DIR}/rag-${TS}.sql.gz.part"
else
    docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" "${DB_NAME}" | gzip > "${BACKUP_DIR}/rag-${TS}.sql.gz.part"
    finalize "${BACKUP_DIR}/rag-${TS}.sql.gz.part" "${BACKUP_DIR}/rag-${TS}.sql.gz" gzip
fi

# 步骤 2/3：uploads 打包——卷模式（prod）优先，未配置卷走宿主目录模式（dev）
if [ -n "${UPLOADS_VOLUME}" ]; then
    log "2/3 uploads 打包开始（prod 卷模式）：volume=${UPLOADS_VOLUME}"
    run docker volume inspect "${UPLOADS_VOLUME}"
    ABS_BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"
    # 临时容器只读挂卷打包，直接打到宿主备份目录（绕过 app 容器与容器内路径布局）；
    # --user 对齐宿主属主，产物不落 root。卷内容整体打包（含 STORAGE_DIR 实际落点，见 runbook §3.6）
    run docker run --rm --user "$(id -u):$(id -g)" -v "${UPLOADS_VOLUME}":/src:ro -v "${ABS_BACKUP_DIR}":/dst \
        "${TAR_IMAGE}" tar -czf "/dst/uploads-${TS}.tar.gz.part" -C /src .
    finalize "${BACKUP_DIR}/uploads-${TS}.tar.gz.part" "${BACKUP_DIR}/uploads-${TS}.tar.gz" tar
elif [ -d "${STORAGE_DIR}" ]; then
    log "2/3 uploads 打包开始（宿主目录模式）：${STORAGE_DIR}"
    if [ "$DRY_RUN" = "1" ]; then
        log "干跑：tar -czf ${BACKUP_DIR}/uploads-${TS}.tar.gz.part -C $(dirname "${STORAGE_DIR}") $(basename "${STORAGE_DIR}")"
    else
        tar -czf "${BACKUP_DIR}/uploads-${TS}.tar.gz.part" -C "$(dirname "${STORAGE_DIR}")" "$(basename "${STORAGE_DIR}")"
        finalize "${BACKUP_DIR}/uploads-${TS}.tar.gz.part" "${BACKUP_DIR}/uploads-${TS}.tar.gz" tar
    fi
else
    log "2/3 警告：上传目录 ${STORAGE_DIR} 不存在且未配置 UPLOADS_VOLUME，跳过 uploads 备份"
    log "    （首次部署可能尚未产生上传；prod 部署机必须配置 UPLOADS_VOLUME，见 runbook §3.6）"
fi

# 步骤 3/3：清理超过保留期的备份（*.gz 正式产物 + *.gz.sha256 边车 + 失败残留 *.part，
# 不触碰 backup-cron.log 等其他文件）
log "3/3 清理 ${RETAIN_DAYS} 天前的旧备份（*.gz / *.gz.sha256 / *.part）"
# 防误删断言（M 系）：清理是 find -delete 批量删除——BACKUP_DIR 配错（指到根/空/无关目录）
# 会误伤他物。先断言目录合法且目录内确有本脚本命名产物（rag-*.sql.gz），任一不满足即拒绝清理。
case "${BACKUP_DIR}" in
    "" | "/" | "." | "..")
        log "错误：BACKUP_DIR 配置为空/根/当前目录（${BACKUP_DIR}），拒绝执行清理"
        exit 1
        ;;
esac
if [ "$DRY_RUN" != "1" ] && ! ls "${BACKUP_DIR}"/rag-*.sql.gz >/dev/null 2>&1; then
    log "错误：${BACKUP_DIR} 内未发现 rag-*.sql.gz 产物，疑似目录配置错误，拒绝执行清理"
    exit 1
fi
if [ "$DRY_RUN" = "1" ]; then
    log "干跑：find ${BACKUP_DIR} ( -name '*.gz' -o -name '*.gz.sha256' -o -name '*.part' ) -mtime +${RETAIN_DAYS} -delete"
else
    find "${BACKUP_DIR}" \( -name "*.gz" -o -name "*.gz.sha256" -o -name "*.part" \) -mtime +"${RETAIN_DAYS}" -delete
fi
log "全部完成：本轮备份 rag-${TS}.sql.gz$( [ -f "${BACKUP_DIR}/uploads-${TS}.tar.gz" ] && echo " + uploads-${TS}.tar.gz" )$( [ "$DRY_RUN" = "1" ] && echo "（干跑，未实际产出）" )"
