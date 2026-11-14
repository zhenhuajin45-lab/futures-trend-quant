# R15 production-code-review

本次单代理生产自审通过软件门；不声称独立双人审查，客户端尚未覆盖/加载/恢复，天然委托成交退出待验。

真实路径一：FiveRunner以新配置显式创建clock_process→BoundedClockCollector→每源唯一daemon槽→包身份校验probe→原measurement共识合同→retain_bounded_sample→原样本JSON→账户和原生最终health。周期ID阻断迟到结果跨轮拼接；8秒预算只约束采集等待，不给旧样本重写at或monotonic。DNS线程不可取消时留在原槽，不无界建线程；持续不可用仍停开。旧采集policy在新配置下被最终门拒绝。真实Python313和310 spawn已运行同一clock_process及health（合成probe、无SDK/网络）；实际公共NTP另为短窗只读诊断，不是生产证明。

真实路径二：SignalWorker接受原闭合分钟/提交journal→同一24000窗口→完整桶缓存→同一CoreStepInput/run_core_step与runtime→CORE_RESULT及原始期限→PaperEngine.open→原账户/服务/报价/信号门→WAL先持久化→合成transport→原生最终guard。跨日/周末/窗口滚动/缺口/重启/修订及十品种双向镜像验证。真实SignalWorker测试已调用新缓存并完成CORE_READY，分段时钟到达最终result。新缓存不入checkpoint；重启重建，不迁移历史bar或改变核心/持仓合同。

历史输入：五份冻结canonical seed哈希核验，昨夜810闭合分钟逐根旧新完整M5/M15相等。各品种最新生产形状输入在同一fresh state-book下经实际核心及状态比较一致；该smoke不是重建昨夜全部54步的完整状态轨迹。聚合性能是本机离线重复工作负载对比，不能换算自然订单收益或断言所有尾段在30秒内完成。供应商/SDK晚到依旧可能过期。

解释：原background gate兼容保留并标scope，B路径明确MACD仅提示，路径许可取entry_permission，执行终态仍取order_events。H1解释的known_at不再把逻辑桶结束冒作真实接收证明；缺全部成员接收证据保持UNKNOWN。核心逻辑known_at及入场算法不改，未声称彻底重建旧历史时钟。

最末门：900相关回归、42项收尾（重叠）及1项新增真实worker通路；原生310合成SDK最终场景；278源码实际Py310编译、Py313嵌入自检与真实spawn。实际entry→WAL→合成transport通过；pause/旧collector/stale clock/service/quote/原30秒均拒绝。挂单/风险/专用SIM/唯一writer/账户对账/2R/退出策略由既有相关及原生场景覆盖，未调用真实柜台或伪造自然样本。

发布/保护：只在原分支发布脱敏源码和报告，私有包/SDK/配置/原件不上传。R15只新增两个工程配置键，核心指纹不变；周五禁开按用户明确选择保留，PA/TQ策略不升级。安装程序重新验证停止及新鲜空仓/未知意图，配置身份迁移之外保护/账本/检查点/候选/墓碑不动；安装不触发resume，不复用R13令牌。实际停机后才捕获state_at_install，之前只准备源码/配置回滚；新run或新事实后拒绝自动回滚。

残余：网络持续不可用、尾柱真实晚到、周五禁开/既有pause仍可合法阻止新开。R15未获新恢复授权；必须用户正常停止后覆盖、本人启动实际加载，再按新授权及新鲜门槛完成一次恢复。无自然样本不能声称成交/止损退出通过。TQ19点退出的具体异常消息仍未知，本轮未声称修复。
