# 国内期货解释层合同 V1

依据任务书 1.1。观察器不产生新信号，不修改 `CLOSED_FACTS_PERMISSION_V1`，不另算指标。`local_direction` 与 `observer_eligible` 本轮恒为 `null`：尚无独立局部结构/跟随政策。短动能方向仅为已有 MACD 柱的正负，不叫新趋势。

## 字段来源

| 输出 | 唯一来源 | 缺项处理 |
|---|---|---|
| `background_direction` | 已审核 `facts.h1_absolute` 的 bull/bear/flat 类别 | 未知，不从当前价格猜测 |
| `production_m15_background` | `TrendDecision.confirmed_state` | 未建立与未知分开 |
| `momentum_direction/state` | 同次 `analysis.indicators.hist` 最后两值 | `null`，不从截图、权限原因或未来行情反推 |
| `episode_id` | 实际合约 + M15 + 已确认 S0 的 `since_bar_id` | `null`；不是新的局部波段 |
| `divergence_events` | 同次 `facts_row` 的持久化事件 | 缺事件关联标未知，不拼其他合约 |
| `production_permission` | 同次 `TradePermission` 原样保留 | 缺失不填允许 |
| `shadow_permission` | 本轮未登记候选 | `null` |
| `range_* / box_*` | 未实现独立箱体 | 全为空，不填0 |

`is_closed/is_complete` 由已审核 `SignalWorker` 的完整M1→闭合M15链证明。研究回放还用独立会话时钟复核，失败保留记录并标 `UNKNOWN`。旧日志未留 MACD 时不重算另一套值用于即时卡片；显示未知。历史首次收到时间只来自原始 `local_observed_at`，历史缓存的下载时钟不能填入。

## 阶段分支优先级

按下列顺序首次命中，版本 `EXISTING_FACTS_MAPPING_V1`：

1. 未闭合、完整性未确认或不完整 → `UNKNOWN`。
2. 当前 S0 没有已确认 bull/bear 背景 → `UNESTABLISHED`。
3. 原权限给出 `technical_exit_direction` → `TECHNICAL_EXIT_PENDING`。表示有退出义务，不等于已成交、也不等于允许反手。
4. 原权限 `weak_closed_bars > 0` 或原趋势提案为 `veto` → `WEAKENING`。保留原计数与 H1 否决事实。
5. EMA位置/斜率/MACD缺失 → `UNKNOWN`。
6. 原背景对应的价格位置、斜率、MACD柱符号全部同向 → `ALIGNED`。仍可因历史背驰或账户规则禁开。
7. 其余 → `MIXED`，仅说证据冲突/状态待定，不判新趋势或确认震荡。

MACD正柱缩短/负柱缩短只比较同次已有数值的绝对值变化。跨零另记 `CROSSED_UP/DOWN`，不与背驰 `B_hist` 的配对公式混淆。短动能变化不重置结构事件，也不修改退出。

## 四种背驰语义

历史事件存在：持久化历史表；结构有效：极值未严格突破且没有失效墓碑；当前预警：本轮没有登记局部阶段政策，明确 `null`；交易否决：仅由原权限实际原因与当前方向共同确定。双向历史结构都在不等于当前同时拥有两个方向授权。

事件标识复用 core 的 kind/mode/两个实际合约柱身份。极值发生与确认时间分列，年龄明确为自然分钟。跨段次数由已确认 S0 段计算；本地阶段段尚不存在，不能宣称已统计。

当前V1没有TTL：顶背驰结构在后续最高价严格突破极值时失效；底背驰反之。新方向/其他权限背景变化也可能令该事件不再否决当前方向。任何一项解除都不代表账户、报价、信号时效、持仓截止等其余检查通过。

## 时间与市场边界

实际合约固定，不跨换月拼结构。会话日历区分正常午休/夜盘休市与缺行情；夜盘交易日归属沿用现有日历。H1维持会话60分钟及15/30分钟短尾桶，显示时长与来源，不称MT5原生小时柱。tick、乘数、整数手、保证金、D+1与周末/长休市规则沿用原执行层。开/平今/平昨真实费率未知，解释层不能签净收益；历史本机接收时钟未知仍保持未知。
