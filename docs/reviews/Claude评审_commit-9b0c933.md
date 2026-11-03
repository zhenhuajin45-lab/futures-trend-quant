# futures-trend-quant（commit 9b0c933）评审与下一步迭代方案

> 评审：Claude｜日期：2026-11-03｜对象：`zhenhuajin45-lab/futures-trend-quant` main @ `9b0c933`
> 依据：仓库全部源码（2,828 行 Python）、3 份 docs、回测摘要与平仓明细、本地复跑 18 个非 GM 依赖单测（全部通过；3 个 GM SDK 契约测试因本机无 `gm` 模块未跑）
> 对照基线：《1.0策略量化复刻_评估与Codex落地方案_v1》（下称"方案 v1"）与 1.0 提示词 v4.6 / v62-v63 总包

---

## 0. 总评

**工程骨架合格，安全边界做得好，但这版是"规格重构的子集"，还不是可验证的策略。** 三个结论：

1. **规则保真度不够。** 趋势层有 2 处方向性 bug（P 条件不分方向、H1 背驰恒为 none）和 5 处明显简化（背驰有效期改成 6 根、S1 丢掉"新高后"上下文、S2 只看最新一根吞没、路径二丢掉形态要求、锚点改成 20 根极值）；入场层缺 M15 动能闸门和冷却；趋势结束不平仓。按现状跑出的任何结果都不能用来评价 1.0 规则。
2. **验证能力缺位。** 没有离线回测台，唯一的回测路径是掘金 `MODE_BACKTEST` 逐 bar 回调，4 天跑一次。方案 v1 §5 预注册的 2023–2026 开发/验证集、每开关一次的消融矩阵，在当前架构下**做不了**。这是最大的结构性缺口。
3. **事实层不稳定。** 320 根滑窗重算全部枢轴 / 背驰 / H1 → 事实随窗口左端漂移；H1 聚合丢掉每日 10:00、14:30、14:45（及 AU/AG 的 02:00、02:15）共约 20% 的日盘柱；H1 EMA34 只用 ~75 根 H1 做 SMA 种子，未收敛。这三点叠加，H1 strong/flat 判定和背驰候选在实盘与任何长历史复算之间**不会一致**，T4 "逐根一致性"验收不可能通过。

回测结论（4 天、6 笔、−0.5R、5 笔未平）Codex 自己已正确定性为"不能评价策略"。我补一条：**58 次组合 5 仓上限拦截**说明当前引擎过度活跃——10 个品种在同一波商品普跌里同时出 Path D 空单，没有闸门、没有冷却、没有簇内相关性约束以外的任何节流。这不是样本问题，是规则缺口。

**做得好的**（应保留）：纯函数 core 与 GM 适配分离、`defined_at` 语义、fail-closed 配置校验（risk_pct ≤1%、execute_enabled 恒 false、四重下单门控）、原子化状态持久化、订单状态机含部分成交/撤单、脱敏日志、文档如实说明"不是 1:1"。

---

## 1. 问题清单（按优先级）

### P0 — 影响正确性，必须先修

| # | 位置 | 现象 | 影响 | 修法 |
|---|---|---|---|---|
| P0-1 | `pipeline.py:196-197` | `p_close_cross` / `p_macd_cross` 对任意方向的穿越都为 True | 多头趋势中价格**上穿**均线也满足 P → 路径一误触发；P1 原义是"收盘在均线另一侧"的**状态**而非事件 | 改为按趋势方向：bull 结束要 `close_side==below` 或 `macd_cross==down`（且在 N 之后） |
| P0-2 | `pipeline.py:201` | `h1_divergence="none"` 硬编码 | H1 走弱 + 背驰 → flip 这一支永远不触发；H1 `weakening` 中"已确认背驰"分支也不触发 | 对聚合 H1 跑同一套 swing/divergence，输出 none/pending/confirmed |
| P0-3 | `pipeline.py:181-183` | 背驰有效期 = `defined_at >= index-6` | 4.7 原义"持续有效直到再创新极值"被改成 6 根 TTL；顶部判出背驰、6 根后才跌破均线的典型情形（提示词 4.7 专门举例）被丢掉 | 有效期改为 `price 未越过 extreme_price`；可选 GDL 式 12 根 TTL 作消融开关 |
| P0-4 | `pipeline.py:98-103` | `_latest_divergence` 从最近枢轴向前找"有背驰的那一对"，可能返回比当前更低的旧高点的背驰 | 价格已创更高高点，旧背驰应失效却仍被引用 | 只取最近一个已确认同类枢轴；它没配对或无背驰就是"无背驰" |
| P0-5 | `pipeline.py:29-60` | H1 按墙钟分桶，`len(bucket)!=4` 的桶直接丢弃 | 每日 10:00、14:30、14:45 三根（日盘 20%）与 AU/AG 02:00、02:15 从 H1 消失；H1 收盘价不含收盘段 | 改为**按交易日内 M15 序号每 4 根一组，余数并入最后一组**（或允许 2–5 根桶）；单测补"全天 M15 根数 = ΣH1 覆盖根数" |
| P0-6 | `gm_strategy.py:149,335-337` + `pipeline.py:122` | 所有事实在 320 根滑窗上全量重算；H1 EMA34 仅 ~75 根 H1 历史 | 事实随窗口漂移；H1 EMA 未收敛（EMA34 需 ≥100 根才摆脱种子影响） | M15 窗口 ≥ 1,200 根（或 H1 单独维护增量 EMA）；枢轴/背驰改**增量确认 + 持久化**（一经 `defined_at` 不再被窗口左移改写） |
| P0-7 | `gm_strategy.py`（无实现） | 趋势切换确认后**不平原方向持仓** | 1.0 规则："趋势结束 → 平台全平"；现在只能等止损/止盈 | 在 `_process_m15` 中，`decision.confirmed and confirmed_state != 持仓方向` → `_submit_close(reason="TREND_EXIT")` |
| P0-8 | `entry.py`（无实现） | 缺 **M15 动能闸门**（金叉状态 + 不追：3 根缩短且偏离 > 2.3 ATR）和 **4 根 M15 同向冷却** | Path D 在任何 M5 穿越 / 吞没都进场；58 次仓位上限拦截即此 | 补 `gate.py`（纯函数）与 `cooldown` 状态；都带开关 |

### P1 — 规则偏离，影响可比性

| # | 位置 | 现象 | 修法 |
|---|---|---|---|
| P1-1 | `pipeline.py:193` | S1 = 当前斜率 flat，丢掉"最近 12 根内创本轮趋势新高（`trend_extreme`）之后才走平" | 用 `state.trend_extreme` 与 12 根窗口实现 4.4 |
| P1-2 | `pipeline.py:194-195` | S2 只检最新一根吞没；无 pinbar+破位；无"距段极值 ≤1 ATR"位置要求 | 按 4.8 实现 pinbar+破位；形态在证据窗口内搜索并校验位置 |
| P1-3 | `pipeline.py:198-199` | 路径二 R = 收盘反侧 ∧ 斜率**完全反转**；原义是 斜率**不再同向**(flat 亦可) ∧ **极值附近反向形态** | 按 4.3.2 三项合取 |
| P1-4 | `state_machine.py:72` | `strong_break_atr_met` 永远 False → 极强例外 4.6 永不触发；`unblock_allowed` 同理 | pipeline 计算 E4（破均线 ≥0.5 ATR）与解禁条件 |
| P1-5 | `gm_strategy.py:384` | 锚点 = 最近 20 根 M15 极值，非 N 字第一段起点 | 用 swing 枢轴：最近一个已确认反向枢轴作为锚点（这与 3.1 的 pivot 列表天然一致）；20 根极值保留为 `anchor_mode=range20` 消融 |
| P1-6 | `divergence.py:60-78` | B_hist 不检查前端 hist/DIF 正负侧（顶背驰要求前高处 hist>0） | 加符号门槛，避免"高点出现在 MACD 零轴下方"时的伪背驰 |
| P1-7 | `pipeline.py` | "将要背驰"（pending）未实现 | 创新极值 + 柱峰未新高 + 柱连续 2 根缩短 |
| P1-8 | `entry.py:21-40` | Path D 的 M5 信号只有穿均线 / 吞没，缺 M5 背驰；吞没无位置要求 | 复用 M5 上的 divergence 算子；吞没加 1 ATR 位置校验 |
| P1-9 | `gm_strategy.py:342` + `config` | `ftq_states` 按**实际合约**键控：换月即趋势归零；`continuous_adjustment: self_back_adjust` 配置项**未实现** | 状态按 `csymbol` 键控；事实计算改在复权连续序列上（T3a 回测台先做，实盘后接） |
| P1-10 | `reports/*summary.json` | 费用 `commission_ratio=1e-4`、滑点 `1e-4` 全品种统一，无平今 | 按品种费率表（含平今）与 tick 滑点 |

### P2 — 工程与可观测性

- `gm_strategy.py:_process_m15` 中 `analyze_market` 异常被 `except Exception` 吞成一条日志后 `return`——事实不可用时应同时**冻结该品种开仓**并计数告警，不能静默跳轮。
- `_entry_guard` 每个信号调用 `get_position()`；回测无妨，实盘要节流。
- 回测用 `on_bar(60s)` 的 high/low 判止损后再市价平仓——隔一根 M1 成交，滑点被低估；回测台里应在触发 bar 内按止损价±1 tick 成交。
- 日志 `SIGNAL` 每根 M15 每品种一条含完整 dict，长期运行需降噪（T0 报告已识别）。
- 没有 `dataset.json` 同源一致性测试、没有 XAUUSD 锚点回归（方案 v1 T1/T2 验收项），当前 32 个单测全部是合成数据。

---

## 2. 对回测结果的解读

| 项 | 数值 | 解读 |
|---|---|---|
| 6 笔已平：2 胜 4 负，−50.8k | ≈ −0.5R（每笔风险预算 10 万 = 1% × 1,000 万） | 单笔盈亏与 1R / 1.8R 吻合，说明止损止盈和手数**算术正确** |
| 11 开 / 58 拦截 | 4 天内 10 品种触发 69 次候选 | 引擎过度活跃（P0-8） |
| 5 笔未平全为空头 | JD/LH/M/LC/AG | 同向暴露集中，簇限制（livestock 2、oilseed 1…）没拦住跨簇同向 | 
| 手数 236（M）、121（JD） | 市价单 | 1,000 万账户下这些手数在主力合约流动性内，但 LC 21 手、LH 27 手已接近盘口一档深度，回测滑点 1e-4 过于乐观 |

**结论：这组数字只证明"工程链能跑通、算术正确"，对策略有效性零信息。**不要据此调任何参数。

---

## 3. 下一步迭代（三个 Sprint）

### Sprint 1：保真 + 离线回测台（约 1 周，Codex 两个并行任务）

**任务 A：事实与状态机保真修复**（P0-1~5、P0-7、P1-1~8）

- 每项一个独立 commit，commit message 引用本文编号。
- 新增测试：
  - `tests/test_dataset_consistency.py`：用 `dataset.json` 8 段 1,570 根逐根比对 EMA34 / DIF / DEA / HIST，容差 1e-6（数值口径对齐）；
  - `tests/test_h1_aggregation_coverage.py`：任一交易日 ΣH1 覆盖根数 = 当日 M15 根数；
  - `tests/test_divergence_validity.py`：背驰在未创新极值前持续有效、创新极值后失效；
  - `tests/test_trend_anchors_xauusd.py`：方案 v1 T2 的三个锚点（10/26 转多应早于 19:30；10/27 13:15–19:30 不判空；10/30 01:45–11:45 全程空头）——用 `dataset.json` 的 c1-front / s1027 / s1030 段，报告 pass/fail，**允许 fail 但必须记录**。

**任务 B：离线回测台 `backtest/`**

- `data/gm_export.py`：用掘金 `history()` 一次性导出 10 品种 2023-01 起 M1（分批、落 parquet）；`fut_get_continuous_contracts` 导出每日主力映射；换月日差价复权生成连续序列（`continuous_adjustment` 真正落地）。
- `backtest/runner.py`：bar-by-bar 事件驱动，M1 推进 → 聚合 M5/M15 → 在 M15 收盘调用**与 `gm_strategy._process_m15` 完全相同的 core 函数链**；止损止盈在 M1 内按触发价 ±1 tick 成交；费用表 `config/fees.json`（含平今）；换月策略 `rollover_policy`。
- `backtest/metrics.py`：方案 v1 §5.2 趋势层指标（事后参考 = 未来 48 根 3 ATR ZigZag）、§5.3 交易层指标、`blocked_at` 分布、按品种 / 路径 / 月分列。
- `backtest/ablation.py`：对配置中每个布尔 / 口径开关单独翻转一次，输出矩阵。
- 验收：同一段数据（如 2026-10-20~23）离线回测与掘金回测的 **M15_FACTS 逐根一致**（close/ema34/atr14/slope/h1_absolute/top_div/bottom_div）；不一致项必须归因（预期来源：P0-6 的窗口差异，修复后应消失）。

**Sprint 1 结束时状态**：规则与 1.0 文本一致（差异仅剩 §4 待定项）；能在本地几分钟内跑完 10 品种 × 2 年。

### Sprint 2：开发集验证 + 消融（约 1 周）

- 冻结 `docs/prereg.md`（方案 v1 §5 门槛原文），**先提交再跑**。
- 开发集 2023-01~2024-12 全池：趋势层四指标 → 不达标先修趋势层，不看收益。
- 消融矩阵：`div_mode`、`swing_mode`、`h1_veto_enabled`、`h1_strong_atr∈{0.4,0.6,0.8}`、`stop_max_atr∈{6,11.5}`、`m1_confirmation_enabled`、`gate_enabled`、`cooldown_bars∈{0,4}`、`pm_mode`、`anchor_mode`。
- 品种池复审：按单手风险表 + 成本比 + 趋势性重排；LH / JD / LC 无夜盘，隔夜跳空对 EMA 穿越逻辑的影响在消融里单列。
- 产出：`docs/04_开发集验证报告.md`，每条结论标"确定 / 很可能 / 待验证"。

### Sprint 3：路径 A/B/C + 模拟盘信号模式（视 Sprint 2 结果）

- **前提**：Tony 把 AURUM 的 `m5m1-combo-v1`（engine.js / levels.js / params.js）、`structure-facts`、31 个黄金用例给 Codex 读权限，否则 A/B/C 继续按"重构版"标注。
- 模拟盘只读信号 ≥5 个交易日 + 飞书推送；每日对账：实盘 `M15_FACTS` vs 离线回放逐根一致。
- 下单开关的审议另起文档，不在本轮。

---

## 4. 需要 Tony 决定

| # | 事项 | 建议 |
|---|---|---|
| 1 | 是否接受 Sprint 1 把规则**拉回 1.0 原文**（上述 P0/P1 全修），还是认可 Codex 的部分简化作为新口径 | 建议全修；简化项若要保留，改成**开关 + 消融**，不能是默认且唯一实现 |
| 2 | AURUM 原始引擎源码能否给 Codex | 能给则 Sprint 3 做 A/B/C 1:1；不能给则 A/B/C 按方案 v1 §1.3 文本重构并永久标注 |
| 3 | 回测账户 1,000 万是否就是目标模拟盘 / 实盘规模 | 若实际更小，M / JD 的手数会掉到个位数，品种池结论会变；需定下来再跑 Sprint 2 |
| 4 | 品种池：是否坚持生猪 / 鸡蛋 / 碳酸锂（无夜盘、LC 单手价值大） | 第一轮保留并在消融中单列；Sprint 2 后按数据决定 |
| 5 | 116 条背驰签认 | 仍是 `div_mode` 定稿的前提；Sprint 2 消融可先出两套口径的对比数字辅助签认 |

---

## 5. 给 Codex 的交接说明（可直接粘贴）

> 评审文档：本文。工作顺序：Sprint 1 任务 A 与任务 B 并行；A 的每个 P0/P1 编号一个 commit；B 的验收以"离线与掘金 M15_FACTS 逐根一致"为唯一通过标准。不要在修复保真度之前调任何阈值。不要用 2026-10-20~23 的结果做任何参数选择。所有新增规则必须带配置开关并进入 `ablation.py`。完成后提交 `docs/04_Sprint1_报告.md`，含 pass/fail 表与未修项清单。