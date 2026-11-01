# 运维手册（Runbook）

> 定位：活文档｜对账：2026-10-07｜同步保障：评审纪律，随里程碑增量补全（§18.4）
> 状态：W3 Task 13 产出骨架，W5 Task 6 定稿备份/恢复章节（第 3 节，§18）；W5 Task 8 增串库双向演练章节（第 5 节，T9 执行）。**T9 已于 2026-09-30 完成恢复演练与串库双向走查，实测数据已回填 3.4 / 3.5 / 5.5，串库判据按实测结论修正（见 5.3/5.4/5.5）**。其余章节随里程碑增量补全（§18.4：启动停止 / 备份恢复 / 常见故障 / 合规轨道切换 / 端口核查）。
> 部署形态：单机 Docker Compose，4 核 16G VM（§12.5）。所有命令在仓库根目录执行（Windows Git Bash 或部署机 bash）。

## 1. 主栈启停

```bash
# 启动
docker compose up -d

# 状态与健康检查（healthcheck 就绪应为 healthy）
docker compose ps
docker inspect -f '{{.Name}} {{.State.Health.Status}}' ragdemo-pg

# 停止（保留数据卷）
docker compose down
```

- 主栈容器清单随里程碑并入：当前为 `pg` / `pg-test`（W3 阶段）；`app` / `mineru` / `tei-embed` / `tei-rerank` / `gotenberg`（2026-10-06 并入：老格式 doc/ppt/xls 自动转换，F-1015）后续并入，并入后 app 健康检查增加 `curl -f http://127.0.0.1/healthz`（发布端口仅 80，§12.2）。
- **gotenberg 处置**：健康卡"老格式转换 (Gotenberg)"行异常时 `bash deploy/recover.sh gotenberg`；停机仅影响 doc/ppt/xls 上传（failed+可重试），问答不受影响；探活端点 `http://gotenberg:3000/health`（容器内网，host 侧经 `docker exec ragdemo-gotenberg curl -sf http://localhost:3000/health`）。
- **禁止**对主栈使用 `down -v`（会删除 `pgdata` 业务数据）；数据清理只允许在明确授权的重建流程中执行。

## 2. RAGFlow 基线环境

### 2.1 定位与合规红线

- 用途：检索准确率评测的 RAGFlow 对照基线（project-brief 决策 11/15），**仅内测期存在，不进生产栈**（§12.2）。配置文件：`deploy/docker-compose.ragflow.yml`（独立 project `ragflow-baseline`，网络/卷与主栈完全隔离）。
- **§7.6 红线：禁止为 RAGFlow 配置任何云 API key 灌真实语料**（否则构成第二条内容出网通道）。只允许 full 镜像内置本地 embedding，或在基线界面配置内网模型端点；语料只用脱敏/合成数据。发现配置了云 key 立即下线整改。
- 凭据：compose 内置默认值仅供首次拉起；部署机建议经 `deploy/.env`（git 已忽略）覆盖 `RAGFLOW_MYSQL_PASSWORD` / `RAGFLOW_MINIO_USER` / `RAGFLOW_MINIO_PASSWORD` / `RAGFLOW_REDIS_PASSWORD`。

### 2.2 启动（错峰顺序）

基线资源上限约 11.75G / 4.5C，主栈满配上限 10C17G（§12.5 预算值，主栈 compose limits 待后续任务落地），主机仅 4 核 16G——**两栈禁止同时满载（§12.5）**，顺序如下：

```bash
# 1. 先确认主栈无重负载：无解析任务（mineru 空闲）、无批量上传/评测在跑；
#    必要时先停主栈（至少停 mineru/tei 等重负载服务）
docker compose down

# 2. 起基线（首次启动拉镜像 + 初始化较慢，属预期）
docker compose -f deploy/docker-compose.ragflow.yml up -d

# 3. 等待五服务全部 healthy（es01 首次初始化最慢）
docker compose -f deploy/docker-compose.ragflow.yml ps

# 4. 浏览器访问 Web UI：http://<vm-ip>:9380
```

### 2.3 停止（错峰恢复主栈）

```bash
# 停基线（保留 baseline-* 卷，下次启动语料仍在）
docker compose -f deploy/docker-compose.ragflow.yml down

# 确认基线全部退出后再恢复主栈
docker compose ps   # 应为空
docker compose up -d
```

### 2.4 资源冲突观察点（合计上限 vs 4 核 16G）

| 观察点 | 命令 | 预警线 |
|---|---|---|
| 内存 | `free -h` | available < 2G 预警，< 1G 立即停一栈 |
| 容器资源 | `docker stats --no-stream` | 任一容器贴 memory limit（被 limit OOM 会频繁重启） |
| 系统 OOM | `sudo dmesg -T \| grep -i "out of memory"` | 出现 kill 记录即资源超卖，回去查错峰纪律 |
| ES 磁盘水位 | 基线界面 / `docker logs baseline-es01` | 5gb/3gb/2gb 水位告警（已内置） |
| 宿主磁盘 | `df -h` | es/mysql/minio 卷增长，剩余 < 20% 预警 |

基线重负载 = 批量导入解析 + embedding 生成，为最坏负载；评测取数期间保持主栈停止，跑完按 2.3 交还资源。

### 2.5 数据清理

```bash
# 基线语料整体作废：删除全部 baseline-* 卷（不影响主栈）
docker compose -f deploy/docker-compose.ragflow.yml down -v

# 只重置单个数据域（先 down 再删对应卷）
docker volume rm baseline-es-data
```

- 卷以 `baseline-` 前缀与主栈（`ragdemo_pgdata` 等）隔离；**清理命令只允许作用于 baseline 前缀卷**。
- 基线是一次性对照数据，不做备份（见第 3 节），销毁后重新导入脱敏语料即可。

### 2.6 镜像 digest 核对登记表（§12.8 风格）

部署机首跑时逐条核对登记（命令见下），**双人复核**后本表方可视为有效；换 tag 属变更，须重新登记：

```bash
docker pull <镜像:tag>
docker inspect --format '{{index .RepoDigests 0}}' <镜像:tag>
```

| 镜像 | tag | digest (sha256) | 登记日期 | 复核人 A | 复核人 B |
|---|---|---|---|---|---|
| infiniflow/ragflow | v0.19.1 | 待部署机登记 | - | - | - |
| docker.elastic.co/elasticsearch/elasticsearch | 8.11.3 | 待部署机登记 | - | - | - |
| mysql | 8.0.39 | 待部署机登记 | - | - | - |
| quay.io/minio/minio | RELEASE.2023-12-20T01-00-02Z | 待部署机登记 | - | - | - |
| valkey/valkey | 8.1.3 | 待部署机登记 | - | - | - |

注：① `infiniflow/ragflow:v0.19.1` 为 full 变体，内置 embedding 模型工件同受 §12.8 约束（模型 hash 复核在镜像内进行，登记表可加行）；② tag 全部钉死、禁 `latest`（§12.1）。

登记后闭环（§12.8「含模型的镜像以 digest 引用」）：在部署机 `deploy/.env`（git 已忽略）把 ragflow 运行引用切到 digest，并重建容器生效——

```bash
# deploy/.env
RAGFLOW_IMAGE=infiniflow/ragflow@sha256:<登记值>

# 重建生效（数据卷不受影响）
docker compose -f deploy/docker-compose.ragflow.yml up -d
```

compose 中 `${RAGFLOW_IMAGE:-infiniflow/ragflow:v0.19.1}` 的 tag 默认值保留作可读回退，部署机以 `.env` 的 digest 引用为准。

## 3. 备份 / 恢复（§18，W5 T6 定稿）

- 规范指针：standards §18——PostgreSQL 每日 `pg_dump` 全量保留 7 天（备份目录权限 0700）、上传目录每日备份、恢复目标 **RPO ≤ 24h / RTO ≤ 2h**（§18.3）、`.env` 凭据副本托管密码管理器。
- **安全须知（§18.1）：备份含全部问答与文档内容，访问权限同生产库**——备份只落独立目录（本方案 `./backups`，脚本已 `chmod 700`），禁止放入 Web 可访问路径或全员可见共享位置；`.env`（含密钥）不在备份内，副本托管密码管理器。
- 备份范围 = 主库 `rag`（容器 `ragdemo-pg`）+ 上传目录：dev=宿主 `./data/uploads`（`STORAGE_DIR` 默认值）；**prod=uploads named volume（`deploy_uploads`），走 §3.6 卷口径——卷内容不在宿主文件系统上，宿主路径备份在 prod 形态下双向落空（备份拿不到数据、恢复落不进卷）**。`ragdemo-pg-test`（`rag_test`，5434 端口）是独立测试实例，**不在备份范围**；state_* 三张易失状态表（`state_cache_entries`/`state_counters`/`state_auth_guards`，0019）随主库一并备份——`STATE_BACKEND=pg` 时 jti 黑名单/限流窗口/缓存随恢复留存（若黑名单不入备份，恢复后已撤销令牌会在 exp 前重新有效——此即纳入备份的安全含义）；memory 后端（缺省）三表为空，备份无实质内容；RAGFlow 基线环境不做备份（一次性对照数据，销毁重建，见 2.5）。
- 备份统一走 `deploy/backup.sh`（顶部参数化区可按环境覆盖容器名/库名/目录/保留期/卷名 `UPLOADS_VOLUME`），每步带 `[backup]` 时间戳日志，任一步失败非零退出。

### 3.1 每日自动备份（调度示例）

Windows（装有 Git Bash 的开发/演示机；在 cmd 执行，Git Bash 与仓库路径按实际调整）：

```bat
schtasks /Create /TN "ragdemo-daily-backup" /SC DAILY /ST 02:30 /TR "\"C:\Program Files\Git\bin\bash.exe\" -c 'cd /d/Project/ZCodeProject/RagDemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1'"
```

Linux（部署机；`crontab -e` 追加一行，仓库路径按实际调整）：

```bash
30 2 * * * cd /opt/ragdemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1
```

- 两版均为每日 02:30 执行，日志追加到 `backups/backup-cron.log`（backup.sh 的保留期清理只删 `*.gz / *.gz.sha256 / *.part`，该日志需手工定期清理）。prod 部署机的调度命令须带卷名环境变量：`UPLOADS_VOLUME=deploy_uploads`（见 §3.6）。
- 前置：`ragdemo-pg` 在运行且 healthy（`docker inspect -f '{{.State.Health.Status}}' ragdemo-pg`）；失败先查上述日志，再手工按 3.2 复现。
- 登记核查：Windows `schtasks /Query /TN "ragdemo-daily-backup"`；Linux `crontab -l`。

### 3.1b 知识缺口周报导出调度（F-1010"每周自动生成"，W7 补齐）

应用内无调度器（AGENTS"能不加就不加"），周报自动化 = OS 调度器拉 CLI，与 backup 同口径。CLI：`python -m app.services.knowledge_gap [--week-offset 1] [--out 目录]`（缺省导出上周榜单 `knowledge-gap-YYYYMMDD.csv`，UTF-8 BOM Excel 直开）。

Windows（Git Bash 语义，管理员 PowerShell 一次性注册；venv 路径按实际调整）：

```bat
schtasks /Create /TN "ragdemo-weekly-gap" /SC WEEKLY /D MON /ST 06:00 /TR "\"D:\Project\ZCodeProject\RagDemo\.venv\Scripts\python.exe\" -m app.services.knowledge_gap --week-offset 1 --out D:\Project\ZCodeProject\RagDemo\backups"
```

Linux（`crontab -e` 追加）：

```bash
0 6 * * 1 cd /opt/ragdemo && .venv/bin/python -m app.services.knowledge_gap --week-offset 1 --out backups >> backups/knowledge-gap-cron.log 2>&1
```

- 前置：`DATABASE_URL` 指向目标库（venv 内运行读 `.env`）；管理界面（/admin 知识缺口页）为实时聚合视图，与导出同 SQL 口径，导出仅是留档交付物。
- 登记核查：Windows `schtasks /Query /TN "ragdemo-weekly-gap"`；Linux `crontab -l`。


### 3.2 手动备份

```bash
bash deploy/backup.sh                                  # dev：宿主目录模式（缺省）
UPLOADS_VOLUME=deploy_uploads bash deploy/backup.sh    # prod：uploads 卷模式（§3.6）
DRY_RUN=1 bash deploy/backup.sh                        # 干跑：只打印动作与产物路径（不碰 docker、不落盘），改参数后先干跑核对
```

- 产物：`backups/rag-YYYYMMDD-HHMMSS.sql.gz`（主库全量 dump，gzip 在宿主侧管道完成）+ `backups/uploads-YYYYMMDD-HHMMSS.tar.gz`（上传目录/卷归档；dev 目录不存在时警告跳过）。
- **产物防残片（I24）**：两件产物先写 `*.part` 临时名 → 非空 + 完整性校验（`gzip -t` / `tar -tzf`）→ 同目录原子改名（rename）→ 落 `*.sha256` 边车。中断/磁盘满只可能残留 `*.part`，**截断残片永远不会顶着正式备份名冒充可用备份**；`*.part` 由保留期清理一并回收，不是备份、禁止用于恢复。
- 速检真伪：`cd backups && sha256sum -c rag-*.sql.gz.sha256 uploads-*.tar.gz.sha256` 逐份核对；`ls -lh backups/` 当日 `.sql.gz` 必须非 0 字节。**没做过恢复读回验证的备份不算可用备份**——至少在每次演练时按 3.3(a) 验证一轮。
- 备份目录 `backups/` 已加入 `.gitignore`（备份含全部问答与文档内容，禁止入库，§18.1），勿移出该忽略项。

### 3.3 恢复步骤（两种口径，动手前先确认口径）

两种口径共用前置：`ragdemo-pg` 在运行；先定位目标备份文件（默认取最新一份，也可手工指定）：

```bash
BK="$(ls -t backups/rag-*.sql.gz | head -n1)"     # 主库备份，如 backups/rag-20260930-023000.sql.gz
BU="$(ls -t backups/uploads-*.tar.gz | head -n1)" # 上传目录备份
echo "BK=$BK BU=$BU"
```

#### (a) 恢复到新建验证库（演练 / 校验备份可用性，不影响生产数据）

```bash
# 1. 建验证库（幂等：先删同名残留）
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE IF EXISTS rag_restore"
docker exec ragdemo-pg psql -U rag -c "CREATE DATABASE rag_restore"

# 2. 恢复（ON_ERROR_STOP=1：任一 SQL 失败即非零退出，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag_restore

# 3. 读回核对：表清单应与生产库一致，核心表行数应与备份时点一致
docker exec ragdemo-pg psql -U rag -d rag_restore -c "\dt"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM messages"

# 4. 与备份时点记录的行数比对（三表同第 3 步口径；演练当天如有新增写入，恢复库行数 ≤ 生产库属正常，差值 = 备份后增量）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM messages"

# 5. 演练结束清理验证库
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE rag_restore"
```

uploads 侧核对：`tar -tzf "$BU" | head` 能列出 `uploads/...` 文件即归档完好；正式演练时抽一个文件解出比对内容。

#### (b) 覆盖生产库（灾难恢复；先停写、后覆盖、再冒烟）

> **【危险】覆盖生产库前必须确认已有一次成功备份——本节会先 DROP 生产库 `rag`。没有非 0 字节的 `rag-*.sql.gz` 时禁止执行本节，先走 3.3(a) 验证库口径排查备份可用性。**

```bash
# 0. 前置检查：目标备份存在且非 0 字节；停机窗口与相关方已确认
ls -lh backups/rag-*.sql.gz

# 1. 停写入方（app 容器并入主栈后生效；并入前确认无业务写入在跑）
docker compose stop app

# 2. 重建生产库（连 postgres 维护库执行；WITH (FORCE) 断开残留连接，PG15 支持）
docker exec ragdemo-pg psql -U rag -d postgres -c "DROP DATABASE IF EXISTS rag WITH (FORCE)"
docker exec ragdemo-pg psql -U rag -d postgres -c "CREATE DATABASE rag"

# 3. 恢复主库（ON_ERROR_STOP=1：任一 SQL 失败即非零退出——灾难恢复的"成功"信号必须可靠，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag

# 4. 恢复上传目录（dev 宿主目录口径：归档内含 uploads/ 一级目录，解到其父目录 ./data；
#    prod 卷口径归档结构与命令不同，见 §3.6）
mkdir -p ./data
tar -xzf "$BU" -C ./data

# 5. 起服务 + 冒烟
docker compose up -d
docker compose ps   # 应全部 healthy
# 冒烟：登录工作台发起一次问答，确认回答带可核对引用；app 并入后增加 curl -f http://127.0.0.1/healthz（§12.2）

# 6. 读回核对（对 -d rag 执行，行数与备份时点一致）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
```

- 红线（与第 1 节一致）：本节 `DROP`/`CREATE` 只针对库 `rag`，禁止波及 `rag_test`；全程禁止 `docker compose down -v`（会删 `pgdata` 业务数据卷）。

### 3.4 演练记录表（§18.3，实测回填）

| 日期 | 类型 | 备份大小 | 恢复耗时 | RTO | RPO | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|
| （示例）2026-09-30 | 验证库恢复 | 12 MB | 8 分钟 | 12 分钟 | 20 小时 | 张三 | 示例行：RTO=恢复+冒烟端到端时长，RPO=距上次成功备份的数据窗口；正式记录自 W5 T9 演练起填 |
| 2026-09-30 | 验证库恢复 | 42 KB（sql.gz）+ 36 KB（uploads） | 36 秒 | ≈4 分钟 | ≈0（演练口径） | T9 测试者（AI） | W5 T9 首次实测：读回断言三表行数/关键行/alembic_version(0003) 与备份时点一致、上传归档字节比对一致、恢复库冒烟（登录/文档列表/问答）通过；RTO=备份定位→恢复→读回→起服务→冒烟端到端，RPO 以灾前即时备份演示（数据窗口 0），声明口径 ≤24h 见 3.5 |
| 2026-10-31 | 验证库恢复 | 80 KB（sql.gz） | <1 秒 | <1 分钟 | — | 工单 09（AI） | verify-restore.sh 首次脚本化实测（2026-09-30 备份产物）：7 核心表行数读回一致 + alembic 版本核对通过；脚本含 ON_ERROR_STOP 与失败自动清理，周期化运行归部署机调度（README 见 deploy/alerting/） |

- 类型口径：`验证库恢复` = 3.3(a)，常规演练；`生产覆盖演练` = 3.3(b)，需审批与停机窗口。

### 3.5 RPO / RTO 目标声明（§18.3）

- **RPO ≤ 24 小时**：由每日 02:30 全量备份决定，最大数据丢失窗口 ≈ 24 小时（上次成功备份到故障点之间新增的数据）。
- **RTO ≤ 2 小时**：定位备份 → 3.3 恢复 → 起服务 → 冒烟核对的端到端时长上限。
- 实测值（W5 T9，2026-09-30，验证库口径）：**RTO ≈ 4 分钟**（其中恢复段 36 秒、读回断言与冒烟约 3 分钟），远低于 2 小时目标；RPO 演示窗口 ≈ 0（灾前即时备份），声明口径 ≤24h 由每日调度保证。正式记录见 3.4。

### 3.6 prod 形态备份 / 恢复（uploads named volume，I26）

**背景**：prod compose（`deploy/docker-compose.prod.yml`）把 uploads 挂在 named volume（`uploads:/srv/ragdemo/data`）。backup.sh 的宿主目录模式按 `./data/uploads` 打包、恢复按 `tar -C ./data` 解包——**卷内容不在宿主该路径上，备份静默拿不到数据、恢复落不进卷，双向失效（I26）**。

**方案取舍（选"临时容器挂卷"而非"prod 改 bind mount"）**：named volume 由 Docker 统一管理（创建/迁移/权限与宿主目录解耦，整机迁移友好）；改 bind mount 需新增宿主目录规划与权限约定、对已建卷做一次性数据迁移、变更 prod compose 挂载点并重建 app 容器——改动面与风险均大于在备份脚本里加一段挂卷打包逻辑，故 prod compose 零改动，备份/恢复经临时容器挂卷完成（backup.sh 已内置）。

**备份**（每日调度命令见 §3.1，prod 版须带卷名）：

```bash
# 卷名 = <compose 项目名>_uploads；deploy.sh 在 deploy/ 目录下执行 compose，项目名固定 deploy。
# 若项目名被 -p / COMPOSE_PROJECT_NAME 改过，先发现实际卷名：
docker volume ls | grep uploads          # 形如 deploy_uploads
UPLOADS_VOLUME=deploy_uploads ./deploy/backup.sh
```

- 产物与防残片校验同 §3.2（`.part` 原子改名 + `.sha256`）。实现：临时容器只读挂卷（`-v <卷>:/src:ro`）整体打包到宿主备份目录，`--user` 对齐宿主属主（app 以 uid 10001 写文件、0644 可读，非 root 可打包）；打包镜像用部署机现成的 `pgvector/pgvector:pg15`（`TAR_IMAGE` 参数可覆盖），免额外拉 alpine。
- app 不停机可打包（与 dev 宿主目录打包同一风险口径：正在写入中的个别文件可能不完整，正式把关靠 §3.3(a) 恢复读回；要严格一致快照则临时 `docker compose stop app` 再备）。
- 卷模式归档内容为**卷根目录整体**（无 dev 归档的 `uploads/` 一级前缀），恢复命令见下，**与 §3.3(b) 第 4 步不可混用**。

**恢复**（先停 app 防恢复期间被写入；pg 主库恢复与 §3.3(b) 同口径，容器名同为 `ragdemo-pg`）：

```bash
# 0. 定位备份（同 §3.3 前置，BU=uploads-*.tar.gz）；停 app（pg/TEI 不动）
docker compose -f deploy/docker-compose.prod.yml stop app

# 1. 解包进卷（覆盖式；如需精确镜像可先清空卷：sh -c 'rm -rf /dst/* /dst/.[!.]*' 谨慎执行）
docker run --rm -v deploy_uploads:/dst -v "$(pwd)/backups":/src:ro \
  pgvector/pgvector:pg15 tar -xzf /src/uploads-YYYYMMDD-HHMMSS.tar.gz -C /dst

# 2. 起 app + 读回核对（管理后台文档列表可见、抽一个历史文档下载/预览成功 = 恢复到位）
docker compose -f deploy/docker-compose.prod.yml up -d app
```

- 恢复后核对落卷：`docker exec ragdemo-app ls /srv/ragdemo/data | head` 有文件即解包成功；无文件先核对卷名与归档结构（`tar -tzf "$BU" | head`）。
- **双演练要求**：prod 口径的"备份→恢复→读回"须在部署机完成一次双向演练并回填 §3.4 记录表（部署机待办，与备份调度实测同批）。

## 4. 端口暴露核查清单（§12.2，每次发布 / 基线启停后执行）

规则基线：**主栈仅 app 发布 80；RAGFlow 基线仅发布 9380；其余一切端口仅 compose 内网互联，禁止 `ports` 发布。**

```bash
# 1. 容器发布端口核查：输出中只允许出现 0.0.0.0:80(app) 与 0.0.0.0:9380(baseline-ragflow)
docker ps --format '{{.Names}}\t{{.Ports}}'

# 2. 宿主监听核查：比对无未登记监听
ss -tlnp   # 或 netstat -tlnp
```

- [ ] `docker ps` 发布端口仅为 80（主栈 app）与 9380（基线），无其他 `0.0.0.0:` 映射
- [ ] 主栈 pg 的 5433/5434 属**开发期临时发布**（本机开发用），生产收口前必须收回为 compose 内网（§12.2）
- [ ] es01/mysql/minio/valkey（9200/3306/9000/9001/6379）无任何宿主发布
- [ ] 防火墙/安全组仅放行 80、9380（22 运维端口按 IT 策略另行管理）
- [ ] 核查结果异常时：先 `docker compose down` 对应栈收敛端口，再排查是谁改了 compose

## 5. 串库双向演练（W5 T8 产出流程，T9 执行）

目的：双向走查串库拒答——客服视角问人力题、人力视角问客服题都必须拒答（检索边界恒 [视角部门, 全员]，F-502；产品红线"知识不足必须拒答"的部门维度）；同时用全员对照题防"全部拒答也全绿"的假阴性。题库由 `tests/fixtures/tools/make_crosstalk_suite.py` 生成，共 24 行：人力制度类串库拒答题 10 条 + 客服售后类串库拒答题 10 条 + 全员对照事实题 4 条，题目均以"演练用合成题"标注（与真实题库区分，演练后可按此前缀识别清理）。

### 5.1 前置（每次演练执行）

```bash
# 0. 主栈已启动（第 1 节）；测试/演示环境（test_mode，stub LLM）
#    题库生成器 import 后端常量，须在项目 venv 内执行（make install / uv sync --extra dev）

# 1. 生成题库（生成物不入库，放临时目录；可重复运行，覆盖输出）
python tests/fixtures/tools/make_crosstalk_suite.py -o /tmp/crosstalk.xlsx

# 2. 生成对照语料（4 条对照题的期望答案取自该文档固定文本）
python tests/fixtures/tools/make_sample_docx.py   # 写出 tests/fixtures/samples/sample.docx
```

- **上传对照语料**：管理后台 → 知识库·文档管理 → ＋上传文档，把对照语料上传为**全员**部门文档（标题如"串库演练对照语料"），等状态 ready。语料缺失或内容改动 = 对照题必假阴性（命中不了期望关键词）。
  **勘误（T9 实测）**：`sample.docx` 有效正文 < 100 字符，直传会被解析质量闸拒收（failed，"疑似扫描件或空文档"）——上传前需在保留四条对照关键词句（本手册适用于全体员工／总部及各分支机构／凭发票据实报销／每周一至周五）逐字不变的前提下增补正文（T9 演练用增补版 214 清洗字符通过；根修复——扩充 make_sample_docx 并同步 test_parser 结构断言——留待后续任务）。
- 题库无单题删除接口，重复导入会累积同题并进入后续所有跑分：重复演练前由运维清理 `eval_questions`（软删 `is_active` 或清表，经 DBA 确认）再重新导入；演练断言按"题面部门 + 题型"分组核对，不依赖 run 总数恰为 24。

### 5.2 导入题库

管理后台 → 评测管理 → "评测题库"页签 → **Excel 导入题目** 上传 crosstalk.xlsx（或 `POST /api/admin/eval-questions/import`），成功返回 `imported: 24`。行级错误整批退回（422）——生成器产物若报错说明后端模板已漂移，先跑 `pytest tests/unit/test_crosstalk_suite.py` 定位。

### 5.3 客服视角跑分

1. "跑分记录"页签 → **发起跑分**：任务名称 `串库演练-客服视角-<日期>`，视角部门=客服（`perspectiveDeptCode: "service"`）。
2. 轮询 `GET /api/admin/eval-runs/{id}`（前端每 10 秒自动刷新）至 `status=done`，逐题核对 detail。**判据分两种 stub 场景（T9 实测修正——stub rerank 分为伪随机值，串库题在边界内有语料时会以占位文案作答，"10/10 拒答"不是 stub 语料在库场景的有效断言）**：
   - **场景一：边界内有语料（对照语料已上传，常规形态）**——断言三条：①**引用零越界**：全部题的 citations 不含越界部门（客服视角下无任何 `refType=doc` 引用指向人力部门块，反之亦然；可用 chunk→dept 对照 SQL 机检）——这是 stub 模式下串库边界的**结构性断言**（F-502 部门过滤，漏一条即串库漏洞，排查 `app/services/retrieval.py` 的 dept 边界）；②**对照题命中**：题面部门=全员的 4 条 answerType ∈ rag/qa（非拒答）→ "评测题库"录分入口人工评判 correct（`POST /api/admin/eval-runs/{id}/judgments`；`autoJudgment=pending` 属预期：非拒答题不自动判，判定协议第 3 条）；③题面部门=本视角的 10 条不要求拒答（本视角内可答）。
   - **场景二：边界内无任何语料（空范围，机制验证形态；T9 于隔离库 rag_test_b 实测）**——人力题 10 条（cross_reject）`autoJudgment` 全部 correct（回答逐字等于拒答话术"知识库中未找到相关内容"），**串库组拒答 10/10**；对照组 4 条同样拒答（pending，无需录分）。此场景验证拒答机制 + 自动判定协议本身。
   - 题面部门=客服的 10 条：库内无客服语料时同样拒答、auto correct 属预期（stub 演练形态）；部署机若已灌客服语料，命中回答记 auto wrong 属预期（本视角内应答），人工评判 correct 覆写即可。
3. 人工录分后点**报告**核对分组口径（byDept / byQtype），报告页与 `GET /api/admin/eval-runs/{id}/report` 响应一并归档。

### 5.4 人力视角跑分（对称）

重复 5.3：任务名称 `串库演练-人力视角-<日期>`，视角部门=人力（`perspectiveDeptCode: "hr"`）。断言对称——**场景一**：引用零越界（无任何 service 部门块出现在 citations）+ 对照组 4 条 rag/qa 后人工 correct；**场景二（空范围）**：题面部门=客服的 10 条 100% 拒答。两轮双向场景均绿 = 双向隔离成立。

### 5.5 存档与登记

- 跑分结果天然存档于 `eval_runs`：收口即锁定 config 快照 9 字段（model / promptVersion / qaThreshold / refuseThreshold / efSearch / topK / topN / temperature / manifestVersion），防跨跑对比被阈值调整污染；审计 `eval.run` / `eval.judge` 留痕（§23）。
- **stub 模式口径（T9 实测修订）**：检索部门过滤的结构性保证已验证——边界内语料齐备时双向引用零越界（Run A，dev 库 run 1/2）；空范围时串库组双向 10/10 拒答（Run B，隔离库 rag_test_b run 1/2）。**stub rerank 分为伪随机值，"越视角题 100% 拒答"在边界内有语料时不可作 stub 判据**（Run A 实测 0/20 拒答、以占位文案作答 auto wrong 属预期）；**真实阈值下的拒答边界须在部署机以真实 embedding + 真实语料复测**（届时人力/客服语料都在库，断言恢复为：越视角组 100% 拒答、对照组全中）。
- **真实模式复测口径（2026-09-30 部署验收实测，runbook §5.5 预设与实际差异勘误）**：试点库实际构成 = 客服手册 + 全员手册（同内容双部门发布）+ 全员对照语料，**hr 语料缺位**——"人力/客服语料都在库"预设不成立，越界断言按机检 chunk→dept 仍可执行（界=[视角部门, 全员]）。实测（真实 TEI bge-m3 + DeepSeek + refuseThreshold=0.55）：**越界引用 0/19** ✓；越视角组拒答 **7/9**（2 例漏拒：rerank 0.6509/0.6578，撞上全员手册「考勤与休假」章——hr 主题语义天然重叠）；对照组实答 2/2 内容正确。**漏拒带（0.65+）与对照命中带（0.63-0.71）重叠，单一 refuseThreshold 无法同时满足"漏拒=0"与"对照全中"**——阈值维持 0.55 不动，漏拒治理走语料侧（hr 主题章节从全员/客服手册剥离或 qa 优先命中），详见部署验收存档 `docs/archive/acceptance-status-2026-09-30.md` §三（原 `.superpowers/sdd/2026-09-30-w5-hardening/deploy-acceptance-report.md` 未随仓库迁移入库，引用以存档为准——2026-10-31 六角色评审 P1-7 修复）。运维注意：真实跑分对 rerank 吞吐敏感，部署机 `.env` 已调高 TEI 超时（embed 60s / rerank 120s），rerank 批量负载饱和时整题会以 infra error 收口（detail.error 留痕，autoJudgment=pending）——跑分窗口应避开批量解析/embedding 作业。
- 结果登记表（一行 = 一次跑分；T9 演练已填，库=主库 rag 除注明外）：

| 日期 | run ID | 视角 | 串库组拒答 | 对照组人工判 | accuracy | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|
| （示例）2026-09-30 | 12 | 客服 | 人力题 10/10 | 4/4 correct | 1.0000 | 张三 | 示例行：accuracy 取录分收口后 summary；串库组按题面部门核对 |
| 2026-09-30 | 1 | 客服 | 人力题 0/10（stub 占位作答，预期内） | 4/4 correct（answerType 均 rag） | 0.1667 | T9 测试者（AI） | Run A 场景一：对照+诱饵语料在库；引用零越界 ✓（机检 0 条 hr 块）；config 快照 9 字段落档 |
| 2026-09-30 | 2 | 人力 | 客服题 0/10（stub 占位作答，预期内） | 4/4 correct（answerType 均 rag） | 0.1667 | T9 测试者（AI） | Run A 对称：引用零越界 ✓（0 条 service 块）；byDept: hr 0 / service 0 / all 1.0 |
| 2026-09-30 | 1（rag_test_b） | 客服 | 人力题 10/10 | 对照组拒答挂人工（空范围无对照语料，不录分） | 0.8333 | T9 测试者（AI） | Run B 场景二：空范围机制验证（隔离库，演练后已清理数据） |
| 2026-09-30 | 2（rag_test_b） | 人力 | 客服题 10/10 | 对照组拒答挂人工（同上） | 0.8333 | T9 测试者（AI） | Run B 对称：byQtype cross_reject accuracy 1.0 |
| 2026-09-30 | 1 | 客服 | 人力题 7/9 拒答（2 漏拒 rerank 0.6509/0.6578；另 1 题 rerank 超时 infra err） | 对照 2/2 录分 correct（另 2 题 rerank 超时挂人工） | 0.7500 | 部署验收（AI） | 真实 embedding 复测：越界引用 0/19 机检 ✓、config 快照 9 字段落档；漏拒根因=全员手册「考勤与休假」语义重叠，阈值/语料治理结论见验收报告 §5.2；执行窗口撞 rerank 批量负载 |
| 2026-09-30 | 3 | 人力 | 客服题实得 2/2 拒答（q11/q18，auto correct）；其余 22 题全部 rerank 超时 infra err（21:09-22:44 执行窗口与用户批量作业撞车，TEI rerank 队列 8-12 深） | 对照组 0/4 实答（全 infra err，无可录分） | 0.0833（失真：infra err 记 pending 计 0） | 部署验收（AI） | hr 视角对称矩阵被负载摧毁，**有效样本 2/24、方向正确但不足判据，留负载解除后复测**；accuracy 0.0833 不具评价意义；复测口径：避开批量解析/embedding 窗口（见 §5.5 真实模式复测口径注） |

## 6. 模型版本表（§12.2-8：升级前/定期对模型目录 hash 复核）

> hash 基准必须在可信外网终端从官方发布页/官方 repo revision commit 获取，双人复核后登记。
> embedding 模型升级即全库重建向量索引（§13.4 红线③）；LLM 变更须附评测对比（§17.3）。

| 模型 | 用途 | 版本/revision | 获取源 | hash（sha256） | 登记日期 | 复核人 |
|------|------|---------------|--------|----------------|----------|--------|
| BAAI/bge-m3 | embedding（1024 维，pooling=cls） | main@（待登记 revision） | hf-mirror.com（构建期/预热期下载） | 待部署机登记 | — | — |
| BAAI/bge-reranker-v2-m3 | rerank（logit→sigmoid 应用侧归一） | main@（待登记 revision） | hf-mirror.com | 待部署机登记 | — | — |
| MinerU 2.5.4 pipeline 模型族 | PDF 版面/表格/OCR（CPU ONNX） | mineru==2.5.4（ModelScope 源） | modelscope | 待首次拉起后登记 | — | — |
| deepseek-flash（云） | 生成 | DeepSeek-V4.1-Flash | api.deepseek.com（云 API，无本地工件） | 不适用（云侧） | 2026-09-29 | — |
| 内网 LLM（预案，企业内网 vLLM 档） | 生成（合规=禁出网时） | Qwen2.5-32B-Instruct AWQ（选型依据见 deploy-requirements §3.2b/决策 17） | 待定 | 待定 | — | — |
| TEI 基镜像（arm64 服务器） | embedding/rerank 运行时 | ragdemo-tei-cpu-arm64:cpu-1.9.2（自构建 `docker/tei-arm64.Dockerfile`，TEI v1.9.2 源码原生构建，Cargo.lock 钉版） | 本地构建 | 不适用（本地构建，模型走卷挂载） | 2026-11-01 | 完整推理冒烟归部署机窗口 |

本地/部署机模型目录：`./data/tei-models/`（docker/download_models.py 预热，hf-mirror +
HF_HUB_DISABLE_XET=1）。升级操作：改 download_models.py 钉的 revision → 重建索引（红线③）→
回归黄金样本与评测集 → 本表登记新 hash。

## 7. 常见故障排查

| 症状 | 首查 | 处置 |
|------|------|------|
| 容器 unhealthy（app） | `docker compose logs app`；`curl -v localhost:8000/healthz` | healthz 500=DB 断连：查 pg 容器与 DATABASE_URL；健康即过 |
| 问答报"生成服务暂时不可用" | TEI/LLM 上游：`docker logs ragdemo-tei-rerank`；DeepSeek 余额 | TEI 未就绪等 healthcheck 转绿；LLM 侧核对 key/额度（错误码 4xxx 脱敏，详见日志 request_id） |
| 问答明显变慢（>30s） | 本机 CPU 负载（rerank bcrypt/解析并发）；`docker stats` | CPU rerank 属 dev 形态；生产 GPU TEI；避开批量解析窗口（见 §5.5 复测口径） |
| 上传 409 | 精确查重 F-1001（content_hash 命中已存在文档） | 属预期行为：复用已有文档或先停用旧版 |
| 上传后"疑似扫描件或空文档" | 解析红线（§10.3）：有效字符 < max(100, 原文×10%) | 文本型 PDF 走 pypdf 降级仍失败=扫描件，需 MinerU（本机未部署时属预期）；核对 parse_error 字段 |
| 登录 401"剩余尝试次数 N" | failed_attempts 累计（§5.4 持久锁） | admin 解锁或等 locked_until 过期；连续失败源于自动化时，重跑 e2e_seed 复位口令后再跑 |
| 登录 429 | 滑动窗口（60s/10 次·IP+用户名） | 等窗口滑出；重启仅 memory 后端（缺省）有效——pg 后端（`STATE_BACKEND=pg`）窗口跨副本存续，重启不清（deploy-requirements §7.2） |
| alembic 迁移失败拒启 | entrypoint fail-fast（§12.2-3） | 看容器日志定位迁移脚本；禁止手改库结构（§4.5），修复迁移后重起 |

## 8. 合规轨道切换（决策 3 双轨：云 API ↔ 内网模型）

切换只改 `.env` 三键（代码零改动，六接口装配层切换，§1.2）：

| 项 | 云轨（现状） | 内网轨 |
|----|--------------|--------|
| LLM_BASE_URL | https://api.deepseek.com/v1 | http://<内网vLLM-IP>:8000/v1 |
| LLM_API_KEY | DeepSeek key | 内网 vLLM 无鉴权时可留空 |
| LLM_MODEL | deepseek-flash | Qwen2.5-32B-Instruct AWQ（选型见 deploy-requirements §3.2b） |

步骤：①合规书面结论回填 [docs/archive/compliance-checklist-2026-09-30.md](archive/compliance-checklist-2026-09-30.md) 第五节 → ②内网轨需
GPU（≥1×24G）+ vLLM 就绪 → ③改 `.env` 三键重启 app → ④跑评测集对比（§17.3，无对比不
上线）→ ⑤runbook 本表与模型版本表同步。RAGFlow 基线环境同受出网白名单约束（§7.6）。

## 9. 生产发布与 `.env.prod` 逐字段表（I23）

生产发布命令：`bash deploy/bootstrap.sh --mode prod --check-only`（体检）→ `./deploy.sh <镜像 digest>`（分档错峰起栈+冒烟，deploy-requirements §4.3）。密钥文件为 `deploy/.env.prod`（git 已忽略），字段全集 = `app/core/config.py` Settings + compose 注入两处来源，下表逐字段给出含义 / 必填性 / 生产取值示例。

### 9.1 最高频事故点：容器内互联必须填 compose 服务名 DNS

app 容器内的 `localhost` 指 app 容器自身——TEI/MinerU/Gotenberg 都是**独立容器**，prod compose 的 environment 只覆盖了 `CONVERT_BASE_URL`（dev compose 覆盖了四项，prod 没有），因此 **`EMBED_BASE_URL` / `RERANK_BASE_URL` / `MINERU_BASE_URL` 三键必须自己在 `.env.prod` 填 compose 服务名**：照抄 `.env.example` 的 `localhost:80xx` 值上线 = 容器内指向自身，问答链路全断（app 健康检查不含 TEI 探活，要到运营概览健康卡才暴露）。

| 服务 | 容器内监听 | 必须填的 compose DNS 名 |
|------|-----------|------------------------|
| TEI embedding（tei-embed） | 80 | `http://tei-embed:80` |
| TEI rerank（tei-rerank） | 80 | `http://tei-rerank:80` |
| MinerU（mineru） | 8000 | `http://mineru:8000` |
| Gotenberg（gotenberg） | 3000 | `http://gotenberg:3000`（compose 已覆盖，无需填） |

### 9.2 逐字段表

**compose 接管字段（值被 prod compose environment 覆盖或硬校验，填法如下）**

| 字段 | 含义 | 必填性 | 生产取值示例 |
|------|------|--------|-------------|
| DATABASE_URL | PostgreSQL 连接串（psycopg3） | 必填（bootstrap 体检核非空）；**运行值由 compose 覆盖**为 `postgresql+psycopg://rag:${POSTGRES_PASSWORD}@pg:5432/rag` | `postgresql+psycopg://rag:<强口令>@pg:5432/rag` |
| POSTGRES_PASSWORD | pg 初始化口令 | **必填**：compose `${POSTGRES_PASSWORD:?}` 缺失/空拒绝起栈；bootstrap 禁缺省 `rag`；已初始化卷不随值变化（改口令须删 pgdata 卷重建） | `openssl rand -base64 24` 产物 |
| CONVERT_BASE_URL | 老格式转换服务地址 | 无需填：compose 覆盖为 `http://gotenberg:3000` | 留空 |
| STATIC_DIR | 前端静态目录 | 无需填：Dockerfile/compose 固定 `/srv/ragdemo/static` | 不填 |

**应用必填字段（bootstrap prod 体检 + 装配 fail-fast 口径）**

| 字段 | 含义 | 必填性 | 生产取值示例 |
|------|------|--------|-------------|
| JWT_SECRET | JWT 签名密钥 | 必填：≥32 字、非 example 占位（bootstrap 硬校验） | `openssl rand -base64 48` 产物 |
| LLM_BASE_URL | OpenAI 兼容生成端点 | 必填（非 TEST_MODE；留空装配期 fail-fast 拒启） | `https://api.deepseek.com/v1`；内网轨 `http://<vLLM 主机>:8000/v1`（§8） |
| LLM_API_KEY | 端点鉴权密钥 | 云端点必填；内网无鉴权留空。密钥只存 `.env.prod` 与密码管理器，禁进仓库/日志/响应 | DeepSeek 控制台 key |
| LLM_MODEL | 对话模型名 | 必填（非 TEST_MODE），须与服务端一致 | `deepseek-flash`；内网 `Qwen2.5-32B-Instruct AWQ` |
| TEST_MODE | 测试桩模式（true=stub LLM 演示回答） | **必须 false**（bootstrap prod 硬校验；演示桩对外=事故） | `false` |
| EMBED_BASE_URL | TEI 向量化地址 | **必填**（prod compose 不覆盖！见 9.1） | `http://tei-embed:80` |
| RERANK_BASE_URL | TEI 重排地址 | **必填**（同上） | `http://tei-rerank:80` |
| STORAGE_DIR | 上传文件落盘目录 | **必核**：必须位于 uploads 卷挂载点 `/srv/ragdemo/data` 之下（推荐就填挂载点本身）；落到别处=写进容器可写层，容器重建数据即丢 | `/srv/ragdemo/data` |

**部署可选字段（缺省值见 `app/core/config.py`，与 `.env.example` 注释一一对应）**

| 字段 | 含义 | 必填性 | 生产取值示例 |
|------|------|--------|-------------|
| MINERU_BASE_URL | MinerU 解析地址（见 9.1 DNS 口径） | 可选。**填了=全部 pdf 走 MinerU，服务未拉起时 pdf 解析失败（可重试）**；留空=全部 pdf 走 pypdf 降级（扫描件解析不出，"疑似扫描件"拒收） | 常驻/按需形态均推荐填 `http://mineru:8000`（可用性由 mineru 容器启停控制，健康卡如实暴露）；试点接受全量降级则留空 |
| MINERU_TIMEOUT_S | MinerU 解析超时（秒） | 可选 | `300` |
| IMAGE_OCR_ENABLED | 内嵌图片 OCR（F-1017） | 可选，开启前提=MINERU_BASE_URL 已配且 mineru 在跑 | `false` |
| EMBED_TIMEOUT_S | embedding 超时（秒） | 可选；部署机实测放宽口径见 §5.5 | `60` |
| RERANK_TIMEOUT_S | rerank 超时（秒） | 可选；真实跑分对 rerank 吞吐敏感（§5.5） | `120` |
| LLM_EFFORT | 推理力度（DeepSeek 专属） | 可选：`high` 会把 max_tokens 烧在推理上致正文空（2026-10-01 事故） | `low` |
| LLM_TIMEOUT_S | LLM 超时（秒） | 可选 | `60` |
| CHAT_MAX_TOKENS | 回答 max_tokens 服务端上限 | 可选 | `4096` |
| ACCESS_TOKEN_EXPIRE_MINUTES | 访问令牌有效期（分钟） | 可选 | `480` |
| PROMPT_VERSION | 提示词包版本 | 必核：与 `prompts/` 头注释一致 | `v1` |
| QA_THRESHOLD / REFUSE_THRESHOLD | 问答对直答阈值 / 拒答阈值 | 可选，调整须附评测对比（§17.3）；部署机真实阈值复测结论见 §5.5 | `0.85` / `0.55` |
| CACHE_TTL_S / RETRIEVER_TOP_K / RERANK_TOP_N / CHUNK_DEDUP_SIM / HNSW_EF_SEARCH | 检索链路调优 | 可选（缺省 300 / 20 / 5 / 0.98 / 40） | 保持缺省 |
| DAILY_QUESTION_LIMIT | 单用户每日提问上限 | 可选 | `200` |
| MESSAGE_RETENTION_DAYS / MESSAGE_CLEANUP_COOLDOWN_DAYS | 提问记录两段式清理 | 可选 | `90` / `30` |

### 9.3 生效方式与项目名纪律

- 改 `.env.prod` 后需**重建** app 生效（env_file 不随 restart 重读）：`docker compose -f deploy/docker-compose.prod.yml up -d --force-recreate app`。
- **compose 项目名与卷名口径**：项目名 = compose 文件所在目录名（v5 实测：`-f` 指定时取文件目录、与执行 cwd 无关；`-p`/`COMPOSE_PROJECT_NAME` 可覆盖）。deploy.sh 用 `deploy/docker-compose.prod.yml` → 项目名固定 `deploy`，卷名 `deploy_uploads` / `deploy_pgdata` / `deploy_mineru-models`。**漂移条件**：复制/改名 compose 文件所在目录、或显式 `-p` 与 deploy.sh 口径不一致——会生成第二套卷与容器（数据"消失"实为新卷为空），§3.6 的备份/恢复卷名随之漂移；任何迁移/备份前先 `docker volume ls | grep uploads` 核对实际卷名。


## 10. 评测自动闭环运维（F-1019，评测集自动更新）

周任务（应用内 APScheduler，UTC）：**周日 04:00** 合成流水（难例采集 → 点踩种子变体 → 文档抽样造题，job id `eval_auto_synth`）、**周日 05:00** 三视角自动跑分（service/hr/all 各建 `[自动]` run，job id `eval_auto_run`）。总闸与预算存 `app_settings`（key=`eval_auto`），管理端「评测管理 → 自动评测设置」在线调整（PUT 即生效，无需重启）。

### 10.1 手动触发（不等周任务）

```bash
# 合成流水（采集+合成+造题一体；总闸关闭时零副作用）
uv run --no-sync python - <<'PY'
from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.assembly import build_llm
from app.db.base import SessionLocal
from app.services.eval_auto import run_synth_pipeline
with SessionLocal() as s:
    print(run_synth_pipeline(s, get_clock(), get_settings(), llm=build_llm(get_settings(), 0.0)))
PY

# 三视角自动跑分（同周幂等）
uv run --no-sync python - <<'PY'
from app.core.clock import get_clock
from app.core.config import get_settings
from app.main import _build_chat_deps
from app.db.base import SessionLocal
from app.services.eval_auto_run import run_weekly_eval
with SessionLocal() as s:
    print(run_weekly_eval(s, get_clock(), get_settings(), deps=_build_chat_deps(get_settings())))
PY
```

### 10.2 核查与排障

- 跑分结果：管理端「评测管理 → 跑分记录」过滤名称 `[自动]` 前缀；报告页看正式/扩充分层与环比。
- 退化告警：概览「语料健康度」卡片红色提示条（阈值 `eval_auto_regression_pp` 百分点）；明细在 run 的 `detail.regression`。
- 单视角失败：该视角 run 置 failed（其余视角照常），重跑 = 管理端删除异常 run 后手动触发，或等下周。
- LLM 配额：合成与跑分共用 DeepSeek 配额；预算三闸（synth_per_seed / doc_sample_per_dept / weekly_max_questions）在设置页调整，`weekly_max_questions` 超限种子自动留待下周。

### 10.3 开关

生产开启：`.env` 加 `EVAL_AUTO_ENABLED=true` 后重建 app（env_file 不随 restart 重读）；运行期临时停用走设置页总闸（立即生效）。
### 10.4 交付前性能取证（商用就绪工单 15）

交付/升版前在**目标部署机**跑一轮 NFR 取证并归档（验收证据链，数字可复现）：

```bash
NFR_USER=<管理账号> NFR_PASS=<口令> python3 deploy/nfr-check.py          # 全量（含 20 并发×2min）
NFR_USER=<管理账号> NFR_PASS=<口令> python3 deploy/nfr-check.py --quick  # 冒烟（01/02 各 10 轮）
```

- 口径：NFR-01 首 token P50/P95、NFR-02 检索全链、NFR-03 并发稳态成功率与 P95（三者达标线均见
  test-cases TC-NFR，不在本表复写数字）；CPU TEI 与 GPU TEI 各留一份基线。
- 产物：`nfr-results/nfr-<时间戳>.json`——随版本登记进 deploy/releases.md 对应行。

## 11. 部署拓扑：scale 多副本与调度抢主（工单 20，2026-10-31）

单机缺省形态（`docker compose up -d`）零变化；需要横向扩容时用 scale 覆盖文件
（**前置条件 `STATE_BACKEND=pg`**，未开禁用——多副本 + 进程内状态 = 状态分裂）：

```bash
# api 副本 ×2（WEB_CONCURRENCY=2，宿主端口 8000-8003）+ scheduler 副本 ×1（不发布端口）
docker compose -f docker-compose.yml -f deploy/docker-compose.scale.yml up -d
```

- **副本分工**：api 副本 `SCHEDULER_ENABLED=false`（不跑调度、entrypoint 跳过自愈）；
  scheduler 副本同镜像同 CMD，调度与启动补偿随 lifespan 内抢主结果启停。
- **恰一 leader 仲裁**：各 worker 进程启动时 `pg_try_advisory_lock` 抢主（会话级锁，
  持锁连接随进程存活）——多副本日志**恰一份**"调度启动"（其余为"调度主锁已被其他
  实例持有，本进程不启动调度"）。
- **锁排查 SQL**（怀疑双调度器/锁漂移时）：

```sql
SELECT * FROM pg_locks WHERE locktype = 'advisory';   -- 会话级锁在册即有持有者
SELECT hashtext('ragdemo:scheduler:leader');           -- 与 pg_locks.objid 对账
```

- **健康检查口径**：scheduler 副本复用 app 同款 healthz（无状态，容器内网消费，不发布
  端口）；compose < v2.24 的部署机退化为独立 project 文件（见覆盖文件头部注释）。
- **部署验证检查单（归部署机复测窗口，工单 13 检查单延续；R6 归属）**：
  1. scale 拓扑起双 app 副本 + scheduler 副本，scheduler 日志恰一份"调度启动"，两
     app 副本日志各一份"主锁被持有"；
  2. `workers=2` 下问答/agent 全链路回归（PG 态正确：state_counters 共享读回、缓存
     跨副本命中）；
  3. `SCHEDULER_ENABLED=false` 的 app 副本重启，日志无自愈行（entrypoint gate 生效）；
  4. leader 容器 `docker kill` 后，存活副本在下一生命周期接管调度（日志出现"调度启动"）。

- **本机预验证记录（2026-11-01，工单 20；与 dev 栈共存窗口，仅动 app/scheduler 两服务）**：
  - 检查单 1/3 **本机验证 ✓**：三副本 healthy；scheduler 日志恰一份 `Scheduler started`
    （APScheduler 字面量，即"调度启动"；8 条 Added job 含 start 后补挂的召回计数刷库）；
    `pg_locks` advisory 恰 1 条且 `objid` 与 `hashtext('ragdemo:scheduler:leader')` 对账一致，
    `pg_stat_activity` 归属 scheduler 副本 IP；运行期一次性容器对同键 `pg_try_advisory_lock`
    返回 False。app 副本 gunicorn master+2 worker、scheduler master+1（覆盖文件未给
    scheduler 设 WEB_CONCURRENCY，按定义如此）。app 副本重启后恰 1 行
    `[entrypoint] alembic upgrade head`、0 行自愈；对照 scheduler（gate=true）启动有自愈行。
  - **实现口径修正**（引文案先对代码）：app 副本（`SCHEDULER_ENABLED=false`）**不打印
    "主锁被持有"**——scheduler.py 选举段整块 gate 在 `scheduler_enabled` 内，false 时静默
    跳过；该行只在 enabled 且抢锁失败时出现。互斥证明以 pg_locks/pg_stat_activity 归属
    为准，检查单 1 的"各一份主锁被持有"按此口径理解。
  - 检查单 2 **部分本机验证 ✓**：登录限流滑动窗口跨副本共享实测（副本 1 第 11 次登录请求
    429/错误码 2005，副本 2 立即同样 429——state_auth_guards 共享读回）；**问答/agent
    全链路（需登录态）待部署机**。检查单 4 **待部署机**（本机窗口中止未做）。
  - **两处部署阻断发现（本机实测，部署前必须解决）**：其一，compose `ports` 合并为**追加**
    语义——基文件 `8000:8000` 与覆盖 `8000-8003:8000` 并存（`docker compose config` 可
    复核），按本节命令启动必报 `Bind for 0.0.0.0:8000 failed: port is already allocated`；
    修复方向（后续工单）：覆盖文件 app.ports 改 `!override`（与既有 `!reset` 同版本线）或
    走上方独立 project fallback。其二，replicas + 范围端口映射**重启漂移**：`compose restart`
    后映射从范围低位漂到末端（`docker port` 实证），stop+start 又复位，且 restart 后宿主
    转发短暂拒绝——部署机需逐副本钉静态端口映射或用独立 project 文件消除漂移面。本机
    验证经不入库的临时端口覆盖（`ports: !override` 换到空闲段）完成；宿主 8000-8003
    是否空闲属部署机前置自查项。
