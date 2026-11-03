"""Fail-closed paired audit of repaired entry gates on the frozen old market."""

import argparse
from collections import Counter
from datetime import date, datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path

from futures_trend_quant.core.models import Bar, TrendState
from futures_trend_quant.core.trend.state_machine import TrendFacts
from .exit_rule_experiment import _score_product, _pool_group_score
from .exit_rule_variants import step_experiment_trend
from .h1_impact_replay import GROUPS, BASE_MANIFEST, BASE_CONFIG

ARCHIVE = Path(__file__).resolve().parents[1] / "reports"
THREE = ("direction_agreement", "trend_segment_coverage", "range_switches_per_product_day")


def metrics(bar, events):
    return {**{k: bar[k] for k in THREE}, "turn_delay_median": events["turn_delay_median"]}


def trend_facts(row):
    inputs = dict(row["trend_inputs"])
    inputs["h1_state"] = inputs.pop("h1_relative")
    return TrendFacts(bar_id=row["bar_id"], slope_state=row["slope_state"],
                      top_divergence=row["top_div"], bottom_divergence=row["bottom_div"],
                      close=row["close"], high=row["high"], low=row["low"], **inputs)


def candidate_diffs(rows):
    c = Counter()
    transitions = Counter()
    examples = []
    for r in rows:
        new = r["entry_evaluation"]
        c["complete_rows"] += 1
        if new is None:
            continue
        old = new["legacy_h1"]
        prior, after = new["prior_state"], new["trend_after"]
        flip = {prior, after} == {"bull", "bear"}
        if new["entry_direction"] != after:
            raise ValueError(f"unexpected direction handoff at {r['bar_id']}")
        source = new["h1_source_eob"]
        if new["h1_absolute"] is not None and source is None:
            raise ValueError(f"missing completed H1 source at {r['bar_id']}")
        if source is not None and datetime.fromisoformat(source) > datetime.fromisoformat(r["eob"]):
            raise ValueError(f"future H1 source at {r['bar_id']}")
        c["entry_h1_unknown"] += int(new["h1_absolute"] is None)
        c["entry_completed_h1_differs_from_trend_h1"] += int(new["h1_absolute"] != r["h1_absolute"])
        c["authorized_rows"] += 1
        c["direct_flip_rows"] += int(flip)
        for version, entry in (("old", old), ("new", new)):
            c[f"{version}_accepted"] += int(entry["accepted"])
        old_path = old["path"] if old["accepted"] else "-"
        new_path = new["path"] if new["accepted"] else "-"
        old_evidence = old["evidence_id"] if old["accepted"] else None
        new_evidence = new["evidence_id"] if new["accepted"] else None
        if (old_path, old_evidence) != (new_path, new_evidence):
            c["selected_or_evidence_changed"] += 1
            c["changed_on_direct_flip"] += int(flip)
            if len(examples) < 4:
                examples.append({k: new[k] for k in ("entry_direction", "prior_state", "trend_after", "h1_absolute", "h1_source_eob",
                                "path_b_h1_reason", "path_d_h1_reason")} | {
                    "eob": r["eob"], "old_reason": old["reason"], "new_reason": new["reason"],
                    "from": old_path, "to": new_path})
        transitions[f"{old_path}->{new_path}"] += 1
        for path in ("B", "D"):
            for version, entry in (("old", old), ("new", new)):
                c[f"{path}_{version}_formed"] += int(path in entry["formed_paths"])
                c[f"{path}_{version}_accepted"] += int(entry["accepted"] and entry["path"] == path)
            before = path in old["formed_paths"]
            now = path in new["formed_paths"]
            c[f"{path}_formed_gained"] += int(now and not before)
            c[f"{path}_formed_lost"] += int(before and not now)
            c[f"{path}_formed_changed_on_flip"] += int(flip and before != now)
            if old_path != new_path and path in (old_path, new_path):
                key = ("appeared" if old_path == "-" else "disappeared" if new_path == "-" else
                       "reselected_to_other" if old_path == path else "reselected_from_other")
                c[f"{path}_{key}"] += 1
                c[f"{path}_selected_changed"] += 1
                c[f"{path}_selected_changed_on_flip"] += int(flip)
    return c, transitions, examples


def summarize(root, local):
    baseline_path = root/"baseline_facts.jsonl"
    if sha256(baseline_path.read_bytes()).hexdigest() != "cdf059b24401722a58f0b05b59ef61990c58242fa1a00f42bf64b34ba6322499":
        raise ValueError("historical S0 facts changed")
    baseline = {}
    with baseline_path.open(encoding="utf8") as f:
        for line in f:
            row = json.loads(line)
            baseline[row["bar_id"]] = row
    archive = json.loads((ARCHIVE/"sprint2_exit_rule_experiment_summary.json").read_text(encoding="utf8"))
    refs = json.loads((ARCHIVE/"sprint2_p0b_reference_comparison.json").read_text(encoding="utf8"))
    products = sorted(archive["groups"]["S0"]["per_product"])
    output = {"study": "HISTORICAL_ENGINEERING_AUDIT", "base": "52301f128c420d2a1e7bc8d118ec892f396c10f6",
              "config_sha256": BASE_CONFIG, "manifest_sha256": BASE_MANIFEST, "groups": {}}
    scores = {g: {} for g in GROUPS}
    timelines = {}
    for group in GROUPS:
        output["groups"][group] = {"by_product": {}, "counts": Counter(), "accepted_transitions": Counter()}
    for symbol in products:
        with gzip.open(local/(symbol + ".jsonl.gz"), "rt", encoding="utf8") as stream:
            records = [json.loads(line) for line in stream]
        bars = [Bar(**{**r["bar"], **{key: datetime.fromisoformat(r["bar"][key]) for key in ("bob","eob")}})
                for r in records]
        dates = [date.fromisoformat(r["date"]) for r in records]
        timelines[symbol] = {"bars": bars, "trading_dates": dates}
        for group in GROUPS:
            pub = archive["groups"][group]["per_product"][symbol]
            rows = [r[group] for r in records]
            # Replay the frozen trend transition separately from the entry evaluator. Compare the
            # entire decision, including leave/establish votes and persisted state, at every key.
            historical = TrendState()
            digest = sha256()
            for row, bar in zip(rows, bars, strict=True):
                decision = step_experiment_trend(historical, trend_facts(row), group=group, veto_enabled=True)
                if row["bar_id"] != bar.identity or row["trend_decision"] != decision.audit():
                    raise ValueError(f"trend delta {group} at {bar.identity}")
                if group == "S0":
                    old = baseline.pop(bar.identity)
                    if any(row[k] != value for k, value in old.items()):
                        raise ValueError(f"historical fact delta at {bar.identity}")
                historical = decision.state
                digest.update(json.dumps([bar.identity, decision.audit()], sort_keys=True).encode())
            score = _score_product(symbol, rows, bars, dates, refs["per_product"][symbol]["independent_turn_events"]["events"])
            m = metrics(score["bar_metrics"], score["independent_events"])
            if m != metrics(pub["bar_metrics"], pub["independent_events"]):
                raise ValueError(f"trend metric delta {group}/{symbol}")
            scores[group][symbol] = score
            counts, transitions, examples = candidate_diffs(rows)
            if counts["old_accepted"] != pub["entry"]["accepted_path_candidates"]:
                raise ValueError(f"legacy accepted candidate count mismatch {group}/{symbol}")
            summary = output["groups"][group]
            summary["counts"].update(counts)
            summary["accepted_transitions"].update(transitions)
            summary["by_product"][symbol] = {"counts": counts, "transitions": transitions, "examples": examples,
                    "trend_identity_state_and_votes_sha256": digest.hexdigest(), "unchanged_four_metrics": m}
    if baseline:
        raise ValueError("missing S0 identities")
    for group in GROUPS:
        summary = output["groups"][group]
        counts = summary["counts"]
        pool = _pool_group_score(scores[group], timelines)
        actual = metrics(pool["bar_weighted"], pool["independent_events"])
        old = archive["groups"][group]["pool"]
        if actual != metrics(old["bar_weighted"], old["independent_events"]):
            raise ValueError(f"pool metric delta for {group}")
        summary["unchanged_four_metrics"] = actual
        summary["state_identity_vote_mismatches"] = 0
        summary["changed_on_direct_flip_share"] = counts["changed_on_direct_flip"] / max(counts["selected_or_evidence_changed"], 1)
        summary["path_selected_changes"] = {
            path: {k: counts[f"{path}_{k}"] for k in ("appeared","disappeared","reselected_to_other",
                   "reselected_from_other","selected_changed","selected_changed_on_flip")} for path in ("B","D")}
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--local-output", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--table", type=Path)
    args = p.parse_args()
    report = summarize(args.data_root, args.local_output)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf8")
    if args.table:
        lines = [
            "# 入场 H1 工程配对核查",
            "",
            "2026-05-07 至 2026-11-02。每个品种使用相同的已闭合行情推进 S0 和历史离场 A，"
            "在同一份方向证据和锚点上并列评价两种 H1 入场口径。旧版传入以翻转前趋势计算的相对 H1，"
            "修复版使用最后一个已完成 H1 桶，按确认后的入场方向解释。表中每行计数以单根 M15 为单位，"
            "回放未注入持仓和成交，表中计数未按交易去重。",
            "",
            "| 组 | 接受行（旧→新） | 改变选中结果 | 其中直接翻转当根 |",
            "|---|---:|---:|---:|",
        ]
        for g, item in report["groups"].items():
            c = item["counts"]
            lines.append(f"| {g} | {c['old_accepted']} → {c['new_accepted']} | "
                         f"{c['selected_or_evidence_changed']} | {c['changed_on_direct_flip']} "
                         f"({item['changed_on_direct_flip_share']:.2%}) |")
        lines.extend(["", "| 组 / 入场路径 | 满足形态及路径条件（旧→新） | 形成增加 / 消失 | "
                      "最终接受（旧→新） | 空→本路径 | 本路径→空 | 本路径→其他 | 其他→本路径 | "
                      "涉及该路径的变更中，直接翻转当根 |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for g, item in report["groups"].items():
            for path in ("B", "D"):
                c = item["counts"]
                delta = item["path_selected_changes"][path]
                n, total = delta["selected_changed_on_flip"], delta["selected_changed"]
                rate = f"{n/total:.2%}" if total else "—"
                lines.append(f"| {g} / {path} | {c[path+'_old_formed']} → {c[path+'_new_formed']} | "
                             f"{c[path+'_formed_gained']} / {c[path+'_formed_lost']} | "
                             f"{c[path+'_old_accepted']} → {c[path+'_new_accepted']} | "
                             f"{delta['appeared']} | {delta['disappeared']} | {delta['reselected_to_other']} | "
                             f"{delta['reselected_from_other']} | {n}/{total} ({rate}) |")
        lines.extend(["", "直接翻转指 bull→bear 或 bear→bull。分母为涉及该路径的最终接受结果变更行。"
                      "“本路径→其他”与另一条路径的“其他→本路径”可能描述同一根 bar。"
                      "候选形成在四路径择优之前统计，最终接受含锚点检查。",
                      "", "两组各自逐根通过趋势身份、完整状态和票数重建校验，"
                      "S0 另与归档的 29,534 行完整趋势事实逐项一致。"
                      "两个组的四项趋势指标均与固定提交 52301f1 的逐品种和全池记录一致。"
                      "完整路径转移矩阵、各品种计数、前几个变更的方向和拒绝原因见机器报告。",
                      "", "## 本次运行与复现", "",
                      "本次测量分两步。首先从授权 M1 逐根回放，保存两组的独立趋势状态、旧入场评估及方向修正后的临时入场评估。"
                      "随后从相同 M1 重新聚合 M5/M15，逐根在原滑窗调用最终版 finalized_entry_h1。只要 B 或 D 的门控结果变化，"
                      "就用原始 M1/M5 重建证据，先核对临时结果中的已选路径、证据和拒绝原因，再调用最终入场函数。"
                      "门控结果相同的行沿用该行的入场决策，更新 H1 分类和来源时间，并定期抽查重建结果。", "",
                      "另外取棉花前 10,000 根 M1，以最终版一次性冷启动完整 run_prepared 回放。"
                      "S0 和 A 各有 416 根可输出 M15，两种生成方法的全部输出字段逐项相同。"
                      "阶段转换使用的临时脚本和原始行情只保存在本机，公开复现入口直接按最终源码运行，无需转换旧临时缓存。",
                      "", "复现命令（仓库根目录运行，原始与中间行情存放在本机）。",
                      "", "```powershell",
                      "python -m backtest.h1_impact_replay --data-root data/cache/gm_rolling_180d "
                      "--local-output data/cache/h1_audit_v3 --workers 6",
                      "python -m backtest.h1_impact_report --data-root data/cache/gm_rolling_180d "
                      "--local-output data/cache/h1_audit_v3 --report reports/v3_h1_impact.json "
                      "--table docs/v3_h1_impact.md", "```", ""])
        args.table.write_text("\n".join(lines), encoding="utf8")
    for g, group in report["groups"].items():
        print(g, group["counts"], group["path_selected_changes"], flush=True)


if __name__ == "__main__":
    main()
