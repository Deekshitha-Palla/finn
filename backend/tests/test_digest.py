import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect, init_db
from app.routers.digest import router
from app.services.digest import build_digest, classify, why_it_matters


def row(id, ts, subject, raw, symbol="RELIANCE"):
    return {"id": id, "symbol": symbol, "ts": ts, "subject": subject, "category_raw": raw,
            "pdf_url": f"https://x/{id}.pdf", "detail": ""}


ROWS = [
    row("fr", "2026-10-27T19:06:47+05:30", "Board Meeting Intimation - issue of non-convertible debentures", "fund_raise"),
    row("out", "2026-10-30T19:38:05+05:30", "Outcome of Board Meeting", "results"),
    row("res", "2026-10-30T19:42:49+05:30",
        "Unaudited Financial Results (Consolidated and Standalone) for quarter and half year ended Sep 30, 2026", "results"),
    row("m1", "2026-11-06T19:17:12+05:30", "Analyst / Investor Meet - Intimation (A)", "investor_meetings"),
    row("m2", "2026-11-12T20:22:14+05:30", "Analyst / Investor Meet - Intimation (B)", "investor_meetings"),
    row("m3", "2026-11-13T18:23:17+05:30", "Analyst / Investor Meet - Intimation (C)", "investor_meetings"),
]


def prep(rows):
    return [{**r, "category": classify(r["category_raw"], r["subject"])} for r in rows]


@pytest.mark.parametrize("raw,subject,expected", [
    ("results", "x", "results"),
    ("M&A", "x", "M&A"),
    ("Board Meeting", "Quarterly Results for Sep 2026", "results"),
    ("Board Meeting", "Considering Financial Results", "results"),
    ("Board Meeting", "Issuance of Debentures", "fund_raise"),
    ("Board Meeting", "Allotment of shares", "fund_raise"),
    ("Board Meeting", "Trading window closure", "unclassified"),
    ("something odd", "x", "unclassified"),
    (None, None, "unclassified"),
])
def test_mapping(raw, subject, expected):
    assert classify(raw, subject) == expected


def test_collapsing():
    d = build_digest(prep(ROWS), "30d")
    results = [c for c in d["ranked"] if c["category"] == "results"]
    assert len(results) == 1
    assert results[0]["subject"].startswith("Unaudited Financial Results")  # richest subject leads
    assert [r["pdf_url"] for r in results[0]["related"]] == ["https://x/out.pdf"]


def test_collapse_keys_respect_ticker_date_category():
    extra = [row("div", "2026-10-30T20:00:00+05:30", "Dividend declared", "dividend"),
             row("oth", "2026-10-30T20:05:00+05:30", "Unaudited Financial Results", "results", symbol="TCS")]
    d = build_digest(prep(ROWS + extra), "30d")
    assert len([c for c in d["ranked"] if c["category"] == "results"]) == 2  # RELIANCE + TCS stay apart
    assert any(c["category"] == "dividend" for c in d["ranked"])            # same day, other category


def test_ranking():
    d = build_digest(prep(ROWS), "30d")
    assert [c["category"] for c in d["ranked"]] == ["results", "fund_raise"]
    assert [c["score"] for c in d["ranked"]] == [80, 45]  # 60 + 20 bonus; 45
    assert "score 80: 60 category + 20 bonus" in d["ranked"][0]["why_it_matters"]
    assert "bonus" not in why_it_matters("fund_raise")


def test_routine_group():
    d = build_digest(prep(ROWS), "30d")
    assert [c["category"] for c in d["routine"]] == ["investor_meetings"] * 3
    assert all(c["category"] != "investor_meetings" for c in d["ranked"])


@pytest.mark.parametrize("window,n_ranked,n_routine", [("24h", 0, 2), ("3d", 0, 2), ("7d", 0, 3), ("30d", 2, 3)])
def test_window_anchored_to_newest(window, n_ranked, n_routine):
    d = build_digest(prep(ROWS), window)
    assert d["now"] == "2026-11-13T18:23:17+05:30"
    assert (len(d["ranked"]), len(d["routine"])) == (n_ranked, n_routine)


@pytest.mark.parametrize("window", ["24h", "3d", "7d", "30d"])
def test_latest_material_ignores_window(window):
    d = build_digest(prep(ROWS), window)
    assert d["latest_material"]["category"] == "results"
    assert d["latest_material"]["ts"] == "2026-10-30T19:42:49+05:30"


def test_empty():
    assert build_digest([], "24h") == {"ranked": [], "routine": [], "now": None, "latest_material": None}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FINN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db = get_settings().db_path
    init_db(db)
    conn = connect(db)
    conn.executemany(
        "INSERT INTO announcements(id,symbol,ts,subject,category_raw,pdf_url,detail)"
        " VALUES(:id,:symbol,:ts,:subject,:category_raw,:pdf_url,:detail)", ROWS)
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), db
    get_settings.cache_clear()


def test_endpoint(client):
    c, db = client
    body = c.get("/api/digest?window=30d").json()
    assert set(body) == {"ranked", "routine", "now", "latest_material"}
    assert [i["category"] for i in body["ranked"]] == ["results", "fund_raise"]
    assert len(body["routine"]) == 3
    assert c.get("/api/digest?window=1y").status_code == 422
    conn = connect(db)  # classifier columns were written
    assert conn.execute("SELECT COUNT(*) FROM announcements WHERE category IS NULL OR priority IS NULL").fetchone()[0] == 0
    conn.close()
