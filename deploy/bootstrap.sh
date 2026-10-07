#!/usr/bin/env bash
# 一键部署前置体检 + 装配（2026-10-02 新增，docs/deploy-requirements.md §4 的机器执行部分）。
#
# 目标：把"部署机是否就绪"从人为判断变成机器判定——任何 FAIL 项直接中止并给修复
# 指引，杜绝带病装配（内存不足三巨头互挤 OOM、密钥留 example 值上线等历史事故）。
#
# 用法：
#   bash deploy/bootstrap.sh --check-only        # 只体检不装配（干跑）
#   bash deploy/bootstrap.sh                     # 体检 + 试点机装配（自建镜像，错峰拉起+冒烟）
#   bash deploy/bootstrap.sh --mode prod         # 生产模式体检（装配走 deploy.sh <digest>）
#
# 体检口径见 docs/deploy-requirements.md §4.1；任何一项 FAIL 退出码 1。
set -uo pipefail
cd "$(dirname "$0")/.."

MODE="pilot"
CHECK_ONLY=0
# 同时支持两词形式（--mode prod，docs/deploy-requirements.md §4.3 文档口径）与等号形式（--mode=prod）
while [ $# -gt 0 ]; do
  case "$1" in
    --mode=prod) MODE="prod" ;;
    --mode)
      if [ "${2:-}" = "prod" ]; then MODE="prod"; shift; else
        echo "参数 --mode 缺取值（支持：--mode prod / --mode=prod）"; exit 1; fi ;;
    --check-only) CHECK_ONLY=1 ;;
    *) echo "未知参数：$1（支持 --check-only / --mode prod / --mode=prod）"; exit 1 ;;
  esac
  shift
done

FAILS=0
WARNS=0
say() { echo "[bootstrap] $*"; }
pass() { echo "  [PASS] $*"; }
warn() { echo "  [WARN] $*"; WARNS=$((WARNS + 1)); }
fail() { echo "  [FAIL] $*"; FAILS=$((FAILS + 1)); }

say "=== 部署前置体检（模式：$MODE）==="

# 1. Docker daemon
if docker info >/dev/null 2>&1; then pass "Docker daemon 运行中"; else
  fail "Docker 不可用——先启动 Docker Desktop / dockerd"; fi

# 2. 内存（Docker 可分配总量，MB 精度比较防整数截断误报；<11G 必 OOM——三巨头互挤事故实证）
# 全常驻阈值唯一常量：MEM_FULL_MB=22528（22G），≥22G 三巨头常驻——deploy.sh 与
# deploy-requirements.md §4.1 同源引用本值，改阈值须四处（含文档）一并改。
MEM_FULL_MB=22528
MEM_TOTAL=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
MEM_MB=$((MEM_TOTAL / 1024 / 1024))
if [ "$MEM_MB" -ge "$MEM_FULL_MB" ]; then pass "内存 $((MEM_MB / 1024))G：三巨头可常驻（WITH_MINERU 自动开启）"
elif [ "$MEM_MB" -ge 11264 ]; then warn "内存 $((MEM_MB / 1024))G：可运行，但 mineru 必须按需启停（解析前 recover.sh mineru）"
elif [ "$MEM_MB" -gt 0 ]; then fail "内存 $((MEM_MB / 1024))G（<11G）：三巨头（embed 2.8g+rerank 3.6g+mineru 3.4g）互挤 OOM；WSL 环境调 .wslconfig memory=12GB+ 后重启 Docker"
else warn "无法读取 Docker 内存限额（docker info MemTotal=0），请人工确认 ≥12G"; fi

# 3. 磁盘（docker 数据根分区，镜像+模型基础占用 ~25G，留数据与备份余量）
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo ".")
DISK_AVAIL_GB=$(df -BG "$DOCKER_ROOT" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')
if [ "${DISK_AVAIL_GB:-0}" -ge 100 ]; then pass "磁盘可用 ${DISK_AVAIL_GB}G（$DOCKER_ROOT）"
elif [ "${DISK_AVAIL_GB:-0}" -ge 50 ]; then warn "磁盘可用 ${DISK_AVAIL_GB}G：可部署，注意备份与日志增长"
elif [ -n "$DISK_AVAIL_GB" ]; then fail "磁盘可用 ${DISK_AVAIL_GB}G（<50G）：镜像 13.5G + TEI 模型 8.6G + 数据备份放不下"
else warn "无法核查磁盘（df 不可达或输出异常），请人工确认可用空间 ≥50G（M22：不可静默跳过）"; fi

# 4. CPU 核数（<8 仅影响重排延迟，不阻断）
NCPU=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)
if [ "$NCPU" -ge 8 ]; then pass "CPU ${NCPU} 核"
elif [ "$NCPU" -gt 0 ]; then warn "CPU 仅 ${NCPU} 核：可运行，但 CPU 重排延迟会翻倍（商用 SLA 建议加 GPU TEI，见部署需求文档 §3）"; fi

# 5. 外网连通（LLM 云轨 DeepSeek 必需；本地大模型轨可跳过——见 WARN 提示）
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://api.deepseek.com/v1/models 2>/dev/null)
[ -z "$HTTP_CODE" ] && HTTP_CODE=000
if [ "$HTTP_CODE" != "000" ]; then
  pass "外网可达 api.deepseek.com（HTTP $HTTP_CODE）"
else
  warn "api.deepseek.com 不可达：云轨 LLM 将不可用。走本地大模型轨则忽略（部署需求文档 §3.2）"
fi

# 6. .env 存在 + 必填键非空 + 密钥不得保留 example 值（安全红线）
ENV_FILE=".env"
[ "$MODE" = "prod" ] && ENV_FILE="deploy/.env.prod"
if [ ! -f "$ENV_FILE" ]; then
  fail "缺少 $ENV_FILE（从 .env.example 复制并填写部署必填项）"
else
  miss=""
  # pilot compose 模式下 EMBED/RERANK/MINERU/POSTGRES_PASSWORD 由 compose environment
  # 注入（内网服务名），不要求出现在 .env；本地大模型轨 LLM_API_KEY 可空，故不列必填
  for key in DATABASE_URL JWT_SECRET LLM_BASE_URL LLM_MODEL; do
    val=$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d ' ')
    if [ -z "$val" ]; then miss="$miss $key"; fi
  done
  if [ -n "$miss" ]; then fail "$ENV_FILE 必填键为空：$miss（.env.example 有逐项说明）"; else pass "$ENV_FILE 必填键齐备"; fi

  jwt=$(grep -E "^JWT_SECRET=" "$ENV_FILE" | tail -1 | cut -d= -f2-)
  if [ "${#jwt}" -lt 32 ] && [ -n "$jwt" ]; then fail "JWT_SECRET 长度不足 32（安全红线）"; fi
  if echo "$jwt" | grep -q "please-generate"; then fail "JWT_SECRET 仍是 .env.example 占位值——必须换随机强密钥（openssl rand -base64 48）"; fi
  pgpwd=$(grep -E "^POSTGRES_PASSWORD=" "$ENV_FILE" | tail -1 | cut -d= -f2-)
  if [ "$pgpwd" = "rag" ]; then fail "POSTGRES_PASSWORD 仍是缺省 rag——必须换强口令（安全红线）"; fi
  test_mode=$(grep -E "^TEST_MODE=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d ' ')
  if [ "$test_mode" = "true" ] && [ "$MODE" = "prod" ]; then fail "生产模式 TEST_MODE 必须为 false（演示桩回答对外是不可接受事故）"; fi
fi

# 7. TEI 模型目录（bge-m3 / bge-reranker 各 ≥1G，缺文件 TEI 起不来）
for m in bge-m3 bge-reranker-v2-m3; do
  size=$(du -sk "data/tei-models/$m" 2>/dev/null | cut -f1)
  if [ "${size:-0}" -ge 1048576 ]; then pass "TEI 模型 $m 就绪"
  else fail "TEI 模型 data/tei-models/$m 缺失或过小（${size:-0}KB）——用 docker/download_models.py 预下（须 HF_HUB_DISABLE_XET=1）"; fi
done

# 8. mineru 镜像（pdf 解析；缺失不阻断装配——pypdf 降级，但功能降级要知情）。
#    prod 口径查 ${MINERU_IMAGE:-mineru-cpu:2.5.4}（docker-compose.prod.yml 同源，与 pilot 自建
#    ragdemo-mineru 不同名）；prod 实际执行由 deploy.sh 第 0 步硬校验（缺失即中止）。
MINERU_EXPECT="ragdemo-mineru"
if [ "$MODE" = "prod" ]; then
  MINERU_EXPECT="${MINERU_IMAGE:-mineru-cpu:2.5.4}"
  if docker image inspect "$MINERU_EXPECT" >/dev/null 2>&1; then
    pass "mineru 镜像在位（$MINERU_EXPECT）"
  else
    warn "mineru 镜像 $MINERU_EXPECT 缺失：内存 ≥22G 时 deploy.sh 会中止发布（常驻档硬校验），低配档延后拉起仅提示；自建见 docker/mineru-cpu.Dockerfile，离线导入后 docker tag 为该引用"
  fi
elif docker images --format '{{.Repository}}' | grep -q "$MINERU_EXPECT"; then
  pass "mineru 镜像在位（11.6G）"
else
  warn "mineru 镜像缺失：pdf 将走 pypdf 降级（表格/版面信息可能丢失）；需要时按 runbook 导入"
fi

# 9. gotenberg 镜像（老格式 doc/ppt/xls 转换；缺失不阻断——老格式拒收，功能降级要知情）
if docker images --format '{{.Repository}}' | grep -q "gotenberg"; then
  pass "gotenberg 镜像在位"
else
  warn "gotenberg 镜像缺失：doc/ppt/xls 上传将被拒收（提示另存新格式）；docker pull gotenberg/gotenberg:8"
fi

echo ""
if [ "$FAILS" -gt 0 ]; then
  say "体检未通过：$FAILS 项 FAIL / $WARNS 项 WARN——按上面指引修复后重跑。"
  exit 1
fi
say "体检通过（$WARNS 项 WARN 供知悉）。"

[ "$CHECK_ONLY" = "1" ] && { say "--check-only 干跑结束，未做任何变更。"; exit 0; }

# ---------- 装配 ----------
if [ "$MODE" = "prod" ]; then
  say "生产模式装配：请执行 ./deploy.sh <ghcr 镜像 digest>（digest 清单见 deploy/releases.md）——"
  say "本脚本不代替发布审批：digest 由发布人工确认后传入（standards §13.2）。"
  exit 0
fi

say "=== 试点机装配：错峰拉起全栈 + 冒烟（内存 ≥22G 时 mineru 常驻）==="
if [ "$MEM_MB" -ge "$MEM_FULL_MB" ]; then
  WITH_MINERU=1 bash deploy/startup-check.sh
else
  bash deploy/startup-check.sh
fi

say "=== 部署完成。验收三步 ==="
echo "  1. 管理员登录 http://localhost:8000 → 运营概览 → 服务健康卡应全绿"
echo "     （登录用冒烟种子账号 e2e-admin / E2ePass#2026，与 app/db/e2e_seed.py 对齐）"
echo "     （mineru 未拉起时为'不可用'黄灯属预期：仅影响 pdf 解析，问答不受影响）"
echo "  2. 上传一份测试文档，状态走到'就绪'（素材：tests/fixtures/golden/hr_制度_v1.docx）"
echo "  3. 智能问答发一问，确认流式回答带 [1] 角标引用"
echo "  诊断：bash deploy/recover.sh（只读）；回滚：deploy/rollback.sh"
