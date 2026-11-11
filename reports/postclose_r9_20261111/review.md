# 2026-11-11 盘后 R9 修复与生产审查

本轮针对今天冻结证据的 D01—D14。当前分支发布；目标仍是原十品种专用 SIM 策略，由用户独占启停。原始行情、账户、凭据与通知原件仅保留本机。本文件与精确指纹回执共同使用；未实际发生的 Git 发布、安装、宿主加载及自然交易分别登记。

|问题|本轮处理|证据边界 / 下一验收条件|
|---|---|---|
|D01 P1 返回后协调器等待|市场、核心、报价队列按全局时间预算及轮转份额消费；交接后、风险前、报告后再次应用 RPC；记录队列等待与阶段耗时|一项工作本身仍可能慢；预算不抢占原生调用。夜盘看真实 cycle_work / DEQUEUED / RPC_APPLIED|
|D02 P1 M5 混源|新增五品种仅核心绑定 TQ seed 与 committed journal；不再读旧 GM m1_observed|冻结数据十品种实际消费者全部成功；不把两源历史差异抹平|
|D03 P1 requested_at 假设|TQ 不套 GM manifest 字段；精确前缀、源绑定及 asof 接收时钟核验|历史缺接收时钟保留 UNKNOWN；错误记录具体字段、文件、阶段|
|D04 P1 M5 批次进度过期|逐品种实际完成工作登记；旧 TQ cursor 有回执迁移，旧错误及 processed IDs 保留|单品种卡住仍按原 30 秒拒绝；阶段完成与 M5 质量分别核验|
|D05 P1 GM 小节尾迟到|原 GM 五合约增加官方已闭合 60s 回调，与同源历史连续去重；新增消费 journal 与重启前缀证明|软件与实际 SDK 形状已验证；官方推送也可能迟到/缺 Bar，原 30 秒从不续期，夜盘待真实样本|
|D06 P1 原生 cash 慢读|保留唯一 writer / 独立 reader，陈旧账户返回仍拒绝；增加 native→队列→应用的分段归因|无法仅靠本轮证明外部 cash 耗时消失；有仓并发退出仍待自然样本|
|D07 P2 TQ 尾柱闭合限制|新增预期交易分钟/M15 watermark 与 PENDING_SOURCE_CLOSURE，PA 同时呈现覆盖和预期尾部|未取得官方闭合证明，不把 iloc[-1] 转正；此数据源限制仍待解决|
|D08 P2 参考止损计划不可行|分开技术资格、冻结计划有效性、实际首拦截；补报价源、事件/接收年龄与检查时钟|今天五个下午计划均超原结构止损限制；没有为成交替换旧锚点或放宽 ATR 上限|
|D09 P2 启动与内存|日历按 section 建向量化分钟索引；spawn bootstrap 不加载 GM SDK，核心/时钟/PA 入口去掉无关 GM 环境|十套完整日历逐项等价；系统单次内存高占用不证明根因；真实十品种启动耗时待复核|
|D10 P2 订阅误判|GM 订阅只与原 GM 五合约比较；TQ 独立源证明；新增 GM M1 订阅就绪诊断|GM 不订新增五合约；启动后核 GM Tick/M1 各五与 TQ 当前代五合约|
|D11 P2 服务/报价诊断不足|service_facts_ready 与 pause、entry_gate_ready 分离；事实失败子条件与实际完成检查时钟进入证明及原生拒绝|新鲜健康不解除保护；同请求 permit 仍须绑定真实正常 WAL，未知请求不能借用|
|D12 P3 五品种标题|汇总卡片按真实池数量呈现；分列 M5 质量、尾部待确认及计划无效原因|提醒不是交易授权|
|D13 P2 首报送达未知|后续服务状态卡关联原通知 ID / 实际状态，明确此前 UNKNOWN|未知发送不重试；恢复 SENT 不能倒推首报已送达|
|D14 自然交易闭环缺样本|软件回归走真实协调器、engine、permit、原生最终出口，SDK 替身明确标识|持久 pause 遮蔽：自然提交、成交、撤单、止损/2R/退出均未因此验收通过|

## 路径与字段审查

GM SDK `bar_callback → host.on_bar → queue_host_bar → ClosedM1Book → engine.m1 / core NEW → SignalWorker → CORE_RESULT → engine.offer → drain_candidates → WAL → 唯一 native submit`。Bar 必须包含实际合约、60s、bob/eob、OHLC、volume/amount/position，经过实际接收时钟和交易日历校验；source / returned_at / enqueued_at / applied_at / core 阶段时钟向后传递。历史与回调重叠不能重复建票，缺口不能跳过，修订不能悄悄替换。

候选保留原 `first_eligible_at / expires_at / evidence_id / path / direction / anchor / stop / account / source_run_id`。执行最终仍检查账户、活动未知意图、持久保护、服务证明、双源时钟、原 3 秒报价、4 tick 价差、原 30 秒候选、1 手/2 仓与原风险上限。最终副作用只由既有 `paper_sim_bridge._serve → 原生 order_volume` 执行。测试替身可记录调用，生产验收必须查 native_submit_invoked 与柜台/成交回报，不能由 RPC SENT 推断。

账户返回实际应用前再核其原请求时效；新增队列单调等待时钟不替代证明时效。退出仍优先于候选，完整读取慢不能创建第二 writer。已有停开、halt、退出义务、墓碑、已消费 ID 与迟到/未知语义全部保留。

## 验证与限制

采用 production-code-review 的触发至最终副作用复核，由本任务单人完成，不冒称独立审查。完整相关回归、实际 Py313 单文件自检与 spawn、实际原生 Py310 最终保护、真实十品种 M5 消费、冻结历史核心比较和真实 SignalWorker 重放结果见 `validation.json`。全库研究测试曾扩大执行后中止，不列为全库通过；最终完成范围明确列出。

冻结原件 1427 份逐哈希核验；同源历史 60 条实际评估比较技术、H1、方向/权限、A/B/C/D 审计，不能把缺失区间补成样本，不能从未成交推断盈利或机会收益。TQ 与 GM 方向历史不混源；PA 与背驰替代仍 SHADOW。

软件通过不等于用户已启动、行情回调已到或自然交易闭环已通过。交接必须明确当前保护、安装状态、夜盘真实 Tick/闭合 Bar 时钟、账户和单控制器；用户启动后再核真实加载指纹。
