"""Rule-based announcement digest. No LLM, no network.

classify -> store -> window (anchored to newest ts) -> collapse -> score -> split routine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.classify import Category

IST = timezone(timedelta(hours=5, minutes=30))
WINDOWS = {"24h": timedelta(hours=24), "3d": timedelta(days=3), "7d": timedelta(days=7), "30d": timedelta(days=30)}
ROUTINE = {"unclassified", "investor_meetings", "earnings_call"}

# Product decision: how much each category deserves your attention (0-100).
WEIGHT = {
    "results": 60, "M&A": 55, "management_change": 50, "litigation": 50, "fund_raise": 45,
    "credit_rating": 40, "dividend": 35, "bonus_split": 35, "capex": 35, "pledging": 30,
    "insider_trading": 30, "press_release": 25, "agm_egm": 15, "postal_ballot": 15,
    "earnings_call": 5, "investor_meetings": 5, "unclassified": 0,
}
BONUS = {"results": 20, "management_change": 15}  # the two the brief singles out
WHY = {
    "results": "Fresh numbers: check Revenue, EBITDA, PBT, PAT vs last quarter and last year",
    "M&A": "Changes what the company owns or is",
    "management_change": "Leadership change at the top",
    "litigation": "Legal or regulatory exposure",
    "fund_raise": "New capital or debt: dilution or leverage may change",
    "credit_rating": "Rating agency view on the company's debt",
    "dividend": "Cash payout declared, note the record date",
    "bonus_split": "Share count changes, no cash",
    "capex": "Capacity spend that shapes future growth",
    "pledging": "Promoter pledge status changed",
    "insider_trading": "Insider bought or sold personal shares",
    "press_release": "Company news not covered elsewhere",
}
_VALID = {c.value.lower(): c.value for c in Category}


def classify(category_raw: str | None, subject: str | None) -> str:
    raw = (category_raw or "").strip().lower()
    if raw in _VALID:
        return _VALID[raw]
    if "board meeting" in raw:
        s = (subject or "").lower()
        if "quarterly" in s or "financial result" in s:
            return "results"
        if any(k in s for k in ("debenture", "shares", "issuance")):
            return "fund_raise"
    return "unclassified"


def score(category: str) -> int:
    return WEIGHT[category] + BONUS.get(category, 0)


def priority(sc: int) -> int:  # 3 alert, 2 high, 1 normal, 0 collapsed
    return 3 if sc >= 60 else 2 if sc >= 40 else 1 if sc >= 20 else 0


def why_it_matters(category: str) -> str:
    base, bonus = WEIGHT[category], BONUS.get(category, 0)
    parts = f"{base} category" + (f" + {bonus} bonus" if bonus else "")
    return f"{WHY.get(category, category.replace('_', ' ').capitalize())} (score {base + bonus}: {parts})"


def classify_and_store(conn) -> list[dict[str, Any]]:
    """Classify every announcement, persist category + priority, return rows (with category)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, symbol, ts, subject, category_raw, pdf_url, detail FROM announcements")]
    for r in rows:
        r["category"] = classify(r["category_raw"], r["subject"])
    conn.executemany("UPDATE announcements SET category=?, priority=? WHERE id=?",
                     [(r["category"], priority(score(r["category"])), r["id"]) for r in rows])
    conn.commit()
    return rows


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _card(group: list[dict[str, Any]]) -> dict[str, Any]:
    group = sorted(group, key=lambda r: _dt(r["ts"]), reverse=True)
    # richest = longest subject+detail; max() keeps the first (newest) on ties
    lead = max(group, key=lambda r: len(r["subject"] or "") + len(r.get("detail") or ""))
    cat = lead["category"]
    return {
        "ticker": lead["symbol"], "ts": group[0]["ts"], "subject": lead["subject"],
        "category": cat, "score": score(cat), "why_it_matters": why_it_matters(cat),
        "pdf_url": lead["pdf_url"],
        "related": [{"subject": r["subject"], "pdf_url": r["pdf_url"], "ts": r["ts"]}
                    for r in group if r is not lead],
    }


def _cards(rows: list[dict[str, Any]], cutoff: datetime | None = None) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        t = _dt(r["ts"])
        if cutoff is None or t >= cutoff:
            groups.setdefault((r["symbol"], t.astimezone(IST).date(), r["category"]), []).append(r)
    cards = [_card(g) for g in groups.values()]
    cards.sort(key=lambda c: _dt(c["ts"]), reverse=True)   # newest first...
    cards.sort(key=lambda c: c["score"], reverse=True)     # ...then by score (stable)
    return cards


def build_digest(rows: list[dict[str, Any]], window: str = "24h") -> dict[str, Any]:
    """rows need symbol, ts, subject, pdf_url, detail and category (see classify_and_store)."""
    if not rows:
        return {"ranked": [], "routine": [], "now": None, "latest_material": None}
    now = max(_dt(r["ts"]) for r in rows)  # anchored to data, not wall clock
    cards = _cards(rows, now - WINDOWS[window])
    material = [c for c in _cards(rows) if c["category"] not in ROUTINE]  # whole history, ignores window
    return {
        "ranked": [c for c in cards if c["category"] not in ROUTINE],
        "routine": [c for c in cards if c["category"] in ROUTINE],
        "now": now.isoformat(),
        "latest_material": max(material, key=lambda c: _dt(c["ts"])) if material else None,
    }
