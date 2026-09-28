# 黄金样本集（golden samples）投放说明

对接人投放真实样例的操作指南。黄金样本回归是解析/清洗/分块代码的硬门禁
（standards §10.7：任何解析代码改动必须先跑黄金样本回归；§15.7：解析改动必须附前后
解析质量对比），样本缺失会在测试输出汇总区打印清单（§15.1 禁止静默跳过）。

## 运行方式

```bash
.venv/Scripts/python -m pytest tests/integration/test_golden.py -v
```

每个样本走全链断言：解析引擎=预期 → 质量红线（§10.3，清洗后 ≥ max(100, 清洗前×10%)）
→ 清洗（rag/clean.py 规则链）→ 乱码检测（F-1005，>5% 判失败）→ 标题层级分块（块数 ≥1）。

## 五类样本含义

| kind | 含义 | 期望解析路径 |
| --- | --- | --- |
| `clean` | 规整单栏文档（标题层级清晰、无复杂版式） | docx→python-docx；pdf→pypdf（配 MinerU 后→mineru） |
| `dual_col` | 双栏/多栏排版 pdf（阅读顺序易错） | mineru（版面分析） |
| `table` | 表格密集文档（表格占比高） | docx→python-docx；pdf→mineru |
| `scanned` | 扫描件/图片型 pdf（需 OCR） | mineru（OCR） |
| `garbled` | 乱码样本（编码损坏），预期全链在乱码检测处**失败**，原因"编码异常" | 任意 |

## 文件命名规范

```
<部门或域>_<主题>[_<特征说明>]_v<主版本号>.<docx|pdf>
```

- 域用英文小写：`hr`（人力资源）、`admin`（行政）、`kf`（客服）。
- 主题与特征可用中文或英文：`hr_制度_v1.docx`、`hr_制度_扫描件_v1.pdf`。
- 禁止空格与特殊符号；同主题升级样例递增版本号（`_v2`），旧样本保留可回溯。

## 投放步骤

1. 将真实文档（**试点真实文档脱敏后**）放到 manifest `file` 字段标注的路径，
   相对 `tests/fixtures/`，例如：`tests/fixtures/golden/hr_制度_表格_v1.docx`。
2. 在 `tests/fixtures/golden/manifest.json` 找到对应条目，**删除 `"pending": true`**
   （或置为 `false`），并核对 `expect`（见下）。
3. 运行上面的 pytest 命令确认该样本由 skip 变为通过；不通过时按报错调整 expect 或
   反馈解析层问题（不要为了让它变绿而放宽红线）。

## manifest 字段怎么填

```json
{"file": "golden/hr_制度_v1.docx", "kind": "clean",
 "expect": {"engine": "python-docx", "min_chars": 300, "min_blocks": 1,
            "garbled_ok": false, "fail_reason": null}}
```

- `file`：相对 `tests/fixtures/` 的路径，必须以 `golden/` 开头，后缀 `.docx`/`.pdf`。
- `kind`：五类之一（见上表）。
- `expect.engine`：`python-docx` / `mineru` / `pypdf`。按解析器选择树：docx 一律
  `python-docx`；pdf 在回归骨架中钉死为进程内路径 `pypdf`（不随部署环境
  `MINERU_BASE_URL` 漂移），`mineru` 类样本待 W2 接入后启用。
- `expect.min_chars`：清洗后字符数下限。投放后先跑一次看实际值，留 20% 余量填写
  （例如实测 380 则填 300），防止文档微调导致回归翻红。
- `expect.min_blocks`：分块数下限，正常填 1。
- `expect.garbled_ok` + `expect.fail_reason`：仅乱码类使用——`true` 与 `"编码异常"`
  成对出现，表示预期全链在乱码检测处以该原因失败；其余样本填 `false` / `null`。
- `pending: true`：样例尚未投放时保留；投放并验证后删除该键。缺失与 pending 条目
  会 skip 并在测试输出汇总区打印清单，**不会静默跳过**。

## 当前清单状态

- 真跑种子（程序化生成，`tests/fixtures/tools/make_golden_seed.py`，可直接重生成）：
  - `golden/hr_制度_v1.docx` — clean，清洗后 381 字符；
  - `golden/admin_handbook_v1.pdf` — clean，清洗后 161 字符；
  - `golden/hr_乱码_v1.docx` — garbled 占位，乱码占比 20.6%，预期失败原因"编码异常"。
- 待投放（pending，等对接人提供真实样例）：`dual_col`（双栏 pdf）、`table`（表格
  密集 docx）、`scanned`（扫描件 pdf）。真实样例到位后可替换程序化种子（同步更新
  expect 与本 README 的状态清单）。
