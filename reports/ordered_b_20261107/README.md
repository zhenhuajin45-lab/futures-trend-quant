# 有序B研究复现入口

研究版本：ORDERED_B_EPISODE_V1。原无序B保留为基线，MACD豁免关闭。本目录contract.json在本轮原件输出前冻结，规则含新增生命周期意义，不能称纯bugfix。

1. `python -X utf8 -m tools.entry_compatibility.diagnose --batch evidence/night_complete_20261106_20261107_r1 --output <新目录>`：原件条件矩阵与修正三态字段；旧公开目录不覆盖。
2. `python -X utf8 -m tools.ordered_b.replay --batch evidence/night_complete_20261106_20261107_r1 --output <新目录> --product DCE.M`：每条实际合约的共同因果预热、分叉后独立core与事件簿；可分别运行五品种，无SDK，无账户写入。
3. `python -X utf8 -m tools.ordered_b.aggregate --output reports/ordered_b_20261107`：聚合本轮指定五个私有分品种输出，按完整M5过程重建案例并核验原基线。各分品种完整状态存放本机evidence，未公开。

`state_trace_*`是闭合柱core状态轨迹，不是接收时延回放或成交账本。周五禁开导致账户均保持零开仓，不能伪造反馈差异。报价／止损参考审计仍受新候选处理时钟未知限制。

`rpc_timeout_recovery_model.csv`是合成监督模型，`rpc_layer_audit.json`为源码层定位。`read_worker_supervisor.py`仅能停止调用者显式自有、只读snapshot假子进程；没有接生产混合读写桥，不自动创建写单进程。

154项定向测试通过；新增重启测试发现并修正了研究序列浅拷贝问题。生产源码、账户配置、单手/组合及0.25%/1%/5%/20%限制、D+1和休市清仓均未改动。
