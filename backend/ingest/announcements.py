"""Corporate announcements: BSE public API (primary) -> NSE (fallback) -> committed snapshot.

    python -m ingest.announcements [fetch] [--tickers ...] [--days 30] [--offline]
    python -m ingest.announcements refresh-snapshot [--tickers ...] [--days 30]

Stored per announcement: ticker, ts (ISO, +05:30), subject, category_raw, pdf_url (+ detail, source).
Idempotent: upsert by exchange id; re-runs never touch our own classification columns
(category / priority / summary).

!! UNVERIFIED AGAINST LIVE SERVERS. The endpoints, params and JSON field names below are from memory of
!! BSE's and NSE's undocumented public APIs and could not be tested from the build sandbox. The parsers are
!! unit-tested against hand-written samples shaped like those responses. First thing to do on a machine with
!! internet: `python -m ingest.announcements -v --tickers RELIANCE`, and adjust the field names in
!! parse_bse / parse_nse if the output is empty. Both APIs are cookie/anti-bot gated and commonly refuse
!! datacenter IPs; the snapshot is the real demo path.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import Settings, get_settings
from app.db import connect, init_db

from .common import Company, FetchError, get_with_retries, load_portfolio, make_client, setup_logging

log = logging.getLogger("ingest.announcements")

IST = timezone(timedelta(hours=5, minutes=30))

BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"  # older files move to AttachHis
BSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}
NSE_HOME = "https://www.nseindia.com/"
NSE_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


# ---- pure parsing (unit-tested) -------------------------------------------------------------------
def _iso_ist(s: Any) -> str | None:
    """'2024-11-14T16:32:01.34' / '2024-11-14 16:32:01' (naive IST) -> '2024-11-14T16:32:01+05:30'."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}:\d{2}))?", str(s or ""))
    if not m:
        return None
    try:
        return datetime.fromisoformat(f"{m[1]}T{m[2] or '00:00:00'}").replace(tzinfo=IST).isoformat()
    except ValueError:
        return None


def _nse_dt(s: Any) -> str | None:
    """'14-Nov-2024 16:32:01' -> ISO IST."""
    try:
        return datetime.strptime(str(s).strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST).isoformat()
    except ValueError:
        return None


def clean_subject(newssub: Any) -> str:
    """BSE prefixes subjects with 'Company Name - 500325 - '. Strip that."""
    s = re.sub(r"\s+", " ", str(newssub or "")).strip()
    return re.sub(r"^.{1,120}? - \d{5,6} - ", "", s)


def _fallback_id(source: str, ticker: str, ts: str, subject: str) -> str:
    return f"{source}:" + hashlib.sha1(f"{ticker}|{ts}|{subject}".encode()).hexdigest()[:16]


def parse_bse(payload: Any, ticker: str) -> tuple[list[dict[str, Any]], int | None]:
    """BSE JSON {"Table":[...], "Table1":[{"ROWCNT":n}]} -> (rows, total). Raises FetchError on a shape that
    is not BSE's (e.g. an HTML/blocked page that happened to parse)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Table"), list):
        raise FetchError("unexpected BSE payload shape (no 'Table' list)")
    rows: list[dict[str, Any]] = []
    for r in payload["Table"]:
        ts = _iso_ist(r.get("NEWS_DT") or r.get("DT_TM"))
        subject = clean_subject(r.get("NEWSSUB")) or str(r.get("HEADLINE") or "").strip()
        if not ts or not subject:
            log.debug("skipping BSE row without date/subject: %r", r)
            continue
        att = str(r.get("ATTACHMENTNAME") or "").strip()
        cat = " / ".join(
            str(x).strip() for x in (r.get("CATEGORYNAME"), r.get("SUBCATNAME")) if x and str(x).strip()
        )
        nid = str(r.get("NEWSID") or "").strip()
        rows.append(
            {
                "id": f"bse:{nid}" if nid else _fallback_id("bse", ticker, ts, subject),
                "ticker": ticker,
                "ts": ts,
                "subject": subject,
                "category_raw": cat or None,
                "pdf_url": BSE_PDF_BASE + att if att else None,
                "detail": (str(r.get("HEADLINE") or "").strip() or None),
                "source": "bse",
            }
        )
    total: int | None = None
    try:
        total = int(payload["Table1"][0]["ROWCNT"])
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return rows, total


def parse_nse(payload: Any, ticker: str) -> list[dict[str, Any]]:
    """NSE JSON list (or {"data":[...]}) -> rows. category_raw is NSE's `desc` (its announcement type)."""
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FetchError("unexpected NSE payload shape (no list)")
    rows: list[dict[str, Any]] = []
    for r in items:
        ts = _iso_ist(r.get("sort_date")) or _nse_dt(r.get("an_dt"))
        subject = re.sub(r"\s+", " ", str(r.get("desc") or "")).strip()
        if not ts or not subject:
            continue
        f = str(r.get("attchmntFile") or "").strip()
        sid = str(r.get("seq_id") or "").strip()
        rows.append(
            {
                "id": f"nse:{sid}" if sid else _fallback_id("nse", ticker, ts, subject),
                "ticker": ticker,
                "ts": ts,
                "subject": subject,
                "category_raw": subject,
                "pdf_url": f if f.startswith("http") else None,
                "detail": (str(r.get("attchmntText") or "").strip() or None),
                "source": "nse",
            }
        )
    return rows


def bse_params(scrip: str, start: date, end: date, page: int) -> dict[str, str]:
    return {
        "pageno": str(page),
        "strCat": "-1",
        "subcategory": "-1",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strToDate": end.strftime("%Y%m%d"),
        "strScrip": scrip,
        "strSearch": "P",
        "strType": "C",
    }


# ---- live fetch -----------------------------------------------------------------------------------
def fetch_bse(client, company: Company, start: date, end: date, *, max_pages: int = 10,
              sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    if not company.bse_code:
        raise FetchError(f"{company.ticker}: no BSE scrip code")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        r = get_with_retries(client, BSE_URL, params=bse_params(company.bse_code, start, end, page),
                             headers=BSE_HEADERS, sleep=sleep)
        try:
            payload = r.json()
        except ValueError as e:
            raise FetchError("BSE returned non-JSON (likely blocked)") from e
        page_rows, total = parse_bse(payload, company.ticker)
        new = [x for x in page_rows if x["id"] not in seen]
        if not new:
            break
        seen.update(x["id"] for x in new)
        rows.extend(new)
        if total is not None and len(rows) >= total:
            break
    return rows


def fetch_nse(client, company: Company, start: date, end: date, *,
              sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    get_with_retries(client, NSE_HOME, headers=NSE_HEADERS, sleep=sleep)  # warm-up: NSE issues cookies here
    r = get_with_retries(
        client, NSE_URL,
        params={"index": "equities", "symbol": company.ticker,
                "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")},
        headers=NSE_HEADERS, sleep=sleep,
    )
    try:
        payload = r.json()
    except ValueError as e:
        raise FetchError("NSE returned non-JSON (likely blocked)") from e
    return parse_nse(payload, company.ticker)


def fetch_live(company: Company, start: date, end: date, *, bse_client, nse_client,
               sleep: Callable[[float], None] = time.sleep) -> tuple[list[dict[str, Any]], str]:
    """BSE first, NSE if BSE fails. An EMPTY result is a valid answer (no filings), not a failure."""
    errors: list[str] = []
    for name, fn, client in (("bse", fetch_bse, bse_client), ("nse", fetch_nse, nse_client)):
        try:
            return fn(client, company, start, end, sleep=sleep), name
        except Exception as e:
            errors.append(f"{name}: {e}")
            log.warning("%s: %s failed: %s", company.ticker, name, e)
    raise FetchError("; ".join(errors))


# ---- snapshot -------------------------------------------------------------------------------------
def snapshot_path(settings: Settings) -> Path:
    return settings.snapshot_dir / "announcements.json"


def load_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        snap = json.loads(Path(path).read_text())
        if isinstance(snap.get("announcements"), list):
            snap.setdefault("tickers", {})
            return snap
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def write_snapshot(path: Path, snap: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


# ---- DB -------------------------------------------------------------------------------------------
def upsert_announcements(conn, rows: list[dict[str, Any]]) -> None:
    """Never touches category / priority / summary (those are ours, written by the classifier)."""
    conn.executemany(
        """INSERT INTO announcements(id,symbol,ts,subject,category_raw,pdf_url,detail,source)
           VALUES(:id,:ticker,:ts,:subject,:category_raw,:pdf_url,:detail,:source)
           ON CONFLICT(id) DO UPDATE SET
             ts=excluded.ts, subject=excluded.subject, category_raw=excluded.category_raw,
             pdf_url=excluded.pdf_url, detail=excluded.detail, source=excluded.source""",
        rows,
    )


# ---- orchestration --------------------------------------------------------------------------------
class _Clients:
    """Creates real httpx clients lazily; tests inject fakes."""

    def __init__(self, bse=None, nse=None):
        self._bse, self._nse, self._owned = bse, nse, []

    @property
    def bse(self):
        if self._bse is None:
            self._bse = make_client(); self._owned.append(self._bse)
        return self._bse

    @property
    def nse(self):
        if self._nse is None:
            self._nse = make_client(); self._owned.append(self._nse)
        return self._nse

    def close(self):
        for c in self._owned:
            c.close()


def ingest_announcements(
    settings: Settings, companies: list[Company], days: int = 30, *,
    bse_client=None, nse_client=None, sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
) -> list[dict[str, Any]]:
    init_db(settings.db_path)
    snap = load_snapshot(snapshot_path(settings))
    end = today or date.today()
    start = end - timedelta(days=days)
    clients = _Clients(bse_client, nse_client)
    conn = connect(settings.db_path)
    results: list[dict[str, Any]] = []
    try:
        for c in companies:
            res: dict[str, Any] = {"ticker": c.ticker, "source": None, "rows": 0, "error": None}
            rows: list[dict[str, Any]] | None = None
            if not settings.offline:
                try:
                    rows, res["source"] = fetch_live(c, start, end, bse_client=clients.bse,
                                                     nse_client=clients.nse, sleep=sleep)
                except Exception as e:
                    res["error"] = str(e)
                sleep(0.5)  # be polite to the exchanges
            if rows is None:
                have = snap is not None and (
                    c.ticker in snap["tickers"] or any(r["ticker"] == c.ticker for r in snap["announcements"])
                )
                if have:
                    rows = [r for r in snap["announcements"] if r["ticker"] == c.ticker]
                    res["source"] = "snapshot"
                    log.warning("%s: using snapshot (%s)", c.ticker, res["error"] or "offline mode")
                else:
                    res["error"] = res["error"] or "offline and not in snapshot"
                    log.error("%s: no live data and no snapshot: %s", c.ticker, res["error"])
                    results.append(res)
                    continue
            upsert_announcements(conn, rows)
            conn.commit()
            res["rows"] = len(rows)
            results.append(res)
    finally:
        conn.close()
        clients.close()
    return results


def refresh_snapshot(
    settings: Settings, companies: list[Company], days: int = 30, *,
    bse_client=None, nse_client=None, sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Fetch LIVE only and merge into data/snapshots/announcements.json. A ticker whose live fetch fails
    keeps its previous snapshot rows. Writes nothing if every ticker failed."""
    path = snapshot_path(settings)
    old = load_snapshot(path) or {"announcements": [], "tickers": {}}
    end = today or date.today()
    start = end - timedelta(days=days)
    clients = _Clients(bse_client, nse_client)
    fresh: dict[str, list[dict[str, Any]]] = {}
    meta = dict(old["tickers"])
    results: list[dict[str, Any]] = []
    try:
        for c in companies:
            try:
                rows, src = fetch_live(c, start, end, bse_client=clients.bse, nse_client=clients.nse, sleep=sleep)
                fresh[c.ticker] = rows
                meta[c.ticker] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  "source": src, "window_days": days}
                results.append({"ticker": c.ticker, "source": src, "rows": len(rows), "error": None})
            except Exception as e:
                results.append({"ticker": c.ticker, "source": None, "rows": 0, "error": str(e)})
            sleep(0.5)
    finally:
        clients.close()
    if not fresh:
        return results
    kept = [r for r in old["announcements"] if r["ticker"] not in fresh]
    merged = kept + [r for rows in fresh.values() for r in rows]
    merged.sort(key=lambda r: (r["ticker"], r["ts"]))
    write_snapshot(path, {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tickers": meta,
        "announcements": merged,
    })
    return results


def _print(results: list[dict[str, Any]]) -> None:
    print(f"{'ticker':<12}{'source':<10}{'rows':>5}  note")
    for r in results:
        print(f"{r['ticker']:<12}{(r['source'] or '-'):<10}{r['rows']:>5}  {r['error'] or ''}")


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tickers", nargs="*", help="NSE tickers (default: whole portfolio.yaml)")
    common.add_argument("--days", type=int, default=30, help="look-back window")
    common.add_argument("-v", "--verbose", action="store_true")
    p = argparse.ArgumentParser(prog="ingest.announcements", parents=[common])
    p.add_argument("--offline", action="store_true", help="snapshot only, no network")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("fetch", parents=[common], help="fetch (default) into SQLite, falling back to the snapshot")
    sub.add_parser("refresh-snapshot", parents=[common], help="live fetch -> data/snapshots/announcements.json")
    a = p.parse_args(argv)
    setup_logging(a.verbose)
    settings = get_settings()
    companies = load_portfolio(tickers=a.tickers)
    if a.cmd == "refresh-snapshot":
        results = refresh_snapshot(settings, companies, a.days)
        _print(results)
        if all(r["error"] for r in results):
            print("\nALL live fetches failed: snapshot NOT modified.", file=sys.stderr)
            return 1
        print(f"\nsnapshot: {snapshot_path(settings)}  (commit it)")
        return 1 if any(r["error"] for r in results) else 0
    if getattr(a, "offline", False):
        settings = dataclasses.replace(settings, offline=True)
    results = ingest_announcements(settings, companies, a.days)
    _print(results)
    return 1 if any(r["error"] and r["source"] != "snapshot" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
