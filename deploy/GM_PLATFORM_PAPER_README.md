# 掘金客户端策略文件：五品种模拟观察

标准策略文件为 [gm_paper_platform_main.py](gm_paper_platform_main.py)，复制到客户端新建的 Python 策略目录并命名 `main.py`。策略名称为“期货S0_五品种模拟观察_平台入口”；真实策略 ID 由本机客户端创建，不由代码随机生成。主文件保留 `gm.api.run()`、`init(context)`、定时回调及 `on_error/on_shutdown`，不在 `init` 中进入无限循环。

## 运行方式和账户

主文件默认引用 `E:\掘金\期货-模拟五品种` 的项目模块，亦可在本机设置 `FUTURES_TREND_QUANT_ROOT`。`GM_PAPER_CONFIG` 默认指向该目录 `config/paper_observation_five.local.json`。账户、基础策略和风控指纹仍由该私有配置验证，保持原五品种、每笔 1 手及组合 2 仓上限。不得以更换启动文件为由新建空白账本或重复联调。

启动参数优先采用客户端标准 `--strategy_id/--token`，其次使用本机环境；客户端目录同级 `paper_platform.local.json` 可登记真实 `strategy_id`，项目 `config/paper_platform_auth.local.json` 可保存本机 Token。两者不上 GitHub。主文件将同一 Token 传给隔离行情和交易进程，不改全局账户、不回退默认账户。

`MODE_LIVE` 在 GM SDK 中表示实时事件模式，不能单独证明是模拟账户。本实现还强制校验既有专用期货模拟账户证明及固定官方仿真端点 `api.myquant.cn:9000`。因为本机 `gm.api.get_orders/get_unfinished_orders/get_execution_reports` 没有 account 参数，保留已验证的官方 `gmtrade` 隔离适配器作显式账户查询及开平撤；没有把这组接口伪装成原生策略订单回调，也不调用默认 `context.account()` 发单。

可在客户端打开 `main.py` 按实时模式运行，或在该策略目录使用本机 GM Python 执行：

```powershell
python main.py
```

该文件是本地模块化策略入口，依赖项目代码和隔离交易环境，不是脱离项目可独立复制的一段单文件策略。

## 生命周期

`init` 核验实际运行模式、配置、认证和账户互斥锁，恢复原五品种状态；随后调用官方 `timer(timer_func=on_timer, period=200, start_delay=200)`。周期单位按接口毫秒契约，实际间隔以本机证据为准。`FiveRunner.cycle()` 为一次有界执行周期，CLI 的 `run()` 也调用同一函数。行情 API、core 预热、官方模拟 RPC 和飞书 HTTP 均在既有隔离进程，定时回调不重复实现交易规则。启动初始化耗时与进入回调后的循环间隔分开记录。

原 CLI 正在控制同一账户时，平台入口会在创建交易桥之前被账户互斥锁拒绝。应核对柜台与本地持仓、活动及不明委托后完成受控交接，禁止同时运行两个入口。源配置、账本、已用证据、原始联调、通知去重和初始资金基准全部沿用。

`GM_PAPER_HOST_READONLY=1` 用于独立只读预检目录，不能下单。只接受字符串 `0` 或 `1`，错误值拒绝启动。若只读预检完成，停止其定时器并保留回执；只读进程不应长期占用资源，验证后按进程身份结束。不能把只读模式误当成正式观察。

`on_error` 留下错误码并停新开，不记录可能含隐私的原始错误文本。`on_shutdown` 停开、落盘并记录柜台是否新鲜且确认空仓，再释放本入口创建的子进程。**点击停止不等于平仓，也不会凭空把敞口归零。** 有持仓时先通过既有控制文件请求停开及受控清仓，柜台确认后再停止策略；强行关闭进程、客户端或电脑会中断本地保护，余仓仍需柜台处理。停止产生的停开状态不会悄悄清除。

## 验证和证据

`tests/test_gm_paper_host.py` 覆盖模式拒绝、私有启动参数、Token 一致传递、重复启动与账户锁、定时器失败、只读完成、重入、运行故障、错误文本脱敏、停机与未知敞口，以及真实 `FiveRunner.cycle` 只读路径。其余五品种执行、桥接、恢复、预热、通知测试保持。宿主文件及客户端入口 LF 归一化摘要写入私有 `gm_platform_host.local.json`，与原执行模块指纹共同留痕。

客户端标准入口真实连接服务、定时器实际驱动、五品种状态恢复及账户对账分别验收，不能用语法通过替代真实启动。未制造新的测试交易；自然成交仍等待策略信号。

参考：[官方基础运行及定时器规范](https://emt.18.cn/api/quant-help/python/python_basic.html)、[官方仿真说明](https://sim.myquant.cn/sim/help/)。接口签名与回调加载同时核对本机 `gm==3.0.186` 实现。费用、市场冲击和盈利边界沿用模拟观察说明，未升级 SDK、未开实盘、未运行独立评分。
