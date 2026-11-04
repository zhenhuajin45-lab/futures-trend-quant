# TqSdk 官方接口核对与本机接入记录

本轮完成官方技能包阅读、独立环境安装和只读接口核对。真实行情接入尚未通过：2026-11-04 的 SDK 认证及网页登录后的单次复核均返回 HTTP 403，白名单错误为 `invalid_grant / Invalid user credentials`。未进入实际合约订阅。此结果不评价免费行情覆盖率或订阅并发容量。

## 已核对的接口约定

1. `TqAuth(user_name, password)` 使用快期服务凭据，可接受用户名、手机号或邮箱；期货公司资金账户是另一个接口。只取数可使用默认本地 `TqSim()`。
2. `get_quote()`、`get_kline_serial(symbol, 60)` 在循环外订阅一次，返回持续更新的引用。`wait_update(deadline=time.time()+...)` 推进业务，结合 `is_changing()` 判断对象变化。超时本身不证明断线。
3. `iloc[-1]` 为形成中的柱，闭合柱使用 `iloc[-2]`。先检查序列就绪、时间和 NaN。纳秒时间按 UTC 时点解释，记录原始起点；M1 终点加一分钟，展示时间另转北京时间。
4. GM 当天主用合约先按**精确交易日**筛选，再订阅同一个实际合约；不把两家的主连视为同一数据。主连不可直接交易，教学中的启动时选合约也不能替代换月管理。
5. 会话在 `finally` 或上下文管理器中关闭。采集只记录行情，源代码无下单、撤单或目标持仓调用。正式 90 日协议和采集审批状态均不改变。

## 安装和验证证据

- 官方包下载地址：<https://doc.shinnytech.com/tqsdk/latest/ai_editor/skills/tqsdk-trading-and-data.zip>。
- 包 SHA256：`24f8debaeab10bb79031a1d18b8ac99653d04950ab19f3fa723d438753157079`。
- Python 3.13.2；新建补源专用 venv，TqSdk 3.10.4，pandas 2.3.3；原 GM 解释器和依赖保留。
- 官方 `check_environment.py` 安装前为 `not_installed`，安装后为 `ready`。已核对本机实际 SDK 的 `TqApi`、`get_kline_serial`、`wait_update` 签名和认证源码。
- 已有离线端到端行情循环测试覆盖初始快照、柱推进、SHA256 链、会话关闭及禁止调用交易方法。它们不代表已经收到真实行情。
- 本机 Windows 用户绑定的 DPAPI 文件位于忽略目录；不在报告内记录账户、密码、令牌或个人资料。认证诊断仅输出状态码和白名单错误。

最小单次示例：`python -m tools.supplemental_tq.hello_tqsdk --symbol SHFE.au2612`。实际执行须使用安装好的 TQ 专用解释器并指定本机 `TQ_AUTH_FILE`。当前认证未通过，不继续重复尝试。密码认证核实后先运行该最小示例，再进行七个缺口合约加沪金的 M1 试采。

本机已查询 2026-11-04 精确日 GM 映射，示例清单与本次映射一致。原始响应、观测时钟和失败试采链保留本机。接入后仍须分别验请求、实际报价、闭合分钟和恢复补回，完整夜盘加日盘通过后再决定是否长期保留此来源。

参考：[快速入门](https://doc.shinnytech.com/tqsdk/latest/quickstart.html)、[TqAuth](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.auth.html)、[程序结构](https://doc.shinnytech.com/tqsdk/latest/usage/framework.html)、[常见问题](https://doc.shinnytech.com/tqsdk/latest/qa.html)。
