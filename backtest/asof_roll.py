"""Causal, additive futures roll series. All decision timestamps use UTC+08:00."""
from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta, timezone
from itertools import groupby
from math import isfinite

import pandas as pd


CN = timezone(timedelta(hours=8))
ONE_MINUTE = timedelta(minutes=1)
PRICE_FIELDS = ("open", "high", "low", "close")


def _times(series, name, nullable=False):
    known = series.dropna()
    if len(known) and not isinstance(known.dtype, pd.DatetimeTZDtype):
        if any(pd.Timestamp(t).tzinfo is None for t in known.unique()):
            raise ValueError(f"{name} must have explicit timezone")
    if not nullable and len(known) != len(series):
        raise ValueError(f"{name} is missing")
    return pd.to_datetime(series, utc=True, format="mixed").dt.tz_convert(CN)


def _sync_pairs(quotes, old, new, day_start, known_at, previous_day):
    """Each candidate is gated by BOTH arrivals. Prefer the verified prior close."""
    legs = quotes[quotes.symbol.isin([old, new]) & quotes.available_at.notna()]
    a = legs[legs.symbol == old].set_index("eob")
    b = legs[legs.symbol == new].set_index("eob")
    pairs = a[["available_at", "close", "trading_date"]].join(
        b[["available_at", "close", "trading_date"]], how="inner", lsuffix="_old", rsuffix="_new"
    )
    if (pairs.trading_date_old != pairs.trading_date_new).any():
        raise ValueError("legs disagree on exchange trading date")
    pairs["ready"] = pairs[["available_at_old", "available_at_new"]].max(axis=1)
    before = pairs[
        (pairs.trading_date_old == previous_day) & (pairs.index < day_start)
        & (pairs.index.hour == 15) & (pairs.index.minute == 0) & (pairs.ready <= day_start)
    ] if known_at <= day_start else pairs.iloc[:0]
    if len(before):
        pick = before.iloc[-1]
        mode = "PREVIOUS_SESSION_CLOSE"
    else:
        later = pairs.loc[pairs.index >= max(day_start, known_at)]
        if later.empty:
            return None
        pick = later.sort_values("ready", kind="stable").iloc[0]
        mode = "FIRST_SYNCHRONOUS_OBSERVATION"
    return {
        "mode": mode, "observation_eob": pick.name,
        "old_eob": pick.name, "new_eob": pick.name,
        "old_available_at": pick.available_at_old, "new_available_at": pick.available_at_new,
        "old_raw_close": float(pick.close_old), "new_raw_close": float(pick.close_new),
        "delta": float(pick.close_new - pick.close_old), "available": pick.ready,
    }


def build_asof_minutes(csymbol: str, mapping: pd.DataFrame, quotes: pd.DataFrame):
    """Return (adjusted/flagged M1 frame, committed roll events) for ONE continuous symbol.

    Input schema and validity contract are documented in docs/15_asof_roll.md.
    Optional recorded decision clocks are independent of arrivals and identical
    across legs. Without that column, the decision clock is the nominal M1 close.
    """
    mapping, quotes = mapping.copy(), quotes.copy()
    for data, keys in ((mapping, {"symbol","trading_date","known_at"}),
                        (quotes, {"symbol","trading_date","bob","eob","available_at",*PRICE_FIELDS})):
        if keys - set(data):
            raise ValueError(f"required columns missing: {sorted(keys-set(data))}")
        if data.empty or data[["symbol", "trading_date"]].isna().any().any():
            raise ValueError("empty input or missing identity")
        if any("".join(ch for ch in str(s) if not ch.isdigit()).upper() != csymbol.upper()
               or not any(ch.isdigit() for ch in str(s)) for s in data.symbol.unique()):
            raise ValueError(f"wrong actual-contract universe for {csymbol}")
        data["trading_date"] = pd.to_datetime(data.trading_date).dt.date
    mapping["known_at"] = _times(mapping.known_at, "known_at", nullable=True)
    for name in ("bob","eob","available_at"):
        quotes[name] = _times(quotes[name], name, nullable=name == "available_at")
    if "decision_at" in quotes:
        quotes["decision_at"] = _times(quotes.decision_at, "decision_at")
        if ((quotes.decision_at < quotes.eob) | (quotes.decision_at >= quotes.eob + ONE_MINUTE)).any():
            raise ValueError("decision_at must be within [eob, eob + 1 minute)")
        if quotes.groupby("eob").decision_at.nunique().ne(1).any():
            raise ValueError("conflicting decision_at across legs")
    else:
        quotes["decision_at"] = quotes.eob
    if quotes.duplicated(["symbol","eob"]).any() or mapping.duplicated(["trading_date","known_at"]).any():
        raise ValueError("duplicate quote or simultaneous mapping revision")
    if ((quotes.eob-quotes.bob != ONE_MINUTE) | (quotes.eob.dt.second != 0)
            | (quotes.eob.dt.microsecond != 0) | (quotes.available_at < quotes.eob)).any():
        raise ValueError("invalid M1 source clock")
    if (quotes[list(PRICE_FIELDS)].isna().any().any() or
        any(not isfinite(x) or x <= 0 for x in quotes[list(PRICE_FIELDS)].to_numpy().ravel()) or
        (quotes.high < quotes[["open","low","close"]].max(axis=1)).any() or
        (quotes.low > quotes[["open","high","close"]].min(axis=1)).any()):
        raise ValueError("invalid contract OHLC")
    if not set(quotes.trading_date) <= set(mapping.trading_date):
        raise ValueError("unregistered trading date")
    quotes.sort_values("eob", kind="stable", inplace=True)
    decisions = {}
    for day, rows in mapping.dropna(subset=["known_at"]).sort_values("known_at").groupby("trading_date"):
        decisions[day] = (list(rows.known_at), list(rows.symbol))
    dates = sorted(set(mapping.trading_date))
    previous_days = dict(zip(dates[1:], dates))
    visible_clock = quotes[quotes.available_at.le(quotes.decision_at)]
    day_starts = visible_clock.groupby("trading_date").bob.min().to_dict()
    active = pending = bridge = None
    offset = 0.
    audit, out = [], []
    for end, batch in groupby(quotes.itertuples(index=False), key=lambda r: r.eob):
        rows = {q.symbol:q for q in batch}
        q = next(iter(rows.values()))
        day, start = q.trading_date, q.bob
        if any(x.trading_date != day for x in rows.values()):
            raise ValueError("contracts disagree on trading date")
        publications, symbols = decisions.get(day, ([], []))
        ix = bisect_right(publications, start) - 1
        target = symbols[ix] if ix >= 0 else None
        known = publications[ix] if ix >= 0 else None
        # A slot inferred *only* from a delayed bar must not be backfilled. The caller
        # compares the returned clock against the separately frozen schedule.
        clock = q.decision_at
        if not any(pd.notna(row.available_at) and row.available_at <= clock for row in rows.values()):
            continue
        record = {"symbol":csymbol, "frequency":"60s", "trading_date":day, "bob":start,
                  "eob":end, "decision_at":clock, "actual_contract":target, "mapping_known_at":known,
                  "eligible":False, "reason":"MAPPING_UNKNOWN", "cumulative_offset":None}
        selected = rows.get(target)
        # Delayed/unknown source bar must never appear in the decision-time stream.
        timely = selected is not None and pd.notna(selected.available_at) and selected.available_at <= clock
        if target and target != active and active is not None:
            if pending != target:
                pending = target
                bridge = _sync_pairs(quotes, active, target, day_starts[day], known, previous_days.get(day))
            if timely and bridge and bridge["available"] <= start:
                offset -= bridge["delta"]
                audit.append({"trading_date":day, "old_contract":active, "new_contract":target,
                              "mapping_known_at":known, **bridge, "cumulative_offset":offset,
                              "effective_bob":start, "effective_eob":end,
                              "cross_night":start.date()!=day or bridge["observation_eob"].date()!=start.date()})
                active, pending, bridge = target, None, None
        elif target:
            pending = bridge = None
            if active is None and timely:
                active = target
        if target:
            if not timely:
                record["reason"] = "ACTIVE_QUOTE_MISSING_OR_LATE"
            elif active != target:
                record["reason"] = "WAIT_SYNCHRONOUS_PAIR"
            else:
                for name in PRICE_FIELDS:
                    price = float(getattr(selected,name))
                    record["raw_"+name] = price
                    record[name] = price + offset
                if record["low"] <= 0:
                    raise ValueError("nonpositive adjusted OHLC")
                record.update(eligible=True, reason="OK", cumulative_offset=offset,
                              quote_available_at=selected.available_at)
                for name in ("volume","amount","position"):
                    record[name] = getattr(selected,name,0.)
        out.append(record)
    result = pd.DataFrame(out) if out else pd.DataFrame(columns=[
        "symbol","frequency","trading_date","bob","eob","decision_at","actual_contract","mapping_known_at",
        "eligible","reason","cumulative_offset",
    ])
    for name in (*PRICE_FIELDS,*("raw_"+x for x in PRICE_FIELDS)):
        if name not in result:
            result[name] = None
    return result, audit


def validity_windows(eligible_m15, pivot, confirmation, warmup=1200):
    """Half-open ranges on a VERIFIED expected M15 clock including invalid/missing slots."""
    mask = list(eligible_m15)
    if not 0 <= pivot <= confirmation < len(mask):
        raise ValueError("invalid pivot/confirmation indices")
    def valid(start, stop):
        return 0 <= start < stop <= len(mask) and all(pd.notna(x) and bool(x) for x in mask[start:stop])
    return {
        "confirmation":valid(confirmation,confirmation+1),
        "future_48_label":valid(confirmation,confirmation+49),
        "pivot_to_confirmation":valid(pivot,confirmation+1),
        "pivot_atr_history":valid(pivot-warmup,pivot+1),
        "strategy_warmup":valid(confirmation-warmup,confirmation+1),
    }
