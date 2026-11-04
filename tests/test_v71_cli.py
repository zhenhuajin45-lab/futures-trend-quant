"""Normal CLI positive/negative fixtures, all 12 registered products/dates."""
from pathlib import Path
import gzip,json,shutil,subprocess,sys
import pandas as pd
import pytest
from backtest.forward_v3 import read,write
from tools.first_entry_v71.fixtures import build,rehash_inputs
from tools.first_entry_v71.clock import timestamp

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def baseline(tmp_path_factory):
    (ROOT/'data/cache/v71_active_cli_pytest_root.txt').write_text(str(tmp_path_factory.getbasetemp()),encoding='utf8')
    existing=ROOT/'data/cache/v71_fixture_cli'
    if (existing/'sample_lock.json').is_file():return existing
    folder=tmp_path_factory.mktemp('v71_full_pool')/'snapshot';build(folder);return folder


def clone(baseline,tmp_path):
    return Path(shutil.copytree(baseline,tmp_path/'snapshot'))


def replay_change(path,transform):
    with gzip.open(path,'rt',encoding='utf8') as stream:
        header=json.loads(next(stream));rows=[json.loads(line) for line in stream]
    rows=transform(rows)
    with gzip.open(path,'wt',encoding='utf8') as stream:
        stream.write(json.dumps(header)+'\n')
        for row in rows:stream.write(json.dumps(row)+'\n')


def cli(folder,output,score=False):
    command=[sys.executable,'-m','tools.first_entry_v71.entrypoint','--manifest',str(folder/'manifest.json'),
        '--input-root',str(folder),'--sample-lock',str(folder/'sample_lock.json'),'--output',str(output),'--fixture']
    if score:command.append('--score')
    return subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=900)


@pytest.mark.parametrize('mutation',['shorten','shift','remove_day','remove_first','remove_last','path_cap'])
def test_date_changes_fail_before_replay(baseline,tmp_path,mutation):
    folder=tmp_path/'snapshot';folder.mkdir()
    for name in ('manifest.json','sample_lock.json'):shutil.copyfile(baseline/name,folder/name)
    manifest=read(folder/'manifest.json')
    if mutation=='shorten':manifest.update(first_scoring_eob='2026-03-02T09:15:00+08:00',last_scoring_eob='2026-03-03T15:00:00+08:00')
    elif mutation=='shift':manifest['first_scoring_eob']='2026-01-06T09:15:00+08:00'
    elif mutation=='path_cap':manifest['last_path_eob']='2026-05-06T21:15:00+08:00'
    else:
        days=manifest['products']['CZCE.CF']['scoring_trade_dates']
        days.pop(0 if mutation=='remove_first' else -1 if mutation=='remove_last' else 20)
    write(folder/'manifest.json',manifest)
    result=cli(folder,tmp_path/'out')
    assert result.returncode==2,result.stdout+result.stderr
    assert not (tmp_path/'out/selection/receipt.json').exists()
    failure=read(tmp_path/'out_failure.json')
    assert failure['code'] in ('REGISTERED_WINDOW_OR_POLICY_CHANGED','REGISTERED_TRADE_DAYS_CHANGED')


@pytest.mark.parametrize('mutation,expected',[
    ('fake_minutes','SCHEDULE_DIFFERS_FROM_INDEPENDENT_CALENDAR'),
    ('header_only','REPLAY_HAS_NO_COVERAGE'),
    ('false_selected','SELECTED_FLAG_BIDIRECTIONAL_MISMATCH'),
    ('missing_frame','REPLAY_PREFIX_FRAME_GAP'),
    ('duplicate_minute','RAW_TIME_NOT_UNIQUE_MONOTONIC'),
    ('wrong_contract','WRONG_OR_EMPTY_ACTUAL_CONTRACT'),
    ('missing_night','SELECTED_LEG_MINUTE_GAP'),
    ('wrong_bob','M1_BOB_EOB_CONVENTION'),
    ('warmup_gap','ACTUAL_LEG_WARMUP_INCOMPLETE')])
def test_admission_counterexamples_normal_cli(baseline,tmp_path,mutation,expected):
    folder=clone(baseline,tmp_path);manifest=read(folder/'manifest.json')
    product=manifest['products']['CZCE.CF'];contract=product['contracts'][0]
    raw_path=folder/contract['raw_path'];replay=folder/contract['replay_path']
    first=timestamp(read(folder/'sample_lock.json')['products']['CZCE.CF']['scoring_anchor_eobs'][0])
    if mutation=='fake_minutes':
        schedule=read(folder/product['schedule_path'])
        schedule=[r for r in schedule if timestamp(r['eob']).minute%15==0]
        write(folder/product['schedule_path'],schedule)
        raw=pd.read_parquet(raw_path);raw=raw[raw.eob.dt.minute%15==0];raw.to_parquet(raw_path,index=False)
    elif mutation=='header_only':replay_change(replay,lambda rows:[])
    elif mutation=='false_selected':
        def change(rows):
            next(r for r in rows if timestamp(r['decision_eob'])==first)['selected']=False;return rows
        replay_change(replay,change)
    elif mutation=='missing_frame':replay_change(replay,lambda rows:[r for r in rows if timestamp(r['decision_eob'])!=first])
    else:
        raw=pd.read_parquet(raw_path)
        if mutation=='duplicate_minute':raw=pd.concat([raw.iloc[:1],raw],ignore_index=True)
        elif mutation=='wrong_contract':raw['symbol']='WRONG.SYNTHETIC'
        elif mutation=='missing_night':raw=raw[~((raw.eob>first-pd.Timedelta(minutes=15))&(raw.eob<=first+pd.Timedelta(minutes=105)))]
        elif mutation=='wrong_bob':raw['bob']=raw.eob-pd.Timedelta(minutes=2)
        elif mutation=='warmup_gap':
            removed=raw.loc[raw.eob<first-pd.Timedelta(minutes=15),'eob'].max()
            raw=raw[raw.eob!=removed]
        raw.to_parquet(raw_path,index=False)
    rehash_inputs(folder)
    result=cli(folder,tmp_path/'out')
    assert result.returncode==2,result.stdout+result.stderr
    failure=read(tmp_path/'out_failure.json')
    assert failure['code']==expected,failure
    assert len(failure['coverage_by_product'])==12
    assert not (tmp_path/'out/selection/receipt.json').exists()


def test_full_coverage_selection_and_score_three_tables(baseline,tmp_path):
    result=cli(baseline,tmp_path/'positive',score=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'SCORED' in result.stdout
    receipt=read(tmp_path/'positive/selection/receipt.json')
    assert receipt['qualified_m15']==receipt['expected_m15']==21760
    assert receipt['s0_opportunities']==2 and receipt['first_selected']==1
    summary=read(tmp_path/'positive/score/summary.json')
    assert summary['quality']['difference']==.5
    assert summary['capacity']['retention']==1
    assert summary['verdict']['decision']=='INSUFFICIENT_EVIDENCE'  # one event cannot give block precision
    for name in ('opportunity_table.csv','episode_table.csv','ordinary_pair_table.csv'):
        assert (tmp_path/'positive/score'/name).is_file()


def test_full_coverage_true_zero_is_not_empty_replay(baseline,tmp_path):
    folder=clone(baseline,tmp_path);product=read(folder/'manifest.json')['products']['DCE.M']
    def clear(rows):
        for row in rows:
            core=row['groups']['S0'];core['trend_decision']['state']['trend_state']='none'
            core['entry_evaluation']=dict(accepted=False,gate_open=True,formed_paths=[],reason='NO_PATH')
        return rows
    replay_change(folder/product['contracts'][0]['replay_path'],clear);rehash_inputs(folder)
    result=cli(folder,tmp_path/'zero',score=True)
    assert result.returncode==0,result.stdout+result.stderr
    receipt=read(tmp_path/'zero/selection/receipt.json')
    assert receipt['status']=='SELECTED_NO_OPPORTUNITIES' and receipt['qualified_m15']==21760
    assert read(tmp_path/'zero/score/summary.json')['verdict']['decision']=='INSUFFICIENT_EVIDENCE'


def two_leg_variant(baseline,tmp_path,first_selected_day):
    folder=clone(baseline,tmp_path);manifest=read(folder/'manifest.json');product=manifest['products']['CZCE.CJ']
    original=product['contracts'][0];second_symbol='CZCE.CJ.SECOND_SYNTHETIC'
    second=dict(symbol=second_symbol,raw_path='second.parquet',replay_path='second.jsonl.gz')
    raw=pd.read_parquet(folder/original['raw_path']);raw=raw[raw.eob>=timestamp('2025-12-15T00:00:00+08:00')].copy()
    raw['symbol']=second_symbol;raw.to_parquet(folder/second['raw_path'],index=False)
    with gzip.open(folder/original['replay_path'],'rt',encoding='utf8') as stream:
        header=json.loads(next(stream));rows=[json.loads(line) for line in stream]
    header['task']['symbol']=second_symbol
    with gzip.open(folder/second['replay_path'],'wt',encoding='utf8') as stream:
        stream.write(json.dumps(header)+'\n')
        for row in rows:
            if timestamp(row['decision_eob'])<timestamp('2025-12-15T00:00:00+08:00'):continue
            row['bar']['symbol']=second_symbol;row['selected']=row['trading_date']>=first_selected_day
            stream.write(json.dumps(row)+'\n')
    def flag_original(rows):
        for row in rows:row['selected']=row['trading_date']<first_selected_day
        return rows
    replay_change(folder/original['replay_path'],flag_original)
    mapping=read(folder/product['selection_path'])
    for day in mapping:
        if day>=first_selected_day:mapping[day]=second_symbol
    write(folder/product['selection_path'],mapping)
    product['contracts']=[second,original]  # Check second leg independently; never borrow original warmup.
    write(folder/'manifest.json',manifest);rehash_inputs(folder)
    return folder


def test_unselected_early_leg_may_warm_up_before_its_later_selection(baseline,tmp_path):
    folder=two_leg_variant(baseline,tmp_path,'2026-04-20')
    result=cli(folder,tmp_path/'later')
    assert result.returncode==0,result.stdout+result.stderr
    assert read(tmp_path/'later/selection/receipt.json')['qualified_m15']==21760


def test_second_leg_cannot_borrow_first_leg_warmup(baseline,tmp_path):
    folder=two_leg_variant(baseline,tmp_path,'2026-01-05')
    result=cli(folder,tmp_path/'early')
    assert result.returncode==2,result.stdout+result.stderr
    assert read(tmp_path/'early_failure.json')['code']=='ACTUAL_LEG_WARMUP_INCOMPLETE'
    assert not (tmp_path/'early/selection/receipt.json').exists()
