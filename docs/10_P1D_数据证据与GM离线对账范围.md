# P1-D 数据证据与 GM/离线对账范围

日期：2026-11-03  
实现代码 SHA：`d72eaa71e1f0df8d163165408aebd2ea9b1568db`  
数据集ID：`gm-rolling-180d-163ec775a06853f54078`  
样本状态：`ROLLING_180D_RESEARCH`  
工作包结论：**FAIL**

## 1. 结论

已为当前可用180日数据生成脱敏、带哈希的可复现 manifest，并在当前分支上重跑既有
2026-10-20～23 GM/离线对账：852/852行、8字段零差异。该结论只证明指定样本、指定
字段的适配/回放一致性；两侧共用 `run_core_step`，不证明策略逻辑独立正确。

P1-D 严格判为 **FAIL**，原因有二：

1. 既有 GM 事实产物没有当前 `trend_inputs`、路径候选、`blocked_at`、订单意图、成交和
   退出的成对记录，因此无法扩展到任务书要求的全链逐事件对账；
2. 仓库没有版本化交易所时段/临时停盘日历，无法把观测到的时间间隔可靠拆成计划休市与
   缺失 M1。manifest 将缺分钟记为 `null/BLOCKED`，没有伪造为0。

## 2. 当前 HEAD 对账重跑

命令：

```powershell
python -m backtest.parity_run `
  --data-dir data/cache/gm_sprint1 `
  --mapping data/cache/gm_sprint1/mapping.json `
  --gm-facts reports/sprint1_gm_m15_facts.jsonl `
  --output reports/p1d_current_head_fact_parity_summary.json `
  --mismatches reports/p1d_current_head_fact_parity_mismatches.json
```

退出码0；GM 852行、离线852行、mismatch 0。原
`reports/sprint1_fact_parity_summary.json` 和 mismatch 文件保持不变，新结果使用独立文件。

### 2.1 已比较

- 行身份和时间边界：`symbol + eob` 精确匹配；
- `close/ema34/atr14/slope_state`；
- `h1_absolute/top_div/bottom_div`；
- 完整嵌套 `trend_decision`，包括该产物中的提议、reason code、投票和状态。

### 2.2 未比较

- `h1_relative`：GM产物中存在，但不在历史8字段范围；
- 当前 `trend_inputs` 的 S1/S2/P/R：该GM产物生成时尚未记录；
- Path A/B/C/D候选、证据ID和锚点；
- `blocked_at`；
- 订单意图、手数和风险预算；
- 部分成交、拒单、撤单、未知状态；
- 止损、止盈、趋势退出及已实现状态。

因此“852行零差异”不得表述为订单链或策略规则验收。

## 3. 脱敏数据集证据

生成命令：

```powershell
python -m backtest.dataset_evidence `
  --data-root data/cache/gm_rolling_180d `
  --facts data/cache/gm_rolling_180d/baseline_facts.jsonl `
  --code-sha d72eaa71e1f0df8d163165408aebd2ea9b1568db `
  --generation-command "python -m futures_trend_quant.data.development_export --output-root data/cache/gm_rolling_180d --start 2026-05-07 --end 2026-11-02 --purpose SPRINT2_ROLLING_180D --resume" `
  --output reports/sprint2_p1d_dataset_manifest.json
```

manifest 首先调用原本地 manifest 的全文件哈希校验；任一 mapping、continuous 或 raw
contract 文件哈希变化都会失败关闭。

| 项目 | 数值 |
|---|---:|
| 品种 | 12 |
| mapping交易日行 | 1,452 |
| 连续M1 | 488,009 |
| 聚合M15 | 32,534 |
| 完成warmup事实 | 29,534 |
| 实际合约 | 37 |
| 换月 | 25 |

关键哈希：

| 输入 | SHA256 |
|---|---|
| 本地数据 manifest | `5dd4f0588e159f37a1d57ca7cd6c2dbe0e8f5fd09ac41721c85d25e47df2144d` |
| `config/strategy.json` | `4932fe7258486a022eb32437565c5104994d3d960b7ff3bd05cc70f67714380d` |
| `config/fees.json` | `6653b048c1e6eef5507ffb10ca09200ee093a966df27b0e6501fbd0fac29e20b` |
| 基准逐根事实 | `cdf059b24401722a58f0b05b59ef61990c58242fa1a00f42bf64b34ba6322499` |

每个品种还记录实际合约、换月日期及价差、mapping/continuous/raw contract文件SHA256、
M1/M15行数、重复、时间、OHLC和交易日字段检查。原始路径只以去身份的相对文件名或合约
文件名出现，原始 parquet 未提交。

## 4. 数据质量结果

12品种均满足：

- `duplicate_symbol_eob_rows=0`；
- `invalid_eob_rows=0`；
- `non_increasing_rows_after_sort=0`；
- `invalid_ohlc_rows=0`；
- `null_trading_date_rows=0`；
- mapping交易日重复=0、空合约=0；
- 时间戳样本为固定 `+08:00`。

`observed_clock_gaps_gt_60s` 是原始钟面间隔计数，包含午休、夜盘间隔、周末和节假日，
不能直接解释为缺 bar。最大观测间隔为669,660秒。由于缺版本化交易所时段及临时停盘
日历：

```text
missing_minute_bars = null
missing_minute_assessment = NOT_ASSERTED_WITHOUT_VERSIONED_EXCHANGE_SESSION_CALENDAR
```

这是 P1-D 未通过的明确证据缺口。

## 5. 连续复权方向复核

冻结定义：换月价差=`新合约同日收盘-旧合约同日收盘`；从换月交易日起，连续序列减去
累计价差。

测试命令：

```powershell
python -m pytest -q tests/test_backtest_continuous.py tests/test_development_export.py
```

合成用例证明：

- 正价差 `+100`：旧3000、新3100，复权后为3000/3000，移除机械跳空；
- 负价差 `-100`：旧3000、新2900、次日2920，复权后3000/3000/3020，换月后原始
  `+20` 方向和幅度保持。

缺任一换月日新旧腿会 `ExportDataError`，不会猜测价差。实时 GM 仍未接入该连续复权
序列，换月后会冻结新开仓；离线连续序列不能代表实时换月信号链。

## 6. 可复现性与限制

- 同一本地数据、manifest、配置和代码可重建488,009 M1、32,534 M15和29,534事实；
- `dataset_id` 同时绑定 manifest、策略配置、事实文件和代码SHA，任一变化会产生新ID；
- 原始行情、Token、账户ID和GM日志不进入Git；
- 180日窗口已用于基准、消融和诊断，不是独立验证集；
- 2023～2024开发集和2025～2026独立验证数据仍被权限阻塞。

## 7. Gate 与安全判定

- P1-D：**FAIL**。数据哈希与结构证据已补齐，但缺分钟日历和全链成对GM证据未完成。
- Gate 1：维持 **PASS**。
- Gate 2：**FAIL**。P0-A订单恢复缺口及本工作包对账覆盖缺口仍在。
- Gate 3：**FAIL**。
- Gate 4：**BLOCKED**。
- `execute_enabled=false`；未启用模拟盘或实盘订单。
- 未使用未核验费率评价收益。

机器证据：

- `reports/sprint2_p1d_dataset_manifest.json`
- `reports/sprint2_p1d_parity_coverage.json`
- `reports/p1d_current_head_fact_parity_summary.json`
- `reports/p1d_current_head_fact_parity_mismatches.json`
