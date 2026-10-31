#!/usr/bin/env bash
# Mac Mini（Apple Silicon）开发环境一键部署。
#
# 用法（仓库根目录）：
#   bash deploy/mac-setup/setup-mac.sh                    # 全量：基础栈+MinerU重建+后端+前端+验证
#   bash deploy/mac-setup/setup-mac.sh --skip-mineru      # 跳过 MinerU arm64 镜像重建（20-40min，可后补）
#   bash deploy/mac-setup/setup-mac.sh --restore-data     # 额外：把包内 dumps/rag-pilot.dump 还原为试点数据
#
# 步骤明细见同目录 README.md；任何一步 FAIL 即中止并给修复指引（fail-fast）。
set -euo pipefail
cd "$(dirname "$0")/../.."   # → 仓库根

SKIP_MINERU=0
RESTORE_DATA=0
for arg in "$@"; do
  case "$arg" in
    --skip-mineru) SKIP_MINERU=1 ;;
    --restore-data) RESTORE_DATA=1 ;;
    *) echo "未知参数：$arg（支持 --skip-mineru / --restore-data）"; exit 1 ;;
  esac
done

say()  { echo ""; echo "======== [setup] $* ========"; }
ok()   { echo "  [OK] $*"; }
warn() { echo "  [WARN] $*"; }
die()  { echo "  [FAIL] $*"; exit 1; }

say "① 前置检查（缺什么装什么：brew install <名字>）"
for cmd in git docker uv node npm python3; do
  command -v "$cmd" >/dev/null 2>&1 || die "缺少 $cmd —— brew install $cmd（docker: 安装 Docker Desktop 或 OrbStack 后启动一次）"
done
docker info >/dev/null 2>&1 || die "Docker 引擎未运行——启动 Docker Desktop（或 OrbStack）后重试"
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
[ "$NODE_MAJOR" -ge 20 ] || die "node 需 ≥20（当前 $(node -v)）——brew install node@20 并按提示 link"
[ -f .env ] || { cp .env.example .env; warn "未找到 .env，已从 .env.example 复制——必须填入 DeepSeek key 等真实值后重跑"; }
ok "git / docker(29+) / uv / node$(node -v) / python3 全就绪，.env 在位"

say "② 基础容器栈（pg 5433 / pg-test 5434 / TEI×2 / gotenberg）"
docker compose up -d pg pg-test tei-embed tei-rerank gotenberg
for i in $(seq 1 60); do
  st=$(docker inspect -f "{{.State.Health.Status}}" ragdemo-pg 2>/dev/null || echo "")
  [ "$st" = "healthy" ] && break
  [ $i -eq 60 ] && die "pg 未就绪（docker logs ragdemo-pg 排查）"
  sleep 5
done
ok "pg healthy；TEI/gotenberg 拉起中（健康检查后台继续）"

say "③ TEI 模型权重（bge-m3 + bge-reranker-v2-m3，TEI 挂载 ./data/tei-models）"
MISSING_MODEL=0
for m in bge-m3 bge-reranker-v2-m3; do
  ls data/tei-models/$m >/dev/null 2>&1 || { warn "缺权重 data/tei-models/$m"; MISSING_MODEL=1; }
done
if [ "$MISSING_MODEL" = "1" ]; then
  MODEL_TAR=$(ls ../ragdemo-models-*.tar deploy/mac-setup/ragdemo-models-*.tar 2>/dev/null | head -1 || true)
  if [ -n "$MODEL_TAR" ]; then
    echo "  从模型包解压：$MODEL_TAR → data/"
    tar -xf "$MODEL_TAR" -C data
    ok "权重已解包"
  else
    die "缺 TEI 权重且无 ragdemo-models-*.tar。两种补法：①把模型包放到仓库上级或 deploy/mac-setup/ 后重跑；②下载到 data/tei-models/：
        huggingface-cli download BAAI/bge-m3 --local-dir data/tei-models/bge-m3
        huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir data/tei-models/bge-reranker-v2-m3
        （CPU 档 rerank 还需 onnx/ 子目录，导出法见 known-issues 与 retrieval-policy 工单 09）"
  fi
else
  ok "权重在位"
fi

say "④ Python 后端（uv sync：uv.lock 为唯一依赖事实源，arm64 锁已覆盖）"
uv sync --locked --extra dev
ok ".venv 就绪"

say "⑤ MinerU arm64 镜像（本地 Dockerfile 重建；首次 20-40min）"
if [ "$SKIP_MINERU" = "0" ]; then
  docker compose --profile parser build mineru
  docker compose --profile parser up -d mineru
  ok "mineru 拉起中（首启模型加载，健康宽限 15m；不影响其余步骤）"
else
  warn "跳过 MinerU——扫描件/双栏 PDF 解析暂不可用，后补：docker compose --profile parser up -d --build mineru"
fi

say "⑥ Dev 库迁移（alembic → .env 的 DATABASE_URL，缺省 localhost:5433/rag）"
uv run --no-sync python -m alembic upgrade head
ok "schema 到 head"

if [ "$RESTORE_DATA" = "1" ]; then
  say "⑥b 还原试点数据（dumps/rag-pilot.dump → dev 库）"
  [ -f dumps/rag-pilot.dump ] || die "找不到 dumps/rag-pilot.dump（迁移主包内自带；--restore-data 需要它）"
  docker compose exec -T pg pg_restore -U rag -d rag --no-owner --clean --if-exists < dumps/rag-pilot.dump
  uv run --no-sync python -m alembic upgrade head   # dump 落后 head 时补齐增量迁移
  ok "试点文档/会话/评测数据已还原（seed 跳过：数据自带账号）"
else
  say "⑥b 灌种子（三部门 + admin，全新库一次性）"
  uv run --no-sync python -m app.db.seed
  warn "admin 初始密码只在本次 seed 输出打印一次，向上翻记录保存"
fi

say "⑦ 前端（npm ci 锁定安装 + Playwright chromium）"
( cd frontend && npm ci )
( cd frontend && npx playwright install chromium )
ok "前端依赖与 E2E 浏览器就绪"

say "⑧ 验证（make 双平台化：make lint/test 与 Mac 同源）"
make lint
make test-unit
make test-api
ok "lint + unit + api 全绿（pg-test 5434 由 conftest 自动迁移+种子）"

say "✅ 部署完成——下一步"
echo "  开发态问答（裸 uvicorn，热重载）：uv run --no-sync uvicorn app.main:app --reload --port 8000"
echo "  全容器化态（含前端 dist）：docker compose up -d app   # 注意与裸 uvicorn 二选一，端口都是 8000"
echo "  全量回归：make test；E2E：cd frontend && npm run e2e（或 deploy/e2e-visible.sh）"
echo "  提醒：app/MinerU 为 arm64 现场重建镜像；TEI 若拉取失败（无 arm64 manifest）见 README §常见问题。"
