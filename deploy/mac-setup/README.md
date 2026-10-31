# Mac Mini 开发环境迁移指南（Windows → Apple Silicon）

> 定位：活文档（迁移工具箱说明）｜产出：2026-10-29｜配套：`package.sh`（Windows 侧打包）/ `setup-mac.sh`（Mac 侧一键部署）
>
> 前提：Mac Mini 24G/1T（Apple Silicon），磁盘预留 ≥25G（模型 8.6G + 镜像 ~10G + 包体）；本指南覆盖**开发环境**全量落地（容器栈/后端/前端/试点数据/验证）。

## 0. 迁移包里有什么

| 文件 | 内容 | 体积 |
|---|---|---|
| `ragdemo-migrate-<日期>.tar.gz` | 代码 + **git 全部历史与未提交快照** + **.env（密钥！）** + `data/uploads` 试点文档 + `dumps/rag-pilot.dump` 试点库（schema+数据） | ~150M |
| `ragdemo-models-<日期>.tar` | TEI 权重（bge-m3 + bge-reranker-v2-m3 含 onnx） | ~8.6G（可跳过，见 §3 ③） |
| `sha256sums-<日期>.txt` | 校验和 | — |
| `README-mac部署步骤.md` | 本文副本（解压前可读） | — |

**安全红线**：主包含密钥与试点语料，**只允许本机到本机传输**（U 盘 / 局域网 scp）；禁止上传公网网盘；Mac 侧验收完成后删除包与 Windows 侧副本。

**排除项（可重建，故不在包内）**：`.venv`、`node_modules`、TEI 权重（在模型包）、`.zcode` 会话台账、构建产物。**包含项**：`.git`（历史无损）、`.superpowers` 台账、`.scratch` 本地工单、golden 夹具。

## 1. 传输

```bash
# Windows（Git Bash，管理员不需要）：
bash deploy/mac-setup/package.sh          # 全量；--skip-models 跳过 8.6G 模型包
# 产出在仓库上级目录 ../ragdemo-migrate-<日期>/

# 局域网直传到 Mac（同网段时）：
scp ../ragdemo-migrate-<日期>/*.tar* 用户名@mac-ip:~/migrate/
```

Mac 侧放哪：建议 `~/migrate/`；解压主包得到仓库目录（内含 `deploy/mac-setup/`）。

## 2. 解压

```bash
mkdir -p ~/dev && cd ~/dev
tar -xzf ~/migrate/ragdemo-migrate-<日期>.tar.gz    # 解出 RagDemo 仓库目录
cd RagDemo                                          # 目录名以实际为准
```

模型包**不用解压**——`setup-mac.sh` 会自动发现并解到 `data/tei-models/`（放到仓库上级目录或 `deploy/mac-setup/` 均可）。

## 3. 一键部署

```bash
bash deploy/mac-setup/setup-mac.sh                  # 全量（含 MinerU arm64 重建，首次 20-40min）
# 可选参数：
#   --skip-mineru     先跳过 MinerU（扫描件解析后补：docker compose --profile parser up -d --build mineru）
#   --restore-data    还原试点库数据（文档/会话/评测，来自 dumps/rag-pilot.dump）
```

脚本按序执行十步，任何一步 FAIL 即停并给修复指引：**前置检查**（git/docker/uv/node≥20/python3，缺啥给 brew 命令）→ **基础容器栈**（pg 5433 / pg-test 5434 / TEI×2 / gotenberg）→ **TEI 权重**（在位检查 / 自动解模型包 / 给下载命令）→ **uv sync**（uv.lock 锁定，arm64 已覆盖）→ **MinerU 重建**（`--profile parser`，本地 Dockerfile 现场编 arm64）→ **alembic** → **seed 或还原试点数据** → **npm ci + Playwright** → **验证**（make lint/unit/api，双平台 Makefile 同源）。

预期收尾输出：`✅ 部署完成` + 两条启动命令（见 §5）。

## 4. 分步对照（想手动或排查时）

| 步 | 命令 | 说明 / 坑位 |
|---|---|---|
| 前置 | `brew install git uv node python3` + Docker Desktop 启动一次 | uv 是依赖事实源载体（uv.lock 入库） |
| 栈 | `docker compose up -d pg pg-test tei-embed tei-rerank gotenberg` | pg healthy 才算数；端口 5433/5434/8091/8082 |
| 权重 | `tar -xf ragdemo-models-*.tar -C data`（或 huggingface-cli 下载，见脚本 FAIL 提示） | TEI 自带下载器与镜像源不兼容，优先用包或 hf 官方源 |
| MinerU | `docker compose --profile parser up -d --build mineru` | **arm64 现场重建**（Windows 的 x86 镜像不可迁移）；首启模型加载 15m 宽限；CPU OCR 单件可达 10min+ |
| 后端 | `uv sync --locked --extra dev` | `--locked` 在 lock 漂移时报错不静默回退 |
| 迁移 | `uv run --no-sync python -m alembic upgrade head` | 连接串读 .env 的 DATABASE_URL（localhost:5433） |
| 数据 | `docker compose exec -T pg pg_restore -U rag -d rag --no-owner --clean --if-exists < dumps/rag-pilot.dump` 后再补一次 alembic | 只在 `--restore-data`；还原后跳过 seed（数据自带账号） |
| 种子 | `uv run --no-sync python -m app.db.seed` | 全新库才需要；**admin 密码仅打印一次** |
| 前端 | `cd frontend && npm ci && npx playwright install chromium` | `npm ci` 不用 install |
| 验证 | `make lint && make test-unit && make test-api` | Makefile 已双平台（Windows/macOS 自动切换 venv 路径） |

## 5. 部署后怎么跑

```bash
# 开发态（热重载，裸 uvicorn）：
uv run --no-sync uvicorn app.main:app --reload --port 8000
# 全容器化态（前端 dist 单容器，启动即迁移）：
docker compose up -d app          # 与裸 uvicorn 二选一（同占 8000）
# 全量回归 / E2E：
make test
cd frontend && npm run e2e        # 或 bash deploy/e2e-visible.sh（有头模式）
```

## 6. Windows → Mac 已知差异对照

| 项 | Windows 现状 | Mac 后 |
|---|---|---|
| Makefile | `.venv/Scripts/python` | 已改 OS 自检测 → `.venv/bin/python`（两平台共用） |
| TEI 镜像 | cpu-1.9.2 x86 实跑 | arm64 manifest 若缺失 → 拉取失败，见 §7；gpu/ONNX-Metal 验证 = retrieval-policy 工单 09 |
| MinerU 镜像 | x86 本机构建 | arm64 现场重建（Dockerfile 在库，`--profile parser`） |
| 5434 端口 | 曾被 Foxmail BOUND 态套接字占用（`netstat -anoq` 可见） | 全新机器无此问题 |
| 裸 uvicorn 端口 | 曾用 80（试点） | dev 默认 8000，需 80 自行加 `--port 80` |

## 7. 常见问题

- **TEI 镜像拉取失败（no matching manifest for linux/arm64）**：说明 cpu-1.9.2 无 arm64 侧。临时用 `platform: linux/amd64`（Rosetta 模拟，慢）；正解是工作包 09 的 arm64/ONNX-Metal 标定后换基镜像。
- **MinerU 构建中途失败**：多为 pip 编译依赖在 arm64 的长耗时/网络抖动，重跑 `docker compose --profile parser build mineru` 续传层缓存。
- **make test-api 连不上 5434**：确认 `docker compose ps` 里 pg-test healthy；Mac 上无 Windows 侧"端口被邮件客户端占用"问题。
- **问答报上游错误但容器都 healthy**：TEI 首次加载模型需数分钟（embed healthcheck 宽限 10m），等 health 变绿再试。
- **迁移验收清单**：`make test-unit`/`test-api` 全绿 → `uv run --no-sync uvicorn app.main:app` 后登录/上传解析/问答带引用/部门隔离抽查五冒烟 → golden 全绿（`pytest tests/integration/test_golden.py`，MinerU 双样本 ~25min）→ pilot 数据已还原（`--restore-data` 时文档列表可见）。
