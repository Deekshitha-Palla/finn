"""Primary documents for one NSE ticker: last ~3 annual reports + last ~6 concall transcripts (PDFs) from
screener.in, saved to data/docs/<TICKER>/ with a manifest.json.

    python -m ingest.fundamentals RELIANCE [TCS ...] [--ar 3] [--concalls 6]
    python -m ingest.fundamentals --all                     # whole portfolio.yaml
    python -m ingest.fundamentals inspect saved_page.html   # show what the parser extracts from a saved page

Idempotent: a file that already exists and is a real PDF is never re-downloaded. Downloads are written to
<name>.part and renamed only after the %PDF check passes, so a crash never leaves a corrupt file.

!! UNVERIFIED AGAINST LIVE screener.in. The HTML structure assumed (div.annual-reports / div.concalls
!! containing <li><a href>; concall rows with a date and a link whose text is "Transcript") is from memory and
!! could not be tested from the build sandbox. The parser is unit-tested on a hand-written sample of that
!! structure. If a run finds no links: save the page (browser "Save as") and run `inspect` on it, then adjust
!! _DocsParser. Also: the document links point at exchange/company hosts that sometimes refuse bots; those
!! show up as status "failed" in the manifest, not as crashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin

from app.config import Settings, get_settings

from .common import FetchError, get_with_retries, load_portfolio, make_client, setup_logging

log = logging.getLogger("ingest.fundamentals")

SCREENER = "https://www.screener.in"
HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Referer": SCREENER + "/"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---- pure parsing (unit-tested) -------------------------------------------------------------------
class _DocsParser(HTMLParser):
    """Collects <li> items inside the 'annual-reports' and 'concalls' document blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.section: str | None = None
        self.section_depth = 0
        self.li: dict[str, Any] | None = None
        self.a: dict[str, Any] | None = None
        self.annual: list[dict[str, Any]] = []
        self.concalls: list[dict[str, Any]] = []

    def handle_starttag(self, tag, attrs):
        at = dict(attrs)
        if tag == "div":
            self.depth += 1
            classes = (at.get("class") or "").split()
            if self.section is None:
                if "annual-reports" in classes:
                    self.section, self.section_depth = "ar", self.depth
                elif "concalls" in classes:
                    self.section, self.section_depth = "cc", self.depth
        elif self.section and tag == "li":
            self._close_li()
            self.li = {"texts": [], "links": []}
        elif self.section and self.li is not None and tag == "a":
            self._close_a()  # tolerate an <a> that was never closed
            self.a = {"href": at.get("href") or "", "text": []}

    def handle_endtag(self, tag):
        if tag == "div":
            if self.section and self.depth == self.section_depth:
                self._close_li()
                self.section = None
            self.depth -= 1
        elif tag == "a":
            self._close_a()
        elif tag == "li":
            self._close_li()

    def _close_a(self):
        a, self.a = self.a, None
        if a is not None and self.li is not None:
            self.li["links"].append({"href": a["href"], "text": _clean("".join(a["text"]))})

    def handle_data(self, data):
        if self.li is None:
            return
        (self.a["text"] if self.a is not None else self.li["texts"]).append(data)

    def _close_li(self):
        self._close_a()
        li, self.li = self.li, None
        if li is None or not li["links"]:
            return
        (self.annual if self.section == "ar" else self.concalls).append(li)


def _clean(s: str) -> str:
    return " ".join(s.split())


def _period(text: str) -> str | None:
    """'Nov 2024' / 'November 2024' -> '2024-11'."""
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b", text)
    if m and m[1][:3].lower() in MONTHS:
        return f"{m[2]}-{MONTHS[m[1][:3].lower()]:02d}"
    return None


def parse_screener_documents(html: str, base_url: str = SCREENER + "/") -> dict[str, list[dict[str, Any]]]:
    """-> {"annual_reports": [{label, fy, url}], "concalls": [{label, period, url}]}, newest first.

    Concall rows without a 'Transcript' link (notes / PPT only) are skipped."""
    p = _DocsParser()
    p.feed(html)
    p.close()

    annual = []
    for li in p.annual:
        link = li["links"][0]
        label = re.sub(r"\s+from\s+(bse|nse)\b.*$", "", link["text"], flags=re.I).strip()
        m = re.search(r"\b(19|20)\d{2}\b", label)
        annual.append({"label": label, "fy": int(m[0]) if m else None, "url": urljoin(base_url, link["href"])})
    annual.sort(key=lambda x: x["fy"] or 0, reverse=True)

    concalls = []
    for li in p.concalls:
        t = next((l for l in li["links"] if l["text"].lower().startswith("transcript")), None)
        if not t or not t["href"]:
            continue
        label = _clean(" ".join(li["texts"]))
        concalls.append({"label": label, "period": _period(label), "url": urljoin(base_url, t["href"])})
    concalls.sort(key=lambda x: x["period"] or "", reverse=True)
    return {"annual_reports": annual, "concalls": concalls}


def looks_like_pdf(head: bytes) -> bool:
    return b"%PDF-" in head[:1024]


def valid_pdf_file(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return looks_like_pdf(f.read(1024))
    except OSError:
        return False


def plan_files(docs: dict[str, list[dict[str, Any]]], n_ar: int, n_cc: int) -> list[dict[str, Any]]:
    """Choose the newest n_ar / n_cc items and give each a stable, unique filename."""
    items: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(kind: str, d: dict[str, Any], stem: str) -> None:
        name = f"{stem}.pdf"
        if name in used:
            name = f"{stem}_{hashlib.sha1(d['url'].encode()).hexdigest()[:6]}.pdf"
        used.add(name)
        items.append({"kind": kind, "label": d["label"], "url": d["url"], "file": name})

    for d in docs["annual_reports"][:n_ar]:
        add("annual_report", d, f"annual_report_FY{d['fy']}" if d["fy"] else
            "annual_report_" + hashlib.sha1(d["url"].encode()).hexdigest()[:8])
    for d in docs["concalls"][:n_cc]:
        add("concall", d, f"concall_{d['period']}" if d["period"] else
            "concall_" + hashlib.sha1(d["url"].encode()).hexdigest()[:8])
    return items


# ---- network --------------------------------------------------------------------------------------
def find_documents(client, ticker: str, sleep: Callable[[float], None] = time.sleep) -> tuple[str, dict]:
    """Try /company/T/consolidated/ then /company/T/. screener slugs are normally the NSE symbol; for the odd
    company whose slug differs, pass the slug (often the BSE scrip code) as the ticker argument."""
    last: Exception = FetchError("no candidate URL")
    for url in (f"{SCREENER}/company/{quote(ticker)}/consolidated/", f"{SCREENER}/company/{quote(ticker)}/"):
        try:
            r = get_with_retries(client, url, headers=HEADERS, sleep=sleep)
        except FetchError as e:
            last = e
            continue
        docs = parse_screener_documents(r.text, url)
        if docs["annual_reports"] or docs["concalls"]:
            return url, docs
        last = FetchError(f"no document links parsed from {url} (layout changed? use `inspect`)")
    raise FetchError(f"{ticker}: could not find screener documents ({last})")


def download_pdf(client, url: str, dest: Path, sleep: Callable[[float], None] = time.sleep) -> int:
    r = get_with_retries(client, url, headers=HEADERS, sleep=sleep, timeout=60.0)
    body: bytes = r.content
    if not looks_like_pdf(body):
        ctype = (getattr(r, "headers", None) or {}).get("content-type", "unknown")
        raise FetchError(f"response is not a PDF (content-type: {ctype})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, dest)
    return len(body)


def ingest_fundamentals(
    settings: Settings, ticker: str, n_ar: int = 3, n_cc: int = 6, *, client=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    out_dir = settings.data_dir / "docs" / ticker
    own = client is None
    client = client or make_client()
    try:
        page_url, docs = find_documents(client, ticker, sleep)
        items = plan_files(docs, n_ar, n_cc)
        if len(docs["annual_reports"]) < n_ar or len(docs["concalls"]) < n_cc:
            log.warning("%s: found %d annual reports / %d transcripts (wanted %d / %d)", ticker,
                        len(docs["annual_reports"]), len(docs["concalls"]), n_ar, n_cc)
        for it in items:
            dest = out_dir / it["file"]
            if valid_pdf_file(dest):
                it.update(status="skipped", bytes=dest.stat().st_size)
                continue
            try:
                it.update(status="downloaded", bytes=download_pdf(client, it["url"], dest, sleep))
            except FetchError as e:
                it.update(status="failed", error=str(e), bytes=0)
                log.warning("%s: %s failed: %s", ticker, it["file"], e)
            sleep(1.0)  # be polite to the hosts
        manifest = {"ticker": ticker, "source_page": page_url,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        os.replace(tmp, out_dir / "manifest.json")
        return manifest
    finally:
        if own:
            client.close()


def _summarize(m: dict[str, Any]) -> str:
    c: dict[str, int] = {}
    for it in m["items"]:
        c[it["status"]] = c.get(it["status"], 0) + 1
    return f"{m['ticker']}: " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())) + f"  ({m['source_page']})"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "inspect":  # offline debugging aid
        docs = parse_screener_documents(Path(argv[1]).read_text(errors="replace"))
        print(json.dumps(docs, indent=1))
        return 0 if (docs["annual_reports"] or docs["concalls"]) else 1
    p = argparse.ArgumentParser(prog="ingest.fundamentals", description=__doc__.split("\n")[0])
    p.add_argument("tickers", nargs="*")
    p.add_argument("--all", action="store_true", help="every ticker in portfolio.yaml")
    p.add_argument("--ar", type=int, default=3, help="annual reports to fetch")
    p.add_argument("--concalls", type=int, default=6, help="concall transcripts to fetch")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    setup_logging(a.verbose)
    if not a.tickers and not a.all:
        p.error("give one or more tickers, or --all")
    tickers = [c.ticker for c in load_portfolio()] if a.all else a.tickers
    settings = get_settings()
    rc = 0
    client = make_client()
    try:
        for t in tickers:
            try:
                print(_summarize(ingest_fundamentals(settings, t, a.ar, a.concalls, client=client)))
            except FetchError as e:
                print(f"{t}: FAILED: {e}", file=sys.stderr)
                rc = 1
    finally:
        client.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
