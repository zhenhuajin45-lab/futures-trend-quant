"""Read-only summary; real material remains in a fresh local restricted directory."""
from pathlib import Path
import argparse,json,tempfile
import pandas as pd
from .statistics import analyze
from .contracts import read,write,sha,validate_freeze,validate_audit,fresh_output

TABLES=('cells','cell_products','within_product','fixed_2r','path')


def run(input_dir,prereg,receipts,out,synthetic=False):
    output=fresh_output(out,not synthetic)
    if synthetic:
        reg=read(prereg);metadata=read(Path(input_dir)/'synthetic.json')
        if not reg['study'].startswith('SYNTHETIC_') or metadata.get('data_kind')!='SYNTHETIC_ONLY':raise ValueError('synthetic contract required')
        f=pd.read_csv(Path(input_dir)/'audit.csv');binding=dict(data_kind='SYNTHETIC_ONLY',independent_two_person_review=False)
        source=dict(audit_sha256=sha(Path(input_dir)/'audit.csv'))
    else:
        reg,binding=validate_freeze(prereg,receipts)
        f,source=validate_audit(input_dir,reg,binding)
    result=analyze(f,reg)
    result['coverage'].update(source)
    result['decision'].update(binding)
    result['decision'].update(data_kind='SYNTHETIC_ONLY' if synthetic else 'REAL_OLD_WINDOW',
        mother_cohort_processed=len(f),prereg_sha256=sha(prereg,True),independent_test_consumed=False,
        next_gate='HUMAN_BLIND_REVIEW_AND_NEW_DATA_ADMISSION_STILL_REQUIRED',real_economic_verdict=False)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage=Path(temporary)/'summary';stage.mkdir()
        for name in TABLES:result[name].to_csv(stage/(name+'.csv'),index=False)
        for name in ('coverage','decision'):write(stage/(name+'.json'),result[name])
        write(stage/'manifest.json',dict(files={p.name:sha(p) for p in sorted(stage.iterdir())}))
        if set(TABLES)-{p.stem for p in stage.glob('*.csv')}:raise ValueError('incomplete output set')
        stage.rename(output)
    return result['decision']


def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--prereg',type=Path,required=True)
    p.add_argument('--receipts',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--synthetic',action='store_true')
    a=p.parse_args()
    if not a.synthetic and a.receipts is None:p.error('--receipts required for real input')
    result=run(a.input,a.prereg,a.receipts,a.out,a.synthetic)
    print(json.dumps(dict(status=result['status'],data_kind=result['data_kind'],processed=result['mother_cohort_processed']),ensure_ascii=False))


if __name__=='__main__':main()
