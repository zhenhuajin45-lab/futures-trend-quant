# R8 production-code-review

结论：确定诊断修复、PA 消费窗口质量修复及独立结构替代 SHADOW 的发布包通过软件复核。客户端未覆盖，新宿主未加载。P1 长期背驰硬否决的 ENFORCE 未准入，不能宣称生产方向阻断已被移除。

| 问题 | 修复与最终出口 | 验证 |
|---|---|---|
| P1 旧结构有效被用于所有 episode 的长期否决 | 历史与墓碑不改；独立事件消费账本用背驰后已确认四个交替枢轴 HH/HL 或 LH/LL 替代当前影子警告；生产 veto 原值保留 | 五品种、多空、双事件顺序、跨周、等值、滚动、墓碑、重启与迁移；ENFORCE 仍阻塞 |
| P2 none 按空头附加反向理由 | evaluate_permission 无方向直接拒绝，只留 NO_DIRECTION_AUTHORIZATION，后续层不评估 | 定向复现、core/engine/native 最终 proof；V2身份贯穿最终验证，V5政策不变 |
| P2 建立误报反转 | priority_changes 按原事件 old→new 分类，不拿 last_direction 升级红色反转卡；去重键不变 | 建立/结束/直接反转；旧已发送事件不重放 |
| P2 PA 旧缺口污染当前 M15 | 新 V2 政策按实际消费 M15/M5 区间核验；区间内缺口仍 GAP，窗口外历史单列 | 旧/区间内/首分钟缺口；V1加法迁移保留 known_at、事件、待落盘和 emitted |
| P2 影子异常可能影响生产结果 | 副本推进、成功提交；异常仅 UNKNOWN，保留独立缺口，不提交半成品 | 异常注入与坏状态，生产 trend/entry/permission 对比相等 |
| P2 回滚 PA 状态兼容 | 显式 prepare-rollback 捕获当前宿主/子树；重新停机与账户核验；仅兼容政策元数据 | 安装/显式回滚、未停机、账户未知、宿主变化、门禁/运行源码篡改拒绝，检查点字节保持 |

## 实际路径与字段合同

CLOSED_M1_RETURN → MarketLoader → SignalWorker（拒绝同键修订，只处理新闭合M15）→ run_core_step → FactsState/原 step_trend → permission/逐路径 select → FiveRunner 候选 → PaperEngine offer/drain/open → WAL/rpc_id → native paper_sim_bridge._serve → api.order_volume。

最终订单仍需原 symbol、path/evidence、方向、anchor/episode、首个30秒时钟、对手价及时间、账户/服务/绑定permit和风险输入。shadow 只进入独立 divergence_shadow 与 facts_row，不替换生产 facts/EntryContext，不进入最终 permit。none 不授权默认方向。V2权限身份进入 validate_execution_proof；PAPER_ENTRY_V5 对 A/B/C/D 的原 proof 校验保持。报价/账户/服务3秒、时钟门槛、信号30秒、挂单10秒、1手/2仓/.25%/1%/5%/20%及截止/退出规则均未放宽。

## 验证与边界

- 547项相关回归通过；最终源码110项定向复核通过（重叠，不相加）。真实Py313的246文件嵌入自检、独立包编译、包内实际Py310最终出口14个合成场景通过。git diff --check通过。
- Py313复核使用当前源码及目标私有SDK的隔离副本。初次把整个runtime放进搜索路径、误导入旧tools的环境问题已纠正；未安装依赖或改全局环境。
- Py310执行生产 _serve 最终校验，vendor函数合成，未创建真实账户控制器。它不能替代真实绑定permit、柜台、成交或有仓退出；绑定permit/异步跨阶段由相关Py313回归覆盖。真实柜台调用为0。
- 同源记录输入机制审计364条，旧动作复算0差异。黄金有1处未观察到的预热状态边界，已明确重接原前态。审计复用原episode相对H1事实，未完成新episode/H1/路径/费用回测，不能作为ENFORCE准入或收益证明。
- 黄金129条观察中none 95→37、方向差异58。影子记录序列首次夜盘bear为21:30，原S0为01:00；3.5小时差异是机制筛查，非可成交确认延迟或新增盈利机会。提前方向有误判、翻转和退出警告变化风险，尚未充分量化。
- 23:00及02:30末分钟首次返回已在原30秒之后。本包不续期，不声称修复供应商返回速度。
- 历史实际接收时钟 UNKNOWN；缓存创建不代表历史可知。影子政策未满足样本外准入，禁止自动ENFORCE。

## 复发预防

固定审查 background、reasons/NOT_EVALUATED、原事件old/new；structural_valid/warning_active/entry_veto_active、consumed[event_id]、replacement.source_ids/rule_confirmed_at_bar_id 与 actual_known_at；PA required_from/to、missing_eob、retained_prefix、迁移源哈希。异常必须 UNKNOWN，不能进入生产许可。检查点、候选、历史时钟不因发布重建。最终包统一LF，原始字节哈希与LF哈希一致，安装器再次绑定主文件/配置/核心指纹。
