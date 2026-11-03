# P0-A 当前 HEAD 验收证据

日期：2026-11-03  
适用 SHA：`ab74d9d788d99ca9bc17ab86546657f2af2f07af`  
复核快照：`ab74d9d788d99ca9bc17ab86546657f2af2f07af`  
结论：**FAIL**

## 1. 结论

当前 HEAD 的已有测试、编译、配置校验和合成工程烟测均可运行，但订单生命周期验收不完整：
`FILL_BEYOND_STOP` 和“部分成交后撤单并立即平仓”已有生产形状测试；零成交拒单/撤单、
重复订单回调、未知订单状态缺少针对性回归。启动时只恢复 `signal_only` 的趋势运行时，
`ftq_trades` 会重新初始化为空，尚未实现经纪商持仓/未完成订单与本地交易记录的重建。

因此 P0-A 不得写成当前版本全面通过，更不构成模拟盘或实盘授权。

## 2. 固定版本与环境

开始执行时工作区干净，分支为 `main`，HEAD 与任务书复核快照一致。执行工作位于本地分支
`sprint2-evaluation-fidelity`，未直接向 `main` 推送，也未创建 PR。

| 项目 | 值 |
|---|---|
| Python | 3.13.2 |
| GM SDK | 3.0.186 |
| pandas | 2.3.3 |
| pyarrow | 22.0.0 |
| pytest | 9.1.1 |
| `config/strategy.json` SHA256 | `4932fe7258486a022eb32437565c5104994d3d960b7ff3bd05cc70f67714380d` |
| `config/fees.json` SHA256 | `6653b048c1e6eef5507ffb10ca09200ee093a966df27b0e6501fbd0fac29e20b` |
| `config/runtime.local.example.json` SHA256 | `4f1f1a653361a42ad778e0f8c736ac8dea817ec224b5f3b7b2f3374a353fcc5b` |

## 3. 可复现检查

| 检查 | 命令 | 退出码 | 结果 |
|---|---|---:|---|
| 全量测试 | `python -m pytest -q -rxX --junitxml=reports/p0a_pytest.xml` | 0 | 109 passed，3 xfailed，1 warning |
| xfail 真实断言 | `python -m pytest tests/test_trend_anchors_xauusd.py --runxfail -vv` | 1 | 3个断言均失败，作为诊断证据 |
| 编译 | `python -m compileall -q futures_trend_quant backtest deploy tests` | 0 | PASS |
| 策略配置 | `python -m json.tool config/strategy.json > $null` | 0 | PASS |
| 费率配置 | `python -m json.tool config/fees.json > $null` | 0 | PASS |
| 合成烟测 | `python -m backtest.smoke --output reports/p0a_synthetic_smoke_summary.json` | 0 | PASS，仅工程链，不解释收益 |
| 订单路径测试 | `python -m pytest -q tests/test_trend_exit_closes_positions.py tests/test_entry_risk_execution.py tests/test_gm_adapter_and_state.py tests/test_gm_sdk_contract.py` | 0 | 24 passed，仍有下述覆盖缺口 |
| 差异检查 | `git diff --check` | 0 | PASS |

`reports/p0a_synthetic_smoke_summary.json` 中虽然保留合成数据的机械交易指标，但
`purpose=ENGINEERING_SMOKE_ONLY` 且 `performance_interpretation_allowed=false`；本报告不作收益解释。

## 4. 预期失败不是通过

| 测试 | `--runxfail` 的实际断言 | 阻塞条件 |
|---|---|---|
| `test_c1_front_turns_bull_no_later_than_2026_10_26_1930` | `'bull' not found in []` | 目标窗口没有完成修正 H1 warmup 的趋势事实 |
| `test_s1027_never_bear_between_1315_and_1930` | `[] is not true: corrected H1 warmup produced no facts in the target window` | 缺更长 XAUUSD 原始历史 |
| `test_s1030_is_bear_throughout_0145_to_1145` | `[] is not true: corrected H1 warmup produced no facts in the target window` | 缺更长 XAUUSD 原始历史 |

三个 xfailed 均记为 **BLOCKED**，没有计入 PASS。`expectedFailure` 仍可能掩盖未来不同断言，
长历史到位后必须改为明确前置检查和普通断言。

## 5. 订单与重启路径复核

真实执行路径为：`on_bar` → `_process_m15` / `_handle_m1_exit` → `_submit_open` /
`_submit_close` → GM `order_volume`；订单状态由 `on_order_status` 更新。

| 项目 | 状态 | 证据或缺口 |
|---|---|---|
| 成交价越过计划止损 | PASS | 立即以 `FILL_BEYOND_STOP` 提交平仓，测试到最终 `order_volume` |
| 部分成交后撤单 | PASS | 已成交数量转为本地持仓并立即提交 `TREND_EXIT` |
| 零成交拒单/撤单 | FAIL（证据缺口） | 实现会移除本地记录，但无专门回归 |
| 重复订单回调 | FAIL（证据缺口） | 无明确幂等验收 |
| 未知订单状态 | FAIL（策略缺口） | 有 `ORDER_STATUS` 日志，但没有明确的冻结、告警或终态策略 |
| 趋势状态重启恢复 | 有限支持 | 仅 `signal_only` 且 `persist_state=true` 时恢复 `StrategyRuntimeBook` |
| 持仓/未完成订单重启恢复 | FAIL | `ftq_trades` 启动时为空，未从经纪商事实重建 |

由于配置只允许 GM `MODE_BACKTEST` 下单，上述缺口当前不会授权到模拟盘或实盘；它们仍是未来
执行验收的明确阻塞，不得用安全门关闭来宣称已经解决。

## 6. Gate 与安全判定

- Gate 2：**FAIL**。当前 HEAD 的基础工程测试可复现，但订单异常和重启恢复验收不完整。
- Gate 1：本工作包未评审，仍待 P0-B 修订标签与事件定义。
- Gate 3：沿用旧口径的三项趋势指标失败，且旧口径将在 P0-B 降级为历史研究诊断。
- Gate 4：真实费率和长历史缺失，**BLOCKED**。
- `execute_enabled=false`；未开启模拟盘或实盘下单。
- 未使用未核验费率运行或解释收益回测。

机器可读明细：`reports/p0a_current_head_acceptance.json`。
