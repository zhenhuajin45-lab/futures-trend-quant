# 国内期货趋势策略（规格重构版）

本项目把附件中的 XAUUSD 1.0 规则重构为国内期货确定性策略。由于原始
`m5m1-combo-v1` 与 `structure-facts` 源码缺失，本项目不声称与原系统逐字节或
逐轮等价；所有差异通过配置、审计和回归用例显式管理。

当前阶段：

- T0：品种配置、主力合约解析、掘金数据探针。
- T1：指标、K 线算术、波段、背驰、H1 状态的纯函数核心。
- T2：趋势状态机、两根 M15 确认、状态持久化。
- T3：Path D 入场、结构/ATR 止损、固定 2R、1% 风险仓位和组合门控。
- T4：掘金回测订单与成交回报闭环。仅 `MODE_BACKTEST` 可下单；
  `execute_enabled` 仍必须为 `false`。

## 品种池

当前配置来自 2026-11-03 用户附件：生猪、豆粕、棉花、碳酸锂、纯碱、
鸡蛋、PVC、乙二醇、沪金、沪银。修改品种只编辑
`config/strategy.json`，不要改策略代码。

## 本地验证

```powershell
python -m json.tool .\config\strategy.json > $null
python -m unittest discover -s .\tests -p "test_*.py" -v
python -m compileall -q .\futures_trend_quant .\deploy .\tests
```

## 掘金数据探针

已在掘金“量化研究 → 我的策略”中创建
`期货趋势重构_v1_只读数据探针`。客户端生成的本地 `main.py` 只负责把回调转发到
`futures_trend_quant/live/gm_strategy.py`；脱敏模板见
`deploy/gm_main.example.py`。当前入口解析主力、订阅行情、生成事实与趋势审计，
并在掘金 `MODE_BACKTEST` 中调用 `order_volume` 完成开平仓。模拟盘和实盘门控保持关闭。

模拟账户由掘金交易界面绑定；账户 ID 不进入版本化配置或日志。

## 安全边界

- 配置支持 `backtest` 与 `signal_only`；只有平台 `MODE_BACKTEST`、
  `backtest_orders_enabled=true` 且 `execute_enabled=false` 同时满足才可发单。
- 掘金生成的 token 只保留在客户端本地策略目录，绝不提交 Git。
- 1% 是下单前风险预算；仓位额外计入 10 bps/至少 3 tick 的成交与跳空缓冲。
  极端跳空仍可能使实际损失超过预算。
- 当前实测覆盖主力映射、三频事实、状态机、订单、成交回报和 M1 止损/止盈，
  只证明工程链可回测，不代表策略收益有效。

## 已验证环境

- Python SDK：`gm 3.0.186`
- C SDK：`3.8.19`
- 回测窗口：`2026-10-20 08:00:00` 至 `2026-10-23 15:01:00`
- 结果：10 品种主力映射成功，852 次 `M15_FACTS`，11 次开仓、6 次平仓，
  无 `GM_ERROR` / `M15_FACTS_REJECT` / `EXIT_REJECT`。
- 该短样本收益为负，且结束时仍有持仓；不能用于评价参数或宣称策略有效。

详细落地与回测证据见 `docs/03_T3T4回测闭环与验证报告.md`。
脱敏后的结构化回测摘要和已平仓明细位于 `reports/`。
