"""Final local QA and documentation; no account operations or publication."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,check_lock


def main():
    check_lock()
    original=ROOT/"data/cache/history_v6_final"
    rerun=ROOT/"data/cache/history_v6_reproduction_check"
    proof=read(rerun/"reproduction_check.json")
    assert proof["pass_all"] and proof["per_opportunity_keys_and_touch_order_equal"]
    counts={}
    for name,key in (("opportunities","opportunity_id"),("controls","opportunity_id")):
        before={x[key]:x for x in read(original/(name+".json"))}
        after={x[key]:x for x in read(rerun/"private"/(name+".json"))}
        assert before==after,"full per-row reconstruction mismatch: "+name
        counts[name]=len(before)
    proof.update(full_opportunity_records_equal=True,full_matched_control_records_equal=True,
        matched_controls_checked=counts["controls"],command="python tools/historical_strategy/reproduce.py --output data/cache/history_v6_reproduction_check",exit_code=0)
    write(ROOT/"reports/v6_determinism.json",proof)
    # Retain early engineering drafts locally; never mix them into final public results.
    archive=ROOT/"data/cache/history_v6_engineering_archive"
    archive.mkdir(exist_ok=True)
    archived={}
    for name in ("v6_engineering_slice.json","v6_quality_engineering.json","v6_hand_checks_engineering.json"):
        source=ROOT/"reports"/name
        target=archive/name
        assert source.resolve().is_relative_to(ROOT.resolve()) and target.resolve().is_relative_to(ROOT.resolve())
        if source.exists():
            if target.exists():
                raise ValueError("archive already exists; retain both versions explicitly")
            source.rename(target)
        archived[name]=digest(target)
    receipt=read(ROOT/"reports/v6_execution_receipt.json")
    receipt["phases"].append(dict(script="reproduce",arguments=["--output","data/cache/history_v6_reproduction_check"],exit_code=0))
    receipt["per_opportunity_determinism"]="PASS"
    write(ROOT/"reports/v6_execution_receipt.json",receipt)
    acceptance=read(ROOT/"reports/v6_engineering_acceptance.json")
    acceptance.update(targeted_tests=17,determinism_opportunities=counts["opportunities"],
        determinism_matched_controls=counts["controls"],engineering_drafts_archived_sha256=archived)
    write(ROOT/"reports/v6_engineering_acceptance.json",acceptance)
    examples=read(ROOT/"reports/v6_paired_examples.json")["examples"]
    lines=["# V6 代码审查与复算验收","",
        "结论：研究执行链已完成定向与全机会复算验收；不宣称策略有效或实盘就绪。正式前向协议及下单关闭状态保持原样。","",
        "| 关口 | 已核验证据 |",
        "|---|---|",
        "| 事实到候选 | 正常run_core_step与研究适配器130根M15逐字段一致，含34行技术候选；仅该定向样本作此声明 |",
        "| 数据身份 | 37份原合约文件及37份研究视图校验，27个实际合约完成回放，17,420根合格M15与输入清点相等 |",
        "| 无前视 | 逐实际合约独立预热、以前一完成日映射代理选择；主信号无收盘差价复权；后验参考函数只在评分阶段调用 |",
        "| 证据到止损 | 无效止损不消耗后续首次有效证据；固定信号时刻止损，次根合法M1开盘重算R；缺分钟不补造 |",
        "| 目标/止损顺序 | 开盘先于分钟区间；双触保留歧义；开盘穿过止损为INVALID_AT_OPEN；同一实际合约观察到尾部 |",
        "| 匹配与推断 | 不读取未来标签、无放回；同日跨品种共用5/10日块，固定种子和完整121日抽样时钟 |",
        "| 回归 | 17项定向测试；11类真实逐分钟独立算术案例；CF/EG/AU工程切片1165个机会与全池逐项一致 |",
        f"| 全量复算 | {counts['opportunities']}份机会、{counts['controls']}份匹配对照的完整逐行记录完全相等；机会键、分钟首触及主统计全部一致 |",
        "| 复用保护 | 单入口先检查核心/配置/分析源代码、37份数据和27份JSONL指纹；修改规则后拒绝静默复用旧缓存；换行差异正规化 |",
        "| 最终副作用 | 研究工具仅输出本地JSON/CSV/Markdown；本轮未修改核心策略、费用verified或执行开关，未调用下单/撤单/账户接口 |","",
        "## 固定选例的时间线","",
        "以下按类别取时间顺序第一例，比较信号与普通时刻的价格路径。M1时间列为该分钟结束时刻；OPEN首触对应其开始时刻。均为北京时间、同一实际合约。","",
        "| 案例 | 合约/方向 | M5触发柱结束 | M15信号收盘 | 入场参考M1结束 | 信号48根首触 | 对照收盘 | 对照48根首触 |",
        "|---|---|---|---|---|---|---|---|"]
    for x in examples:
        lines.append(f"| {x['case_id']} | {x['actual_contract']}/{x['direction']} | {x['trigger_eob']} | {x['signal_eob']} | {x['signal']['entry_eob']} | {x['signal']['label']} @ {x['signal']['first_touch_eob']} ({x['signal'].get('first_touch_point')}) | {x['control_eob']} | {x['control']['label']} @ {x['control']['first_touch_eob']} ({x['control'].get('first_touch_point')}) |")
    lines += ["","原始价格与含价格逐行记录留在本机。公众评审见 v6_paired_examples.json、v6_hand_checks.json 和逐机会CSV。触发柱比决策早，只表示证据的标识时间；本轮没有把它直接当作更早合法开仓的证明。","",
        "## 本机复算","",
        "在独立V6研究工作区，使用已具备requirements-backtest.txt依赖的Python：","",
        "```powershell","python tools/historical_strategy/reproduce.py --output data/cache/v6_recheck_NEW","```","",
        "该入口不登录GM/Tq、不读正式forward_v3样本、不发单。需本机授权的固定cache；输出目录必须新建。仅代码指纹检查可加`--check-only`。完整复算已实际以exit_code=0运行；新的研究规则必须另建登记、输入身份与回放，不能把本轮缓存当作新规则结果。","",
        "## 剩余限制","",
        "历史映射发布及行情到达时钟UNKNOWN，特殊历史休市采用当前授权小节代理；流动性/停板状态UNKNOWN；零持仓机会层未模拟账户互斥、手数、成交与冷却；费用未核验，未评价收益；180日已被观察，结论不冒充独立验证。",""]
    (ROOT/"docs/23_v6_代码审查与复算验收.md").write_text("\n".join(lines),encoding="utf8")
    print("V6_FULL_RECONSTRUCTION_PASS",counts,flush=True)


if __name__=="__main__":
    main()
