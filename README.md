# 国内期货趋势策略（规格重构版）

本项目把附件中的 XAUUSD 1.0 规则重构为国内期货确定性策略。由于原始
`m5m1-combo-v1` 与 `structure-facts` 源码缺失，本项目不声称与原系统逐字节或
逐轮等价；所有差异通过配置、审计和回归用例显式管理。

当前阶段：

- T0：品种配置、主力合约解析、掘金数据探针。
- T1：指标、K 线算术、波段、背驰、H1 状态的纯函数核心。
- T2：趋势状态机、两根 M15 确认、状态持久化。
- T3/T4：尚未启用交易。`execute_enabled` 必须为 `false`。

## 品种池

当前配置来自 2026-11-03 用户附件：生猪、豆粕、棉花、碳酸锂、纯碱、
鸡蛋、PVC、乙二醇、沪金、沪银。修改品种只编辑
`config/strategy.json`，不要改策略代码。

## 本地验证

```powershell
python -m json.tool .\config\strategy.json > $null
python -m unittest discover -s .\tests -p "test_*.py" -v
python -m compileall -q .\futures_trend_quant .\tools .\tests
```

## 掘金数据探针

已在掘金“量化研究 → 我的策略”中创建
`期货趋势重构_v1_只读数据探针`。客户端生成的本地 `main.py` 只负责把回调转发到
`futures_trend_quant/live/gm_strategy.py`；脱敏模板见
`deploy/gm_main.example.py`。当前入口只解析主力、订阅行情、生成数据证据和趋势审计，
不导入任何下单函数。

模拟账户由掘金交易界面绑定；账户 ID 不进入版本化配置或日志。

## 安全边界

- 当前只支持 `signal_only`，配置加载器拒绝 `execute_enabled=true`。
- 掘金生成的 token 只保留在客户端本地策略目录，绝不提交 Git。
- 1% 是已确认的单笔风险上限；仓位计算与订单执行尚未实现，因此当前不会发单。
- 当前实测覆盖主力映射、三频闭合 bar 和 M15 指标入口，不代表策略收益有效。

## 已验证环境

- Python SDK：`gm 3.0.186`
- C SDK：`3.8.19`
- 研究窗口：`2026-10-20 08:00:00` 至 `2026-10-20 09:16:00`
- 结果：10 品种主力映射成功，`60s/300s/900s` 数据到达，10 个品种均输出
  `M15_FACTS`，进程退出码为 `0`。
