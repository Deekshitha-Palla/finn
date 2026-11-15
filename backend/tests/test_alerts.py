import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect, init_db
from app.routers.alerts import router
from app.services.alerts import build_alerts


def px(sym, date, close, vol=1000, avg=1000.0):
    return {"symbol": sym, "date": date, "close": close, "volume": vol, "avg_vol_20": avg}


def pair(sym, prev_close, close, vol=1000, avg=1000.0):
    return [px(sym, "2026-11-12", prev_close), px(sym, "2026-11-13", close, vol, avg)]


def alerts(rows, v=2.0, m=3.0):
    return build_alerts(rows, v, m)["alerts"]


def test_volume_threshold_is_inclusive():
    hit = alerts(pair("A", 100, 100, vol=2000))
    assert [(a["triggers"], a["ratio"]) for a in hit] == [(["volume"], 2.0)]
    assert alerts(pair("A", 100, 100, vol=1999)) == []


@pytest.mark.parametrize("close,expected", [(103, 3.0), (97, -3.0), (102.9, None), (97.1, None)])
def test_move_threshold_inclusive_and_absolute(close, expected):
    hit = alerts(pair("A", 100, close))
    assert (hit[0]["move_pct"] if hit else None) == expected


def test_both_triggers_and_reason_string():
    (a,) = alerts(pair("A", 100, 104, vol=2500))
    assert a["triggers"] == ["volume", "move"]
    assert a["reason"] == "Volume 2.5x its 20-day average; Close +4.0% vs prior session"
    assert (a["ticker"], a["date"], a["close"], a["volume"], a["avg_vol_20"]) == ("A", "2026-11-13", 104, 2500, 1000.0)


def test_avg_vol_null_skips_volume_rule():
    assert alerts(pair("A", 100, 100, vol=5000, avg=None)) == []
    (a,) = alerts(pair("A", 100, 105, vol=5000, avg=None))  # a price move still fires
    assert a["triggers"] == ["move"] and a["ratio"] is None and a["avg_vol_20"] is None


def test_avg_vol_zero_does_not_divide():
    assert alerts(pair("A", 100, 100, vol=5000, avg=0)) == []


def test_single_row_has_no_move():
    (a,) = alerts([px("A", "2026-11-13", 100, vol=5000)])
    assert a["move_pct"] is None and a["triggers"] == ["volume"] and "Close" not in a["reason"]
    assert alerts([px("A", "2026-11-13", 100, vol=5000, avg=None)]) == []


def test_missing_previous_close_or_volume():
    assert alerts(pair("A", 0, 100)) == []
    assert alerts(pair("A", None, 100)) == []
    (a,) = alerts([px("A", "2026-11-12", 100), px("A", "2026-11-13", 110, vol=None)])
    assert a["triggers"] == ["move"] and a["ratio"] is None


def test_latest_session_wins_regardless_of_row_order():
    rows = [px("A", "2026-11-11", 50), px("A", "2026-11-13", 100), px("A", "2026-11-12", 100)]
    assert alerts(rows) == []  # the 50 -> 100 jump is two sessions back


def test_tickers_independent_and_strongest_first():
    rows = (pair("QUIET", 100, 101) + pair("VOL", 100, 100, vol=6000) + pair("MOVE", 100, 110)
            + [px("OLD", "2026-11-01", 100)])
    out = build_alerts(rows)
    assert [a["ticker"] for a in out["alerts"]] == ["MOVE", "VOL"]  # 10%/3 = 3.3 beats 6x/2 = 3.0
    assert out["count"] == 2 and out["as_of"] == "2026-11-13"


def test_empty():
    assert build_alerts([]) == {"alerts": [], "count": 0, "as_of": None, "vol_mult": 2.0, "move_pct": 3.0}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FINN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db = get_settings().db_path
    init_db(db)
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), db
    get_settings.cache_clear()


def test_endpoint(client):
    c, db = client
    assert c.get("/api/alerts").json()["count"] == 0  # empty table
    conn = connect(db)
    conn.executemany("INSERT INTO prices(symbol,date,close,volume,avg_vol_20) VALUES(:symbol,:date,:close,:volume,:avg_vol_20)",
                     pair("RELIANCE", 100, 104, vol=2500) + pair("TCS", 100, 100))
    conn.commit()
    conn.close()
    body = c.get("/api/alerts?vol_mult=2&move_pct=3").json()
    assert body["count"] == 1 and body["alerts"][0]["ticker"] == "RELIANCE" and body["as_of"] == "2026-11-13"
    assert c.get("/api/alerts?vol_mult=3&move_pct=5").json()["count"] == 0  # thresholds are live
    for q in ("vol_mult=0", "move_pct=-1", "vol_mult=abc", "move_pct=1000"):
        assert c.get(f"/api/alerts?{q}").status_code == 422
