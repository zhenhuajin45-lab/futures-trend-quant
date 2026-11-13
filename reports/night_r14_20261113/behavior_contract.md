# R14 行为合同与迁移范围

复盘N01—N10逐项登记，已知缺陷修复与外部阻塞分开。不承诺凭缺失旧Tick原件确定历史根因。

- GM_NOTICE_COMMITTED_INPUT_V1_20261113：GM提醒读与主核心相同的canonical缓存、SHA和checkpoint绑定的journal精确前缀。历史asof仅使用source_known_at和recorded_at均已到达的提交分钟；无真实时钟保持UNKNOWN。旧非canonical配置保持旧协议，TQ仍独立。
- QUOTE_HANDOFF_EVIDENCE_V1_20261113：不改事件顺序和优先级；采样留存原incoming/previous时钟和book、source/run/config、拒绝或源切换与抑制数量。每symbol/source/outcome最多每5秒一条，源切换每symbol额外最多每秒一条，并记录抑制计数。不把旧原生Tick强行覆盖新报价。
- NTP_RAW_SAMPLE_IDENTITY_V1_20261113：增加每源客户端/服务端真实时钟、解析IP、原packet哈希和失败阶段/耗时。0.5/0.1/1/90门及persist pause不变。不改系统时间，不复用R13已消费恢复授权。
- HISTORY输入原样传播source；observer携带真实返回/回调接收时钟，computed_at保留生产完成点；未知不填bar_eob/mtime/回放now。
- 合并拒绝补product_ready/coverage/frozen/fault/engine/anchor子条件，关联evidence_id和候选中间态，不宣称已提交。

主核心903a381b不变，PAPER_ENTRY_V5/PAPER_EXEC_V5及30秒原截止、3秒账户/报价/服务、风险/止损2R保持。PA及背驰替代仍SHADOW。配置原字节与全部状态/账本/墓碑/检查点保留；仅已停止且真实账户可解释才能覆盖，不代启停。

N03历史根因、N04供应商真实闭合延迟、N08自然提交成交退出、N10策略改进不靠修改门槛宣称解决；必须独立记录待真实样本或策略研究。本次源码与单文件包、回归、自检、实际双Python、production-code-review、可回滚回执分别完成。Git发布、安装、加载、自然交易验收分开登记。
