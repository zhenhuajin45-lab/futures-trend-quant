# 有限窗口机制只读预检

只做本批 P0-1 至 P0-4：源索引、保护范围、字段及过去特征、容量和资源分流。不计算 P&L、未来收益、MFE/MAE、目标/止损先达，不运行账户或委托。

```powershell
python -m tools.mechanism_preflight.inventory --source-index '<本机授权索引.json>' --protected tools/mechanism_preflight/protected.json --out data/cache/mechanism_preflight/inventory_001
python -m tools.mechanism_preflight.check --inventory data/cache/mechanism_preflight/inventory_001 --mechanism tools/mechanism_preflight/mechanism.yaml --mode features_only --out data/cache/mechanism_preflight/check_final
python -m pytest tests/test_mechanism_preflight.py -q
```

目录须全新；源索引及完整库存表留在本机。`mechanism.yaml` 使用 JSON 格式的 YAML 合法子集，以避免额外依赖。沿用已有 Python 3.13、pandas/numpy/pyarrow，不升级依赖。

索引 JSON 包含 `sources`，每项至少有 `source_id/path/kind/purpose/authorization`，已封存来源另有 `expected_sha256`；并指定 `feature_source_ids`、`calendar_source_id`、`mapping_source_id`。不会按文件名猜版本。每个源记录字节 SHA、字段证据、时间范围、粒度、授权和可读层级，未知标 `UNKNOWN`/`UNVERIFIED`。

Parquet 先读 footer，CSV 先读表头；普通 pickle 不反序列化。唯一例外是公开源摘要已绑定、整个窗口在旧研究范围内且用户本批授权作字段盘点的 38 个稀疏历史盘口小包，只检查列名、时钟和档数，没有做价格研究。索引明确记录这些小包反序列化过，未把它写成零次读记录。

保护范围先于读价判定。旧窗仅有本任务明确授予的工程字段/容量例外，其他保护窗和日期不明的混合来源不读价格正文。主特征读价严格投影 `symbol/eob/open/high/low/close/volume`，只请求当日 09:01–09:30 和 14:30 的行；入场/退出价格不在请求中。任何结果列出现在待检 schema 时，读价入口拒绝。

本批只核 `DCE.M` 的日盘定义，以原日历和实际合约映射确定全部日机会；换月不借另一合约的价格。每个交易日仅一个预定槽，不把一分钟重复触发当成独立样本。历史缺接收时钟时标 `EVENT_TIME_PROXY`，不能签成交时延。五档 schema 门槛独立，M1/L1 不能替代。

容量同时报告 115 个非零过去方向槽和 63 个共同有效风险槽的上界敏感性，均非实测独立配对数。方差与最低有效增量未经登记，不给精确功效或通过结论。纯函数裁决支持四种资源状态；即使合成输入得到 `READY_FOR_DEVELOPMENT_DESIGN`，也不授权任何回测或下单。

受限输出：`inventory.csv`、`capacity.csv`、`capacity_budget.csv`、`feasibility.json`、`decision.json`、局部案例与自检。公开仅汇总、代码和来源卡。统计代码不依赖旧 631 笔成交账本，不沿用 V11 的九格/五品种投入线，也不重新定义 V11 的归档结果。
