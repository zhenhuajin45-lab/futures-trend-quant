# 自检：非生产发布

结论：隔离诊断可复算；不具备候选接管订单条件。

调用链：diagnose读取冻结清单→逐项SHA256校验→只读已观察M1→complete_bars与原build_entry_evidence→原evaluate_entries→单独evaluate_selected纯函数→派生文件。没有GM SDK调用或订单、账户配置写入。唯一新增副作用是指定新诊断目录写文件。交易宿主不导入此包；没有原生policy证明放行。`enabled`默认False，A/C/D返回原对象。

参数和字段：原direction、close_side、slope、hist、opposite_divergence、closed_h1_known、calendar_qualified、previous_weak、changed、eob完整传给原权限。B试验只重新评估新开条件，保留原技术退出、反手、weak计数及原始hist。None与非有限hist不能因符号豁免而获资格。只读矩阵不能传播为执行证明。

复发预防：禁止把`first_observed_in_scope`、`path_trigger_eob`或原`first_eligible_at`相互替代。真正首次成立未知时写UNKNOWN，不刷新`expires_at`。原始证据没有episode身份时`same_process=UNKNOWN`，不是自动PASS。确认柱极值越过仅作描述，不暗加失效参数。缺账户或报价时不能从静态允许反推出可下单。

新增镜像测试、B其余条件拒绝、A/C/D逐字段一致、None和缺H1／日历拒绝、未知hist拒绝及原退出保留均通过。真实PaperEngine假通道测试追至最终平仓意图、回报、快照归零，区别本地义务与真实发送。未知提交、部分成交、迟到回报与重启沿用原回归。

生产源码、实际main指纹、账户／策略配置及前轮报告未变。没有部署或热覆盖；没有声称真实报价、实际止损时延或盈利已经验收。恢复运行及发布是后续受控动作，本包没有可执行启动脚本。
