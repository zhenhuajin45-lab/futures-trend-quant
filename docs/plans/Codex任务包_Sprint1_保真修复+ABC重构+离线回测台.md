# Codex 任务包 · Sprint 1：保真修复 + 路径 A/B/C 重构 + 离线回测台

> 基线：`futures-trend-quant` main @ `9b0c933`
> 依据：《评审_futures-trend-quant_9b0c933_与下一步迭代》（下称"评审"）、1.0 提示词 v4.6、v62/v63 总包
> Tony 决定（2026-11-03）：① 不使用 AURUM 原始源码，A/B/C 按核心思路重构，本文给出全部缺失定义；② **目标账户规模 100 万元**（回测里的 1,000 万作废），risk_pct=1% → 单笔风险预算 1 万元；掘金回测 `initial_cash` 改为 1,000,000。
> 规则：每个编号一个 commit，message 引用编号；所有新增规则必须带配置开关并进入消融矩阵；在保真修复完成前不调任何阈值；不得用 2026-10-20~23 的结果做参数选择。

---

## 任务 A：事实与状态机保真（评审 P0-1~7、P1-1~9）

按评审清单逐项修复。补充几条实现约束：

### A-1 枢轴与背驰的增量确认（P0-6）

- 枢轴一经 `defined_at` 确认即写入每品种的 `facts_state`（持久化），后续窗口左移**不得**改写已确认枢轴；交替过滤与 S-A 腿长过滤只对新枢轴生效。
- 背驰对象 `(prev, cur)` 与质量一经确认同样持久化；有效期 = `price 未越过 cur.price`；新同类枢轴确认时重新配对。
- M15 窗口 ≥ 1,200 根（`subscription_count` 调高或 `history_n` 补足）；H1 从持久化 M15 序列聚合，不从 320 根滑窗。
- 单测：同一历史用 320 / 1,200 / 全量三种窗口回放，`defined_at ≤ 当前` 的枢轴与背驰集合必须一致。

### A-2 H1 聚合（P0-5）

- 每个**交易日**（含其夜盘，夜盘归下一交易日）内按 M15 出现顺序每 4 根一组，最后不足 4 根并入前一组；不按墙钟。
- 单测：任一交易日 ΣH1 覆盖根数 == 当日 M15 根数。

### A-3 趋势层逐项对齐 v4.6（P0-1~4、P1-1~4、P1-7）

| 事实 | 定义（bull 结束为例，bear 镜像） |
|---|---|
| N | M15 顶背驰已确认且有效 |
| S1 | 最近 12 根 M15 内 `high ≥ state.trend_extreme`（创本轮新高）之后，斜率由 up 转 flat |
| S2 | 证据窗口（最近 6 根 M15）内出现看跌吞没或上影 pinbar+下破，且该形态 K 线距本段高点 ≤ 1 ATR(M15) |
| P1 | 最新收盘 `close_side == below`（状态，非事件） |
| P2 | N 确认之后出现 `macd_cross == down` |
| R | `close_side == below` ∧ 斜率 ∈ {flat, down} ∧ S2 |
| E4 | `ema34 − close ≥ 0.5 × ATR(M15)` |
| pending 背驰 | 创新极值（相对 prev 枢轴）∧ 当前同色段 hist 峰 < 前段峰 ∧ hist 连续 2 根缩短 |
| H1 背驰 | 对聚合 H1 跑同一套 swing + divergence，输出 none / pending / confirmed |
| 解禁 | `blocked_dir` 非空时，以原趋势为基准 H1 ∈ {flat, reverse} 或 weakening+反向背驰 → 解禁 |

### A-4 趋势结束平仓（P0-7）

`decision.confirmed` 且新状态 ≠ 持仓方向 → 对该品种全部持仓 `_submit_close(reason="TREND_EXIT")`，同时撤销未成交开仓。

---

## 任务 B：入场引擎重构（A/B/C/D 全部路径 + 闸门 + 冷却）

> 原则：把 1.0 文本中的每个名词落成一个纯函数，名词未定义处采用下表定义并标注 `[重构定义]`，全部进配置与消融。

### B-1 M15 动能闸门 `core/entry/gate.py`（P0-8）

```
方向要求: bull 须 DIF > DEA 且 hist > 0（bear 相反）；反向交叉即关闭，重新交叉即打开
不追:    顺势 |hist| 连续 3 根缩短 ∧ |close − EMA34| > gate_distance_atr × ATR14(M15)  → 关闭；|hist| 重新放大即打开
参数:    gate_enabled=true, gate_distance_atr=2.3
```

### B-2 M5 / M1 证据算子 `core/entry/evidence.py`

全部只用已收盘 K 线；M5 指标用 M5 自身的 EMA34 / ATR14 / MACD；窗口：M5 最近 6 根、M1 最近 15 根。

| 名词 | 定义 | 标注 |
|---|---|---|
| M5 背驰 | 对 M5 跑 swing(S-A, k=2, leg 1.2 ATR_M5) + divergence(`div_mode`) ，取最近有效者 | 沿用 1.0 "structure-facts 口径" |
| M5 均线走平 | 最新 M5 的斜率比值 ∈ (−0.35, 0.35)，且前 6 根内曾为反向（bull 入场时曾 down） | [重构定义] |
| 高低点附近形态 | 吞没或 pinbar+破位，形态 K 线距窗口极值（6 根内）≤ 1 ATR_M5 | 1.0 原文 |
| M5 回调到均线 | 6 根内任一 M5 `|close − EMA34_M5| ≤ 0.5 ATR_M5` | 1.0 原文 |
| M5 横盘 | 最近 ≥4 根 M5 收盘全部在 EMA34_M5 顺势一侧，且这些根 `max(high) − min(low) ≤ 1.5 ATR_M5` | 1.0 原文 |
| 横盘突破 | 横盘之后一根 M5 收盘越过横盘区间顺势边界 | [重构定义] |
| M5 穿均线 | `close_ema34_cross` 顺势方向 | 1.0 原文（路径 D） |
| M1 信号 | M5 触发之后 15 根 M1 内任一：顺势 `close_ema34_cross`、顺势吞没、顺势 MACD 交叉 | [重构定义]，默认 `m1_confirmation_enabled=false` |
| M1 明确反向强势 | M1 斜率反向 ∧ 收盘在 EMA34 反侧 ∧ hist 反向 | 沿用现有实现 |

### B-3 四条路径 `core/entry/paths.py`（bull 入场为例）

| 路径 | 前提 | M5 条件 | M1 | 适用 |
|---|---|---|---|---|
| A 反转起点 | 已确认 bull | {M5 底背驰, M5 走平, 低点附近看涨形态} **三选二** | 可选 | 趋势初期 |
| B 回调均线 | 已确认 bull；H1 非反向 strong（`path_b_h1_exception` 关闭） | 回调到均线 ∧ 低点附近看涨形态 ∧ 无 M5 顶背驰 | 可选 | 趋势中回调 |
| C 横盘突破 | 已确认 bull；该品种无同向持仓 | 均线上方横盘 ∧ (突破 ∨ 看涨形态) | 可选 | 整理 |
| D 强趋势顺势 | H1 bull_strong ∧ M15 斜率 up ∧ 无 M15 顶背驰 ∧ 闸门开 | 6 根内 {M5 底背驰, 上穿均线, 看涨吞没} 任一 | 不得明确反向强势 | 单边 |

- 多条同时成立：取**触发 K 线更靠后**者；记录 `path` 与 `trigger_bar_id`。
- 证据只用一次：`evidence_id = path|direction|trigger_bar_id`。
- 冷却：同品种同向成交后 `cooldown_bars=4`（M15）内不开新仓；路径 D 例外：与上一笔同向成交价差 > `path_d_cooldown_atr=1.5` ATR 免冷却。
- 路径 D 允许同向加仓（≤ `path_d_max_stack=2`），其余路径同向持仓不加仓。
- 每条路径独立开关：`path_a_enabled` … `path_d_enabled`。

### B-4 锚点与止损 `core/entry/anchor.py`（P1-5）

```
anchor_mode = "swing"（默认） | "range20"（现状，作消融）
swing: bull 锚点 = 最近一个已确认 M15 低枢轴（S-A）；该枢轴之后出现的高枢轴为 A 线；
       收盘突破 A 线后，锚点跳到 A 线之后形成的新低枢轴；回调中不改近
止损 = 锚点 ∓ stop_buffer_atr×ATR（≥2 tick）；不足 stop_min_atr 放宽；超过 stop_max_atr 放弃（不改用更近锚点）
止盈 = 2R；pm_mode = fixed | be_at_1r
```

---

## 任务 C：离线回测台 `backtest/`

- `data/gm_export.py`：掘金 `history()` 分批导出 10 品种 **2023-01-01 起** M1 → parquet；`fut_get_continuous_contracts` 导出每日主力；换月日用新旧合约同一 M15 收盘差做差价复权，生成连续 M1 序列（`continuous_adjustment` 真正落地，`gm_strategy` 的 `self_back_adjust` 配置项在此实现或删除）。
- `backtest/runner.py`：M1 推进 → 聚合 M5 / M15 / H1 → M15 收盘调用**与 `gm_strategy._process_m15` 相同的 core 函数链**（抽成 `core/pipeline/step.py` 供两端共用）；止损止盈在 M1 内按触发价 ±1 tick 成交；趋势结束平仓；费用表 `config/fees.json`（开仓 / 平仓 / 平今，按品种）；滑点 `slippage_ticks`（默认 1，LC/LH 2）；换月 `rollover_policy ∈ {hold, roll_at_close}`。
- `backtest/metrics.py`：评审 §3 / 方案 v1 §5 的趋势层四指标（事后参考 = 未来 48 根 3 ATR ZigZag）、交易层指标、`blocked_at` 分布、按品种 / 路径 / 月分列。
- `backtest/ablation.py`：对 `rules` 中每个布尔与枚举项单独翻转，输出矩阵 CSV。
- **验收（唯一通过标准）**：2026-10-20~23 同段数据，离线 `M15_FACTS` 与掘金回测 `M15_FACTS` 逐根一致（close / ema34 / atr14 / slope_state / h1_absolute / top_div / bottom_div / trend_decision）。不一致必须归因；修完任务 A 后预期为零。

---

## 任务 D：100 万账户下的品种池矩阵（Tony 决策输入）

`backtest/sizing_table.py`，用近 1 年数据，账户 **100 万**、risk_pct=1%（预算 1 万元/笔），另附 50 万 / 200 万两档作敏感性，输出：

| 品种 | M15 ATR 中位数 | 乘数 | 1.5 ATR 单手风险(元) | 止损 1.5 / 3 / 6 ATR 下可开手数 | 成本比 (双边费+2 跳)/(1.5 ATR×乘数) | 日均成交额 | 夜盘 |
|---|---|---|---|---|---|---|---|

规则：
- 1.5 ATR 止损下可开手数 < 2 的品种标 **边际**；3 ATR 下为 0 手的标 **不可交易**；成本比 > 15% 标 **高成本**。
- 预判：沪金（1,000 克/手）1.5 ATR 单手风险约 4,000–6,000 元，是池内最紧的品种，止损超过 ~2.5 ATR 即 0 手；碳酸锂、生猪约 5 手量级；豆粕、纯碱、棉花、PVC、乙二醇在 20 手以上。表出来后由 Tony 决定 AU 去留（备选：保留 AU 但对其单独允许 `risk_pct_override` 或改以 AG 代表贵金属簇）。
- 手数上限：`max_lots_per_trade` 按品种设为日均成交量的 0.1%，防 100 万账户在 LC/LH 上过度集中。

---

## 任务 E：新增测试（全部必须）

| 测试 | 内容 |
|---|---|
| `test_dataset_consistency` | `dataset.json` 8 段逐根比对 EMA34 / DIF / DEA / HIST，容差 1e-6 |
| `test_facts_window_invariance` | 320 / 1,200 / 全量窗口下已确认枢轴与背驰集合一致 |
| `test_h1_coverage` | 任一交易日 ΣH1 根数 == M15 根数 |
| `test_divergence_validity` | 未创新极值持续有效；创新极值失效；pending 定义 |
| `test_trend_direction_semantics` | bull 中上穿均线 / 金叉不满足 P；bear 镜像 |
| `test_trend_anchors_xauusd` | dataset.json c1-front / s1027 / s1030：10/26 转多早于 19:30；10/27 13:15–19:30 不判空；10/30 01:45–11:45 全程空头。允许 fail，必须记录 |
| `test_paths_abcd` | 每条路径正例 / 反例各 ≥2；多路径同时成立取靠后触发；证据只用一次；冷却与路径 D 价差例外 |
| `test_gate` | v62 测试表四例按 ATR 单位重算后复现（10/27 18:45 关闭、13:40 打开、21:50 方向关闭） |
| `test_trend_exit_closes_positions` | 趋势切换确认 → 平仓 + 撤单 |
| `test_sizing_table` | 四档账户手数与标注 |

---

## 交付

1. `docs/04_Sprint1_报告.md`：每个编号的 commit、pass/fail 表、未修项与原因、离线 vs 掘金逐根一致性结果、`[重构定义]` 清单。
2. `reports/sizing_table.csv` + 一页说明。
3. 不改 `execute_enabled`，不接模拟盘。