# 交易员背驰签认数据集

**8段标记数量如下；均按“来源＋前后极值对”去重，不是出现轮次数。** 同一标记在重叠片段中保持相同ID；全文件跨片段去重后116条：GDL 65、平台5、C1模型12、T20261030-02模型34。共1570条展示K线（含片段间重复），`dataset.json`约0.49 MB。所有标记都待交易员签认，没有预置“是/不是”标签。

| segment id | 核心区间（MT5 UTC+3） | bars含背景 | GDL137 | 平台 | model_c1 | model_t1030 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| s1014 | 10/14 00:00–12:00 | 188 | 2 | 1 | 0 | 0 |
| s1027 | 10/27 06:00–16:00 | 184 | 5 | 0 | 2 | 14 |
| s1028 | 10/28 03:00–14:00 | 188 | 14 | 0 | 0 | 4 |
| c1-front | 10/26 01:00–10/27 12:00 | 280 | 11 | 1 | 12 | 23 |
| s1030 | 10/30 01:00–14:00 | 178 | 9 | 2 | 0 | 6 |
| shape-trend | 09/28 01:00–11:00 | 184 | 23 | 0 | 0 | 0 |
| shape-range | 10/08 12:00–22:00 | 184 | 4 | 0 | 0 | 0 |
| shape-v | 10/06 04:00–14:00 | 184 | 2 | 1 | 0 | 0 |

表格统计`marks.length`。收录条件是首次识别时刻在核心区间内（含结束边界），**或**当前极值柱在核心区间内且随后12小时内识别；后者方便签认需要右侧确认的极值。不是简单以极值时间等同识别时间。`first_detect_in_core_counts`另给仅首次识别落在核心区间的数量。只收录核心相关标记，背景柱上不另外铺满标记。

## 字段与网页用法

顶层为`schema_version / symbol / timeframe / time / algorithms / selection_protocol / model_audit / provenance / segments`。网页只需读取这个单文件，不需要访问原工程、数据库、证据目录或运行脚本。

| 字段 | 口径 |
| --- | --- |
| `segments[].id/title/start_t/end_t` | 片段身份和核心区间；核心K线为`start_t <= t < end_t` |
| `bars[].t` | **服务器钟面秒编码**，数值按Unix刻度保存钟面标签，**不是UTC瞬间**；与冻结MT5 rates原`t`一致 |
| `bars[].mt5_label` | 如`2026-10-30 13:45:00 UTC+03:00`；直接格式化`t`，不再额外加3小时 |
| `bars[].utc_offset_minutes` | 该历史日期的实际偏移；本集9–10月全部180。11/01起规则为120，有冬令时测试；本集不含11月柱 |
| `bars[].o/h/l/c` | 原始已收盘M15 OHLC；保留精度，不插值、不补休市柱 |
| `bars[].dif/dea/hist/ema34` | 平台TA-Lib v0.6.4口径，MACD12/26/9，DEA为EMA，`HIST=DIF-DEA`，**不乘2**；EMA34。每柱使用截至该柱的251根已收盘预热，保留完整数值 |
| `marks[].id` | 来源前缀＋稳定SHA摘要；由来源、类型、前后时价和算法输入批次生成。相同输入批次内同一配对只保留一次，重叠片段复用同ID |
| `marks[].source` | `gdl137 / platform / model_c1 / model_t1030`；模型两批不能混成同一实验 |
| `marks[].type` | `top / bottom`；模型类型按当轮原prior恢复：结束bull→top、结束bear→bottom |
| `marks[].prev/cur` | `{t,price}`，均为极值柱开盘标签及该来源给出的价格；不是识别柱 |
| `marks[].detect_t/detect_mt5_label` | 本冻结序列逐根观察的首次可知闭合边界；模型用原冻结输入槽。不是实时API响应到达时间，也不声称重建了盘中首次出现时刻 |
| `marks[].pair_key` | `M15:type:prev.t:cur.t`，按类型和柱时间生成，**不含来源或价格**。用于三方合并显示；合并时仍需显示各方价格差异 |
| `marks[].score/score_kind` | GDL首次可显示时的研究分`chart_confidence`；平台quality2/3；模型自报quality。不可跨来源比较分值，GDL分数不是交易许可或已验证概率；不可评分为null |
| `marks[].detail` | 简短中文说明；GDL通道/配对方法/状态，或平台三项，或模型原声明及引用是否匹配 |
| `last_seen_t/observations` | 同一输入批次中该配对最后可见时刻/重复观察数。不是有效期承诺，也不代表独立事件数；score仍是首次时刻的值 |
| `origin_run` | 算法输入批次`early / c2-extended`；不同暖机历史不能冒充同一初始化。跨批次同一对仍可按pair_key合并 |
| `notes` | 已知交易员/正式策略讲解原文摘录及仓库路径、起止行；空数组表示没有找到该补选片段的交易员讲解，不能当反例标签 |
| `coverage` | 核心实际柱数、缺失槽、前后背景数；历史休市空槽如实列出 |

JavaScript图表可使用`t`作为墙钟显示轴，并自行格式化UTC字段；**不要用浏览器本地时区再次转换它**。若另需真实UTC，按`utc_t = t - utc_offset_minutes * 60`转换，不要从`mt5_label`去掉偏移后猜时区。10/26–10/31的180分钟由冻结rates元数据及线上快照`time_server_msc-time_utc_msc`逐柱核对；不是把11月当前偏移套回历史。

平台候选保留top和bottom，供标注研究；本次没有按当前趋势过滤。GDL仅收录正式1.37收盘`Preview`有证据且符合实时显示资格的候选，不收mask0的纯价格观察或事后`Review`。保留方法3及状态6“新增配对未校准”，不临时加分数过滤。GDL单柱MACD算法与平台不同，图中展示的DIF/DEA/HIST统一是**平台口径**；GDL标记的通道含义见detail，不可据平台图线反推GDL算法计算出错。

平台三项/quality在各自识别时点按C2归档代码计算。C1槽直接使用当时冻结的完整市场；其余按相同251预热、120可见条目（含1根排除的形成占位）、摆点左右2/合并3重建。图表每根柱的MACD以该柱为窗口尾；观察时点中更早极值的指标可能因滚动预热种子产生很小差异，不能用图表舍入值替代当轮完整证据核验。

模型只取`N_divergence=yes`且`source=new`。C1原67次声明中31次new、36次prior；prior全省略，new重复同对也合并。T20261030-02有72次new声明，按同样方式去重和截取本集相关时段。保留引用不实的原时价，不修成“正确标记”；两条T1030标记的previous时间不是实际M15柱，`prev_bar_present=false`及`price_matches_bars=false`已标明。网页应显示“引用不匹配”，可画在原时间坐标，不要偷偷吸附到最近柱。不能把这两条排除后再声称模型引用全部匹配。

10/14和三个补选段没有这两批模型的同刻冻结输出，所以模型标记为0，**不是模型判定无背驰**。完整12小时的10/14候选与上一报告05:30–07:30的较窄窗口不同，不能直接比较数量。

## 补选理由：只看价格，不看标记结果

从09/28–10/23的冻结行情中枚举**266个整点起、连续40根M15的10小时窗口**，排除休市间断和已指定10/14，顺序选单边、震荡、V形；三段不重叠，评分相同取较早者。未使用模型、背驰是否成功、GDL评分、交易结果或未来收益选样。完整规则及指标保存在`selection_protocol`。

- **单边：09/28 01:00–11:00。** 方向效率`abs(末收盘-首开盘)/逐柱绝对变化和`最高，约0.695；4279.72→4155.55，主要是持续下行。
- **震荡：10/08 12:00–22:00。** 按`(1-方向效率)*min(中位线穿越数,8)/8`最高；方向效率约0.00286，中位线穿越12次，4126.41→4126.85。中位线取本窗口路径最高/最低的中点。
- **V形：10/06 04:00–14:00。** 按首开盘＋40个收盘的路径选中部谷底，下降/回升均达到收盘路径范围65%，最大化两腿效率的较小值×回收比例；中部08:45收盘4119.98，4150.85→4119.98→4158.49。价格形态复核时将最初的“最低下影线”改为“收盘谷底”，避免把长针当作V；此修改只依据价格，不依据任何背驰结果。

## 行情来源、背景与缺口

核心10月段使用原C2 `rates.json`，M15从10/08起持续推进GDL。补选使用已存在的`T20261101-06/data/rates.json`，M15从09/15起持续推进；两批重叠的已收盘OHLC核对一致。没有读当前行情代替历史。

C2采集时10/30 13:30 M15尚在形成，先剔除。为补齐14:00及背景，只读`main.inference_snapshots`既有快照**14740、14776**的`klines_json`，解压`gzip-base64:`；只用`bars_ago`已知且在采集时已闭合的条目，用原生`time_server_msc`联结，并逐个核对重叠OHLC。没有模型调用或MT5连接，没有修改数据库。原始快照留在忽略的证据目录，交付JSON仅含去身份后的行情/标记。

每段至少前96根、后48根背景，端点超出时向前扩展。10/30例外：已完整覆盖01:00–14:00的52根核心柱，后方仅有14:00–17:45共16根冻结柱（4小时），如实标记裁尾，没有制造18:00以后的数据。10/14 00:00–00:45、C1段10/27 00:00–00:45无行情，各4个空槽列在`missing_core_slots`；它们不是未收盘柱，也不插补。

`notes`引用v4.6总包02第190、191、273–275行及总包03第4–10行；引用不等于已签认具体前后极值。10/30“无背驰”讲解范围仍有待裁定，网页应让交易员逐条判断，不能预先把该时段标记判错。

## 复现与检查

只在本AURUM worktree内输出文件，外部GDL工程只读。生成入口依次为：

```powershell
python scripts/prepare-divergence-review.py
python scripts/inspect-gdl137.py --rates docs/claude/evidence/entry-engine/divergence-review/early-rates.json --output-dir docs/claude/evidence/entry-engine/divergence-review/early-gdl
python scripts/inspect-gdl137.py --rates docs/claude/evidence/entry-engine/divergence-review/c2-extended-rates.json --output-dir docs/claude/evidence/entry-engine/divergence-review/c2-extended-gdl
node scripts/build-divergence-review.mjs
npx vitest run tests/ai/divergence-review-dataset.test.js tests/ai/gdl137-comparison.test.js
```

复现依赖原本机冻结档案及忽略目录中的两份只读快照，不会自动查询数据库、连接MT5或补行情；缺少证据应报错。网页使用dataset.json本身没有这些依赖。原始文件及GDL源码SHA前后一致；GDL header按原源码构建并与正式1.37逐字节对照。数据集固定字节及来源SHA见证据`divergence-review/dataset-validation.json`和完成清单。

本包13项测试通过，检查8段闭合与背景、夏冬令时、HIST口径、稳定ID/跨段复用、极值时价、可知边界、仅new根及错误引用保留、10/14正例及C1 08:45根、完整10/30覆盖。额外复核源OHLC、GDL原C2前缀、原文行号及重复生成字节一致性；不改变策略4.7、候选规则或提示词。**本地提交后停下，交给Claude制作网页，不推送、不部署、不调用模型。**
