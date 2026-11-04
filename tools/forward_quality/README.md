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

质量回执记录三文件摘要。v5 要求通过后由操作员带项目签批 --approval 加 --seal。批准原件的哈希及 binding 必须匹配本次质量回执，审批缺失时零封存。该选项先制作全部已通过文件的临时字节快照，核对与回执一致，再逐日调用冻结 ingest，逐份校验正式 receipt。重复输入幂等，已封存异文拒绝。中途失败后保留已成功写入的前缀，补齐问题后使用相同内容重试。正式 study 须已 prepare。

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

## v5 首夜与签批

来源登记额外要求 coverage 对象，含 csymbols、from_day、through_day、fields、chain_explanation。fields 至少覆盖 known_at/available_at/bob/eob，实录模式再含 decision_at。owner 和 independent_reviewer 应为经人工核实的不同身份。

批准 JSON 包括 decision=APPROVE_SEAL、project_owner、independent_reviewer、signed_at、approval_ref、approval_sha256、binding。approval_ref 指 staging 内项目签批原件，签署时间应晚于通过的截止日收盘。binding 使用 preflight.approval_binding(report)，绑定首日、截至日、协议/日历/来源摘要及日包哈希。操作员只在原件真实签署后传 --approval。程序只校验身份与字节对应，真实性须由签署流程支持。

默认只验当前已收盘前缀，未来日标 AWAITING_CLOSE。quality/proofs 按内容寻址保留前缀证明。来源版本变更后需独审及重新核验，新证明与旧 REVOKED 记录并存。--as-of 是 QA 历史快照，禁止用于 seal 和正式证明。

recorder_supervisor_v5.py 启动独立市场证据进程，至11-05 15:05自动结束。它仅调用行情和映射接口，真实回调原文逐字段保留，SDK receive_local_time 与本机接收时钟分存，不生成 decision_at。原始日志记录每次重连、异常退出和覆盖限制。日志与状态位于本机 data/cache/first_night_v5。实际启动须以 health 与原始回调验证，不能用进程存在代替采集成功。

```powershell
python tools/forward_quality/recorder_audit_v5.py --root data/cache/first_night_v5 --report reports/v5_collection_status.json
python tools/forward_quality/history_recheck_v5.py --gm-root data/cache/gm_rolling_180d --local-output data/cache/history_v5/NEW_QUERY_ID --report reports/v5_history_recheck.json
```

历史重查必须使用新目录，没有 resume。旧日志未找到时，只给导出范围遗漏的机制支持等级。新 parquet、邻分钟价量和源回调全部保留本机。

实时采集以连续品种代码调用 fut_get_continuous_contracts，再按独立交易时段的目标交易日取该日真实合约。主连、次主连和月份连续不会传给实时 subscribe。目标日映射缺失或有歧义时记录错误，不能取回复末行或沿用别日猜测。真实代码遵守 CZCE 大写三位数字，CFFEX 大写四位数字，其余市场小写四位数字。M1 使用60s，M5/M15从实际合约M1聚合。历史回复原文保留，额外订阅只准备目标日的相邻交易日换月腿。
