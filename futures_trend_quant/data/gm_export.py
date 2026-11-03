from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


GM_HISTORY_FIELDS = (
    "symbol,eob,bob,open,high,low,close,volume,amount,position,pre_close"
)


class ExportDependencyError(RuntimeError):
    pass


class ExportDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportManifest:
    symbol: str
    frequency: str
    start_time: datetime
    end_time: datetime
    rows: int
    output_path: Path


def _load_pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportDependencyError("pandas is required for GM parquet export") from exc
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportDependencyError("pyarrow is required; CSV fallback is intentionally disabled") from exc
    return pd


def _load_gm_api():
    try:
        from gm.api import fut_get_continuous_contracts, history
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportDependencyError("GM SDK is unavailable; install and authenticate gm.api") from exc
    return history, fut_get_continuous_contracts


def _chunk_ranges(start: datetime, end: datetime, chunk_days: int):
    if end < start:
        raise ValueError("end_time must not precede start_time")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days) - timedelta(microseconds=1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(microseconds=1)


def export_history_to_parquet(
    *,
    symbol: str,
    start_time: datetime,
    end_time: datetime,
    output_path: str | Path,
    frequency: str = "60s",
    chunk_days: int = 20,
    history_fetcher: Callable[..., Any] | None = None,
) -> ExportManifest:
    """Export raw, unadjusted GM bars in bounded requests.

    Actual SDK call contract (GM SDK 3.x):
    ``history(symbol, frequency, start_time, end_time, fields=None,
    skip_suspended=True, fill_missing=None, adjust=None,
    adjust_end_time='', df=False)``.
    """

    pd = _load_pandas()
    if not symbol or "." not in symbol:
        raise ValueError("symbol must be an exchange-qualified actual contract")
    if history_fetcher is None:
        history_fetcher, _ = _load_gm_api()
    frames = []
    for chunk_start, chunk_end in _chunk_ranges(start_time, end_time, chunk_days):
        frame = history_fetcher(
            symbol=symbol,
            frequency=frequency,
            start_time=chunk_start,
            end_time=chunk_end,
            fields=GM_HISTORY_FIELDS,
            skip_suspended=False,
            fill_missing=None,
            adjust=0,
            adjust_end_time="",
            df=True,
        )
        if frame is None:
            raise ExportDataError(f"GM history returned None for {symbol} {chunk_start}..{chunk_end}")
        if not hasattr(frame, "columns"):
            frame = pd.DataFrame(frame)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise ExportDataError(f"no M1 history returned for {symbol}; no file was written")
    combined = pd.concat(frames, ignore_index=True)
    required = {"symbol", "eob", "open", "high", "low", "close", "volume"}
    missing = required.difference(combined.columns)
    if missing:
        raise ExportDataError(f"GM history missing columns: {sorted(missing)}")
    combined = combined.drop_duplicates(subset=["symbol", "eob"], keep="last").sort_values("eob")
    if combined.empty or combined[["open", "high", "low", "close"]].isna().any().any():
        raise ExportDataError("history contains no usable rows or null OHLC values")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path, index=False, engine="pyarrow")
    return ExportManifest(symbol, frequency, start_time, end_time, len(combined), path)


def export_main_mapping_to_parquet(
    *,
    csymbol: str,
    start_date: date,
    end_date: date,
    output_path: str | Path,
    mapping_fetcher: Callable[..., Any] | None = None,
) -> Path:
    """Export GM's daily main-contract mapping.

    Actual SDK call contract is
    ``fut_get_continuous_contracts(csymbol, start_date='', end_date='')``.
    """

    pd = _load_pandas()
    if mapping_fetcher is None:
        _, mapping_fetcher = _load_gm_api()
    raw = mapping_fetcher(
        csymbol=csymbol,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    if raw is None:
        raise ExportDataError(f"GM main mapping returned None for {csymbol}")
    frame = raw if hasattr(raw, "columns") else pd.DataFrame(raw)
    if frame.empty:
        raise ExportDataError(f"GM main mapping is empty for {csymbol}")
    date_column = _first_column(frame.columns, ("trade_date", "trading_date", "date"))
    symbol_column = _first_column(frame.columns, ("symbol", "contract", "main_contract"))
    if date_column is None or symbol_column is None:
        raise ExportDataError(f"unrecognized mapping columns: {list(frame.columns)}")
    normalized = frame[[date_column, symbol_column]].rename(
        columns={date_column: "trading_date", symbol_column: "symbol"}
    )
    normalized["trading_date"] = pd.to_datetime(normalized["trading_date"]).dt.date
    if normalized["trading_date"].duplicated().any() or normalized["symbol"].isna().any():
        raise ExportDataError("main mapping contains duplicate dates or null symbols")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized.sort_values("trading_date").to_parquet(path, index=False, engine="pyarrow")
    return path


def _first_column(columns: Iterable[str], candidates: Sequence[str]) -> str | None:
    column_set = set(columns)
    return next((candidate for candidate in candidates if candidate in column_set), None)


def calculate_roll_differences(
    mapping: Mapping[date, str], closes: Mapping[tuple[date, str], float]
) -> list[dict[str, object]]:
    """Calculate additive roll gaps using same-day old/new closes.

    Missing either leg is fatal.  Silently substituting a nearby close would
    change the adjustment convention and invalidate facts parity.
    """

    output: list[dict[str, object]] = []
    previous_contract: str | None = None
    for trading_date, contract in sorted(mapping.items()):
        if previous_contract is not None and contract != previous_contract:
            old_key = (trading_date, previous_contract)
            new_key = (trading_date, contract)
            if old_key not in closes or new_key not in closes:
                raise ExportDataError(
                    f"roll {trading_date} lacks same-day close for {previous_contract} or {contract}"
                )
            output.append(
                {
                    "trading_date": trading_date,
                    "old_contract": previous_contract,
                    "new_contract": contract,
                    "price_difference": closes[new_key] - closes[old_key],
                }
            )
        previous_contract = contract
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entrypoint.  Authentication is read only from an env var."""

    import argparse
    import os

    parser = argparse.ArgumentParser(description="Fail-closed GM futures data exporter")
    parser.add_argument("--token-env", default="GM_TOKEN", help="environment variable containing GM token")
    subparsers = parser.add_subparsers(dest="command", required=True)
    history_parser = subparsers.add_parser("history")
    history_parser.add_argument("--symbol", required=True)
    history_parser.add_argument("--start", required=True)
    history_parser.add_argument("--end", required=True)
    history_parser.add_argument("--output", required=True)
    mapping_parser = subparsers.add_parser("mapping")
    mapping_parser.add_argument("--csymbol", required=True)
    mapping_parser.add_argument("--start", required=True)
    mapping_parser.add_argument("--end", required=True)
    mapping_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"missing GM token in environment variable {args.token_env}")
    try:
        from gm.api import set_token
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportDependencyError("GM SDK is unavailable") from exc
    set_token(token)
    if args.command == "history":
        export_history_to_parquet(
            symbol=args.symbol,
            start_time=datetime.fromisoformat(args.start),
            end_time=datetime.fromisoformat(args.end),
            output_path=args.output,
        )
    else:
        export_main_mapping_to_parquet(
            csymbol=args.csymbol,
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            output_path=args.output,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised manually with GM credentials
    raise SystemExit(main())
