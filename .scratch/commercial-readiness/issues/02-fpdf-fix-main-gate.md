# 02: fpdf2 依赖声明——主门禁复绿

**What to build:** `make ci` 恢复退出码 0。golden 防漂移测试（test_golden.py::test_committed_seeds_match_builder_output 与 test_crosstalk_suite.py 构建确定性）当前因 `ModuleNotFoundError: No module named 'fpdf'` 失败（tests/fixtures/tools/make_golden_seed.py:68 懒导入），根因是 fpdf2 未声明进任何依赖组——pyproject.toml、uv.lock、Dockerfile、ci.yml 均无。远端 CI 用 `uv sync --locked` 同样装不到，test job 必红，按"失败即停"阻塞全部合入与发布。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] fpdf2 加入 pyproject.toml dev 依赖组并登记用途（golden 样本构建工具，§2.8 新依赖登记惯例）
- [ ] `uv lock` 更新锁文件，`uv sync --locked` 可复现安装
- [ ] `make ci` 全量复绿（1204 passed + 0 failed，退出码 0）
- [ ] AGENTS.md"当前状态"数字待本工单完成后由工单 12 统一刷新（本工单不改活文档）
