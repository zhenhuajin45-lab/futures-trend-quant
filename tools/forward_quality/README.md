# 首包质量工具

从项目根目录执行，依赖项目现有 pandas、pyarrow 和 pytest。工具位于冻结哈希范围外，调用 42825e5 的日历、as-of 换月与 M15 聚合函数。默认只读待交接目录并写质量结果。

```powershell
python tools/forward_quality/preflight.py --bundle-root data/cache/forward_v4/staging --calendar-manifest data/cache/forward_v4/calendar/calendar.json --output data/cache/forward_v4/quality --study data/cache/forward_v3
python -m pytest -q tools/forward_quality/test_preflight.py tools/forward_quality/test_edb_compare.py
```

退出码为 0 时，该连续前缀的结构、预热、时钟及来源审核通过。2 表示已取得材料未通过检查，3 表示缺材料、缺审核或尚未收盘。退出码 2 的可恢复性由原始证据决定。项目决定及负责人签署另外保留。

staging 下按 YYYY-MM-DD 放三个原格式日包文件。独立 calendar.json 包含 authority、version、sessions（全部原 sessions.json 对象，按真实交易日连成连续序列）、session_sha256_by_day、fixed_first_day 和 decision_clock_mode（NOMINAL 或 RECORDED）。该日历必须覆盖冻结相邻日和首个评分日；先补充足够的前置日历与完整预热日包。当前 calendar_basis.py 生成的短日历只确立首日，预热日期范围仍待来源补齐。

staging/provenance.json 含 owner、independent_reviewer、evidence 对象。evidence 的 calendar、mapping、quote 三项分别提供 source_status=VERIFIED、owner、evidence_ref 和 sha256。RECORDED 模式还需 decision。evidence_ref 指向 staging 内实际原始日志及来源审核证据。审核人应核对字段的含义和真实时钟来源，文件摘要用于核对交接身份。未知来源不能写为 VERIFIED。

preflight 全前缀调用一次 build_asof_minutes，保留跨日实际合约和偏移上下文。按预期分钟表逐槽核查主用合约，任意腿收到次数独立统计。M15 需全部计划分钟合格、同一实际合约且存在闭合决定时钟。未被选中的闲置合约晚到只记警示。

质量回执记录三文件摘要。通过后由操作员使用相同命令追加 --seal。该选项先制作全部已通过文件的临时字节快照，核对与回执一致，再逐日调用冻结 ingest，逐份校验正式 receipt。重复输入幂等，已封存异文拒绝。中途失败后保留已成功写入的前缀，补齐问题后使用相同内容重试。正式 study 须已 prepare。

收到、已封存和已证明合格的评分日分别计数。预热不足、材料待交、来源未审等情况下不增加合格评分日。工具运行不调用策略回放、评分或开盲。

## 数据源核验入口

```powershell
python tools/forward_quality/gm_source_probe.py data/cache/forward_v4/gm_probe.json
python tools/forward_quality/calendar_basis.py --gm-probe data/cache/forward_v4/gm_probe.json --local-output data/cache/forward_v4/calendar --summary reports/v4_calendar_basis.json
python tools/forward_quality/source_inventory.py --gm-root data/cache/gm_rolling_180d --study data/cache/forward_v3 --calendar-basis reports/v4_calendar_basis.json --report reports/v4_source_inventory.json
python tools/forward_quality/edb_compare.py --gm-root data/cache/gm_rolling_180d --local-output data/cache/edb_v4 --report reports/v4_edb_three_cases.json
```

gm_source_probe 使用已有 GM_TOKEN，只查询日历、交易时段及映射，当前观测时钟不赋给历史行。EDB 对账按实际合约免费请求四个固定样例，起始纳秒转为北京时间 bob，eob=bob+一分钟。原始响应与完整差异 CSV 保留在本机，公开摘要包含来源哈希、数量和偏移诊断。--reuse-response 可只读复算已下载文件。

EDB 的 OHLC、数量及持仓对账不能补齐掘金历史接收记录。冻结首日及原协议维持原版本。官方日级行情对账、Sina 争议抽查、TqSdk 双路采集需各自完成来源验证后另行交接。
