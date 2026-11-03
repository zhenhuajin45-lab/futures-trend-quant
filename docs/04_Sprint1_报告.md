# Sprint 1 保真修复、A/B/C 重构与离线回测台报告

日期：2026-11-03

基线：`9b0c933`

目标账户：1,000,000 元；单笔风险预算 1%；模拟盘/实盘下单未启用。

## 结论

Sprint 1 的代码闭环已经形成：事实层、趋势状态机、A/B/C/D 入场、风险计划、
GM 回测适配和离线 M1 事件回放共同调用 `run_core_step`。本机 GM
`MODE_BACKTEST` 已以 100 万元运行 2026-10-20～23 全品种回测，进程退出码为 0，
没有 `M15_FACTS_REJECT`、`GM_ERROR` 或 `EXIT_REJECT`。

本轮不允许用收益调参。GM 短样本为负且结束时仍有 5 个持仓，只能证明工程链可运行。
唯一硬验收已经通过：GM 与离线回放在同段 852/852 行上零字段差异。
最终本地回归结果为 `89 passed, 3 xfailed`；3 项 `xfail` 均为历史不足的
XAUUSD 锚点验收，没有通过降低 H1 warmup 门槛来掩盖。

## 验收表

| 项目 | 状态 | 证据 |
|---|---|---|
| 100 万账户与 1% 风险基线 | PASS | `config/strategy.json`、GM 摘要 |
| P0-1～5 与 P1-1～4/6/7 事实保真 | PASS | 方向、背驰、H1、S1/S2 专项测试 |
| P0-6 增量事实账本 | PASS | `FactsStateBook` 按 `csymbol` 隔离及窗口测试 |
| P0-7 趋势结束平仓/撤单 | PASS | `TREND_EXIT` 专项测试；GM 实际触发 3 次 |
| P0-8 动能闸门与冷却 | PASS | gate/cooldown 测试与 GM 信号原因分布 |
| Path A/B/C/D | PASS | 各路径正反例、冲突选择、证据去重、D 两层上限 |
| swing/range20 锚点 | PASS | 持久 bar ID、防滑窗漂移与回归测试 |
| dataset 指标递推 | PASS | DIF/DEA/HIST 1e-6；EMA34 3e-4（源 OHLC 已舍入） |
| XAUUSD 三段趋势锚点 | EXPECTED FAIL | 前置仅 96～110 根 M15，少于修正后 H1 所需约 188 根；未降低 warmup 伪造通过 |
| 离线 M1 事件回放 | PASS | 300 次 M15 工程烟测，50 条完整事实；报告禁止收益解释 |
| GM 真实 SDK 回测 | PASS | 852 条 M15 事实、10 开、5 平、退出码 0 |
| GM vs 离线逐根事实 | PASS | 852/852 行、8 个字段，`mismatch_count=0` |
| 券商实际费率 | BLOCKED | `config/fees.json` 保持 `verified=false/null`，收益回测 fail-closed |
| 2023 起完整开发集收益回测 | BLOCKED | 费率未核验；本轮不得用占位费用生成报告 |

## GM 回测事实

回测窗口：2026-10-20 08:00:00 至 2026-10-23 15:01:00。

- `M15_FACTS`：852；`M15_FACTS_REJECT`：0。
- 开仓 10，平仓 5；结束时持仓 5。
- 平仓原因：`TREND_EXIT=3`、`STOP=2`。
- 路径候选通过：A 9、B 72、C 43、D 45；最终下单仍受组合、集群、结构止损和手数门控。
- `PORTFOLIO_POSITION_CAP` 拦截 150 次，`CLUSTER_POSITION_CAP` 拦截 2 次。
- `COOLDOWN_ACTIVE` 4 次、`EVIDENCE_ALREADY_USED` 1 次。
- GM 指标：`pnl_ratio=-0.0090599689`，5 笔已平全部亏损。样本短、含 5 个未平仓，禁止据此评价路径或阈值。

结构化摘要见 `reports/sprint1_gm_backtest_summary.json`；逐根事实见
`reports/sprint1_gm_m15_facts.jsonl`。原始日志位于 Git 忽略目录 `evidence/`。

## 逐根一致性验收

最终比较字段：`close`、`ema34`、`atr14`、`slope_state`、`h1_absolute`、
`top_div`、`bottom_div`、`trend_decision`。结果：GM 852 行、离线 852 行，
缺行 0、数值/状态差异 0。

验收过程中修复了两类真实数据问题：

1. AU/AG M1 偶发缺分钟时，按“连续 15 根”聚合会把后续 M15 整体错位；现改为
   交易所墙钟 M5/M15 边界，和 GM 原生 300s/900s 一致。
2. `history()` 分块在午夜边界按开盘时间选柱；现相邻块重叠一个完整 bar，避免遗漏
   `23:59→00:00` 的 M1 柱，并保留前一块的完整重复柱。

机器可读结果见 `reports/sprint1_fact_parity_summary.json`，差异文件
`reports/sprint1_fact_parity_mismatches.json` 当前为空数组。

## `[重构定义]` 清单

- M5 均线走平：斜率比值进入 ±0.35，且前 6 根内曾为反向。
- 横盘突破：收盘越过最近横盘区间的顺势边界。
- M1 确认：触发后 15 根内的顺势均线穿越、吞没或 MACD 交叉；默认关闭。
- N 字锚点：最近已确认反向枢轴；突破 A 线后才允许更新到后续新枢轴。

其余 M5 背驰、1 ATR 形态位置、0.5 ATR 回调和 4 根/1.5 ATR 横盘按任务包定义实现。

## 关键提交

| Commit | 内容 |
|---|---|
| `14df387` | 锁定 Sprint 1 配置与 100 万基线 |
| `26fbbef` | 纳入 XAUUSD 参考数据集 |
| `165aa74`～`1569928` | B-1～B-4 gate/evidence/paths/anchor |
| `c7a79d7` | P0-6 增量事实持久化 |
| `4a619cb` | P1-6 B_hist 符号门槛 |
| `bf83c09` | P0-1～5、P1-1～4/7 趋势事实对齐 |
| `f52a850` | 离线回测台、费用、复权、metrics、ablation、parity |
| `e970225` | 100 万品种矩阵（证据缺失项 fail-closed） |
| `482c968` | GM/离线共享 `run_core_step` |
| `39d89a0` | P0-7/P0-8 接入 GM 订单链 |
| `d0a3919` | 共享核心离线工程烟测 |

## 尚未解除的限制

1. `config/fees.json` 缺模拟账户的开仓、平仓、平今真实费率。未核验前离线收益回测拒绝启动。
2. XAUUSD 三个趋势锚点需要更长原始历史才能按修正后的 H1 EMA 口径复跑。
3. 116 条背驰人工签认尚未提供；`div_mode` 仍应进入 Sprint 2 消融。
4. 实际合约换月时，GM 会以 `ROLLOVER_ADJUSTMENT_REQUIRED` 冻结新开仓，仍允许退出；离线差价复权已实现，但尚未把复权连续序列接入实时 GM。
5. 模拟盘和实盘下单继续关闭。账户 ID、客户端 token、原始日志和历史 parquet 均不进入 Git。

## 复现命令

```powershell
python -m pytest -q
python -m compileall -q .\futures_trend_quant .\backtest .\deploy .\tests
python -m json.tool .\config\strategy.json > $null
python -m json.tool .\config\fees.json > $null
python -m backtest.smoke --output .\reports\synthetic_smoke_summary.json
git diff --check
```
