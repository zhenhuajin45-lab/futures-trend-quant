# 天勤独立行情试采入口

当前状态：`AUTH_REJECTED`。独立环境 TqSdk 3.10.4 / Python 3.13.2 已通过官方导入检查；项目 pandas 固定为 2.3.3。用户已提供账号，Windows DPAPI 加密配置已生成。SDK 返回 HTTP 403、`invalid_grant / Invalid user credentials`，尚未完成真实订阅或覆盖验收。网页已登录的截图不能替代 SDK 密码认证的运行结果。

官方要求先在[快期账户中心](https://account.shinnytech.com/)注册，再以 `TqAuth` 登录。注册及服务条款由用户本人处理。凭据只留本机，不进入聊天、Git 或报告。

在独立 venv 安装 `tqsdk`，避免改变运行中的 GM Python 环境；记录安装版本。先执行：

```powershell
python tools/supplemental_tq/collect.py --doctor
```

配置本机进程环境 `TQ_USER`、`TQ_PASSWORD`，或通过 `credentials.py` 隐藏输入生成 DPAPI 文件并以 `TQ_AUTH_FILE` 指定路径。凭据修正应输出到新文件保留原证据。认证核实后，先运行官方结构的单次最小示例：

```powershell
python -m tools.supplemental_tq.hello_tqsdk --symbol SHFE.au2612
```

成功收到报价后，再显式传入经当日 GM 映射核对的实际合约：

```powershell
python tools/supplemental_tq/collect.py --symbols CZCE.SR701 DCE.eg2701 DCE.jd2612 DCE.lh2701 DCE.m2701 DCE.v2701 GFEX.lc2701 SHFE.au2612 --output data/cache/tq_trial/RUN_UNIQUE --seconds 3600
```

这只是历史交接中的示例合约，启动前须核对日期和合约身份，不自动沿用。换月腿另外明确加入。入口没有下单、撤单或实盘账户接口调用。未提供账号时不会连接网络，也不会改动 GM 订阅或进程。

`raw.jsonl` 按序记录本机首次观察时钟、单调时钟及 SHA256 链，`datetime` 为 TQ 原始 K 线起点，转换后的 `eob` 为起点加一分钟。每次更新中的 quote 为 SDK 合并快照，并非声称完整 Tick 原始报文。初始历史快照、恢复和修订不会被标成实时收到的分钟；只有已观察的当前柱推进到下一柱时标记 `OBSERVED_BAR_ROLLOVER`。这个标记仍须对交易日历、时钟误差、及时性和断连上下文另行验收。SDK 内部短暂重连未必能从业务更新间隔识别，因此该入口不能直接签发严格样本资格。

后续须盘中覆盖夜盘、日盘，分别统计请求、实际更新、分钟闭合、恢复补回和缺失；与 GM 同实际合约对账。TQ 本机时钟不能补写成 GM 历史接收时钟，`decision_at`、映射原始 `known_at` 保留 `UNKNOWN`。正式 90 日协议不由此改变。

依据：[官方快速入门](https://doc.shinnytech.com/tqsdk/latest/quickstart.html)、[行情与 K 线](https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html)、[API](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)。

官方技能包已下载并解压，SHA256 为 `24f8debaeab10bb79031a1d18b8ac99653d04950ab19f3fa723d438753157079`。已阅读 `SKILL.md` 及 environment-setup、market-data、wait-update-and-update-loop、object-fields、error-faq，使用包内 `scripts/check_environment.py` 核验安装前后环境。没有修改安装包源码或原 GM Python 环境，也没有启用指南中的交易示例。
