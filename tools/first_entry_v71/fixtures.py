"""Synthetic full-pool CLI fixtures; never historical strategy evidence."""
import gzip,json
from pathlib import Path
import pandas as pd
from backtest.forward_v3 import read,write,digest
from tools.historical_strategy.reproduce import canonical_digest
from tools.first_entry_v71.clock import dates,calendar_clock,anchor_rows,make_lock,object_hash,timestamp,DAY_SESSIONS,NIGHT_END

ROOT=Path(__file__).resolve().parents[2]


def rehash_inputs(folder):
    """Re-freeze a distinct synthetic input mutation before its first CLI run."""
    manifest=read(folder/'manifest.json');calendar=read(folder/'calendar.json');lock=read(folder/'sample_lock.json')
    assert calendar['purpose']=='ENGINEERING_FIXTURE_ONLY'
    hashes={p['path']:digest(folder/p['path']) for p in calendar['evidence_files']}
    for product in manifest['products'].values():
        for kind in ('schedule','selection'):
            relative=product[kind+'_path'];product[kind+'_sha256']=digest(folder/relative);hashes[relative]=product[kind+'_sha256']
        for contract in product['contracts']:
            raw_hash=digest(folder/contract['raw_path'])
            replay=folder/contract['replay_path']
            with gzip.open(replay,'rt',encoding='utf8') as stream:
                header=json.loads(next(stream));tail=stream.read()
            if header.get('input_sha256')!=raw_hash:
                header['input_sha256']=raw_hash
                with gzip.open(replay,'wt',encoding='utf8') as stream:stream.write(json.dumps(header)+'\n'+tail)
            for kind in ('raw','replay'):
                relative=contract[kind+'_path'];contract[kind+'_sha256']=digest(folder/relative);hashes[relative]=contract[kind+'_sha256']
    calendar['input_file_sha256']=hashes;write(folder/'calendar.json',calendar)
    lock.update(calendar_sha256=digest(folder/'calendar.json'),input_file_sha256=hashes)
    write(folder/'sample_lock.json',lock)
    manifest['sample_lock_sha256']=object_hash(lock);write(folder/'manifest.json',manifest)


def reseal(folder):
    manifest=read(folder/'manifest.json');calendar=read(folder/'calendar.json')
    hashes={}
    for proof in calendar['evidence_files']:
        proof['sha256']=digest(folder/proof['path']);hashes[proof['path']]=proof['sha256']
    for cs,product in manifest['products'].items():
        for kind in ('schedule','selection'):
            relative=product[kind+'_path'];product[kind+'_sha256']=digest(folder/relative);hashes[relative]=product[kind+'_sha256']
        for contract in product['contracts']:
            for kind in ('raw','replay'):
                relative=contract[kind+'_path'];contract[kind+'_sha256']=digest(folder/relative);hashes[relative]=contract[kind+'_sha256']
    calendar['input_file_sha256']=hashes;write(folder/'calendar.json',calendar)
    registration=read(ROOT/'tools/first_entry_v7/registration.json')
    lock=make_lock(calendar,registration,canonical_digest(ROOT/'tools/first_entry_v7/registration.json'),digest(folder/'calendar.json'))
    write(folder/'sample_lock.json',lock)
    manifest.update({key:lock[key] for key in ('registration_sha256','source_policy','first_scoring_eob','last_scoring_eob','last_path_eob')})
    manifest['sample_lock_sha256']=object_hash(lock)
    for cs,product in manifest['products'].items():product['scoring_trade_dates']=lock['products'][cs]['scoring_trade_dates']
    write(folder/'manifest.json',manifest)
    return manifest,lock


def build(folder,zero_opportunities=False):
    folder.mkdir(parents=True,exist_ok=False)
    reg=read(ROOT/'tools/first_entry_v7/registration.json')
    # Full civil-day records intentionally use a synthetic weekday calendar.
    records=[];previous=None
    for day in dates('2025-06-01','2026-05-06'):
        opened=pd.Timestamp(day).weekday()<5
        records.append(dict(date=day,is_open=opened,night_start_date=previous if opened else None))
        if opened:previous=day
    proof=folder/'fixture_proof.json';write(proof,dict(purpose='ENGINEERING_FIXTURE_ONLY',not_official_calendar=True))
    calendar=dict(coverage_start='2025-06-01',coverage_end='2026-05-06',purpose='ENGINEERING_FIXTURE_ONLY',
        independent_of_quote_minutes=True,evidence_files=[dict(path=proof.name,sha256=digest(proof))],
        source_policy=dict(source='SYNTHETIC',mapping='SYNTHETIC_CONSTANT_ACTUAL_LEG',arrival_at='UNKNOWN',
            zero_volume='KEEP_ORIGINAL_NO_FILL',volume='LOTS',position='END_OI'),products={})
    products={}
    for cs in reg['new_sample']['universe']:
        product_days=[dict(r,night_start_date=r['night_start_date'] if cs in NIGHT_END else None) for r in records]
        calendar['products'][cs]=dict(days=product_days,day_sessions=[list(x) for x in DAY_SESSIONS],night_end=NIGHT_END.get(cs))
        clock=calendar_clock(calendar,cs);anchors=anchor_rows(clock)
        symbol=cs+'.SYNTHETIC';prefix=cs.replace('.','_')
        schedule_path=folder/(prefix+'_schedule.json');write(schedule_path,[dict(eob=r['eob'],trading_date=r['trading_date']) for r in clock])
        selection_path=folder/(prefix+'_selection.json');write(selection_path,{r['date']:symbol for r in product_days if r['is_open']})
        eobs=pd.DatetimeIndex([r['eob'] for r in clock if r['eob']>='2025-09-01'])
        raw=pd.DataFrame(dict(symbol=symbol,bob=eobs-pd.Timedelta(minutes=1),eob=eobs,
            open=100.,high=101.,low=99.,close=100.,volume=1.,position=100.))
        if cs=='DCE.M' and not zero_opportunities:
            raw.loc[raw.eob==timestamp('2026-01-05T09:16:00+08:00'),'high']=109.
            raw.loc[(raw.eob>timestamp('2026-01-05T09:30:00+08:00'))&(raw.eob<timestamp('2026-01-09T15:01:00+08:00')),'low']=95.
        raw_path=folder/(prefix+'.parquet');raw.to_parquet(raw_path,index=False)
        replay_path=folder/(prefix+'.jsonl.gz')
        with gzip.open(replay_path,'wt',encoding='utf8') as stream:
            stream.write(json.dumps(dict(task=dict(csymbol=cs,symbol=symbol),input_sha256=digest(raw_path)))+'\n')
            for index,anchor in enumerate(anchors):
                time=timestamp(anchor['eob'])
                if time<timestamp('2025-09-01T00:00:00+08:00'):continue
                if time>timestamp('2026-04-24T15:00:00+08:00'):break
                is_bull=cs=='DCE.M' and not zero_opportunities and timestamp('2026-01-05T09:15:00+08:00')<=time<=timestamp('2026-01-05T10:15:00+08:00')
                accepted=is_bull and time.minute in (15,30) and time.hour==9
                entry=dict(accepted=accepted,gate_open=True,formed_paths=['D'] if accepted else [],reason='PASS' if accepted else 'NO_PATH')
                if accepted:entry.update(path='D',evidence_id='evidence_'+time.isoformat(),trigger_bar_id=time.isoformat(),anchor=dict(anchor_price=98.))
                core=dict(close=100.,atr14=2.,trend_decision=dict(state=dict(trend_state='bull' if is_bull else 'none')),entry_evaluation=entry)
                bar=dict(symbol=symbol,bob=(time-pd.Timedelta(minutes=15)).isoformat(),eob=time.isoformat(),frequency='900s',open=100.,high=101.,low=99.,close=100.,volume=15.,position=100.)
                row=dict(decision_eob=time.isoformat(),bar=bar,complete_m15=True,selected=True,
                    trading_date=anchor['trading_date'],groups=dict(S0=core),i15=index)
                stream.write(json.dumps(row)+'\n')
        products[cs]=dict(schedule_path=schedule_path.name,selection_path=selection_path.name,
            contracts=[dict(symbol=symbol,raw_path=raw_path.name,replay_path=replay_path.name)])
    write(folder/'calendar.json',calendar)
    write(folder/'manifest.json',dict(status='QUALIFIED_BEFORE_SCORING',registered_before_labels=True,
        calendar_path='calendar.json',products=products))
    return reseal(folder)


if __name__=='__main__':
    import sys
    build(Path(sys.argv[1]),'--zero' in sys.argv[2:])
    print('SYNTHETIC_FULL_POOL_BUILT',flush=True)
