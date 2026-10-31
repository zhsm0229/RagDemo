# 10: 许可合规法律意见——MinerU AGPL 等 + 无 MinerU 降级交付档

**What to build:** 商用签约前拿到许可合规结论（架构师评审 P0-6）。MinerU 为 AGPL-3.0（docker/mineru-cpu.Dockerfile:29 钉 mineru[core]==2.5.4）：独立容器 HTTP 调用通常不构成衍生作品，但私有化交付会向客户分发含 AGPL 组件的镜像并带自建 Dockerfile 修改，义务边界需正式意见；BGE-M3/TEI/Gotenberg 条款一并确认。

**Blocked by:** None (can start immediately，外部依赖项)。

**Status:** ready-for-agent（本工单主体为外部法律意见 + 文档登记，agent 可完成预案部分）

- [ ] 出许可合规意见（AGPL 义务边界 + 各组件条款清单：MinerU/BGE-M3/TEI/Gotenberg/pgvector/DeepSeek 服务条款）
- [ ] 结论登记进交付清单（deploy/releases.md 或合规清单附录）
- [ ] "无 MinerU 降级交付档"预案：pypdf 路径已内建，编写降级部署形态说明（功能取舍清单 + 性能差异声明）
- [ ] 若合规结论要求，准备可切换的 compose profile（mineru 有/无两档实测）
