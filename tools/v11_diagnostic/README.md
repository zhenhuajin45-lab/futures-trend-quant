# V11 一次旧窗二维诊断

本目录只分析冻结 V10 无成本简单入场的 631 笔已平，另保留 5 笔未平的分母说明；不运行策略账户、不取新行情、不占用独立检验、不下单。任务 4–6 未授权执行。

原件四个 SHA256 固定于 `contracts.EXPECTED`。统计、工作流、测试、任务书、环境、原工具和预注册均列入排序后的 `freeze_manifest.json`，项目文本统一 LF 哈希，原附件及二进制采用字节哈希。旧工具源码与 7 个用例在 `reference_tool/`，未包含包内带结果的公开逐笔文件。

用户明确安排两种角色均由 Codex 承担。回执记录 `SINGLE_AGENT_USER_OVERRIDE`、同一身份及 `independent_person=false`。两轮自检不是两名独立人员签收，不能用来证明独立盲判。时间归档保留 GitHub 冻结版本发布后读取清单的 HTTP Date 与远端 blob；本地时间或摘要自身不作独立时间证明。

## 使用

沿用项目 Python 3.13 与已装 pandas/numpy，不安装或升级依赖。

```powershell
python -m pytest tests/test_v11_diagnostic.py -q
python -m unittest discover -s tools/v11_diagnostic/reference_tool -p 'test_*.py'
```

正式入口要求冻结登记、同一摘要的执行/复核回执及时间归档；原始 ZIP 单独解压在 `data/cache/v11_review_package`，不得改变旧 `diagnose.py`。

```powershell
python -m tools.v11_diagnostic.workflow manual --prereg tools/v11_diagnostic/prereg.json --receipts data/cache/v11_restricted/receipts --tool-dir data/cache/v11_review_package --prepared '<原冻结目录>/prepared.pkl' --out data/cache/v11_restricted/manual_001
# 阅读局部输入及算术核对结果后，另留 hand_review.json，绑定 manual_checks.json 哈希。
python -m tools.v11_diagnostic.workflow full --prereg tools/v11_diagnostic/prereg.json --receipts data/cache/v11_restricted/receipts --tool-dir data/cache/v11_review_package --prepared '<原冻结目录>/prepared.pkl' --manual-dir data/cache/v11_restricted/manual_001 --out data/cache/v11_restricted/run_001
```

直接汇总已受控生成的原诊断目录：

```powershell
python -m tools.v11_diagnostic.summarize --input '<受限诊断目录>' --prereg tools/v11_diagnostic/prereg.json --receipts '<回执目录>' --out data/cache/v11_restricted/summary_001
```

输出 `cells.csv`、`cell_products.csv`、`within_product.csv`、`fixed_2r.csv`、`path.csv`、`coverage.json`、`decision.json`、摘要清单。`fixed_2r.csv` 同时列出直接门槛组统计、合格品种时间层差值和最终等权差，不以分位数表现替代实际阈值。

输出目录必须全新。正式逐笔、案例和汇总均留在本机受限 cache；公开只交代码、合成证据和不识别案例的状态。即使 `READY_FOR_BLIND_REVIEW`，也只准准备后续盲判，不释放新开发窗、独立评分或模拟权限。

一次完整诊断在原件/回执/手算门槛通过后才占用运行标记；未运行不计次数。入口拒绝第二次完整运行，不自动重试。一次修复上限只适用于已有明确同字节定位、解析或可复现计算缺陷的证据，须保留新旧代码与输出、原复核角色复签后单独处理；不能通过删除运行标记获得重考。交易或历史样本本来不足，不允许补第 21 段、补新源、改阈值、删样本或延窗。

工程链为：原件及源清单校验 → 回执/时间证明 → 稳定 ID 手算抽例 → 独立算术算法自检 → 一次原工具诊断 → 队列及毛 R 恒等核验 → 三种分层、路径及自动裁决 → 临时目录校验 → 原子发布。合成输出必须显式 `SYNTHETIC_ONLY`，不能进入正式目录。
