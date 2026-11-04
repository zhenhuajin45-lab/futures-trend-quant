"""Fail-closed independent-calendar admission, prior to strategy selection."""
from bisect import bisect_left
from collections import Counter
from pathlib import Path
import gzip,json
import numpy as np
import pandas as pd
from backtest.forward_v3 import read,digest
from tools.historical_strategy.reproduce import canonical_digest,check_sources
from tools.first_entry_v71.clock import AdmissionError,calendar_clock,anchor_rows,make_lock,timestamp,time_index,object_hash,CHINA

ROOT=Path(__file__).resolve().parents[2]
DEFAULT_LOCK=ROOT/'config/v71_sample_lock.json'


def declaration(manifest,registration,lock):
    if manifest.get('status')!='QUALIFIED_BEFORE_SCORING':
        raise AdmissionError('READY_FOR_DATA',reason='independent history not qualified')
    if lock.get('status')!='FROZEN_BEFORE_LABELS':
        raise AdmissionError('SAMPLE_LOCK_NOT_FROZEN')
    if manifest.get('registered_before_labels') is not True:
        raise AdmissionError('METADATA_NOT_PREREGISTERED')
    if manifest.get('sample_lock_sha256')!=object_hash(lock):
        raise AdmissionError('SAMPLE_LOCK_CHANGED')
    starts=[p['scoring_anchor_eobs'][0] for p in lock['products'].values()]
    ends=[p['scoring_anchor_eobs'][-1] for p in lock['products'].values()]
    if lock['first_scoring_eob']!=min(starts,key=timestamp) or lock['last_scoring_eob']!=max(ends,key=timestamp):
        raise AdmissionError('LOCK_ANCHOR_ENDPOINT_INCONSISTENT')
    for key in ('first_scoring_eob','last_scoring_eob','last_path_eob','registration_sha256','source_policy'):
        if manifest.get(key)!=lock.get(key):
            raise AdmissionError('REGISTERED_WINDOW_OR_POLICY_CHANGED',field=key)
    if lock['last_path_eob']!=registration['new_sample']['proposed_price_path_end']:
        raise AdmissionError('REGISTERED_PATH_ENDPOINT_CHANGED')
    if set(manifest.get('products',{}))!=set(registration['new_sample']['universe']):
        raise AdmissionError('FROZEN_UNIVERSE_CHANGED')
    for cs in registration['new_sample']['universe']:
        product=manifest['products'][cs]
        fixed_days=lock['products'][cs]['scoring_trade_dates']
        if not fixed_days or [fixed_days[0],fixed_days[-1]]!=registration['new_sample']['proposed_scoring_trade_dates']:
            raise AdmissionError('LOCK_REGISTRATION_RANGE_CHANGED',csymbol=cs)
        if product.get('scoring_trade_dates')!=lock['products'][cs]['scoring_trade_dates']:
            raise AdmissionError('REGISTERED_TRADE_DAYS_CHANGED',csymbol=cs)
    return True


def verified(root,relative,expected,lock):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise AdmissionError('PATH_OUTSIDE_SNAPSHOT',file=relative)
    if not path.is_file():
        raise AdmissionError('MISSING_INPUT_FILE',file=relative)
    if digest(path)!=expected or lock['input_file_sha256'].get(relative)!=expected:
        raise AdmissionError('INPUT_FILE_CHANGED',file=relative)
    return path


def normalize_times(frame):
    """Carry checked timezone-aware values into the object used by labels."""
    for column in ('bob','eob'):
        values=pd.DatetimeIndex(frame[column])
        if values.tz is None or values.hasnans:
            raise AdmissionError('RAW_TIMEZONE_OR_TIMESTAMP_MISSING',column=column)
        frame[column]=values.tz_convert(CHINA)
    return frame


def validate_raw(frame,symbol,clock):
    fields={'symbol','bob','eob','open','high','low','close','volume','position'}
    if not fields.issubset(frame.columns):
        raise AdmissionError('RAW_COLUMNS_MISSING',symbol=symbol,fields=sorted(fields-set(frame.columns)))
    if frame.empty or set(frame.symbol)!={symbol}:
        raise AdmissionError('WRONG_OR_EMPTY_ACTUAL_CONTRACT',symbol=symbol)
    normalize_times(frame)
    eobs=pd.DatetimeIndex(frame.eob)
    bobs=pd.DatetimeIndex(frame.bob)
    if eobs.tz is None or bobs.tz is None or eobs.hasnans or bobs.hasnans:
        raise AdmissionError('RAW_TIMEZONE_OR_TIMESTAMP_MISSING',symbol=symbol)
    if eobs.has_duplicates or not eobs.is_monotonic_increasing:
        raise AdmissionError('RAW_TIME_NOT_UNIQUE_MONOTONIC',symbol=symbol)
    if np.any((eobs-bobs)!=pd.Timedelta(minutes=1)) or np.any(eobs!=eobs.tz_convert('UTC').floor('min')):
        raise AdmissionError('M1_BOB_EOB_CONVENTION',symbol=symbol)
    expected=time_index([r['eob'] for r in clock])
    extra=eobs.difference(expected)
    if len(extra):
        raise AdmissionError('RAW_OUTSIDE_VERIFIED_SESSIONS',symbol=symbol,first=extra[0].isoformat())
    numbers=frame[['open','high','low','close','volume','position']].to_numpy(dtype=float)
    if not np.isfinite(numbers).all() or (frame.volume<0).any() or (frame.position<0).any():
        raise AdmissionError('RAW_NONFINITE_OR_NEGATIVE_QUANTITY',symbol=symbol)
    if (frame.high<frame[['open','low','close']].max(axis=1)).any() or (frame.low>frame[['open','high','close']].min(axis=1)).any():
        raise AdmissionError('RAW_OHLC_INVALID',symbol=symbol)
    # Expected clock comes from calendar proof, not from frame/schedule.
    observed=set(eobs)
    expected_anchors=time_index([r['eob'] for r in anchor_rows(clock)])
    counts=pd.Series(eobs.ceil('15min')).value_counts()
    complete=[end for end in expected_anchors if counts.get(end,0)==15]
    return observed,complete


def admit(manifest_path,input_root,lock_path=DEFAULT_LOCK,fixture=False):
    manifest=read(manifest_path);registration=read(ROOT/'tools/first_entry_v7/registration.json')
    lock=read(lock_path)
    declaration(manifest,registration,lock)  # No replay opened before date/day/endpoint checks.
    if lock['registration_sha256']!=canonical_digest(ROOT/'tools/first_entry_v7/registration.json'):
        raise AdmissionError('REGISTERED_VERSION_CHANGED')
    if (lock.get('purpose')=='ENGINEERING_FIXTURE_ONLY')!=bool(fixture):
        raise AdmissionError('FIXTURE_AND_RESEARCH_MUST_NOT_MIX')
    check_sources(read(ROOT/'reports/v6_reproduction_lock.json'))
    calendar_path=(input_root/manifest['calendar_path']).resolve()
    if not calendar_path.is_relative_to(input_root.resolve()) or digest(calendar_path)!=lock['calendar_sha256']:
        raise AdmissionError('INDEPENDENT_CALENDAR_CHANGED')
    calendar=read(calendar_path)
    for proof in calendar['evidence_files']:
        verified(input_root,proof['path'],proof['sha256'],lock)
    context=dict(manifest=manifest,lock=lock,products={},coverage=[],raw_paths={},raw_hashes={},traces={},expected={},anchors={})
    reconstructed_products={};last_partial=None
    start,end=timestamp(lock['first_scoring_eob']),timestamp(lock['last_scoring_eob'])
    for cs,product in sorted(manifest['products'].items()):
        clock=calendar_clock(calendar,cs)
        partial=make_lock(calendar,registration,lock['registration_sha256'],lock['calendar_sha256'],{cs:clock},only_product=cs)
        for key in set(partial)-{'products','first_scoring_eob','last_scoring_eob'}:
            if partial[key]!=lock.get(key):raise AdmissionError('LOCK_DOES_NOT_MATCH_INDEPENDENT_CALENDAR',field=key)
        if partial['products'][cs]!=lock['products'][cs]:
            raise AdmissionError('LOCK_PRODUCT_CLOCK_CHANGED',csymbol=cs)
        reconstructed_products[cs]=partial['products'][cs];last_partial=partial
        anchors=anchor_rows(clock)
        clock_eobs=time_index([r['eob'] for r in clock])
        anchor_eobs=time_index([r['eob'] for r in anchors])
        expected_days=dict(zip(anchor_eobs,(r['trading_date'] for r in anchors)))
        score_eobs=set(time_index(lock['products'][cs]['scoring_anchor_eobs']))
        schedule=read(verified(input_root,product['schedule_path'],product['schedule_sha256'],lock))
        supplied=[(r['eob'],r['trading_date']) for r in schedule]
        independent=[(r['eob'],r['trading_date']) for r in clock]
        if supplied!=independent:
            raise AdmissionError('SCHEDULE_DIFFERS_FROM_INDEPENDENT_CALENDAR',csymbol=cs)
        del supplied,independent,schedule
        selection=read(verified(input_root,product['selection_path'],product['selection_sha256'],lock))
        days=lock['products'][cs]['scoring_trade_dates']
        instrument_metadata={}
        if not fixture:
            metadata=read(verified(input_root,product['instrument_metadata_path'],product['instrument_metadata_sha256'],lock))
            instrument_metadata={r['symbol']:r for r in metadata['metadata'][cs]}
        registered={c['symbol'] for c in product['contracts']}
        if any(selection.get(day) not in registered for day in days):
            raise AdmissionError('SELECTED_LEG_MISSING',csymbol=cs)
        counts_by_day=Counter(r['trading_date'] for t,r in zip(anchor_eobs,anchors) if t in score_eobs)
        coverage={day:dict(csymbol=cs,trading_date=day,actual_contract=selection[day],
            expected_m15=counts_by_day[day],
            actual_frames=0,qualified_frames=0) for day in days}
        complete_by_leg={};seen_selected=set()
        for contract in product['contracts']:
            symbol=contract['symbol']
            raw_path=verified(input_root,contract['raw_path'],contract['raw_sha256'],lock)
            scope=pd.DatetimeIndex(pd.read_parquet(raw_path,columns=['eob']).eob)
            if scope.tz is None or scope.hasnans:raise AdmissionError('RAW_TIMEZONE_OR_TIMESTAMP_MISSING',symbol=symbol)
            if len(scope) and scope.max()>timestamp(lock['last_path_eob']):
                raise AdmissionError('RAW_BEYOND_REGISTERED_LABEL_CAP',symbol=symbol)
            raw=pd.read_parquet(raw_path)
            observed,complete=validate_raw(raw,symbol,clock)
            if not fixture:
                instrument=instrument_metadata.get(symbol)
                if not instrument or not instrument.get('listed_date') or not instrument.get('delisted_date'):
                    raise AdmissionError('INSTRUMENT_LISTING_METADATA_MISSING',symbol=symbol)
                raw_trade_days=pd.DatetimeIndex(raw.eob).ceil('15min').map(expected_days)
                if min(raw_trade_days)<str(instrument['listed_date'])[:10] or max(raw_trade_days)>str(instrument['delisted_date'])[:10]:
                    raise AdmissionError('RAW_OUTSIDE_INSTRUMENT_LISTING_RANGE',symbol=symbol)
            complete_by_leg[symbol]=set(complete)
            should_score=sorted(t for t in score_eobs if selection[expected_days[t]]==symbol)
            if should_score:
                first=should_score[0]
                previous=anchor_eobs[anchor_eobs<first]
                required=previous[-1200:]
                if len(required)!=1200 or not set(required)<=set(complete):
                    raise AdmissionError('ACTUAL_LEG_WARMUP_INCOMPLETE',csymbol=cs,symbol=symbol,
                        first_required_anchor=first.isoformat(),complete_past=bisect_left(complete,first),
                        missing_required=len(set(required)-set(complete)))
                should_score_set=set(should_score)
                independent_index=pd.DatetimeIndex(clock_eobs)
                missing=set(independent_index[independent_index.ceil('15min').isin(should_score_set)])-observed
                if missing:
                    raise AdmissionError('SELECTED_LEG_MINUTE_GAP',csymbol=cs,symbol=symbol,first=min(missing).isoformat())
            del observed
            replay_path=verified(input_root,contract['replay_path'],contract['replay_sha256'],lock)
            traces=[];seen=set();previous=None
            with gzip.open(replay_path,'rt',encoding='utf8') as stream:
                try:header=json.loads(next(stream))
                except (StopIteration,json.JSONDecodeError):raise AdmissionError('REPLAY_HEADER_MISSING',symbol=symbol)
                if header.get('task',{}).get('symbol')!=symbol or header.get('task',{}).get('csymbol')!=cs or header.get('input_sha256')!=contract['raw_sha256']:
                    raise AdmissionError('REPLAY_IDENTITY_MISMATCH',symbol=symbol)
                for line in stream:
                    row=json.loads(line);now=timestamp(row['decision_eob'])
                    if now>end:break
                    if now in seen or (previous is not None and now<=previous):
                        raise AdmissionError('REPLAY_NOT_UNIQUE_MONOTONIC',symbol=symbol)
                    seen.add(now);previous=now
                    if now not in expected_days or row['trading_date']!=expected_days[now]:
                        raise AdmissionError('REPLAY_CLOCK_OR_TRADE_DATE_MISMATCH',symbol=symbol,time=now.isoformat())
                    if row['bar'].get('symbol')!=symbol or timestamp(row['bar']['eob'])!=now:
                        raise AdmissionError('REPLAY_BAR_IDENTITY_MISMATCH',symbol=symbol)
                    if bool(row['complete_m15'])!=(now in complete_by_leg[symbol]):
                        raise AdmissionError('REPLAY_COMPLETE_FLAG_MISMATCH',symbol=symbol,time=now.isoformat())
                    if now in score_eobs:
                        day=expected_days[now]
                        expected_selected=selection[day]==symbol
                        if type(row['selected']) is not bool or row['selected']!=expected_selected:
                            raise AdmissionError('SELECTED_FLAG_BIDIRECTIONAL_MISMATCH',csymbol=cs,symbol=symbol,time=now.isoformat())
                        if expected_selected:
                            coverage[day]['actual_frames']+=1
                            if now not in complete_by_leg[symbol]:
                                raise AdmissionError('SELECTED_FRAME_NOT_COMPLETE',symbol=symbol,time=now.isoformat())
                            coverage[day]['qualified_frames']+=1
                            seen_selected.add(now)
                    row['groups']={'S0':row['groups']['S0']}
                    traces.append(row)
            if should_score and not traces:
                raise AdmissionError('REPLAY_HAS_NO_COVERAGE',csymbol=cs,symbol=symbol,
                    expected_m15=len(should_score),actual_frames=0,qualified_frames=0)
            # A hole in the observed replay prefix makes episode history unprovable.
            if traces:
                first_frame=timestamp(traces[0]['decision_eob']);last_frame=timestamp(traces[-1]['decision_eob'])
                required_prefix={t for t in complete if first_frame<=t<=last_frame}
                if required_prefix-seen:
                    raise AdmissionError('REPLAY_PREFIX_FRAME_GAP',symbol=symbol,missing=len(required_prefix-seen))
            context['raw_paths'][(cs,symbol)]=raw_path
            context['raw_hashes'][(cs,symbol)]=contract['raw_sha256']
            context['traces'][(cs,symbol)]=traces
            del raw
        if seen_selected!=score_eobs:
            raise AdmissionError('REPLAY_SELECTED_COVERAGE_GAP',csymbol=cs,expected=len(score_eobs),
                received=len(seen_selected),first_missing=min(score_eobs-seen_selected).isoformat() if score_eobs-seen_selected else None,
                coverage=list(coverage.values()))
        context['products'][cs]=product
        context['expected'][cs]=pd.DatetimeIndex(clock_eobs)
        context['anchors'][cs]=anchors
        print('ADMISSION_PRODUCT_PASS',cs,len(score_eobs),flush=True)
        context['coverage'].extend(coverage.values())
    regenerated=dict(last_partial,products=reconstructed_products,
        first_scoring_eob=min((p['scoring_anchor_eobs'][0] for p in reconstructed_products.values()),key=timestamp),
        last_scoring_eob=max((p['scoring_anchor_eobs'][-1] for p in reconstructed_products.values()),key=timestamp))
    if regenerated!=lock:raise AdmissionError('LOCK_DOES_NOT_MATCH_INDEPENDENT_CALENDAR')
    context['status']='DATA_QUALIFIED'
    return context
