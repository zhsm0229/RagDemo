#!/usr/bin/env bash
# 回滚脚本（standards §13.4）：镜像回退到上一 digest 引用。
# 用法：./rollback.sh <上一版镜像 digest 引用>（来源：deploy/releases.md 发布记录）
# 三条回滚红线自查（§13.4，回滚前逐条过）：
#   ① 新增列是否可空/带默认值——否则旧镜像 INSERT 失败；
#   ② 枚举 CHECK 加新值按破坏性处理——须先在部署机执行降级脚本再回镜像；
#   ③ 换过 embedding 模型——回退后必须重建向量索引（能跑但检索静默劣化）。
# 数据迁移仅追加（§4.5）时：直接回镜像即可，无需动库。
set -euo pipefail

IMAGE_REF="${1:?用法: $0 <上一版镜像 digest 引用>}"
cd "$(dirname "$0")"

echo "[1/3] 拉取回滚目标 $IMAGE_REF"
APP_IMAGE="$IMAGE_REF" docker compose -f docker-compose.prod.yml pull app

echo "[2/3] 回退 app 容器（pg/TEI/MinerU 不动）"
APP_IMAGE="$IMAGE_REF" docker compose -f docker-compose.prod.yml up -d --no-deps app

# 探活/冒烟一律打宿主 80（prod compose 唯一对外端口 "80:8000"，8000 不发布到宿主）
echo "[3/3] 等待 healthz + 冒烟"
ok=0
for _ in $(seq 1 60); do
  if curl -sf http://localhost/healthz >/dev/null; then ok=1; break; fi
  sleep 5
done
[ "$ok" -eq 1 ] || { echo "回滚后 healthz 未就绪——立即按 runbook §常见故障排查"; exit 1; }
curl -sf -o /dev/null http://localhost/
echo "回滚完成：$IMAGE_REF（记得回填 deploy/releases.md 回滚记录）"
