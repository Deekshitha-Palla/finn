"""Unusual price/volume moves from the prices table. No network, no LLM."""
from __future__ import annotations

from typing import Any

# Latest two sessions per ticker: enough for a close-to-close move.
SQL = """SELECT symbol, date, close, volume, avg_vol_20 FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn FROM prices
) WHERE rn <= 2"""


def load_prices(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(SQL)]


def _check(latest: dict, prev: dict | None, vol_mult: float, move_pct: float):
    close, vol, avg = latest["close"], latest["volume"], latest["avg_vol_20"]
    prev_close = prev["close"] if prev else None
    # None means "can't tell" (one row, NULL/0 baseline, missing close): never a flag, never a crash.
    move = round((close / prev_close - 1) * 100, 6) if close is not None and prev_close else None
    ratio = round(vol / avg, 6) if vol is not None and avg else None
    hit_vol = ratio is not None and ratio >= vol_mult
    hit_move = move is not None and abs(move) >= move_pct
    if not (hit_vol or hit_move):
        return None
    reasons = []
    if hit_vol:
        reasons.append(f"Volume {ratio:.1f}x its 20-day average")
    if hit_move:
        reasons.append(f"Close {move:+.1f}% vs prior session")
    severity = max(ratio / vol_mult if hit_vol else 0, abs(move) / move_pct if hit_move else 0)
    return severity, {
        "ticker": latest["symbol"], "date": latest["date"], "close": close,
        "move_pct": None if move is None else round(move, 2), "volume": vol, "avg_vol_20": avg,
        "ratio": None if ratio is None else round(ratio, 2),
        "triggers": [k for k, hit in (("volume", hit_vol), ("move", hit_move)) if hit],
        "reason": "; ".join(reasons),
    }


def build_alerts(rows: list[dict[str, Any]], vol_mult: float = 2.0, move_pct: float = 3.0) -> dict[str, Any]:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r)
    hits = []
    for symbol, rs in by_symbol.items():
        rs.sort(key=lambda r: r["date"], reverse=True)  # each ticker's own latest session
        hit = _check(rs[0], rs[1] if len(rs) > 1 else None, vol_mult, move_pct)
        if hit:
            hits.append(hit)
    hits.sort(key=lambda h: (-h[0], h[1]["ticker"]))  # strongest signal first
    alerts = [h[1] for h in hits]
    return {"alerts": alerts, "count": len(alerts), "as_of": max((r["date"] for r in rows), default=None),
            "vol_mult": vol_mult, "move_pct": move_pct}
