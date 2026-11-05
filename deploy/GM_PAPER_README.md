# 掘金期货模拟观察部署

用户已授权 `S0_SHORT_PAPER_V1` 在专用期货模拟账户观察。实盘禁止。原 S0/V10、V11、EDB 研究结项不变；本入口不运行历史收益、独立评分或正式封存。

初期只运行 `DCE.M`，每笔 1 手、不加仓。实际合约由 GM 当日映射确定；换月冻结新开、旧腿继续退出，不在盘中拼接复权。旧 `config/strategy.json` 和 core、V9 持仓纪律未修改，代码和规则摘要固定在 paper 配置。普通隔夜允许；最迟 D+1 日盘结束前 5 分钟清仓；周五日盘、长休市前同样清仓，周五夜盘不重开。

## 账户和环境

本机 GM 数据 SDK 为 Python 3.13 / `gm==3.0.186`。其部分订单查询依赖全局上下文，不能满足本任务全部显式账户查询，因此使用必要的 GM 官方 `gmtrade==3.0.6` 仿真适配器，连接固定 `api.myquant.cn:9000`。官方轮子需要 Python 3.10，装在项目隔离环境，未升级原环境。行情和交易 SDK 在不同进程，不混载原生库。

终端模拟账户页面及指定账户的官方仿真现金查询共同验证目标。每次现金、持仓、委托、成交查询和下单均给出精确账户；撤单给出账户和订单 ID。无默认账户、终端实盘端口或本地实盘回退。本地 profile 就是本实施的策略到账户绑定，不虚构终端 `strategy_id` 或 SDK 未提供的 `account_type`。

真实账户凭据和绑定放在忽略的 `config/paper_account.local.json`、`config/paper_observation.local.json`。后者绑定前者 SHA256，并指向隔离环境解释器；凭据仅从已有本机 `GM_TOKEN` 读取。公开模板不能直接运行。原始行情、运行状态及账户日志均留本机。

```powershell
# 先进行只读账户、完整预热和策略状态重建检查
python .\tools\paper_preflight.py --config .\config\paper_observation.local.json --max-seconds 600

# 只读通过后启动；有效交易时段自动进行已授权的一手开平撤联调
python .\deploy\gm_paper_main.py --config .\config\paper_observation.local.json

# 停止新开并撤待开；原有风险继续监督
python .\tools\paper_control.py --config .\config\paper_observation.local.json --stop-new

# 同时要求清仓，仍以柜台确认平仓为完成
python .\tools\paper_control.py --config .\config\paper_observation.local.json --stop-new --flatten
```

这些命令使用原 GM Python 环境；交易解释器由本机配置指定，不使用全局安装。不要直接复制账户文件到公开仓库，也不要在命令行参数中传 Token。账户互斥锁和状态锁拒绝重复控制器。修改账户、策略参数或配置后不能继续套用旧状态。

## 运行和风险边界

行情是 GM `current()` 一档快照（盘中约 1 秒）和已闭合 M1 `history_n()`（约 5 秒）轮询，**不是 tick 推送流**，当前流式订阅数 0。行情、core 重建及模拟 SDK 分进程，执行主进程不反序列化整个预热行情，仅传递有 SHA256 的私有缓存路径。报价事件时间、本机 API 返回时间、core 完成时间、意图落盘/发送、回报接收分别记录。M1 的历史 OHLC 只能触发退出，不据此伪造成交价。实际模拟成交价格来自 GM 回报。

需要 1,200 根完整 M15，按 DCE 日历及实际交易小节核对全部应有 M1；H1 使用原 core 的已闭合 M15 聚合和 finalized-entry 判定。预热只恢复状态，不下单、不遗留预热委托。信号仅在 M15 收盘后按原规则判断，不启用 M5 提速。数据缺口、修订或换月禁止新开；定时截止和已知持仓管理继续。初版数据线程失败时须人工核查/受控恢复，不假定有无缝自动行情修复。

报价不得超过 3 秒，点差不得超过 4 tick；限价不超出当时对手价；信号超过 30 秒跳过。开仓待 10 秒未完撤单。最终 SDK 发单前再核对账户、报价、数量、可用持仓/资金与风险。每手结构风险不超过净值 0.25%，保证金占用不超过 20%；日亏 1%、观察回撤 5% 停新开。退出不随停新开关闭。

意图先原子落盘再发送，提交结果不明不重发；重启先查询柜台，累计成交去重、终态不倒退，部分/迟到成交即时纳入风险。清仓确认要求柜台余量为零、无本地及柜台活动委托且对账一致；发出平仓不是平仓完成。未知仓位或未知委托保持 `PENDING_RECON`。拒撤、缺报价、无可用量和断线保留应平事件及剩余风险。非本策略仓位不自动反向处理。

联调的未成交撤单及一手开平全部标 `CONNECTIVITY_TEST`，只有柜台确认平且无活动单才转 `NATURAL_SIGNAL_OBSERVATION`。若拒单或有限尝试耗尽，停新开处理具体执行问题，不自动扩大滑点或信号阈值。策略自然成交记 `STRATEGY`；无信号不凑交易。

## 证据及复核

本机 `evidence/paper_observation/` 保存 `run_manifest*.json`、`status.json`、`signals.jsonl`、`order_events.jsonl`、`fills.csv`、`position_recon.jsonl`、`daily_report*.md`、已观察数据登记及原始报价/M1。`state/paper_observation/` 保存原子状态和控制指令。日报原始数据私有；只发布脱敏运行回执。

第 5、20 个完整交易日设置复核标记。首个半日不计完整日，分钟计数同时要求及时收到闭合 M1 和有效报价。20 日后停新开、继续管理余仓，等待复核，不自动延长、调参或切实盘。本机终端关闭、睡眠、重启会中断本地程序，应重新运行同一入口并先完成恢复对账；当前不安装系统服务或自启动任务。

毛成交损益、GM 模拟扣费、模拟净值分别记录；未核实的真实费用、冲击、时延不能由模拟账本替代。本次不运行简单入场影子账户，也不声称已证明策略盈利或真实成交可达。

本次可见行情登记为 `PAPER_OBSERVATION_SEEN`；遵循原协议更严格范围，保守标记原正式 90 日全池日期块不再未见。原固定 82 日和 V7.1 历史保护保留，未执行独立评分。加入 paper 模块会使旧盲测入口的全包代码哈希检查拒绝当前分支；原冻结分支不变，不能为通过检查重签旧锁。

官方依据：[GM 仿真说明](https://sim.myquant.cn/sim/help/)、[官方 Python API](https://sim.myquant.cn/sim/help/Python.html)。仿真不模拟市场冲击。
