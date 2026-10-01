#!/usr/bin/env bash
# 可视化界面自动化（headed 模式）：浏览器窗口可见，全程可看自动登录与页面操作。
# 流程：拉起隔离栈（独立 pg-e2e 库，不碰试点库）→ 种子 → 有头浏览器跑 Playwright
#       → 自动恢复试点栈。报告：frontend/e2e/.artifacts/report/index.html
# 用法（Git Bash，仓库任意目录）：
#   bash deploy/e2e-visible.sh                    # 全量用例
#   bash deploy/e2e-visible.sh auth.spec.ts       # 只跑指定 spec（参数透传 playwright）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] 拉起隔离测试栈（独立 pg-e2e + TEST_MODE 应用）..."
docker compose -f docker-compose.yml -f deploy/docker-compose.e2e.yml \
  -f deploy/docker-compose.e2e-isolated.yml up -d pg-e2e app
echo "      等待应用健康..."
st=""
for _ in $(seq 1 30); do
  st=$(docker inspect ragdemo-app --format '{{.State.Health.Status}}' 2>/dev/null || true)
  [ "$st" = "healthy" ] && break
  sleep 3
done
if [ "$st" != "healthy" ]; then
  echo "[错误] 应用未就绪：请确认 Docker Desktop 正在运行后重试。"
  exit 1
fi

echo "[2/4] 初始化测试库（部门种子 + E2E 账号/夹具）..."
docker compose exec -T app python -m app.db.seed
# 口令不落本脚本字面量：缺省值由工具链自持（app/db/e2e_seed.py 与 auth.setup.ts
# 同源缺省，仅作用于每轮重建的一次性隔离库）；自定义时 export E2E_PASSWORD 后再跑。
docker compose exec -T -e E2E_WIPE=1 \
  ${E2E_PASSWORD:+-e E2E_PASSWORD="$E2E_PASSWORD"} \
  app python -m app.db.e2e_seed

echo "[3/4] 有头浏览器运行界面自动化（将弹出 Edge 窗口，请勿动鼠标键盘）..."
cd frontend
export E2E_CHANNEL=msedge
export E2E_WIPE=1
npx playwright test --config=e2e/playwright.config.ts --headed "$@"
cd ..

echo "[4/4] 恢复试点栈（真实模式应用）并停隔离库..."
docker compose -f docker-compose.yml up -d app
docker stop ragdemo-pg-e2e >/dev/null 2>&1 || true

echo ""
echo "完成。HTML 报告：frontend/e2e/.artifacts/report/index.html（直接双击打开）"
