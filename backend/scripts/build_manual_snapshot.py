"""Convert a hand-copied list of BSE filings into data/snapshots/announcements.json.

Input line format:  date | subject | category | pdf_url
Usage (from backend/):  python -m scripts.build_manual_snapshot RELIANCE
"""
import json
import sys
from pathlib import Path
import hashlib

IST = "+05:30"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "snapshots" / "announcements_manual.txt"
OUT = ROOT / "data" / "snapshots" / "announcements.json"

# Exchange-disseminated times from the BSE screenshots, keyed by PDF filename.
# Rows without a known time fall back to 09:00 IST on the given date.
KNOWN_TIMES = {
    "880087f6-7ecc-40a9-8cac-de48d4616b25.pdf": "2026-10-30T19:42:49",
    "3f614a9e-94b0-42ef-8145-ba0055ba7be5.pdf": "2026-11-13T18:23:17",
    "55f9d4f5-8638-4669-abdb-7cc4127221fc.pdf": "2026-11-12T20:22:14",
    "bab324f6-6fab-42fc-8b79-a3881f4fb7c7.pdf": "2026-11-06T19:17:12",
    "9d4cb3ed-a184-4662-91fc-f4112ce354b7.pdf": "2026-10-30T19:38:05",
    "5670a3e4-4e3c-489d-9160-455cb124704f.pdf": "2026-10-27T19:06:47",
}

CATEGORIES = {
    "results", "M&A", "fund_raise", "management_change", "litigation",
    "insider_trading", "earnings_call", "investor_meetings", "credit_rating",
    "dividend", "bonus_split", "capex", "press_release", "postal_ballot",
    "agm_egm", "pledging", "unclassified",
}


def parse_line(line: str, ticker: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 4:
        raise ValueError(f"expected 4 fields, got {len(parts)}: {line!r}")
    date, subject, category, url = parts
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r} in: {line!r}")
    filename = url.rsplit("/", 1)[-1]
    stamp = KNOWN_TIMES.get(filename, f"{date}T09:00:00")
    return {
        "id": hashlib.sha1(f"{ticker}|{date}|{subject}|{url}".encode()).hexdigest(),
        "ticker": ticker,
        "ts": f"{stamp}{IST}",
        "subject": subject,
        "category_raw": category,
        "pdf_url": url,
        "detail": "",
        "source": "manual_bse_snapshot",
    }


def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    rows = []
    for raw in SRC.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            rows.append(parse_line(raw, ticker))  
    snap = {"announcements": rows, "tickers": {ticker: {"source": "manual_bse_snapshot"}}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(rows)} filings for {ticker} -> {OUT}")

if __name__ == "__main__":
    main()