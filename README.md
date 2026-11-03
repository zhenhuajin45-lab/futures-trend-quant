# 国内期货趋势策略（规格重构版）

本项目把附件中的 XAUUSD 1.0 规则重构为国内期货确定性策略。由于原始
`m5m1-combo-v1` 与 `structure-facts` 源码缺失，本项目不声称与原系统逐字节或
逐轮等价；所有差异通过配置、审计和回归用例显式管理。

当前工程已完成 Sprint 1，并进入 Sprint 2 滚动研究：

- 事实层：修复方向化 P/R、背驰有效期、H1 全覆盖聚合、H1 背驰、S1/S2、
  E4，并用按 `csymbol` 隔离的增量事实账本消除滑窗左端漂移。
- 入场层：按 1.0 文本和任务包中的 `[重构定义]` 实现 Path A/B/C/D、M15
  动能闸门、M5/M1 证据、4 根 M15 冷却、D 路径两层上限和 swing 锚点。
- 执行层：趋势确认切换触发 `TREND_EXIT`；未成交开仓先撤单，部分成交后立即平仓。
- 回测层：GM 与离线回放共享同一个 `run_core_step`；离线台支持 M1 触发 bar
  止损/止盈、平今费用、换月差价复权、指标、消融和逐根事实比对。
- 一致性：2026-10-20～23 的 GM/离线 `M15_FACTS` 为 852/852 行、8 字段零差异。
- 安全边界：仅 `MODE_BACKTEST` 可发单；`execute_enabled` 必须保持 `false`。

Sprint 2 近期180日滚动趋势验证已完成：12品种、488,009根连续 M1、29,534条
完成 warmup 的 M15 事实。基准只通过震荡切换门槛，方向一致率、趋势覆盖和转向延迟
均未通过；5个趋势相关单变量消融也没有合格 case。详细证据见
`docs/06_近期180日趋势层验证报告.md`。事件级审计进一步确认慢转向主要卡在
S1/P/完整离场路径稀缺，而非单一 H1 veto。因此当前保持基准参数和关闭下单，不做收益调参。

2026-11-03 P0-B 已纠正评估命名与事件口径：逐 bar 对象为未来首触方向，不是 ZigZag；
转向延迟改用 `ATR_DIRECTIONAL_CHANGE_V1` 独立确认事件。新口径810个事件、793个完整
观察窗，延迟中位数6根，仍未达到<=4门槛。任务书执行总报告见
`docs/11_Sprint2任务书执行总报告.md`，外部阻塞见 `docs/blockers.md`。

趋势离场四组对照实验已经完成。候选 A 将延迟中位数降到3根、本段4根及时命中提高到
430/793，但方向一致率52.52%、趋势覆盖57.16%仍失败；候选 B 整体退化。按预注册停止
条件，本轮不冻结候选、不在同一180日窗口追加变体。详见
`docs/13_趋势离场对照实验报告.md`。

v3 已修复入场 H1 门控，并为换月映射、两腿报价加入首次可知时钟。A 只保留一次独立复验，离场 B 已归档。前向窗口为冻结后连续 90 个完整交易日，当前处于 `READY_FOR_DATA`。评审从[工程验收与数据缺口](docs/14_v3_工程验收与复验入口.md)和[前向协议及命令](docs/16_独立90日盲测协议.md)进入。

## 品种池

当前配置来自 2026-11-03 用户附件及后续增补：生猪、豆粕、棉花、碳酸锂、纯碱、
鸡蛋、PVC、乙二醇、沪金、沪银、红枣、白糖。修改品种只编辑
`config/strategy.json`，不要改策略代码。

## 本地验证

```powershell
python -m json.tool .\config\strategy.json > $null
python -m pytest -q
python -m compileall -q .\futures_trend_quant .\deploy .\tests
python -m backtest.smoke --output .\reports\synthetic_smoke_summary.json
```

## 掘金数据探针

已在掘金“量化研究 → 我的策略”中创建
`期货趋势重构_v1_只读数据探针`。客户端生成的本地 `main.py` 只负责把回调转发到
`futures_trend_quant/live/gm_strategy.py`；脱敏模板见
`deploy/gm_main.example.py`。当前入口解析主力、订阅行情、生成事实与趋势审计，
并在掘金 `MODE_BACKTEST` 中调用 `order_volume` 完成开平仓。模拟盘和实盘门控保持关闭。
当前基准资金为 100 万元，单笔风险预算为权益的 1%。

模拟账户由掘金交易界面绑定；账户 ID 不进入版本化配置或日志。

## 安全边界

- 配置支持 `backtest` 与 `signal_only`；只有平台 `MODE_BACKTEST`、
  `backtest_orders_enabled=true` 且 `execute_enabled=false` 同时满足才可发单。
- 掘金生成的 token 只保留在客户端本地策略目录，绝不提交 Git。
- 1% 是下单前风险预算；仓位额外计入 10 bps/至少 3 tick 的成交与跳空缓冲。
  极端跳空仍可能使实际损失超过预算。
- `config/fees.json` 的券商手续费仍未核验，字段保持 `null`；真实离线收益回测会
  fail-closed，不能用占位费率生成看似有效的收益报告。
- `reports/synthetic_smoke_summary.json` 只验证事件链，明确禁止作收益解释。
- 当前实测覆盖主力映射、三频事实、状态机、订单、成交回报和 M1 止损/止盈，
  只证明工程链可回测，不代表策略收益有效。

## 已验证环境与旧基线

- Python SDK：`gm 3.0.186`
- C SDK：`3.8.19`
- 下列数字属于旧基线 `9b0c933`，不是 Sprint 1 新规则结果：回测窗口
  `2026-10-20 08:00:00` 至 `2026-10-23 15:01:00`，10 品种主力映射成功，
  852 次 `M15_FACTS`，11 次开仓、6 次平仓，
  无 `GM_ERROR` / `M15_FACTS_REJECT` / `EXIT_REJECT`。
- 该短样本收益为负，且结束时仍有持仓；不能用于评价参数或宣称策略有效。

旧版基线证据见 `docs/03_T3T4回测闭环与验证报告.md`；Sprint 1 的当前结论、
pass/fail 表和残余阻塞见 `docs/04_Sprint1_报告.md`。
脱敏后的结构化回测摘要和已平仓明细位于 `reports/`。
