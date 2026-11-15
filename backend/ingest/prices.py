"""End-of-day prices + volume via yfinance -> SQLite `prices`, plus a 20-session volume baseline.

    python -m ingest.prices [--tickers RELIANCE TCS] [--days 90] [--offline] [-v]

Idempotent: rows upsert on (symbol, date); avg_vol_20 is recomputed from the stored history each run.
Every fetch goes through app.fetch.cached_fetch, so a dead Yahoo falls back to the last snapshot.
Running during market hours stores a partial bar for today; the next run overwrites it.

avg_vol_20 on a row = mean volume of the 20 sessions strictly BEFORE that date (so "today's volume vs.
recent average" never includes today). It is NULL until 20 prior sessions exist.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import sys
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from app.config import Settings, get_settings
from app.db import connect, init_db
from app.fetch import SnapshotMissing, cached_fetch

from .common import Company, load_portfolio, setup_logging

log = logging.getLogger("ingest.prices")
AVG_WINDOW = 20
DEFAULT_DAYS = 90


# ---- pure logic (unit-tested) ---------------------------------------------------------------------
def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def normalize_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """yfinance DataFrame records -> [{date, open, high, low, close, volume}] sorted by date.

    Tolerates Timestamp or str dates, any key casing, NaNs. Rows without a close are dropped (Yahoo
    sometimes returns an empty bar for today). Duplicate dates keep the last occurrence."""
    by_date: dict[str, dict[str, Any]] = {}
    for rec in records:
        r = {str(k).lower(): v for k, v in rec.items()}
        raw = r.get("date", r.get("datetime"))
        if raw is None:
            continue
        d = str(raw)[:10]
        try:
            date.fromisoformat(d)
        except ValueError:
            continue
        close = _num(r.get("close"))
        if close is None:
            continue
        vol = _num(r.get("volume"))
        by_date[d] = {
            "date": d,
            "open": _num(r.get("open")),
            "high": _num(r.get("high")),
            "low": _num(r.get("low")),
            "close": close,
            "volume": int(vol) if vol is not None else None,
        }
    return [by_date[k] for k in sorted(by_date)]


def rolling_avg_volume(volumes: list[int | None], window: int = AVG_WINDOW) -> list[float | None]:
    """Mean of the `window` values strictly before each index; None if fewer than `window` or any gap."""
    out: list[float | None] = []
    for i in range(len(volumes)):
        prev = volumes[i - window : i] if i >= window else []
        if len(prev) == window and all(v is not None for v in prev):
            out.append(round(sum(prev) / window, 2))  # type: ignore[arg-type]
        else:
            out.append(None)
    return out


# ---- DB -------------------------------------------------------------------------------------------
def upsert_prices(conn, ticker: str, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT INTO prices(symbol,date,open,high,low,close,volume)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(symbol,date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume""",
        [(ticker, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
    )


def recompute_avg_volume(conn, ticker: str, window: int = AVG_WINDOW) -> None:
    rows = conn.execute("SELECT date, volume FROM prices WHERE symbol=? ORDER BY date", (ticker,)).fetchall()
    avgs = rolling_avg_volume([r["volume"] for r in rows], window)
    conn.executemany(
        "UPDATE prices SET avg_vol_20=? WHERE symbol=? AND date=?",
        [(a, ticker, r["date"]) for a, r in zip(avgs, rows)],
    )


# ---- live fetch (network) -------------------------------------------------------------------------
def fetch_history(yahoo_symbol: str, days: int, today: date | None = None) -> list[dict[str, Any]]:
    import yfinance as yf  # lazy: heavy, and absent in unit tests

    end = (today or date.today()) + timedelta(days=1)  # yfinance's `end` is exclusive
    start = end - timedelta(days=days + 1)
    df = yf.Ticker(yahoo_symbol).history(
        start=start.isoformat(), end=end.isoformat(), auto_adjust=False, actions=False
    )
    if df is None or df.empty:
        return []
    return normalize_rows(df.reset_index().to_dict("records"))


# ---- orchestration --------------------------------------------------------------------------------
def ingest_prices(
    settings: Settings,
    companies: list[Company],
    days: int = DEFAULT_DAYS,
    history_fn: Callable[[str, int], list[dict[str, Any]]] = fetch_history,
) -> list[dict[str, Any]]:
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    results: list[dict[str, Any]] = []
    try:
        for c in companies:
            res: dict[str, Any] = {"ticker": c.ticker, "source": None, "rows": 0, "last_date": None,
                                   "vol_ratio": None, "error": None}
            try:
                f = cached_fetch(
                    settings, "prices", c.yahoo, lambda sym=c.yahoo: history_fn(sym, days)
                )
                upsert_prices(conn, c.ticker, f.data)
                recompute_avg_volume(conn, c.ticker)
                conn.commit()
                last = conn.execute(
                    "SELECT date, volume, avg_vol_20 FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
                    (c.ticker,),
                ).fetchone()
                res.update(source=f.source, rows=len(f.data), last_date=last["date"])
                if last["avg_vol_20"] and last["volume"] is not None:
                    res["vol_ratio"] = round(last["volume"] / last["avg_vol_20"], 2)
                if f.source == "snapshot":
                    log.warning("%s: live fetch failed (%s); used snapshot from %s", c.ticker, f.error, f.fetched_at)
            except SnapshotMissing as e:
                res["error"] = str(e)
                log.error("%s: %s", c.ticker, e)
            except Exception as e:  # never let one ticker kill the batch
                res["error"] = f"{type(e).__name__}: {e}"
                log.exception("%s: unexpected failure", c.ticker)
            results.append(res)
    finally:
        conn.close()
    return results


def _print(results: list[dict[str, Any]]) -> None:
    print(f"{'ticker':<12}{'source':<10}{'rows':>5}  {'last':<11}{'vol/avg20':>9}  note")
    for r in results:
        print(f"{r['ticker']:<12}{(r['source'] or '-'):<10}{r['rows']:>5}  {(r['last_date'] or '-'):<11}"
              f"{(r['vol_ratio'] if r['vol_ratio'] is not None else '-'):>9}  {r['error'] or ''}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingest.prices", description=__doc__.split("\n")[0])
    p.add_argument("--tickers", nargs="*", help="NSE tickers (default: whole portfolio.yaml)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--offline", action="store_true", help="snapshots only, no network")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    setup_logging(a.verbose)
    settings = get_settings()
    if a.offline:
        settings = dataclasses.replace(settings, offline=True)
    results = ingest_prices(settings, load_portfolio(tickers=a.tickers), a.days)
    _print(results)
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
