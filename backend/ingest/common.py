"""Shared ingest plumbing: portfolio loading, resilient HTTP, logging.

Ingest is the ONLY place (besides app/llm.py) that touches the network. httpx / yfinance are imported
lazily so parsing logic stays importable and testable without them.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

PORTFOLIO_PATH = Path(__file__).resolve().parents[1] / "portfolio.yaml"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

log = logging.getLogger("ingest")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class FetchError(RuntimeError):
    """A fetch failed in a way the caller should handle (fall back / skip)."""


# ---- portfolio ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Company:
    ticker: str
    name: str = ""
    sector: str = ""
    cap: str = ""
    bse_code: str | None = None

    @property
    def yahoo(self) -> str:
        return f"{self.ticker}.NS"


def load_portfolio(path: Path = PORTFOLIO_PATH, tickers: Iterable[str] | None = None) -> list[Company]:
    """Load portfolio.yaml. With `tickers`, return just those; tickers not in the portfolio become
    ad-hoc Companies (prices/fundamentals work for any NSE ticker; announcements then use NSE only)."""
    import yaml

    data = yaml.safe_load(Path(path).read_text()) or {}
    companies: list[Company] = []
    seen: set[str] = set()
    for r in data.get("companies") or []:
        t = str(r["ticker"]).strip().upper()
        if t in seen:
            raise ValueError(f"duplicate ticker in portfolio: {t}")
        seen.add(t)
        bse = str(r["bse_code"]).strip() if r.get("bse_code") else None
        if bse and not re.fullmatch(r"\d{6}", bse):
            raise ValueError(f"{t}: bse_code must be 6 digits, got {bse!r}")
        companies.append(
            Company(t, str(r.get("name", "")), str(r.get("sector", "")), str(r.get("cap", "")), bse)
        )
    if tickers is None:
        return companies
    by_ticker = {c.ticker: c for c in companies}
    return [by_ticker.get(t.strip().upper()) or Company(t.strip().upper()) for t in tickers]


# ---- HTTP -----------------------------------------------------------------------------------------
def make_client(headers: dict[str, str] | None = None, timeout: float = 20.0):
    import httpx

    return httpx.Client(
        headers={"User-Agent": UA, **(headers or {})}, follow_redirects=True, timeout=timeout
    )


def get_with_retries(
    client: Any,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    backoff: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 20.0,
):
    """GET with exponential backoff + jitter. Retries network errors and RETRY_STATUS codes (honouring a
    numeric Retry-After, capped at 30s). Other 4xx fail immediately. Raises FetchError when exhausted."""
    last: str = ""
    for attempt in range(retries + 1):
        delay = backoff * (2**attempt) + random.uniform(0, 0.25 * backoff)
        try:
            r = client.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as e:  # timeouts, DNS, connection resets
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code not in RETRY_STATUS:
                raise FetchError(f"{last} for {url}")
            ra = (getattr(r, "headers", None) or {}).get("Retry-After", "")
            if str(ra).isdigit():
                delay = min(float(ra), 30.0)
        if attempt < retries:
            log.debug("retry %d/%d for %s (%s) in %.1fs", attempt + 1, retries, url, last, delay)
            sleep(delay)
    raise FetchError(f"gave up on {url} after {retries + 1} attempts ({last})")
