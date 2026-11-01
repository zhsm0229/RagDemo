# 29: arm64 TEI 基镜像自构建钉版

**What to build:** TEI 官方镜像无 arm64 manifest，arm64 服务器（含国产化 ARM 平台）`docker compose pull` 直接失败——自构建 linux/arm64 TEI 基镜像（bge-m3 / bge-reranker 双模型目录）并钉版，登记 deploy-requirements §2（CPU 架构行临时方案转正解）与 runbook §6（revision/hash，同模型升级操作口径）。本机 arm64（Apple Silicon）可直接验证构建。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent（路线图阶段二 item 3，2026-11-01 code-review spec 轴缺口；workload 较重，HF_HUB_DISABLE_XET=1 + hf-mirror 构建期下载）

- [ ] Dockerfile 构建 arm64 TEI 基镜像（TEI 版本钉版 + 双模型目录预热，构建参数与 download_models.py 同源口径）
- [ ] 本机 `docker buildx` 构建 linux/arm64 并起容器冒烟：/healthz 就绪、embed/rerank 探活真实推理一问
- [ ] 镜像 hash/TEI 版本/模型 revision 登记 deploy-requirements §2 与 runbook §6（升级操作三步口径一致）
- [ ] deploy-requirements §2 表行更新：arm64 场景从"临时方案 mac-setup §7"改指自构建镜像（保留 mac-setup 链接作历史）
