# 国内期货趋势策略（规格重构版）

## 最新交付：V8 成交日合约校正与独立验证准入

成交时再次复核当日主用实际合约，修正跨休市意图在旧腿补录尾段新开仓的缺陷。
同一输入八账户已重跑并对平，尾段新开为0，已有旧腿仍可退出。
主情景S0不变：期末模型损益 **+38.98万元 / 最大M1收盘回撤13.00%**；简单入场修正为 **+26.13万元 / 22.27%**。
旧窗末段39次开仓报价观察中，36次可见对手量足够且不利偏移≤2 tick；这不是保证成交或全期成本核验。
**独立回撤预算按用户确认冻结为15%。2025年9—12月82日候选窗口受分钟与报价权限阻塞，真实独立回放未启动，唯一检验未消耗。**
63项定向检查通过；继续保留S0研究、暂缓模拟盘，不追加旧窗变体。

- [成交修正、实际差额、报价原件证据和独立准入](docs/30_V8_成交日合约校正与独立回测准入.md)
- [八账户修前/修后比较](reports/v8_roll_comparison.csv)、[修后决策表](reports/v8_decision_table.csv)、[原版完整副本](reports/v8_baseline_da7a664/)
- [独立规则与15%预算](tools/v8_roll_review/independent_registration.json)、[真实准入状态](reports/v8_independent_admission.json)

## V8 初版（da7a664，已由上节校正）

旧180日缓存按真实合约、独立资金账户比较 S0 与简单入场；同一覆盖与风控，补齐所有截断旧腿的退出行情。
参考手续费加每边1 tick下，S0期末模型损益 **+38.98万元**、最大M1收盘回撤 **13.00%**；
简单入场为 **+36.10万元 / 18.15%**。毛平均每笔为 **+0.1880R / +0.1350R**。
**这是部分覆盖旧样本的诊断，不是12品种180日完整业绩或独立验证。** 3 tick下S0平均净R转负，暂缓模拟盘。
保留冻结S0供不重叠样本复验，不在旧窗追加入场变体；V7.1仍为 `READY_FOR_DATA`，M5保持停止。

- [完整结果、盈亏来源、成本及继续裁决](docs/29_V8_完整交易回测与成本裁决.md)
- [两组四情景决策表](reports/v8_decision_table.csv)、[逐月贡献](reports/v8_monthly.csv)、[利润集中度](reports/v8_concentration.json)
- [实际账本检查](reports/v8_ledger_checks.json)、[逐品种有效覆盖](reports/v8_product_coverage.csv)
- [预登记](tools/full_trade_v8/registration.json)、[成本假设](tools/full_trade_v8/cost_reference.json)、[执行修订](tools/full_trade_v8/execution_amendments.json)

## 最新交付：两腿22800分钟完整原件保全

两段完整OHLC、成交量、期初及期末持仓原件已在本机保全；新增22,800根M1，缺失、重复、多余均为0。
累计代理M15：`jd2602` 615/1200、`eg2605` 943/1200；`jd2605` 仍为1185/1200，缺11月4日日盘225分钟。
用户确认没有现成新源授权或原件，重叠校准状态为 `NO_CANDIDATE_SOURCE`，严格合格仍为0/29。
本轮9项定向原件/校准测试通过，未重跑此前74项或19项CLI，未运行真实评分。

- [本轮完整字段原件保全与225分钟状态](docs/28_V7_1_两腿完整预热保全与225分钟核验.md)
- [合并覆盖、原件哈希和精确缺口](reports/v712_supplement_receipts.json)、[本轮检查](reports/v712_checks.json)

## V7.1 裁决补丁与三腿预热取证（1df7180）

**上一版完整分母与全名额分母不一致时的误保留已修复；真实评分仍为 `READY_FOR_DATA`。**
补丁提交 `c9ab8af`，当前重跑 70 项逻辑及 4 项原件合同测试通过；原 19 项 CLI 回执属于 `bc54d7a` 基线，本轮未重跑。
`jd2605` 已保全 17,775 根 M1，按待核验时段代理可组成 1,185 根 M15，仍缺 2025-11-04 日盘 225 分钟。
`jd2602`、`eg2605` 各取得 225、345 根对照日 M1；指定早段 EDB 返回403，GM 返回2002，尚未形成合格预热包。

- [本轮结论与下一项原件](docs/27_V7_1_裁决修补与三腿预热取证.md)
- [补丁与实际回归证据](reports/v711_review_checks.json)、[三腿原件覆盖与缺失区间](reports/v711_warmup_receipts.json)
- [29腿取数清单](reports/v711_29_leg_fetch_plan.csv)、[清单口径与边界投影](reports/v711_29_leg_fetch_plan.json)

以下 V7.1 工程报告及指纹保留在 `bc54d7a` 基线含义下；当前补丁源码指纹以 `v711_review_checks.json` 为准。

## V7.1 基线交付：独立样本准入与一次评分（bc54d7a）

**准入反例已修复，一次评分器仅用合成数据验收；真实首次筛选仍为 `READY_FOR_DATA`。**
四所各73个评分交易日、12品种、29个实际合约已列目录。免费 EDB 对12个首批合约的
2025-12-15小包返回成功，对2025-09-15均返回403；早期预热与历史时段依据仍缺。
固定窗口、首次名额、+2个百分点/50%有效段/70%匹配门槛不变，M5维持停止。

- [V7.1 工程与数据交付](docs/26_V7_1_准入修复与一次评分交付.md)
- [逐实际合约缺口 CSV](reports/v71_actual_leg_data_gaps.csv)、[完整缺口与缺失日期](reports/v71_actual_leg_data_gaps.json)
- [正常 CLI 正负例](reports/v71_cli_checks.json)、[工程检查](reports/v71_engineering_checks.json)
- [一次评分入口](tools/first_entry_v71/entrypoint.py)、[真实样本锁状态](config/v71_sample_lock.json)

## 最新研究：V7 等尺度复核与首次筛选（2026-11-04）

**M5 提速保持停止；首次筛选器已完成工程验证，新样本状态 `READY_FOR_DATA`。**
固定原 2,493 对，双方独立采用自身闭合 ATR×1.5 后，S0 先达目标 948 对、普通时刻 1,042 对，
差 **−3.77 个百分点**，5 日块 95% 区间 **[−5.18，−2.42]**。原 V6 表完整保留。
新窗口历史请求被当前 GM 账户以 2002 权限拒绝；未在旧窗试跑首次筛选，也未触碰正式 90 日资料。

- [V7 裁决、边界与交付](docs/25_V7_等尺度复算与首次筛选交付.md)
- [预登记](docs/24_V7_研究登记.md)、[机器参数](tools/first_entry_v7/registration.json)
- [等尺度摘要](reports/v7_symmetric_summary.json)、[新旧配对 CSV](reports/v7_symmetric_pairs.csv)、[全部分类变化](reports/v7_symmetric_changed_pairs.json)
- [12 品种独立样本资格](reports/v7_new_sample_qualification.json)

## 最新研究：V6 历史趋势与 2R 机会验证（2026-11-04）

**当前S0技术入场未显示正向富集，停止M5提速试验。下一轮只研究入场筛选的增量价值。**
近期180日为已使用的探索样本。12品种、27个预热合格实际合约、17,420根可评分M15，
产生3,787份首次有效独立证据。2,493个完整配对中，S0明确目标先达828/2,493（33.21%），
同条件普通时刻893/2,493（35.82%），差−2.61个百分点；5日块95%区间[−4.00, −1.30]。
这些是价格路径标签，不是成交或净收益。下单保持关闭，正式90日协议不变。

- [完整研究报告](docs/21_v6_历史趋势与2R机会验证报告.md)
- [策略裁决和下一轮单一瓶颈](docs/22_v6_策略裁决与下一轮单一瓶颈.md)
- [机器摘要](reports/v6_price_events_summary.json)、[逐机会CSV](reports/v6_opportunity_pairs.csv)、[分层配对](reports/v6_paired_strata.json)
- [输入与版本](reports/v6_input_manifest.json)、[原数据补片影响](reports/v6_recovery_sensitivity.json)

在具备本机授权缓存的研究工作区使用已有研究Python运行，复算不需要实时订阅或登录行情源：

```powershell
python tools/historical_strategy/reproduce.py --output data/cache/v6_recheck_NEW
```

入口先校验源代码、37份研究合约数据和27份回放指纹，再重新计算逐笔首触与配对统计；
任何策略/数据版本变化会拒绝复用旧缓存。原始行情及含价格明细仅保留本机，公开附件为派生标签和标准化距离。

以下保留此前各轮工程与研究记录；旧复权缓存的数字不作为V6主研究结果。

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

v5 已启动本机首夜原始行情采证，并修复已到期前缀与项目签批绑定。当前受 GM 实时订阅额度限制，全池覆盖尚未成立，准入维持 PENDING_SOURCE。历史 GM 重查已找回 EG 新腿120根及 AG 新腿330根。进展与证据见[v5 交接](docs/18_v5_首夜采证与窗口裁决.md)。

v4 首包准入交接见[决定草案与数据来源](docs/17_v4_首包准入决定.md)。封存前预检工具位于冻结范围外的 [tools/forward_quality](tools/forward_quality/README.md)，12 品种资格表、独立日历依据与 EDB 三类样例对账见报告链接。

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
