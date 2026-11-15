import math
from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import Settings
from app.db import connect
from ingest.common import Company
from ingest.prices import ingest_prices, normalize_rows, rolling_avg_volume


def test_normalize_handles_timestamps_nans_dupes_and_casing():
    recs = [
        {"Date": pd.Timestamp("2026-11-12 00:00:00", tz="Asia/Kolkata"), "Open": 1, "High": 2, "Low": 0.5,
         "Close": 1.5, "Volume": 100.0},
        {"Date": "2026-11-11", "Open": 1, "High": 2, "Low": 1, "Close": float("nan"), "Volume": 5},  # no close
        {"date": "2026-11-10", "close": 3, "volume": float("nan")},                                  # no volume
        {"Date": "2026-11-12", "Open": 9, "High": 9, "Low": 9, "Close": 9, "Volume": 7},             # dup date wins
        {"Date": "garbage", "Close": 1},
        {"Close": 1},
    ]
    rows = normalize_rows(recs)
    assert [r["date"] for r in rows] == ["2026-11-10", "2026-11-12"]
    assert rows[0]["volume"] is None and rows[0]["open"] is None
    assert rows[1]["close"] == 9 and rows[1]["volume"] == 7


def test_normalize_real_dataframe_shape():
    df = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [2, 3], "Low": [1, 2], "Close": [1.5, 2.5], "Volume": [10, 20]},
        index=pd.DatetimeIndex(["2026-11-10", "2026-11-11"], tz="Asia/Kolkata", name="Date"),
    )
    rows = normalize_rows(df.reset_index().to_dict("records"))
    assert [r["date"] for r in rows] == ["2026-11-10", "2026-11-11"]
    assert all(isinstance(r["volume"], int) for r in rows)


def test_rolling_avg_excludes_today_and_needs_full_window():
    vols = list(range(1, 26))  # 1..25
    out = rolling_avg_volume(vols, 20)
    assert out[:20] == [None] * 20
    assert out[20] == sum(range(1, 21)) / 20  # mean of sessions 0..19, NOT including index 20
    assert out[24] == sum(range(5, 25)) / 20


def test_rolling_avg_gap_poisons_window():
    vols = [10] * 25
    vols[3] = None
    out = rolling_avg_volume(vols, 20)
    assert out[20] is None and out[22] is None  # windows containing the gap
    assert out[23] is None and out[24] == 10.0  # gap (index 3) leaves the window at index 24


def _history(n=30, last_volume=None):
    start = date(2026, 10, 1)
    rows = []
    for i in range(n):
        d = start + timedelta(days=i)
        rows.append({"date": d.isoformat(), "open": 100.0, "high": 101.0, "low": 99.0,
                     "close": 100.0 + i, "volume": 1000 + i})
    if last_volume:
        rows[-1]["volume"] = last_volume
    return rows


def test_ingest_is_idempotent_and_computes_baseline(tmp_path):
    s = Settings(data_dir=tmp_path)
    co = [Company("AAA", bse_code="123456")]
    fn = lambda sym, days: _history(30, last_volume=5000)
    r1 = ingest_prices(s, co, 90, history_fn=fn)
    r2 = ingest_prices(s, co, 90, history_fn=fn)
    assert r1[0]["source"] == "live" and r1[0]["rows"] == 30
    conn = connect(s.db_path)
    n = conn.execute("SELECT COUNT(*) FROM prices WHERE symbol='AAA'").fetchone()[0]
    rows = conn.execute("SELECT * FROM prices WHERE symbol='AAA' ORDER BY date").fetchall()
    conn.close()
    assert n == 30  # re-run added nothing
    assert rows[19]["avg_vol_20"] is None and rows[20]["avg_vol_20"] is not None
    expected = sum(1000 + i for i in range(9, 29)) / 20  # sessions 9..28 precede index 29
    assert rows[29]["avg_vol_20"] == pytest.approx(expected)
    assert r2[0]["vol_ratio"] == pytest.approx(round(5000 / expected, 2))


def test_incremental_run_overwrites_partial_bar_and_extends(tmp_path):
    s = Settings(data_dir=tmp_path)
    co = [Company("AAA")]
    ingest_prices(s, co, history_fn=lambda sym, d: _history(25))
    longer = _history(26)
    longer[24]["close"] = 555.0  # corrected value for a previously stored day
    ingest_prices(s, co, history_fn=lambda sym, d: longer)
    conn = connect(s.db_path)
    rows = conn.execute("SELECT close FROM prices WHERE symbol='AAA' ORDER BY date").fetchall()
    conn.close()
    assert len(rows) == 26 and rows[24]["close"] == 555.0


def test_live_failure_falls_back_to_snapshot_and_one_bad_ticker_does_not_stop_batch(tmp_path):
    s = Settings(data_dir=tmp_path)
    co = [Company("AAA"), Company("BBB")]
    ingest_prices(s, co, history_fn=lambda sym, d: _history(25))

    def flaky(sym, d):
        if sym == "AAA.NS":
            raise ConnectionError("yahoo down")
        return []  # BBB: Yahoo returns nothing

    res = ingest_prices(s, co, history_fn=flaky)
    assert [r["source"] for r in res] == ["snapshot", "snapshot"] and not any(r["error"] for r in res)

    res = ingest_prices(s, [Company("NEVERSEEN")], history_fn=flaky)
    assert res[0]["error"] and res[0]["source"] is None


def test_offline_setting_never_calls_fetcher(tmp_path):
    ingest_prices(Settings(data_dir=tmp_path), [Company("AAA")], history_fn=lambda s, d: _history(25))
    called = []
    res = ingest_prices(Settings(data_dir=tmp_path, offline=True), [Company("AAA")],
                        history_fn=lambda s, d: called.append(1))
    assert res[0]["source"] == "snapshot" and not called
    assert not math.isnan(res[0]["rows"])
