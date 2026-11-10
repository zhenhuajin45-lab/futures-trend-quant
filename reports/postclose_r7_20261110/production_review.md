# R7最终生产路径复核

结论：本轮软件修改的真实调用路径已通过离线验证，安装已完成；实际新宿主、市场Tick、自然订单/成交/退出待用户启动及交易时段验收。保留现有暂停，未调用真实订单/撤单接口。

|生产链路|必要合同与最后副作用|验证|
|---|---|---|
|NTP proof发布→health→engine.open→native latest_quote|policy/source/config/run、原样本单调年龄90秒、偏差0.5秒、两源差0.1秒、原报价3秒；最终读失败保守拒绝|Windows开放句柄替换、3次/15ms读取、JSON损坏/持续拒绝/具体文件分类；3.10 native证明新鲜/过期验证|
|原候选→offer→drain→WAL→下一FiveRunner.cycle→服务绑定→bridge.order_volume|symbol/csymbol/side/effect/volume/price/stop/tick/multiplier/margin、原anchor/proof、V5 B有序过程、H1/权限、原30秒、账户与服务3秒、唯一写入者请求id|当前油脂五池schema2的P/OI做空，真实协调器与原生guard、隔离假柜台；各一次调用、ACK为0成交、信号去重；影子队列故障结果不变|
|pause_entries→execution.json|reason/source/at、first_known/history、旧metadata语义、pause=true；重复不重写|未知历史首次不伪造，保存当时已知与后续原因；停止恢复不自动清除|
|真实通知工作→TaskProgress→supervisor|阶段开始/结束、序号、耗时、最后成功、每阶段失败、pid/年龄|其它阶段成功不掩盖失败；阶段卡住超过原30秒仍故障；未知投递规则及原通知回归保留|
|history_n→NEW_BARS→m1_observed→core→signals|请求/返回/last_eob/new_rows/入队/入库/计算/最终评估时钟|原接收链保留且增加返回证据；修订/缺数仍拒绝，原期限不续|
|Observer.offer→有界队列→独立进程→Pipeline→独立state/pending输出→本地卡片|仅闭合事实/独立参数，无GM_TOKEN/账户控制器/下单模块；历史来源/接收/确认/计算/当前asof|实际Py313子进程0订单、无token；因果/未来柱与未来接收拒绝、restart/dedupe、长路径、角色转换；SHADOW故障不改变原执行|
|release.prepare→validation→stopped/flat_stopped→install→selfcheck|全部所属进程退出、明确专用SIM、30秒内只读交接、空仓无活动/未知、config/core/主文件身份、状态哈希|真实安装26份状态/5检查点字节保留；新保护不清除；安装/回滚和负向发布门槛回归通过|

验证总量：477项paper测试通过；241份嵌入文件源码/配置自检。专项测试与总数重叠，不相加。实际SDK TickLikeDict2离线适配、主宿主Python3.13自检、原生Python3.10 stdlib guard导入通过。完整主宿主不由缺numpy的3.10交易环境运行；未向全局或交易环境补依赖。

终审新增：M5短箱体对照原C路径同一前缀，使用M5 ATR、1.5倍宽度和四根收盘同侧EMA，不使用M15 ATR替代；对应跨定义回归通过。原候选假设观察区分触及、影线越界、闭合破坏及首次破坏时点；明确没有实际持仓假设、不改变真实退出。

最后对照：原核心与执行配置指纹相同；1手/2仓/.25%/1%/5%/20%、D+1/周末长假、未知提交撤单不重发、合法退出均由原门槛保持。新增PA不形成新入场门槛，费用或历史接收依据不足均UNKNOWN。今天全日75评估、2个B资格、0意图/订单/成交；下游自然闭环NOT_EVALUATED。没有根据无仓或停开样本宣称BROKER_BUSY或退出缺陷消失。

剩余事实限制：历史PermissionError失败文件、历史通知卡顿阶段、早期history_n返回内容均未留原件，不能唯一归因。新增日志用于以后定位；没有声称改变供应商最后一分钟发布延迟。历史seed接收时钟缺失导致本日75个PA结构回放为UNKNOWN，非造数补齐；统计准入未满足。

回滚：只能用户停止后调用R7 rollback；恢复已审代码及独立观察配置，保留最新真实账本、五检查点、暂停和独立观察状态，禁止自动回滚或复用过往恢复授权。

最终包保留两次受控覆盖的回滚链。需要回到原R6时，在停止、重新核验新鲜空仓及无未知委托后，先回滚`postclose_r7_20261110_final`至首次R7包，再回滚`postclose_r7_20261110`至原R6；两个包均保留在本机。原R6批准主文件SHA256为7002306ecb0a4af52e6813d6782bf508c00a03c55e17e4ece3d712ae03896b2b，不能拿旧交易状态覆盖最新账本。
