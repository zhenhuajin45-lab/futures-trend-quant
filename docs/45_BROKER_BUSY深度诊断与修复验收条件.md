# BROKER_BUSY 深度诊断与修复验收条件

2026-11-05 22:21，北京时间。结论：**P1 本地执行调度缺陷，已确定性复现；不是这两笔订单收到掘金拒单。** 本次只读取现场与运行离线复现，没有修改运行代码、重启进程或补发旧信号。

## 证据与因果边界

运行客户端的 `paper_broker.py`、`paper_transport.py`、`paper_five.py`、`paper_sim_bridge.py` 经LF归一化后均与审阅源码一致。输入日志按实际读到的字节前缀记录SHA256，原件不上传。

| 信号收盘 | 实际评估 | 当时结果 | 上一笔成功对账 | 下一笔成功对账 | 距评估间隔 |
|---|---|---|---|---|---:|
|21:45:00|21:45:05.036419|BROKER_BUSY|21:45:04.340739|21:45:05.555122|0.518703秒|
|22:00:00|22:00:03.264475|BROKER_BUSY|22:00:02.384180|22:00:03.430337|0.165862秒|

两次之前均无正持仓、无活动委托。21:00以来已有4,741次成功对账，间隔中位数约1.031秒，记录到的夜盘 `RPC_FAILED` 数为0。两次没有生成订单意图，也没有到达原生 `order_volume`。

历史日志没有逐次记录快照的请求操作、发出/返回/应用时间，不能直接从日志恢复当根 `current` 对象。结合当时无交易意图、正常轮询和调用链，忙于账户快照是有依据的推断；不把这一推断冒充已经取得的逐RPC轨迹，也不据两次样本声称所有信号必然被拦截。

## 第一层：名称误导，忙标志来自本机

`paper_transport.py::SimTransport.busy` 只检查 `current is not None or not jobs.empty()`。任何在途请求或排队请求均算忙，包括普通账户查询，不限于下单、撤单。

`paper_broker.py::PaperEngine.open` 检查该标志并自行返回 `(False, 'BROKER_BUSY')`。这不是GM SDK错误码、交易所拒单、模拟账户权限拒绝，也没有证据指向行情五标的限制。原生桥接中的实际下单入口仍是带专用模拟账户的 `api.order_volume`，这两笔没有走到那里。

单通道串行访问原生SDK本身有保护作用，不能直接删掉忙检查或创建第二个控制器以争抢账户。

## 第二层：把可恢复的占用当成最终失败

`SignalWorker` 每个新闭合M15只输出一次 `CORE_RESULT`。`FiveRunner.core_events` 调用 `engine.open` 一次，收到 BROKER_BUSY 后只写日志、统计和候选通知，没有建立未提交候选队列。

即使0.17秒或0.52秒后通道恢复，原M15结果已被消费，不会因报价再次触及参考价而重放。30秒配置只是 `now - signal_at` 的新开资格上限，当前没有“在30秒内持续等待执行”的实现。候选卡也不是挂到平台的条件单。

## 第三层：处理顺序增加不必要的拒绝

`FiveRunner.cycle` 的顺序为 `market_events/core_events → transport.drain/engine.accept → engine.tick → 定期snapshot`。新信号可能在完成回执尚未应用之前被评估；普通snapshot又约每秒占用同一串行通道。

单纯把 drain 提前只能处理已经完成的请求，无法解决尚在查询的占用。单纯降低查询频率也不能证明信号不会丢失。修复应保留账户一致性，并明确区分“未发送候选等待”与“发送结果不明的委托”。

## 离线复现

`tools/broker_busy_audit.py` 构造真实 `SimTransport.busy`、`PaperEngine` 和 `FiveRunner.cycle`，以纯内存队列代替原生进程，没有启动GM SDK或建立网络连接。

结果：查询在途→信号被拒→快照完成→账户ready→下一轮循环仍无开仓意图，信号只评估一次。在合成价格与风险均合格的案例中，明确重新评估可建立一个合成队列请求；把一手风险改为超限后，仍返回 `ONE_LOT_RISK_LIMIT`、队列为零。测试没有向实际账户提交订单。

三个新诊断用例与执行、五品种、模拟桥接、平台回调专项合计 **100 passed**（一个已有protobuf弃用警告）。这些是固定旧版本缺陷的诊断用例，不能把“测试通过”解释成缺陷已经修复；修复后应改成通过新安全行为的回归断言。

运行：

```powershell
python -X utf8 -m tools.broker_busy_audit --client '<掘金客户端目录>' --output reports/broker_busy_baseline_audit.json
python -X utf8 -m pytest tests/test_broker_busy_audit.py tests/test_paper_execution.py tests/test_paper_five.py tests/test_paper_bridge.py tests/test_gm_paper_host.py -q
```

## 修复必须满足的具体合同

本节是待实现的验收设计，当前运行代码尚未应用。

1. **接收完成回执优先。** 先应用已经完成的柜台回执，再评价新开；已知仓位止损、截止清仓及撤单保持优先。
2. **仅对尚未提交的候选等待。** 本地查询忙时进入 `DEFERRED_LOCAL_RPC`；保留 `csymbol、symbol、evidence_id、source_bar_id、original_signal_at、expires_at、direction、anchor、atr、rules_digest、deferred_at、defer_reason、submission_intent_id`。不得覆盖原信号时刻或延长截止。
3. **等待结束重新验全套条件。** 使用新鲜对账和最新实际报价，复核方向授权、品种就绪、停开/风险标志、原结构锚点、止损目标、一手预算、资金、组合仓位、交易窗口和原30秒截止。正常候选处理完后再启动新的普通轮询，避免轮询占用优先级。
4. **故障或过期终止。** 趋势已变、数据故障、停开、期限届满时丢弃未提交候选并记录原因；本次旧信号不补单。重启时未提交候选不得自动恢复下单。
5. **未知提交不重发。** `submission_intent_id` 已建立或 `used_signals` 已消耗后按原订单恢复/对账逻辑管理。出现 `submit_may_have_reached_broker` 或未知订单时保持禁止，不把它当作本地查询忙再次发送。
6. **同源去重与公平调度。** 每个“证据+原评估柱”最多提交一次，等待与实际送单分开计数；同品种仓位、组合上限不变。不能每200毫秒重复打印一条“拒单”通知。
7. **增加可观测性。** 私有RPC轨迹记录 `op、enqueued_at、sent_at、reply_received_at、applied_at、elapsed_seconds、queue_depth`，候选记录 `busy_op、busy_age_seconds、snapshot_age_seconds、expires_at、final_disposition`。不记录token、机器人密钥或默认账户凭据。

最少回归包括：查询0.5秒后完成→期限内重验且只提交一次；持续忙到过期→零提交；候选等待期间风控/趋势/数据变化→零提交；一手风险超限→零提交；未知submit/部分成交/重复回报→不重复开仓；同时有退出任务→先处理退出；回执已完成但未应用→先对账。

## 与黄金一手风险的关系

本地调度错误和一手风险限制是两件事。截图对应候选的原止损计划超出当前一手风险额度，忙标志提前返回后没有继续走到风险检查。后续通知中的风险核算是原件参考重算，不是历史柜台拒单。

因此不能承诺“修好BROKER_BUSY就应当买入这两笔黄金”，更不能放宽风险预算、压缩结构止损来强行成交。当前现场空仓、无活动委托，修复方案应在离线回归和受控发布后使用新的自然信号验证。
