# Sprint 2 任务书执行总报告

日期：2026-11-03

执行起点：`main@ab74d9d788d99ca9bc17ab86546657f2af2f07af`

任务书复核快照：`ab74d9d788d99ca9bc17ab86546657f2af2f07af`

工作分支：`sprint2-evaluation-fidelity`

工作性质：研究代码与证据链改进，不是上线授权。

## 1. 总结论

已按 P0-A → P0-B → P0-C → P1-D → P1-E 顺序完成所有不依赖新增外部材料的工作，
每个工作包独立提交并保留机器证据。没有直接向 `main` 推送，没有创建或合并 PR。

最终门槛：

| Gate | 状态 | 结论 |
|---|---|---|
| Gate 1 评估口径正确 | PASS | 首触逐 bar 与独立转折事件已分离，人工边界测试通过 |
| Gate 2 当前版本工程可信 | FAIL | 订单异常/重启恢复和GM全链成对证据不完整 |
| Gate 3 趋势层达标 | FAIL | 方向、覆盖和独立事件延迟三项失败 |
| Gate 4 经济性与执行 | BLOCKED | 长历史和真实费率未到位，本Sprint未执行收益评价 |

当前不能写“策略有效”“整体可用”或“接近达标”，也不能进入仿真下单验收。

## 2. 工作包结果

### P0-A｜FAIL

- 适用基线：`ab74d9d`。
- 重建结果：109 passed、3 xfailed；三个 xfailed 通过 `--runxfail` 确认均为目标窗口
  无完成H1 warmup事实，不计为通过。
- `FILL_BEYOND_STOP`、部分成交撤单有端到端测试。
- 零成交拒撤、重复回调、未知状态缺专门回归；`ftq_trades` 启动重建未实现。
- 证据：`docs/07_P0A_当前HEAD验收证据.md`。

### P0-B｜PASS

- `FUTURE_FIRST_TOUCH_V1`：逐 bar 未来首触对象，不再误称 ZigZag。
- `ATR_DIRECTIONAL_CHANGE_V1`：逐品种独立确认方向转折，用于延迟。
- 旧1,891次标签变化保留为 legacy；新口径810事件、793可评分、17 censored。
- 新延迟中位数6根，仍 FAIL。
- 证据：`docs/08_P0B_趋势参考定义修订报告.md`。

### P0-C｜PASS

- 810事件全部有组件/状态/投票/reason/H1影子/换月/时段明细。
- 416慢事件中333次确认当根 `maintain`、56次 `veto`；完整 path1 51次、path2 29次。
- 30个慢事件样本含可复核事实时间线。
- 未修改状态机或H1 veto；结论只陈述观察，不宣称因果。
- 证据：`docs/09_P0C_趋势组件事件诊断报告.md`。

### P1-D｜FAIL

- 当前分支重跑既有样本：GM 852行、离线852行、8字段零差异。
- 明确未比较当前 `trend_inputs`、路径、拦截、订单、成交和退出。
- 数据集ID：`gm-rolling-180d-163ec775a06853f54078`；488,009 M1、32,534 M15、
  29,534完整事实、37合约、25换月。
- 重复、非法时间、非法OHLC均为0；缺M1因缺版本化交易时段日历保持 null/BLOCKED。
- 正负换月价差合成测试均证明连续价和换月后收益方向正确。
- 证据：`docs/10_P1D_数据证据与GM离线对账范围.md`。

### P1-E｜PASS（台账建立）

- 长历史、真实费率、116背驰签认、XAUUSD长历史、AURUM源码/31用例、交易时段日历
  均有负责人、最小字段、格式、验证方法和被阻塞Gate。
- 所有外部材料状态保持 BLOCKED，没有默认值替代。
- 证据：`docs/blockers.md`。

## 3. 冻结指标结论

| 指标 | 结果 | 门槛 | 状态 |
|---|---:|---:|---|
| 方向一致率 | 52.36% | >=60% | FAIL |
| >=2 ATR趋势段覆盖 | 55.93% | >=70% | FAIL |
| 震荡切换 | 0.096次/品种日 | <=1 | PASS |
| 独立转折延迟中位数 | 6根M15 | <=4 | FAIL |
| 未解决独立事件 | 163/793 | 无硬门槛 | 风险高 |

本轮没有调门槛、样本、标签阈值、风险参数或策略默认值来追求通过。

## 4. 版本与证据索引

| 工作 | 提交 |
|---|---|
| P0-A当前HEAD证据 | `1c9b715` |
| P0-B定义冻结 | `e374ee9` |
| P0-B实现 | `97b8f94` |
| P0-B并行报告 | `6968f3b` |
| P0-C诊断冻结 | `7d13fa1` |
| P0-C实现/修复/慢事件摘要 | `152d7e6` / `0d67968` / `c8e7521` |
| P0-C报告 | `66c900a` |
| P1-D实现 | `d72eaa7` |
| P1-D报告 | `6759ef6` |
| P1-E阻塞台账 | `09172df` |

主要机器证据：

- `reports/p0a_current_head_acceptance.json`
- `reports/sprint2_p0b_reference_comparison.json`
- `reports/sprint2_p0c_event_diagnostics.json`
- `reports/sprint2_p1d_dataset_manifest.json`
- `reports/sprint2_p1d_parity_coverage.json`
- `reports/sprint2_external_blockers.json`

## 5. 复核限制

- 180日窗口已经参与基准、消融、定义修订和诊断，不是独立验证集。
- 2023～2024开发集及2025～2026验证集受GM权限阻塞。
- 费率均未核验，未执行净收益、PF、胜率排名或最优参数评价。
- 116条背驰没有人工真值签认。
- 三个XAUUSD锚点缺长历史。
- 无AURUM原始模块和31黄金用例，项目继续称“规格重构版”。
- 实时GM未接入离线连续复权；换月后信号链仍有限制。

## 6. 安全确认

- `config/strategy.json` 保持 `execute_enabled=false`。
- GM只有 `MODE_BACKTEST`、配置 `mode=backtest`、`backtest_orders_enabled=true` 且
  `execute_enabled=false` 同时满足时才允许回测订单。
- 未启用模拟盘或实盘下单。
- 未提交原始 parquet、账户ID、Token或GM原始日志。
- 未使用未核验费率评价收益。

机器总表：`reports/sprint2_taskbook_execution_summary.json`。

## 7. 最终分支复核

- 全量测试：120 passed、3 xfailed、1 warning；JUnit：
  `reports/sprint2_final_pytest.xml`。
- 订单门控、趋势退出、部分成交撤单、超止损成交、保本退出与GM SDK契约专项：
  28 passed、1 warning。
- `compileall`、`strategy.json` / `fees.json` 解析和强制
  `execute_enabled=false` 校验均通过。
- 合成烟测通过，且报告明确 `performance_interpretation_allowed=false`；其机械盈亏
  不进入本报告的收益结论。
- 三个 xfailed 仍为XAUUSD目标窗口未形成完成H1 warmup事实，未转记为通过。
