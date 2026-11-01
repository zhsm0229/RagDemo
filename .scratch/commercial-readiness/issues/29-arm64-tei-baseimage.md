# 29: arm64 TEI 基镜像自构建钉版

**What to build:** TEI 官方镜像无 arm64 manifest，arm64 服务器（含国产化 ARM 平台）`docker compose pull` 直接失败——自构建 linux/arm64 TEI 基镜像（bge-m3 / bge-reranker 双模型目录）并钉版，登记 deploy-requirements §2（CPU 架构行临时方案转正解）与 runbook §6（revision/hash，同模型升级操作口径）。本机 arm64（Apple Silicon）可直接验证构建。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：arm64 原生构建成功；完整推理冒烟受本机 Docker 内存预算约束归部署机复测）

- [x] `docker/tei-arm64.Dockerfile` 落盘：TEI v1.9.2 源码 arm64 原生构建（cargo-chef 两阶段+cache mounts；**去 sccache/MKL**——官方拉 x86_64 二进制与 intel-oneapi 无 arm64 包，CPU 走 ort/candle 特性）；模型不烘进镜像（与 compose 卷挂载口径一致）；本机 `buildx --platform linux/arm64` 构建成功，产物 `ragdemo-tei-cpu-arm64:cpu-1.9.2`（456MB，inspect 实证 `arm64/linux`）
- [x] 结构冒烟：容器起动→模型加载→tokenization→backend 启动全部成功（走至 warmup 阶段）；**warmup 阶段 OOM(137) 为本机 Docker VM 预算约束**（15.6GB 中 dev 栈常驻 ~7.5GB，bge-m3 峰值不足；停 mineru 复试同样）——非镜像缺陷，完整推理冒烟（/health + /embed 真实一问）归部署机复测窗口（工单 13 清单）
- [x] 镜像 hash/构建口径登记 deploy-requirements §2 与 runbook §6（TEI 版本 v1.9.2/Cargo.lock 钉版；模型 revision 待部署机登记沿用 §6 口径）
- [x] deploy-requirements §2 表行更新：arm64 场景从"临时方案 mac-setup §7"改指自构建 Dockerfile（保留 mac-setup 链接作历史）
