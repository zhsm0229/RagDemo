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

| kind | 含义 | 期望解析引擎（`expect.engine`，同 `documents.parse_engine`） |
| --- | --- | --- |
| `clean` | 规整单栏文档（标题层级清晰、无复杂版式） | docx→`docx`；pdf→`pypdf`（配 MinerU 后→`mineru`） |
| `dual_col` | 双栏/多栏排版 pdf（阅读顺序易错） | `mineru`（版面分析） |
| `table` | 表格密集文档（表格占比高） | docx→`docx`；pdf→`mineru` |
| `scanned` | 扫描件/图片型 pdf（需 OCR） | `mineru`（OCR） |
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
 "expect": {"engine": "docx", "min_chars": 300, "min_blocks": 1,
            "garbled_ok": false, "fail_reason": null}}
```

- `file`：相对 `tests/fixtures/` 的路径，必须以 `golden/` 开头，后缀 `.docx`/`.pdf`。
- `kind`：五类之一（见上表）。
- `expect.engine`：`docx` / `mineru` / `pypdf`——与 `documents.parse_engine` 同一
  词汇（入库即此值）。按解析器选择树：docx 一律 `docx`；pdf 在回归骨架中钉死为
  进程内路径 `pypdf`（不随部署环境 `MINERU_BASE_URL` 漂移），`mineru` 类样本待
  W2 接入后启用。
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
  - `golden/hr_乱码_v1.docx` — garbled 占位，清洗后乱码占比 18.6%，预期失败原因"编码异常"。
- 待投放（pending，等对接人提供真实样例）：`dual_col`（双栏 pdf）、`table`（表格
  密集 docx）、`scanned`（扫描件 pdf）。真实样例到位后可替换程序化种子（同步更新
  expect、`_GOLDEN_SEEDS` 与本 README 的状态清单）。

## 投放真实样例后需同步的测试点

清单断言已写成对投放状态不敏感的不变式（`tests/integration/test_golden.py`）：
投放样例、去 pending、新增条目**不需要改测试**。但以下硬编码点在动清单结构时必须同步：

1. `_GOLDEN_SEEDS`（test_golden.py）：程序化种子三件套的 (file, kind, engine) 三元组。
   替换/移除程序化种子时同步此表——不变式要求这三条始终"在册且生效"（非 pending、
   文件在盘、kind/engine 不漂移）。
2. 五类 kind 覆盖不变式（`test_manifest_schema`）：manifest 必须五类条目齐全；新增
   kind 需同步 `KINDS` 常量与本 README 的类别表。
3. `expect.engine` 词汇校验（`ENGINES` 常量）：只收 `docx` / `mineru` / `pypdf`
   （与 `documents.parse_engine` 同词汇）；新解析器实现需同步 `_engine_name` 映射。
4. pending 全部投放后缺失清单自然为空（`missing_samples()` 不再打印汇总区清单，
   测试仍绿）；只要还有 pending 条目，清单断言要求它们逐条出现在缺失清单中。
5. 本 README"当前清单状态"一节：投放/替换种子后更新字符数与乱码占比实测值
   （以 `python tests/fixtures/tools/make_golden_seed.py` 的自检输出为准）。
