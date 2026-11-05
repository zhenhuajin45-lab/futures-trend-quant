# 五品种模拟观察与飞书提醒

以用户确认的豆粕、黄金、乙二醇、鸡蛋、红枣为观察池，不含甲醇。独立分支 `codex/gm-paper-five-feishu-v2`，部署目录 `E:\掘金\期货-模拟五品种`。原豆粕目录及证据保留。

策略 core、基础配置、原持仓纪律不变。每笔最多 1 手、同品种不加仓、组合最多 2 仓；单笔风险上限净值 0.25%、保证金占用 20%、日亏 1% 停开、观察回撤 5% 停开。停新开仍监督已知余仓；不自动清除停开标志。黄金一手超过预算时照常拦截。

## 本机配置和入口

按 `config/paper_observation_five.example.json` 创建被忽略的 `config/paper_observation_five.local.json`，绑定精确模拟账户及证明哈希。沿用 `deploy/requirements-paper-trade.lock.txt` 的隔离交易环境；禁止默认账户或实盘端点回退。私有 `config/paper_feishu.local.json` 使用示例结构填写用户指定机器人，不能提交真实地址。若机器人开启签名，将密钥只放本机 `FEISHU_SIGNING_SECRET`。

```powershell
python deploy/gm_paper_five_main.py --config config/paper_observation_five.local.json --preflight --max-seconds 600
```

只读预检不能下单或发送卡片。预热重建和行情调用分别在子进程执行，五个品种都要求独立日历、实际合约和 1,200 根完整 M15。实时使用 GM `current()` 批量查询和各实际合约已闭合 M1 轮询，没有声称获得五路完整 Tick 订阅权限。主力映射变化冻结对应品种新开，保留旧腿管理退出，需受控更新，不能自动将历史状态归零后追开。

账户交接前确认旧控制器身份、无活动或不明订单、柜台与本地累计成交一致，再停止本项目旧控制器。不得停止其他交易进程。新目录首次运行必须有迁移回执：

```powershell
python tools/paper_five_migrate.py --old-config 'E:\掘金\期货-模拟观察\config\paper_observation.local.json' --new-config config/paper_observation_five.local.json
python deploy/gm_paper_five_main.py --config config/paper_observation_five.local.json
```

迁移保留联调成交、订单 ID、初始资金和扣费基准、豆粕 core 检查点、风险停开标志。账户互斥锁阻止双控制器。程序重启须先恢复同一路径和配置，不能删状态或重新联调；持仓或未知提交存在时必须核对原件。

## 卡片与回执

卡片使用标题分类、双色字段列、交易理由／风控依据、执行与数据依据。候选、受理、成交、待退出及柜台确认平仓分别通知。入场附方向授权、已闭合 H1、M15 斜率、具体 A/B/C/D 规则和触发证据；成交附真实平台回报价、数量、止损、目标、截止时间、扣费和时点。离场附退出触发及按实际成交价差计算的毛盈亏／毛 R，不冒充已核验净收益。拒撤、对账及数据异常单列。

日盘 15:05 后发送当日去重账户摘要；夜盘及敞口继续监督。第 5/20 个完整交易日按固定日历槽复核，首个半日不计，缺数日不从分母剔除。第 20 日停新开并继续管理余仓。

`state/paper_observation_five/control.json` 的 `stop_new_open=true` 为本机人工停开入口，`flatten=true` 请求受控清仓；请求不等于成交。无有效对手价、停板、断连、未成交及不明结果继续保留风险。禁止用飞书消息作为下单指令。

私有证据：`evidence/paper_observation_five/status.json`、`daily_report.md`、`order_events.jsonl`、`fills.csv`、`position_recon.jsonl`、`signals.jsonl`、`notifications/`；逐品种原件在 `products/` 和 `data/cache/paper_market/`。历史零成交核验原件及批准清单只在本机，原 GM parquet 不覆盖。缺失历史接收时钟始终 UNKNOWN。

发送进程仅明确 vendor code 0 记 SENT；429 有限重试。超时或服务器错误若可能已送达，记 UNKNOWN_DELIVERY，不自动补发造成重复。通知失败不改交易状态。会继续处理待发送消息，不补发此前未配置机器人的旧消息；未知交易提交不自动重发。

官方依据：[掘金仿真说明](https://sim.myquant.cn/sim/help/)、[仿真 Python 接口](https://sim.myquant.cn/sim/help/Python.html)、[飞书机器人卡片](https://open.feishu.cn/document/feishu-cards/quick-start/send-message-cards-with-custom-bot)、[EDB 时间字段及历史范围](https://doc.shinnytech.com/edb/latest/md_server.html)。模拟成交和本地轮询时延不能证明真实冲击、真实延迟或净盈利。当前自检不冒充独立复核。
