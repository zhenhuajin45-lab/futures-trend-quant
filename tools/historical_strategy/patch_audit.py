"""Account for recovery query overlap and isolate the 120/330 recovered nights."""
import argparse
import copy
from pathlib import Path
import shutil
import sys
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backtest.forward_v3 import read,write,digest,stamp


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--data-root",type=Path,required=True)
    p.add_argument("--original-root",type=Path,required=True)
    p.add_argument("--patch-root",type=Path,required=True)
    p.add_argument("--counterfactual-root",type=Path,required=True)
    p.add_argument("--summary",type=Path,required=True)
    a=p.parse_args()
    if a.counterfactual_root.exists():
        raise ValueError("fresh counterfactual directory required")
    (a.counterfactual_root/"contracts").mkdir(parents=True)
    manifest=read(a.data_root/"manifest.json")
    original=read(a.original_root/"manifest.json")
    counter=copy.deepcopy(manifest)
    counter["tasks"]=[]
    counter["products"]={}
    audit=[]
    for csymbol,symbol,day,target_count in (("DCE.EG","DCE.eg2610","2026-08-24",120),("SHFE.AG","SHFE.ag2610","2026-07-20",330)):
        item=next(x for x in manifest["products"][csymbol]["contracts"] if x["symbol"]==symbol)
        name=Path(item["path"]).name
        original_file=a.original_root/"raw"/name
        patch_file=a.patch_root/name
        assert digest(original_file)==item["original_sha256"]
        assert digest(original_file)==next(x["sha256"] for x in original["products"][csymbol]["raw_files"] if Path(x["path"]).name==name)
        base=pd.read_parquet(original_file)
        patch=pd.read_parquet(patch_file)
        base["eob"]=[stamp(x) for x in base.eob]
        patch["eob"]=[stamp(x) for x in patch.eob]
        assert not base.duplicated(["symbol","eob"]).any() and not patch.duplicated(["symbol","eob"]).any()
        common=base.merge(patch,on=["symbol","eob"],suffixes=("_old","_new"))
        revisions={k:int((common[k+"_old"]!=common[k+"_new"]).sum()) for k in ("open","high","low","close","volume","position")}
        assert not any(revisions.values()),"common rows changed; do not substitute source history"
        view=pd.read_parquet(a.data_root/item["path"])
        added=set(patch.eob)-set(base.eob)
        remove=set(view.loc[(view.trading_date==day)&view.eob.isin(added),"eob"])
        assert len(remove)==target_count
        without=view.loc[~view.eob.isin(remove)].copy()
        target=a.counterfactual_root/item["path"]
        without.to_parquet(target,index=False)
        task=copy.deepcopy(next(x for x in manifest["tasks"] if x["symbol"]==symbol))
        task["rows"]=len(without)
        counter["tasks"].append(task)
        counter["products"][csymbol]=copy.deepcopy(manifest["products"][csymbol])
        for suffix in ("_selection.json","_schedule.json"):
            shutil.copyfile(a.data_root/(csymbol+suffix),a.counterfactual_root/(csymbol+suffix))
        audit.append(dict(csymbol=csymbol,actual_contract=symbol,target_trading_date=day,
            original_sha256=digest(original_file),patch_sha256=digest(patch_file),
            original_rows=len(base),requery_rows=len(patch),common_rows=len(common),common_revisions=revisions,
            total_query_additions=len(added),target_recovered_minutes=len(remove),
            other_prior_day_additions=len(added-remove),counterfactual_sha256=digest(target),
            augmentation_query_start="2026-05-09T00:00:00+08:00",original_immutable=True))
    counter["source_manifest_sha256"]=digest(a.data_root/"manifest.json")
    counter["variant"]="REMOVE_ONLY_RECOVERED_TARGET_NIGHTS; KEEP_OTHER_PREHISTORY_IDENTICAL"
    write(a.counterfactual_root/"manifest.json",counter)
    write(a.summary,dict(cases=audit,total_recovered_target_minutes=sum(x["target_recovered_minutes"] for x in audit),
                         total_query_additions=sum(x["total_query_additions"] for x in audit)))


if __name__=="__main__":
    main()
