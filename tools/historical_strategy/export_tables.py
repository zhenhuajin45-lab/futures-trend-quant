"""Flat review tables and fixed first positive/negative matched examples."""
import csv
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write


def main():
    rows=read(ROOT/"reports/v6_opportunity_pairs.json")["events"]
    flat=[]
    for row in rows:
        item={k:row[k] for k in ("case_id","opportunity_id","csymbol","actual_contract","direction","path","time",
            "trading_date","session","month","roll_status","episode","age","matched","repeat_rows")}
        item["control_time"]=None if row["control"] is None else row["control"]["time"]
        for prefix,source in (("signal",row),("control",row["control"])):
            for h in ("8","48"):
                for field in ("label","complete","entry_eob","first_touch_eob","r_over_atr","mfe_r","mae_r"):
                    item[f"{prefix}_{h}_{field}"]=None if source is None else source["labels"][h].get(field)
        flat.append(item)
    with (ROOT/"reports/v6_opportunity_pairs.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    examples=[]
    for direction in ("bull","bear"):
        for signal_label,control_label in (("TARGET_FIRST","STOP_FIRST"),("STOP_FIRST","TARGET_FIRST")):
            cohort=sorted((r for r in rows if r["direction"]==direction and r["control"] and
                r["labels"]["48"].get("complete") and r["control"]["labels"]["48"].get("complete") and
                r["labels"]["48"]["label"]==signal_label and r["control"]["labels"]["48"]["label"]==control_label),key=lambda r:(r["time"],r["opportunity_id"]))
            if cohort:
                row=cohort[0]
                examples.append(dict(case_id=row["case_id"],csymbol=row["csymbol"],actual_contract=row["actual_contract"],direction=direction,
                    selection="FIRST_CHRONOLOGICAL_COMPLETE_CASE_IN_CATEGORY",trigger_eob=row["trigger_bar_id"].split("|")[-1],
                    signal_eob=row["time"],signal_path=row["path"],signal=row["labels"]["48"],
                    control_eob=row["control"]["time"],control=row["control"]["labels"]["48"],
                    trend_exit_decision=row["trend_exit_decision"]))
    write(ROOT/"reports/v6_paired_examples.json",dict(examples=examples,scope="PRICE_PATH_EXAMPLES; NO_STRATEGY_VARIANT_OR_EXECUTION_PNL"))
    print("V6_REVIEW_TABLES_READY",len(flat),len(examples),flush=True)


if __name__=="__main__":
    main()
