#!/usr/bin/env bash
# Windows 侧打包（Git Bash 运行）：把项目完整迁往 Mac Mini 的迁移包。
#
# 产出（写在仓库同级目录，避免自包含）：
#   ../ragdemo-migrate-<日期>.tar.gz   主包：代码 + git 历史/未提交快照 + .env(密钥!) + 试点上传件 + 试点库 dump
#   ../ragdemo-models-<日期>.tar       模型包：TEI 权重 8.6G（不压缩——权重无压缩收益）
#   ../sha256sums-<日期>.txt           两个包的校验和
#
# 用法：
#   bash deploy/mac-setup/package.sh               # 全量（含模型包）
#   bash deploy/mac-setup/package.sh --skip-models # 跳过 8.6G 模型包（Mac 上重新下载权重）
#
# 安全红线：主包含 .env（DeepSeek key/JWT）与试点语料——只允许本机到本机传输
# （U 盘/LAN scp），禁止上传公网网盘或任何远端；迁移完成并验收后删除两侧副本。
set -euo pipefail
cd "$(dirname "$0")/../.."

STAMP=$(date +%Y%m%d)
OUT="$(pwd)/../ragdemo-migrate-$STAMP"
SKIP_MODELS=0
[ "${1:-}" = "--skip-models" ] && SKIP_MODELS=1
mkdir -p "$OUT/dumps"

echo "[package] ① 试点库导出（pg_dump -Fc，含 schema+数据+alembic 版本）"
WAS_RUNNING=$(docker inspect -f "{{.State.Running}}" ragdemo-pg 2>/dev/null || echo "missing")
docker start ragdemo-pg >/dev/null
for i in $(seq 1 24); do
  st=$(docker inspect -f "{{.State.Health.Status}}" ragdemo-pg 2>/dev/null || echo "")
  [ "$st" = "healthy" ] && break
  sleep 5
done
docker exec ragdemo-pg pg_dump -U rag -d rag -Fc > "$OUT/dumps/rag-pilot.dump"  # stdout 直出：-f /tmp 会被 Git Bash 路径改写（MSYS）
if ! head -c 5 "$OUT/dumps/rag-pilot.dump" | grep -q PGDMP; then
  echo "[package] dump 头校验失败（非 PGDMP 格式）"; exit 1
fi
[ "$WAS_RUNNING" = "true" ] || docker stop ragdemo-pg >/dev/null  # 还原打包前状态
echo "[package]   dump → dumps/rag-pilot.dump ($(du -h "$OUT/dumps/rag-pilot.dump" | cut -f1))"

echo "[package] ② 主包（代码 + .git 历史 + .env + uploads；排除可重建物）"
tar -czf "$OUT/ragdemo-migrate-$STAMP.tar.gz" \
  --exclude='./.venv' \
  --exclude='./frontend/node_modules' \
  --exclude='./data/tei-models' \
  --exclude='./.zcode' \
  --exclude='./.v2c' \
  --exclude='./.video_agent' \
  --exclude='./test-results' \
  --exclude='./.pytest_cache' \
  --exclude='./frontend/dist' \
  --exclude='./frontend/e2e/.artifacts' \
  --exclude='__pycache__' \
  .
echo "[package]   主包 → $(du -h "$OUT/ragdemo-migrate-$STAMP.tar.gz" | cut -f1)"

if [ "$SKIP_MODELS" = "0" ]; then
  echo "[package] ③ 模型包（TEI 权重 8.6G，不压缩；Mac 侧也可选择重新下载）"
  tar -cf "$OUT/ragdemo-models-$STAMP.tar" -C data tei-models
  echo "[package]   模型包 → $(du -h "$OUT/ragdemo-models-$STAMP.tar" | cut -f1)"
else
  echo "[package] ③ 跳过模型包（Mac 上按 setup-mac.sh 指引重新下载权重）"
fi

echo "[package] ④ 校验和"
{ [ -f "$OUT/ragdemo-models-$STAMP.tar" ] && sha256sum "$OUT"/ragdemo-models-$STAMP.tar; \
  sha256sum "$OUT/ragdemo-migrate-$STAMP.tar.gz"; } > "../sha256sums-$STAMP.txt" || true
cat "../sha256sums-$STAMP.txt"
cp deploy/mac-setup/README.md "$OUT/README-mac部署步骤.md"
echo "[package] 完成 → $OUT（含部署步骤 README 副本）"
echo "[package] 提醒：主包含密钥与试点语料，仅限本机间传输；验收后删除两侧副本。"
