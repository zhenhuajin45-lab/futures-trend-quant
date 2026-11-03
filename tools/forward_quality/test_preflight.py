from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location("qa", Path(__file__).with_name("preflight.py"))
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
S = "CZCE.CF"
OLD, NEW = "CZCE.CF701", "CZCE.CF705"


@pytest.fixture
def prefix(tmp_path, monkeypatch):
    class FinishedDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026,11,5,16,tzinfo=tz)
    monkeypatch.setattr(qa.frozen, "datetime", FinishedDay)
    lock = dict(qa.frozen.check_lock(), universe=[S])
    monkeypatch.setattr(qa.frozen, "check_lock", lambda: lock)
    root = tmp_path/"staging"
    root.mkdir()
    sessions, hashes = [], {}
    days = pd.date_range(end="2026-11-03", periods=50).append(pd.DatetimeIndex(["2026-11-04"]))
    prev = (days[0]-timedelta(days=1)).date().isoformat()
    for i, date in enumerate(days):
        day = date.date().isoformat()
        p = root/day
        p.mkdir()
        begin = qa.frozen.stamp(day+"T09:00:00+08:00")
        end = begin+timedelta(hours=6) if i<50 else begin+timedelta(minutes=15)
        session = dict(date=day,previous=prev,source="SYNTHETIC",products={S:[[begin.isoformat(),end.isoformat()]]})
        qa.frozen.write(p/"sessions.json",session)
        t = pd.DatetimeIndex(qa.frozen.expected_times(session["products"][S]))
        q = pd.DataFrame(dict(csymbol=S,trading_date=day,symbol=OLD,bob=t-timedelta(minutes=1),
                             eob=t,available_at=t,open=100.,high=101.,low=99.,close=100.))
        q.to_parquet(p/"quotes.parquet",index=False)
        pd.DataFrame([dict(csymbol=S,trading_date=day,symbol=OLD,known_at=begin)]).to_parquet(p/"mapping.parquet")
        sessions.append(session)
        hashes[day] = qa.frozen.digest(p/"sessions.json")
        prev=day
    calendar = tmp_path/"calendar.json"
    qa.frozen.write(calendar,dict(authority="SYNTHETIC",version="test",sessions=sessions,
                                 session_sha256_by_day=hashes,fixed_first_day="2026-11-04",decision_clock_mode="NOMINAL"))
    (root/"source.txt").write_text("SYNTHETIC SOURCE REVIEW")
    evidence = dict(source_status="VERIFIED",owner="fixture",evidence_ref="source.txt",
                    sha256=qa.frozen.digest(root/"source.txt"))
    qa.frozen.write(root/"provenance.json",dict(owner="fixture",independent_reviewer="fixture-review",
                                             evidence={k:evidence for k in ("calendar","mapping","quote","decision")}))
    return root,calendar,tmp_path/"study",qa.frozen.stamp("2026-11-05T16:00:00+08:00")


def run(prefix):
    root,calendar,study,now=prefix
    return qa.preflight(root,calendar,study,now)


def change(prefix, file, transform):
    path=prefix[0]/"2026-11-04"/file
    df=pd.read_parquet(path)
    transform(df).to_parquet(path,index=False)


def test_positive_repeat_and_seal_protects_approved_bytes(prefix):
    report=run(prefix)
    assert report["exit_code"]==0 and report["proven_score_days"]==1
    assert report["products"][0]["warmup_valid_m15"]==1200
    assert report==run(prefix)
    root,calendar,study,_=prefix
    qa.frozen.prepare(study)
    # Seal an immutable snapshot, then compare the real frozen ingest receipt.
    qa.seal_passed(report,root,study)
    assert run(prefix)["sealed_score_days"]==1
    qa.seal_passed(report,root,study)
    change(prefix,"quotes.parquet",lambda df:df.assign(close=100.5))
    with pytest.raises(ValueError,match="INPUT_CHANGED_AFTER_QA"):
        qa.seal_passed(report,root,study)
    with pytest.raises(ValueError,match="SEALED_CONTENT_CONFLICT"):
        run(prefix)
    assert not (study/"unblinded").exists()


@pytest.mark.parametrize("case",["missing_active","missing_bridge","late_mapping","late_quote","mixed_m15"])
def test_bad_minutes_cannot_hide_behind_old_contract(prefix,case):
    def mutate(df):
        spare=df.assign(symbol=NEW,open=110.,high=111.,low=109.,close=110.)
        if case=="missing_active":
            return pd.concat([df.iloc[1:],spare],ignore_index=True)
        if case=="missing_bridge":
            return spare
        if case=="late_quote":
            df.loc[0,"available_at"]+=timedelta(seconds=1)
        if case=="mixed_m15":
            return pd.concat([df,spare],ignore_index=True)
        return df
    change(prefix,"quotes.parquet",mutate)
    if case in ("missing_bridge","late_mapping","mixed_m15"):
        def mapping(df):
            if case=="late_mapping":
                return df.assign(known_at=df.known_at+timedelta(minutes=1))
            if case=="mixed_m15":
                return pd.concat([df,df.assign(symbol=NEW,known_at=df.known_at+timedelta(minutes=5))])
            return df.assign(symbol=NEW)
        change(prefix,"mapping.parquet",mapping)
    result=run(prefix)
    assert result["exit_code"]==2
    p=result["products"][0]
    assert p["first_failure"]["csymbol"]==S
    if case=="missing_active":
        assert p["days"]["2026-11-04"]["received_any_leg"]==15
        assert p["days"]["2026-11-04"]["strict_valid"]==14


def test_sources_and_unclosed_session_do_not_count_as_pass(prefix):
    (prefix[0]/"provenance.json").unlink()
    assert run(prefix)["exit_code"]==3
    early=(*prefix[:3],qa.frozen.stamp("2026-11-04T09:01:00+08:00"))
    result=run(early)
    assert result["proven_score_days"]==0 and result["failures"][0]["reason"]=="AWAITING_CLOSE"


def test_1199_completed_buckets_are_insufficient(prefix):
    root,calendar,study,now=prefix
    day="2026-09-15"
    p=root/day
    s=qa.frozen.read(p/"sessions.json")
    s["products"][S][0][1]=day+"T14:45:00+08:00"
    qa.frozen.write(p/"sessions.json",s)
    q=pd.read_parquet(p/"quotes.parquet")
    q[q.eob<=qa.frozen.stamp(s["products"][S][0][1])].to_parquet(p/"quotes.parquet",index=False)
    c=qa.frozen.read(calendar)
    c["sessions"][0]=s
    c["session_sha256_by_day"][day]=qa.frozen.digest(p/"sessions.json")
    qa.frozen.write(calendar,c)
    result=run(prefix)
    assert result["products"][0]["warmup_valid_m15"]==1199
    assert result["exit_code"]==2


def test_recorded_clock_and_idle_late_leg():
    begin=qa.frozen.stamp("2026-11-04T09:00:00+08:00")
    t=pd.date_range(begin+timedelta(minutes=1),periods=15,freq="min")
    q=pd.DataFrame(dict(csymbol=S,trading_date="2026-11-04",symbol=OLD,bob=t-timedelta(minutes=1),eob=t,
                        available_at=t+timedelta(milliseconds=50),decision_at=t+timedelta(milliseconds=100),
                        open=100.,high=101.,low=99.,close=100.))
    m=pd.DataFrame([dict(symbol=OLD,known_at=begin,trading_date="2026-11-04")])
    sessions=[dict(date="2026-11-04",products={S:[[begin.isoformat(),t[-1].isoformat()]]})]
    result=qa.inspect_product(S,q,m,sessions,None)
    assert result["strict_valid_m1"]==15 and result["valid_m15"]==1 and result["first_failure"] is None
    nominal=qa.inspect_product(S,q.drop(columns="decision_at"),m,sessions,None)
    assert nominal["strict_valid_m1"]==0
    spare=q.assign(symbol=NEW,available_at=q.eob+timedelta(seconds=5))
    joined=pd.concat([q,spare])
    result=qa.inspect_product(S,joined,m,sessions,None)
    assert result["strict_valid_m1"]==15 and result["late_nonselected_quote_warnings"]==15
    spare.loc[0,"decision_at"]+=timedelta(milliseconds=1)
    assert qa.inspect_product(S,pd.concat([q,spare]),m,sessions,None)["first_failure"]
    q.loc[0,"decision_at"]=pd.NaT
    assert qa.inspect_product(S,q,m,sessions,None)["first_failure"]


def test_real_twelve_product_cardinality_and_one_insufficient_blocks_all(prefix,monkeypatch):
    root,calendar,study,now=prefix
    real_lock=qa.frozen.read(qa.frozen.LOCK)
    monkeypatch.setattr(qa.frozen,"check_lock",lambda:real_lock)
    cal=qa.frozen.read(calendar)
    for session in cal["sessions"]:
        day=session["date"]
        folder=root/day
        q=pd.read_parquet(folder/"quotes.parquet")
        m=pd.read_parquet(folder/"mapping.parquet")
        qs,ms=[],[]
        intervals=session["products"][S]
        session["products"]={}
        for symbol in real_lock["universe"]:
            actual=symbol+"701"
            qs.append(q.assign(csymbol=symbol,symbol=actual))
            ms.append(m.assign(csymbol=symbol,symbol=actual))
            session["products"][symbol]=intervals
        pd.concat(qs).to_parquet(folder/"quotes.parquet",index=False)
        pd.concat(ms).to_parquet(folder/"mapping.parquet",index=False)
        qa.frozen.write(folder/"sessions.json",session)
        cal["session_sha256_by_day"][day]=qa.frozen.digest(folder/"sessions.json")
    qa.frozen.write(calendar,cal)
    passed=run(prefix)
    assert passed["exit_code"]==0 and len(passed["products"])==12
    # Shorten only AU's first day by a whole 15 minute bucket, without minute gaps.
    day=cal["sessions"][0]["date"]
    cal["sessions"][0]["products"]["SHFE.AU"]=[[day+"T09:00:00+08:00",day+"T14:45:00+08:00"]]
    folder=root/day
    qa.frozen.write(folder/"sessions.json",cal["sessions"][0])
    cal["session_sha256_by_day"][day]=qa.frozen.digest(folder/"sessions.json")
    qa.frozen.write(calendar,cal)
    q=pd.read_parquet(folder/"quotes.parquet")
    q[~(q.csymbol.eq("SHFE.AU") & (q.eob>qa.frozen.stamp(day+"T14:45:00+08:00")))].to_parquet(folder/"quotes.parquet",index=False)
    blocked=run(prefix)
    assert blocked["exit_code"]==2 and blocked["proven_score_days"]==0
    au=next(p for p in blocked["products"] if p["csymbol"]=="SHFE.AU")
    assert au["warmup_valid_m15"]==1199
