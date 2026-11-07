# 长查询阻塞：定位、隔离原型和未决边界

真实调用链：PaperEngine -> SimTransport队列 -> 唯一循环线程stdin写请求 -> stdout.readline匹配回执 -> paper_sim_bridge.snapshot串行get_cash/positions/orders/unfinished/execution_reports -> gmtrade._biglock内py_gmi同步原生调用。

已安装gmtrade Python包装未提供这些查询的逐次timeout参数；具体哪一个原生子调用耗时72秒，旧日志没有逐调用时钟，保持UNKNOWN。当前hung()仅3秒软判定，无原生取消、杀子进程或释放通道。新测试直接运行真实SimTransport._loop与阻塞假管道：软阈值到达仍busy；迟到回执未应用仍busy；应用后才释放。

可运行研究原型：RpcLease将调用超时、请求停止、进程退出证明、回执、订单未知、对账与人工写入者交接分开。stop_owned_read_worker只允许显式自有READ_ONLY+snapshot子进程；真实受控假子进程测试证明wait/poll退出。该原型没有接生产，不能拿它杀掉现有混合读写bridge，也不启动替代写单控制器。

建议的运输改造边界：把周期性历史只读汇总与唯一写单者分离；读子进程在硬超时后必须确认自身终止，允许丢弃查询结果，但不能把提交/撤单UNKNOWN当查询重试。写单者仍需最终持仓、可平和报价检查，原生服务端不可用仍可能阻塞；分离读进程不承诺真实止损成交时延。旧SDK跨进程连接行为和服务端限制未经核验，故本批不部署该架构。

完整恢复模型时间线见rpc_timeout_recovery_model.csv：退出义务第1秒产生，第3秒调用超时，第5秒仅请求停但租约未释放；72秒迟到回执后查询可重建快照，提交/撤单仍须定位原订单，禁止重发；示例第75秒确认平仓，暴露累计74秒。75秒为合成场景给定，不是真实成交承诺。实际夜盘空仓不能测得真实有仓暴露时长。

UNKNOWN_DELIVERY保持历史未知；连接恢复不清pause_new。此项完成的是阻塞层定位、可运行监督原型与故障回归，生产72秒阻塞尚未宣称解决。
