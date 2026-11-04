"""Verify immutable source/fact prefixes and audit appended M15 completeness."""
from pathlib import Path
import argparse,gzip,json
import pandas as pd
from backtest.forward_v3 import read,write,digest


def records(path):
    with gzip.open(path,'rt',encoding='utf8') as stream:return json.loads(next(stream)),[json.loads(x) for x in stream]


def main():
    p=argparse.ArgumentParser();p.add_argument('--original-root',type=Path,required=True)
    p.add_argument('--original-replay',type=Path,required=True);p.add_argument('--extended',type=Path,required=True);a=p.parse_args()
    m=read(a.extended/'data/manifest.json');rows=[]
    for task in m['tasks']:
        old_path=a.original_root/task['path'];new_path=a.extended/'data'/task['path']
        old=pd.read_parquet(old_path);new=pd.read_parquet(new_path)
        for frame in (old,new):
            for key in ['bob','eob']:frame[key]=pd.to_datetime(frame[key],utc=True).dt.tz_convert('Asia/Shanghai')
        pd.testing.assert_frame_equal(old,new.iloc[:len(old)][old.columns],check_dtype=False)
        original_replay=a.original_replay/(task['symbol']+'.jsonl.gz');target=a.extended/'replay'/(task['symbol']+'.jsonl.gz')
        old_header,old_rows=records(original_replay);header,all_rows=records(target)
        assert all_rows[:len(old_rows)]==old_rows,task['symbol']
        seen=set(new.eob);partial=0
        for row in all_rows[len(old_rows):]:
            assert not row['selected'];end=pd.Timestamp(row['bar']['eob'])
            row['complete_m15']=pd.Timestamp(row['decision_eob'])==end and all(end-pd.Timedelta(minutes=k) in seen for k in range(15))
            partial+=not row['complete_m15']
        if not header.get('v8_tail_completeness_audited'):
            previous_sha=digest(target);header.update(input_sha256=digest(new_path),v8_original_input_sha256=digest(old_path),
                v8_original_replay_sha256=digest(original_replay),v8_tail_completeness_audited=True)
            temporary=target.with_suffix('.audit.tmp')
            with gzip.open(temporary,'wt',encoding='utf8') as out:
                out.write(json.dumps(header)+'\n')
                for row in all_rows:out.write(json.dumps(row,default=str,ensure_ascii=False,allow_nan=False)+'\n')
            temporary.replace(target)
        else:previous_sha=None
        rows.append(dict(symbol=task['symbol'],old_m1=len(old),new_m1=len(new)-len(old),
            original_m1_prefix_unchanged=True,original_fact_rows_unchanged=len(old_rows),
            appended_fact_rows=len(all_rows)-len(old_rows),partial_appended_m15=partial,
            original_raw_sha256=digest(old_path),extended_raw_sha256=digest(new_path),
            original_replay_sha256=digest(original_replay),pre_completeness_audit_replay_sha256=previous_sha,
            audited_replay_sha256=digest(target)))
    write(a.extended/'tail_integrity_audit.json',dict(status='PASS',products=rows))
    print('V8_TAIL_PREFIX_INTEGRITY_PASS',len(rows),sum(r['new_m1'] for r in rows))


if __name__=='__main__':main()
