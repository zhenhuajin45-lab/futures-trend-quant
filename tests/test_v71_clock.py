import pytest
import pandas as pd
from tools.first_entry_v71.clock import calendar_clock,anchor_rows,DAY_SESSIONS,NIGHT_END,AdmissionError,make_lock
from tools.first_entry_v71.admission import validate_raw


def bundle(cs='CZCE.CJ'):
    days=[dict(date='2026-01-09',is_open=True,night_start_date=None),
          dict(date='2026-01-10',is_open=False,night_start_date=None),
          dict(date='2026-01-11',is_open=False,night_start_date=None),
          dict(date='2026-01-12',is_open=True,night_start_date='2026-01-09' if cs in NIGHT_END else None)]
    return dict(coverage_start='2026-01-09',coverage_end='2026-01-12',products={cs:dict(days=days,
        day_sessions=[list(x) for x in DAY_SESSIONS],night_end=NIGHT_END.get(cs))})


def test_full_day_calendar_has_225_minutes_and_15_complete_bars():
    clock=calendar_clock(bundle(),'CZCE.CJ')
    assert len(clock)==450 and len(anchor_rows(clock))==30


def test_weekend_night_uses_previous_open_civil_day():
    clock=calendar_clock(bundle('SHFE.AU'),'SHFE.AU')
    monday=[r for r in clock if r['trading_date']=='2026-01-12']
    assert monday[0]['eob']=='2026-01-09T21:01:00+08:00'
    assert any(r['eob']=='2026-01-10T02:30:00+08:00' for r in monday)
    assert len(monday)==555


def test_night_cannot_be_moved_to_sunday():
    value=bundle('DCE.M');value['products']['DCE.M']['days'][-1]['night_start_date']='2026-01-11'
    with pytest.raises(AdmissionError,match='NIGHT_DATE_INVALID'):calendar_clock(value,'DCE.M')


def test_day_removed_from_calendar_is_rejected():
    value=bundle();value['products']['CZCE.CJ']['days'].pop(1)
    with pytest.raises(AdmissionError,match='CALENDAR_DAY_COVERAGE'):calendar_clock(value,'CZCE.CJ')


def test_independent_template_cannot_be_last_minute_only():
    value=bundle();value['products']['CZCE.CJ']['day_sessions']=[['09:14','09:15']]
    with pytest.raises(AdmissionError,match='DAY_TEMPLATE_NOT_VERIFIED'):calendar_clock(value,'CZCE.CJ')


def test_sparse_raw_does_not_get_full_bar_credit():
    clock=calendar_clock(bundle(),'CZCE.CJ');eobs=pd.DatetimeIndex([r['eob'] for r in clock if pd.Timestamp(r['eob']).minute%15==0])
    raw=pd.DataFrame(dict(symbol='CZCE.CJ.SYNTHETIC',eob=eobs,bob=eobs-pd.Timedelta(minutes=1),
        open=100.,high=101.,low=99.,close=100.,volume=0.,position=1.))
    _,complete=validate_raw(raw,'CZCE.CJ.SYNTHETIC',clock)
    assert not complete


def test_microsecond_parquet_times_have_same_minute_semantics():
    clock=calendar_clock(bundle(),'CZCE.CJ')
    eobs=pd.DatetimeIndex([r['eob'] for r in clock]).as_unit('us')
    raw=pd.DataFrame(dict(symbol='CZCE.CJ.SYNTHETIC',eob=eobs,bob=eobs-pd.Timedelta(minutes=1),
        open=100.,high=101.,low=99.,close=100.,volume=0.,position=1.))
    _,complete=validate_raw(raw,'CZCE.CJ.SYNTHETIC',clock)
    assert len(complete)==30


def test_iso_timestamp_columns_are_carried_into_price_path_reader():
    from tools.historical_strategy.price_paths import label_opportunity,ExpectedClock
    clock=calendar_clock(bundle(),'CZCE.CJ');times=[pd.Timestamp(r['eob']) for r in clock]
    raw=pd.DataFrame(dict(symbol='CZCE.CJ.SYNTHETIC',eob=[t.tz_convert('UTC').isoformat() for t in times],
        bob=[(t-pd.Timedelta(minutes=1)).tz_convert('UTC').isoformat() for t in times],
        open=100.,high=101.,low=99.,close=100.,volume=1.,position=1.))
    validate_raw(raw,'CZCE.CJ.SYNTHETIC',clock)
    assert isinstance(raw.eob.dtype,pd.DatetimeTZDtype)
    records=raw.to_dict('records')
    indexed=dict(list=records,bobs=list(raw.bob),by_eob={r['eob']:r for r in records},clock_index=ExpectedClock(times))
    result=label_opportunity(dict(time=times[14],direction='bull',atr=2.,close=100.,anchor=98.),indexed,times,1.,
        dict(stop_buffer_atr=.6,stop_min_atr=1.5,stop_max_atr=6.),48)
    assert result['entry_eob']==times[15].isoformat()


def test_real_freeze_requires_historical_authority_proofs():
    value=bundle()
    value.update(coverage_start='2025-06-01',coverage_end='2026-05-06',purpose='INDEPENDENT_RESEARCH',
        independent_of_quote_minutes=True,evidence_files=[dict(path='self_assertion.json',sha256='x')],source_policy={})
    reg=dict(new_sample=dict(proposed_scoring_trade_dates=['2026-01-05','2026-04-24'],
        proposed_prehistory_start='2025-06-01',proposed_price_path_end='2026-05-06T15:00:00+08:00',universe=['CZCE.CJ']))
    with pytest.raises(AdmissionError,match='HISTORICAL_AUTHORITY_EVIDENCE_UNVERIFIED'):
        make_lock(value,reg,'r','c')


def test_real_freeze_binds_reused_selector_and_tick_inputs(monkeypatch):
    import json
    from pathlib import Path
    import tools.first_entry_v71.clock as clock_module
    import tools.historical_strategy.reproduce as reproduction
    root=Path(__file__).resolve().parents[1]
    days=json.loads((root/'config/v71_trade_date_lock.json').read_text(encoding='utf8'))['trade_dates_by_exchange']['CZCE']
    # Synthetic metadata only: isolate code-fingerprint closure, no price/replay access.
    anchors=[dict(eob=day+'T09:15:00+08:00',trading_date=day) for day in days]
    monkeypatch.setattr(clock_module,'calendar_clock',lambda value,cs:anchors)
    monkeypatch.setattr(clock_module,'anchor_rows',lambda minutes:minutes)
    value=bundle()
    value.update(coverage_start='2025-06-01',coverage_end='2026-05-06',purpose='INDEPENDENT_RESEARCH',
        independent_of_quote_minutes=True,evidence_files=[dict(path='synthetic_proof.json',sha256='x')],
        historical_attestations=[dict(exchange='CZCE',kind=kind,status='VERIFIED',evidence_sha256='x',
            covers_start='2025-06-01',covers_end='2026-05-06') for kind in ('calendar','holiday_night_closures','historical_sessions')],
        source_policy=dict(source='SYNTHETIC_TEST',mapping='SYNTHETIC_TEST',volume='LOTS',position='END_OI',
            zero_volume='PRESERVE',timezone='Asia/Shanghai',raw_time_label='EOB',normalized_time_label='CLOSED_M1_EOB',
            price_adjustment='UNADJUSTED_ACTUAL_CONTRACT',calibration_sha256='x',arrival_at='UNKNOWN',mapping_known_at='UNKNOWN'))
    reg=dict(new_sample=dict(proposed_scoring_trade_dates=['2026-01-05','2026-04-24'],
        proposed_prehistory_start='2025-06-01',proposed_price_path_end='2026-05-06T15:00:00+08:00',universe=['CZCE.CJ']))
    locked=make_lock(value,reg,'r','c')
    fingerprints=locked['research_source_canonical_sha256']
    for name in ('tools/first_entry_v7/selector.py','tools/first_entry_v7/symmetric.py','config/fees.json'):
        assert fingerprints[name]==reproduction.canonical_digest(root/name)
    original=reproduction.canonical_digest
    monkeypatch.setattr(reproduction,'canonical_digest',lambda path:'CHANGED' if path.name=='selector.py' else original(path))
    assert make_lock(value,reg,'r','c')!=locked
