"""Create local review artifacts from completed V6 results, no external publishing."""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest


def pct(x):
    return "—" if x is None else f"{100*x:.2f}%"


def number(x):
    return "—" if x is None else f"{x:.3f}"


def quartiles(value):
    return "/".join(number(value.get(k)) for k in ("q25","median","q75"))


def safe_label(value):
    fields=("label","complete","entry_bob","entry_eob","r_over_atr","mfe_r","mae_r","first_touch_eob",
        "first_touch_point","first_touch_after_session_break","entry_gap","cross_session","one_price_entry",
        "price_limit_status","entry_gap_from_signal_atr","known_touch_before_censor","reason")
    return {key:value[key] for key in fields if key in value}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--analysis-root",type=Path,required=True)
    p.add_argument("--engineering-root",type=Path,required=True)
    a=p.parse_args()
    summary=read(ROOT/"reports/v6_price_events_summary.json")
    assert summary["all_contracts_processed"],"do not produce full-pool report from a partial run"
    inventory=read(ROOT/"reports/v6_sample_inventory.json")
    manifest=read(a.data_root/"manifest.json")
    ops=read(a.analysis_root/"opportunities.json")
    controls={r["opportunity_id"]:r for r in read(a.analysis_root/"controls.json")}
    events=read(a.analysis_root/"events.json")
    assert len({x["opportunity_id"] for x in ops})==len(ops)
    public=[]
    for op in ops:
        control=controls.get(op["opportunity_id"])
        fields=("opportunity_id","csymbol","actual_contract","direction","path","evidence_id","trigger_bar_id","time",
            "trading_date","session","month","roll_status","episode","age","repeat_rows","matched",
            "trend_exit_decision","trend_exit_before_touch","trend_exit_same_time_as_touch")
        row={k:op[k] for k in fields}
        row["case_id"]=hashlib.sha256(op["opportunity_id"].encode()).hexdigest()[:12]
        row["atr_over_price"]=op["atr"]/op["close"]
        row["gate"]=op["gate"]
        row["labels"]={h:safe_label(value) for h,value in op["labels"].items()}
        row["trend_exit_reference_bob"]=None if op["trend_exit_reference"] is None else op["trend_exit_reference"]["bob"]
        row["control"]=None if control is None else dict(time=control["time"],age=control["age"],gate=control["gate"],
            atr_over_price=control["atr"]/control["close"],labels={h:safe_label(v) for h,v in control["labels"].items()})
        public.append(row)
    write(ROOT/"reports/v6_opportunity_pairs.json",dict(prices_redacted=True,events=public,
        bootstrap_trading_days=sorted({x["trading_date"] for symbol in manifest["products"] for x in
                                      read(a.data_root/(symbol+"_schedule.json"))}),
        full_bootstrap_calendar_source="dataset calendar: all 121 fixed trading days, including zero-pair days"))
    write(ROOT/"reports/v6_reference_legs.json",dict(prices_redacted=True,events=events))
    strata={}
    for field in ("csymbol","direction","path","session","month","roll_status"):
        strata[field]={}
        for value in sorted({o[field] for o in public}):
            cohort=[o for o in public if o[field]==value]
            pairs=[o for o in cohort if o["control"] is not None and o["labels"]["48"].get("complete") and o["control"]["labels"]["48"].get("complete")]
            modes={}
            for optimistic in (False,True):
                def target(label):
                    return label=="TARGET_FIRST" or (optimistic and label=="BOTH_SAME_M1")
                av=sum(target(o["labels"]["48"]["label"]) for o in pairs)
                bv=sum(target(o["control"]["labels"]["48"]["label"]) for o in pairs)
                modes["optimistic" if optimistic else "pessimistic"]=dict(signal_target=av,control_target=bv,
                    difference=(av-bv)/len(pairs) if pairs else None)
            strata[field][value]=dict(unique=len(cohort),feature_matches=sum(o["matched"] for o in cohort),
                complete_pairs=len(pairs),descriptive_only=len(pairs)<30,comparisons=modes)
    write(ROOT/"reports/v6_paired_strata.json",dict(strata=strata,no_subgroup_winner_selection=True))
    # Independent engineering run on the preregistered three-product slice.
    engineering=read(a.engineering_root/"opportunities.json")
    final_by_id={o["opportunity_id"]:o for o in ops}
    compared=0
    for earlier in engineering:
        current=final_by_id[earlier["opportunity_id"]]
        assert current["time"]==earlier["time"]
        for h in ("8","48"):
            for field in ("label","complete","entry_eob","r_over_atr","mfe_r","mae_r"):
                assert current["labels"][h].get(field)==earlier["labels"][h].get(field),(earlier["opportunity_id"],h,field)
        compared+=1
    cases=read(ROOT/"reports/v6_hand_checks.json")
    assert cases["real_case_count"]>=8 and all(x["passed"] for x in cases["independent_arithmetic_examples"])
    write(ROOT/"reports/v6_engineering_acceptance.json",dict(
        normal_core_parity=read(ROOT/"reports/v6_core_parity.json"),
        engineering_opportunities_reproduced=compared,hand_arithmetic_real_cases=cases["real_case_count"],
        all_case_checks_passed=True,full_pool_complete=True,
        registration_sha256=digest(ROOT/"tools/historical_strategy/registration.json"),
        final_summary_sha256=digest(ROOT/"reports/v6_price_events_summary.json")))
    lines=["# V6 历史趋势与 2R 机会验证报告","",
        "样本：2026-05-07 至 2026-11-02，共180个日历日、121个交易日。属于已经使用过的探索样本。",
        "结果为同一实际合约上的价格路径标签，不是账户交易胜率或净收益。历史映射发布时间及接收钟仍为 UNKNOWN。","",
        "## 数据基线与口径","",
        "采用前一已完成交易日主力映射代理选择当日合约，每个实际合约独立预热。信号及未来价格均为未复权同一腿，旧收盘价差复权不进入主分析。",
        "预热门槛为每实际合约1200根完整观察M15；历史交易小节采用已授权当前元数据代理，特殊历史夜盘休市尚未逐日核验。缺口按该保守时钟删失，不把代理缺分钟直接认定为供应商漏数。",
        "S0 保留修复后的闭合H1。锚点/止损为冻结规则：buffer 0.6 ATR、下限1.5 ATR、上限6 ATR、tick对齐；止损固定后用下一合法M1开盘重算R和2R。",
        "机会层使用零持仓、无成交上下文；账户手数、保证金和成交驱动冷却没有模拟。流动性与停板资格不作已证实承诺。","",
        "| 品种 | 可评分M15 | 首个合格时点 | 唯一证据 | 完整48根分母 | 目标先达 | 止损先达 | 双触 | 无触及 | 未解删失 | 开盘无效 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for csymbol,item in inventory["products"].items():
        tasks=[t for t in manifest["tasks"] if t["csymbol"]==csymbol]
        bars=sum(summary["funnels"].get(t["symbol"],{}).get("selected_complete_m15",0) for t in tasks)
        group=summary["by"]["csymbol"].get(csymbol,{}).get("summary",{})
        complete=group.get("complete_counts",{})
        labels=group.get("labels",{})
        lines.append(f"| {csymbol} | {bars} | {item['first_qualifying_eob']} | {group.get('unique',0)} | {group.get('complete_denominator',0)} | {complete.get('TARGET_FIRST',0)} | {complete.get('STOP_FIRST',0)} | {complete.get('BOTH_SAME_M1',0)} | {complete.get('NEITHER_BY_HORIZON',0)} | {labels.get('CENSORED',0)} | {labels.get('INVALID_AT_OPEN',0)} |")
    lines += ["","完整标签列之和等于完整48根分母；未解删失、开盘无效及截尾前已知首触分别列示，不能相加混用。完整起点随品种预热而异，不表示12品种均有180日可评分历史。","",
        "## 独立机会与配对主比较","",
        f"唯一机会 {summary['opportunities']}；趋势 episode {summary['episodes']}；剔除重复有效证据行 {summary['repeated_rows']}；匹配率 {pct(summary['matching_coverage'])}；主比较完整配对 {summary['complete_matched_pairs']}。",
        "对照同实际合约、方向、交易日、小节与动能状态，ATR/价格比例差不超过20%。按授权年龄差、时间差、较早时点排序，无放回；匹配前没有价格结果标签。","",
        "| 约定 | 完整配对 | 信号目标先达 | 对照目标先达 | 差值（百分点） | 5日块95%区间（百分点） | 10日块95%区间（百分点） |",
        "|---|---:|---:|---:|---:|---|---|"]
    for mode in ("pessimistic","optimistic"):
        r=summary["pair_comparison"][mode]
        ci=["—" if x is None else f"[{100*x['low']:.2f}, {100*x['high']:.2f}]" for x in r["intervals"]]
        lines.append(f"| {mode} | {r['n']} | {r['signal_target']}/{r['n']} ({pct(r['signal_target_fraction'])}) | {r['control_target']}/{r['n']} ({pct(r['control_target_fraction'])}) | {100*r['difference']:.2f} | {ci[0]} | {ci[1]} |")
    lines += ["","移动交易日分块在品种间使用相同日期块；5/10日块、10,000次、seed=20261104。双触最坏配对界、止损先达差、8根配对结果与未匹配分层见机器摘要。","",
        "主比较只覆盖可匹配且两边未来时钟完整的机会；未匹配和删失群体保留单列，不据配对子集外推全体。各品种及其他固定分层的配对计数见 v6_paired_strata.json。","",
        "| 队列/窗口 | 唯一机会 | 完整分母 | 目标先达下界—上界 | MFE/R Q25/中位/Q75 | MAE/R Q25/中位/Q75 | R/ATR Q25/中位/Q75 |",
        "|---|---:|---:|---|---:|---:|---:|"]
    for cohort in ("signals","controls"):
        for horizon in ("8","48"):
            r=summary[cohort][horizon]
            lines.append(f"| {cohort}/{horizon} | {r['unique']} | {r['complete_denominator']} | {pct(r['target_first_lower'])}—{pct(r['target_first_upper'])} | {quartiles(r['mfe_r'])} | {quartiles(r['mae_r'])} | {quartiles(r['r_over_atr'])} |")
    lines += ["","## 趋势授权后剩余空间","",
        "以下分母为本轮实际合约样本内、48根内再次反转且未来时钟完整的快速参考段；与旧613段不是同一数据身份。可行动测量时点沿用本段内连续两根同向的第二根收盘；参考当根已同向另列，不能把这些例子误说成系统首次授权发生得晚。","",
        "| 组 | 完整快速段 | 收盘极值前完成 | 已到/过极值 | 未完成两根确认 | 参考当根已同向 | 确认后8根顺向ATR中位 | 段内剩余盘中ATR中位 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for group,r in summary["fast_reference_legs"].items():
        lines.append(f"| {group} | {r['n']} | {r['before_extreme']} | {r['at_or_after_extreme']} | {r['no_pair']} | {r['already_authorized_at_reference']} | {number(r['actionable_space8_forward'].get('median'))} | {number(r['remaining_leg_intrabar_forward'].get('median'))} |")
    lines += ["","A仅为历史离场方案的时机参照，不是入场Path A；没有新增离场变体。EMA34只输出趋势，不生成入场候选，候选空值不参与T2入场比较。T1段内连续两根同向是诊断评分约定，T2仍按原核心当根确认状态评估入场，不额外增加一次确认；候选早于T1第二根不等于早于核心授权。旧公开613段复算保持S0/A/EMA34极值前282/292/430，原记录保留。","",
        "## 复核、分层和限制","",
        f"预定CF/EG/AU工程切片 {compared} 个机会的键、信号时刻、8/48根标签、R/ATR与MFE/MAE在全池复跑中逐项一致；正常核心链路另有130根M15、34行技术候选逐字段对照。实际逐分钟独立算术案例 {cases['real_case_count']} 类均通过，含长短、双触、换月附近、趋势提前退出、休市跳空、开盘无效与删失。",
        "补片身份另列：目标夜盘EG120+AG330=450分钟；重查还覆盖前一日日盘各225分钟，因此两个新腿相对旧缓存总新增900行。共有日盘各225行的OHLC、量、持仓修订均为0。目标夜盘影响使用仅移除450分钟、其他预热相同的对照，见patch_signal_impact。",
        "按品种、方向、路径、交易小节、月份、换月状态的全部计数与低于30机会的描述性标记保留在机器摘要。不按品种挑赢家。损益、费用、保证金与实际成交均未评价。","",
        "本地授权原始M1、含价格的逐信号明细及回放轨迹留在忽略缓存；评审附件仅发布派生标签、时间、无量纲距离和哈希。缺失历史时钟保持UNKNOWN。本轮不改变正式90日协议、主分支策略或下单开关。","",
        "## 评审附件","",
        "- reports/v6_price_events_summary.json：完整分母、配对区间、上下界、路径几何、趋势退出和固定分层。",
        "- reports/v6_opportunity_pairs.json：脱敏逐机会及配对标签。",
        "- reports/v6_reference_legs.json：三组逐段时点与剩余空间。",
        "- reports/v6_paired_strata.json：六类预先固定分层的配对计数。",
        "- reports/v6_hand_checks.json：独立逐分钟算术案例。",
        "- reports/v6_patch_identity.json / v6_patch_signal_impact.json：补片身份与传导。",
        "- reports/v6_execution_receipt.json / v6_engineering_acceptance.json：实际命令、退出码、代码及数据哈希。",
        "- docs/22_v6_策略裁决与下一轮单一瓶颈.md：研究裁决及停止条件。",""]
    auth=read(ROOT/"reports/v6_authorization_space.json")["summaries"]
    supplement=["## 实际新授权及风险空间诊断","",
        f"另按S0真实新方向状态转换计334个合格新授权episode，排除可评分段起点已经继承的方向。48根未来完整 {auth['48']['complete_future']} 个，其中 {auth['48']['future_3atr_reached']} 个顺向最大距离达到3 ATR，中位数 {number(auth['48']['mfe_atr'].get('median'))} ATR；8根完整 {auth['8']['complete_future']} 个，其中 {auth['8']['future_3atr_reached']} 个达到3 ATR。该诊断不把参考确认当根已同向误记成系统刚刚授权。",
        f"同episode的新授权与首次有效候选有 {auth['48']['paired_authorization_candidate_n']} 个共同完整48根窗口。两锚点各自的顺向空间中位数为 {number(auth['48']['paired_authorization_mfe_atr'].get('median'))}/{number(auth['48']['paired_candidate_mfe_atr'].get('median'))} ATR；候选延迟中位 {number(auth['48']['candidate_delay_m15'].get('median'))} 根M15。它不是M5合法前提的反事实回放，不能据此声称改频率可提升表现。",
        "完整48根信号中1972/3374曾给出至少3 ATR顺向幅度；289/3374达到3 ATR但没达到其结构2R，608/3374则先触止损、后来才出现2R幅度。R/ATR中位1.529，不能把主要问题笼统归因为结构止损普遍太宽。",
        "纯价格目标先达且趋势退出在之前的有63例；相应止损先达有286例。两者都是机会层时序标签，未测退出参考价的成交与损益。",
        "移除仅450个找回夜盘分钟、保留其他预热后，全池主差为−2.69个百分点（主数据−2.61），5/10日区间仍为负。两版有3773个共同机会键、各14个独有键。补片修正指标/状态/候选，但未扭转主结论。","",
        "更多逐episode证据见 reports/v6_authorization_space.json；补片完整敏感性见 reports/v6_recovery_sensitivity.json。",""]
    index=lines.index("## 评审附件")
    lines[index:index]=supplement
    (ROOT/"docs/21_v6_历史趋势与2R机会验证报告.md").write_text("\n".join(lines),encoding="utf8")
    print("LOCAL_REVIEW_ARTIFACTS_READY",len(public),len(events),flush=True)


if __name__=="__main__":
    main()
