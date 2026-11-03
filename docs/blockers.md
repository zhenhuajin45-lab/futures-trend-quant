# Sprint 2 外部阻塞台账

更新日期：2026-11-03  
适用分支：`sprint2-evaluation-fidelity`  
原则：阻塞项不得用代码默认值、占位费率、人工猜测或当前180日研究结果替代。

## 状态总览

| ID | 材料 | 负责人 | 状态 | 阻塞 |
|---|---|---|---|---|
| EXT-01 | GM长历史或等效授权M1 | 用户/GM券商支持 | BLOCKED | 正式开发集、独立验证集、Gate 3外推 |
| EXT-02 | 模拟账户逐品种真实费率 | 用户/GM券商支持 | BLOCKED | 交易层净收益、Gate 4 |
| EXT-03 | 116条背驰人工签认 | 指定交易员/标注负责人 | BLOCKED | 背驰口径人工验收 |
| EXT-04 | 足够长XAUUSD原始历史 | 用户/数据提供方 | BLOCKED | 三个趋势锚点普通断言 |
| EXT-05 | AURUM源模块与31黄金用例 | AURUM代码所有者 | BLOCKED | A/B/C/D 1:1等价性 |
| EXT-06 | 版本化交易所交易时段/临时停盘日历 | 用户/数据提供方 | BLOCKED | 精确缺M1统计、P1-D完整验收 |

## EXT-01｜GM 2023～2026完整历史

- 负责人：用户协调 GM/券商数据权限；或提供等效、有授权的数据集。
- 当前证据：`reports/sprint2_data_access_blocker.json` 记录最早可查询日期为
  2026-05-07，状态码2002，当前只有滚动180日研究窗。
- 所需格式：
  - 12品种实际合约 M1 parquet/CSV，含 `symbol/bob/eob/open/high/low/close/volume/amount/position`；
  - 每日主力映射 `trading_date/csymbol/symbol`；
  - 时区、复权状态、数据源、SDK/导出版本和授权说明；
  - 覆盖至少2022-10-01预热至2026-10-31。
- 最小质量字段：唯一 `symbol+eob`、明确交易日、OHLC合法、原始文件SHA256、导出命令。
- 验证方法：manifest哈希校验、合约窗口和换月双腿检查、M1聚合一致性、开发/验证边界
  检查；验证集在候选冻结前不得读取结果。
- 解除后工作：按 `docs/prereg.md` 重跑2023～2024开发集和一次性2025～2026验证集。
- 阻塞：Gate 3正式历史验收、Gate 4、独立样本外结论。

## EXT-02｜模拟账户真实费率证明

- 负责人：用户从掘金模拟账户/券商费率表导出；必要时由券商确认。
- 当前状态：`config/fees.json` 的12品种费率均为 `null`、`verified=false`。
- 每品种最小字段：
  - `exchange/product/account_scope/effective_from/effective_to`；
  - 开仓：固定元/手或成交额比例及单位；
  - 平仓：固定元/手或成交额比例及单位；
  - 平今：固定元/手或成交额比例及单位；
  - 经纪商加收、最低收费、来源文件名/截图编号、出具日期。
- 格式：CSV/XLSX/PDF均可；若截图，需能辨认账户类型、品种、单位和生效日期。
- 验证方法：双人核对单位；选至少一笔模拟成交按成交额/手数手算并与账单一致；再写入
  `config/fees.json`，每个品种单独 `verified=true`。
- 解除后工作：运行 fail-closed 费率加载测试；仍需满足趋势层和历史样本前置条件才能
  开始交易层研究。
- 阻塞：净收益、PF、胜率排名、收益率、最佳品种/参数和 Gate 4。

## EXT-03｜116条背驰人工签认

- 负责人：指定交易员；需要记录标注人身份或稳定ID。
- 来源：`data/reference/dataset.json`，116个跨片段去重标记；来源标记不是正确性真值。
- 最小字段：
  - `mark_id/pair_key/source/type/prev_t/cur_t/detect_t`；
  - `swing_pair_valid/channel_valid/divergence_valid` 三项；
  - `decision=accept|reject|disputed|insufficient_history`；
  - `annotator/annotated_at/comment/review_version`。
- 格式：UTF-8 CSV或JSONL；禁止修改原 `mark_id`。
- 验证方法：ID与冻结数据集一一匹配、无重复/遗漏；争议项不强制归类；抽样复核时价和
  可知边界；至少两名标注人时报告一致率但不把多数票当绝对真值。
- 解除后工作：分别报告 A47/B_hist 对人工标签的结构一致性，不以收益选择背驰口径。
- 阻塞：背驰规则人工验收；不阻塞纯工程回放。

## EXT-04｜XAUUSD长历史与时间标签

- 负责人：用户/原策略数据提供方。
- 当前证据：三个 `expectedFailure` 实际断言均因目标窗口无完成H1 warmup事实而失败。
- 最小字段：原始已收盘M15 OHLC、服务器时区/UTC偏移、交易日历、每段至少目标窗口前
  1,200根M15；不得只提供已计算指标。
- 格式：CSV/parquet；含 `symbol/bob/eob/open/high/low/close/timezone` 和文件SHA256。
- 验证方法：时间标签与原MT5服务器钟面交叉检查；重算EMA/MACD/H1；目标窗口前
  warmup充分后，把三个 `expectedFailure` 改为普通断言并逐项运行。
- 阻塞：XAUUSD三个趋势锚点验收；不允许通过降低warmup解除。

## EXT-05｜AURUM原始引擎与黄金用例

- 负责人：AURUM代码所有者。
- 所需材料：`m5m1-combo-v1`、`structure-facts` 源码及其锁文件/运行版本；31个黄金用例
  的输入、预期事实、预期路径和版本SHA。
- 最小字段：用例ID、全部bar输入及时间口径、参数、预期结构事实、候选路径、拒绝原因、
  预期信号时间；若涉及状态，需初始状态和最终状态。
- 验证方法：在隔离环境逐用例运行原引擎和本仓库重构实现，字段级diff；任何差异记录为
  规格差异，不允许只对最终买卖方向。
- 阻塞：A/B/C/D及结构事实与原策略1:1等价性声明。
- 阻塞期间允许表述：仅“规格重构版”。

## EXT-06｜版本化交易所时段与临时停盘日历

- 负责人：用户/数据提供方，可来自交易所官方规则或经授权的数据服务。
- 最小字段：`exchange/product/effective_from/effective_to/trading_date/session_start/session_end`，
  以及节假日、临时停盘、提前收盘、夜盘取消标记；时区必须明确。
- 格式：版本化CSV/JSON，附来源URL或文件、抓取日期和SHA256。
- 验证方法：用已知交易日抽样重建预期M1槽位；计划休市不记缺失，交易时段内缺口单独
  输出；版本变化导致数据集ID变化。
- 阻塞：P1-D `missing_minute_bars` 的确定值及完整数据质量PASS。

## 内部工程阻塞（不依赖外部材料）

以下不是外部输入，不得混入上述“等待用户”状态：

- 订单零成交拒撤、重复回调、未知状态缺针对性回归；
- 启动时未从经纪商持仓/未完成订单重建 `ftq_trades`；
- 新版 `trend_inputs`、路径、拦截、订单和成交尚无成对 GM/离线事件产物。

这些项目在未来进入只读或仿真验收前由工程迭代解决，当前安全门继续关闭。
