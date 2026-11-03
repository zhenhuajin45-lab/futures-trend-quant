# 换月时钟与连续价格 V3

版本 `ASOF_SYNCHRONOUS_V1`。原 180 日连续序列及其行情哈希继续使用既有版本名。两者并列的脱敏结果见 [25 次换月审计](../reports/v3_asof_roll_audit.json)。

### 输入与输出

`backtest.asof_roll.build_asof_minutes(csymbol, mapping, quotes)` 接收单一品种的两张 DataFrame，返回每分钟的决策输入及已经生效的换月事件。时间字段写入带时区的时间戳，程序统一按 UTC+08:00 比较。研究方须保留映射发布和两腿接收的真实记录。

| 表 | 字段 | 约定 |
|---|---|---|
| mapping | trading_date, symbol, known_at | 每日适用交易日、实际合约、发布记录首次可见时间。同一交易日可有多次发布，按 known_at 生效。同品种每天均需覆盖 |
| quotes | symbol, trading_date, bob, eob, available_at, open, high, low, close | 实际合约已收盘 M1，两腿各自保留 eob 和源记录可见时间。每合约每分钟仅一条不可变记录 |
| quotes | volume, amount, position | 传递至已选合约行 |
| quotes | decision_at（可选） | 逐分钟独立决策时钟的真实记录。同一分钟所有腿必须一致，满足 `eob <= decision_at < eob + 1min`。提供该列时所有行必填，不可混用两种时钟 |
| 输出 | symbol, actual_contract, mapping_known_at, eligible, reason, bob, eob | symbol 为连续品种标识。actual_contract 取截至当根开始时已经发布的合约 |
| 输出 | open/high/low/close、raw_open/raw_high/raw_low/raw_close、cumulative_offset | 有资格时输出复权价格、对应实际合约价格和已生效累计偏移 |
| 输出 | decision_at | 实际用于检验当根行情首次到达时间的截止时刻。整表无此列时沿用名义 eob |
| 换月事件（本地） | trading_date, old_contract, new_contract, mapping_known_at, old_eob, new_eob, old_available_at, new_available_at, old_raw_close, new_raw_close, delta, cumulative_offset, effective_bob, effective_eob, cross_night | 发生切换时记录。公开审计去掉两腿原始报价；cross_night 表示观测到使用期间跨日历日期或使用夜盘归属次交易日 |

### 已知信息如何生效

1. 每根 bar 以 bob 时可见的当日映射选合约。只有 `available_at <= decision_at` 的活跃报价才可进入该次决策。采集方须独立记录每分钟的决策时刻，不可从已收到的价格或两腿到达时刻反推。缺少独立决策钟列时沿用名义 eob，带毫秒延迟的行情会在该模式被拒绝。前缀实验比较 `decision_at <= 截点` 的历史行，追加读取端只使用截至截点已经发生的决策钟、已接收的报价及已发布的映射。已到达但尚未到决策时刻的分钟保留在缓冲区。有至少一条及时报价才从行情生成分钟钟点，采集方另用预登记的交易时段分钟表检出整分钟缺失。
2. 对固定的 12 个国内商品期货品种，预知新映射且上一个交易日最后日盘收盘（北京时间 15:00）有已到达的同步两腿，使用该共同收盘观测。其余场景从新交易日开始和映射发布时刻的较晚者起，等待首个两腿齐备的同步观测，同一可见时刻按较早 eob 优先。两腿盘中价差算 `new.close - old.close`，累计偏移减去该差。仅从 bob 已达到两腿可见时间的下一根有资格 M1 起应用。
3. 处于 `MAPPING_UNKNOWN`、`WAIT_SYNCHRONOUS_PAIR`、`ACTIVE_QUOTE_MISSING_OR_LATE` 的行保留身份和原因，禁止将缺失价插值后接到原尺度。前向评分入口逐品种对照交易时段分钟表和 eligible，任何缺口隔离整份研究输入。
4. 标签与策略读同一份经检验的连续序列。独立检查事件确认当根、未来 48 根标签窗、极值至确认、极值处 ATR 来源和策略前 1,200 根 M15 预热。机会后 8 根价格观察以实际合约价格记录。对整窗缺时钟的数据，这五类严格有效样本数均为 0。

### 历史核验与复现

180 日授权缓存覆盖 1,452 个品种日、488,009 根旧版主力 M1。映射只有交易日期和实际合约，实际合约行情只有 bar 时点。25 次换月均缺历史发布/到达时间，严格时点资格为 `DATA_BLOCKED`。

工程试验对真实两腿价格赋予明确的假定时钟（交易日第一条观测的 bob 获知当日映射、报价 eob 即可见）。它只用于检验生成器的等待、累计偏移和因果性质。487,984 根旧版分钟有工程版对应行，25 根等待双腿观测；275,061 根可比收盘的绝对差异超过 1e-8。这些数字不能用于策略晋级。

三类真实样例为棉花 08-14 夜盘、红枣 08-11 纯日盘及乙二醇 08-24 换月当日部分分钟缺一腿。前缀检验改变交易日尾段的合约收盘及映射发布，分别用完整文件、变更后的完整文件、只追加到截点的文件重放。检验完整报价行及 S0 的趋势输入、决策、状态和技术候选逐行一致。缺价分钟只在此工程检查中跳过，未赋予其后策略状态独立样本资格。合成测试另覆盖正负差价、延迟抵达，以及本段报价始终不齐备。

```powershell
python -m pytest -q tests/test_asof_roll_v3.py
python -m backtest.asof_roll_audit --data-root 'E:\掘金\期货\data\cache\gm_rolling_180d' --report reports/v3_asof_roll_audit.json
```
