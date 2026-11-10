# R7启动验收发现的监督竞态与R7.1修补

R7主文件421c7a5389aeda27589776e1034853e005c90017b1efd2fc2d38f95dbe0d83fa已由用户启动，实际宿主30848，run_id=20261110T081919336932。五品种ready=5，账户RECONCILED/空仓/无活动未知意图；WRITER32472、READ_ONLY22576各一，趋势19572、PA观察27432存活，五品种PA已生成独立输出。双源时钟约-0.11秒、连接和订阅正常。

启动未获“全部健康”验收：16:22起NOTIFICATION_HEALTH_STALE/UNKNOWN和随后恢复反复出现，pause持续保留。原发布的477项回归遗漏了监督方时钟与并发发布窗口；不能把离线通过写成运行缺陷已消失。新启动回执明确记录该阻塞。

## 已复现的两个条件

1. `FiveRunner.cycle/report`传入较早的`now`，监督方在后续读取子进程刚更新的`health.at`时，`now-health.at`为负。原`progress_problem`把正常新证明当成未来证明，产生STALE。修补改为读取完成后的真实`checked_at`判断；实际未来、超过30秒、错误PID、阶段卡死/失败仍拒绝，不使用abs(age)或宽限负年龄。
2. Windows `ReplaceFileW`内容替换兼容旧读句柄，但实际名字查找会有短暂FileNotFoundError(WinError2)。此前只重试PermissionError，故可能产生UNKNOWN。现场只读1191个样本捕获6次OPEN/FileNotFoundError，均未尝试同路径重试。修补将该短暂名字缺失纳入原3次/15ms总等待预算；持续缺失仍BLOCKED，不返回缓存证明、不更新旧样本时间、不延长账户/服务/报价3秒门槛。

只读组合探针使用新读取器访问仍在运行的R7通知进程，123个样本中旧周期时钟拒绝14次，读取完成时钟拒绝0次、读取错误0次。该探针不修改运行代码或账户，也不等于新宿主已加载修补。历史每一条UNKNOWN的当时错误未保存，不能把所有历史个例唯一归因这一次复现。

## 修补范围与防复发

- `paper_five.supervise_trend_notifier`保留`report_cycle_at/checked_at/proof_at/proof_age_seconds/pid/expected_pid/stage/stage_started_at/status/stage_failures`，最终读失败另留`file/stage/category/errno/winerror/attempts`。仅在故障转换时落盘，不按每个轮询刷异常。
- 监督真实工作进度、通知进程退出、未知投递不重发、原门槛和持久pause全部保留。健康恢复事件不清pause。
- `test_paper_supervision_clock.py`覆盖真实监督入口：周期后发布、真正未来、真正陈旧、错误PID、挂起阶段、失败阶段、未知读取详情、恢复仍停开及原子替换名字窗口。发布工具明确登记R7.1身份；原执行配置、PA参数、核心、交易协议均不变。
- 生产路径为通知实际发布→同路径共享读/有界重试→读取完成时钟→原PID/时效/阶段门槛→pause_entries及诊断文件。没有新增开仓路径、第二控制器或对退出的额外门槛。

R7.1仅在本机发布包和源码中准备；实际覆盖必须先由用户正常停止目标策略并验证全部所属组件退出、账户及未知委托/敞口可解释。覆盖保留最新交易账本、五检查点、PA状态和pause，再由用户启动验证。未安装/未加载不得用源码测试替代运行结果；自然订单和退出仍NOT_EVALUATED。
