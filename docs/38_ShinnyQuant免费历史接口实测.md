# ShinnyQuant 匿名历史接口实测

日期：2026-11-05，北京时间。基线：`8baa4e100428f523e0fef78ed3888900570fa9e8`。本次仅响应用户要求测试指定 SDK，未重开策略研究。

**结论：匿名近期 M1 和早年日线可读，但 2024 年 M1 仍返回 HTTP 403。ShinnyQuant 没有为本项目增加一个不同权限的数据源，未落实六条早年合约的分钟覆盖。**

| 固定请求 | 实际结果 |
|---|---|
| DCE.m2701，2026-11-03 09:00–09:05，M1，仅 volume | HTTP 200；pandas DataFrame 6 行，重复时间 0 |
| DCE.m2405，2024-01-15 09:00–09:05，M1，仅 volume | HTTP 403；SDK ApiError.status_code=403 |
| DCE.m2405，2024-01-15，日线，仅 volume | HTTP 200；pandas DataFrame 1 行 |

三次请求，无登录、无 token、无凭据读取、无重试、无实时订阅和订单。通过显式空认证阻止 requests 从 .netrc 隐式加载认证。只请求 datetime_nano 与 volume，未取 OHLC、未计算毛响应、首触或收益。六行时钟/量不构成完整交易日或研究样本准入；日线也不能重建分钟路径。

近期小样仅为独立于旧窗结束和正式 90 日开始之间的 2026-11-03 技术探测，不能凭此宣称新开发/独立样本成立。2024-01-15 沿用上一批固定 SOURCE_QA 日；未读取任何既有保护区。进入端实际合约是否仍为当日主力不属于此次匿名接口测试，未用主连替代实际合约。

## 接口含义

PyPI 项目说明将 ShinnyQuant 定义为 EDB 数据服务的 Python 客户端，并将 TqSdk 列为另一款相关产品。因此“原 TqSdk”的说法不准确。本次核验 wheel 的 client.py 实际请求 https://edb.shinnytech.com/md/kline，结果与已有 EDB 权限边界一致。

最近一年分钟线、任意历史日线是免费层文档范围，不是“近期开发窗加更早分钟独立窗”已经具备。当前 2024–2025 年 8 月六腿仍需历史分钟权限。本次未验证实时行情或 15 分钟延迟，历史 SDK 测试不能回答实时能力。

SDK 将 datetime_nano 转为 Asia/Shanghai 的 datetime 索引，但该索引仍是 bob：本次 09:00–09:05 对应 eob 09:01–09:06。接入 M1 时必须加 60 秒；不对日线机械加一分钟。

依据：[PyPI 项目说明](https://pypi.org/project/shinny-quant/)、[EDB 官方接口文档](https://doc.shinnytech.com/edb/latest/md_server.html)。本次包源依据为 PyPI wheel 及其已核对摘要，不把包描述本身当成市场数据完整性证明。

## 环境、复现和交付

现有 Python 3.13 环境未安装 shinny-quant。新建隔离 venv，启用 system-site-packages 只读复用现有 pandas 2.3.3 / requests 2.32.5；以 --no-deps --no-index 安装下载并静态检查过的 wheel，没有升级或改动掘金、TqSdk 及其依赖。

分发版本 0.1.1，wheel SHA256：
`d7c1b7fa6a7e5f1e0beefda2f29e78d814d0b393e3f2bdacd44ab8beb91039ba`。
包内 __version__ 仍为 0.1.0，回执同时记录两者，没有据此掩盖分发版本。

```powershell
<隔离venv>/Scripts/python.exe tools/shinnyquant_probe.py --out data/cache/shinnyquant_probe/新目录
```

这是已完成的一次测试，不建立重复探测任务。新增脚本仅允许写入忽略的 data/cache；读保护范围先于 SDK 导入及网络请求。公开脱敏结果、计划、脚本和指纹，原始响应和 DataFrame 留本机。脚本语法及哈希检查通过；没有增加策略测试或以测试数量代替数据进展。执行和复核为 SINGLE_AGENT_SELF_CHECK。

本轮资源结论不变：早年新 M1 尚未取得，未批准新响应研究或模拟盘。后续需要实际历史权限和合格原件，不能靠更换同一后端的封装突破权限，也不能把重叠旧行情重新认作独立数据。
