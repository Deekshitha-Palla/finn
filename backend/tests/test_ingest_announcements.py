import json
from datetime import date
from pathlib import Path

import pytest
from fakes import FakeClient, FakeResp, no_sleep

from app.config import Settings
from app.db import connect
from ingest import announcements as A
from ingest.common import Company, FetchError

FX = Path(__file__).parent / "fixtures"
BSE = json.loads((FX / "bse_sample.json").read_text())
NSE = json.loads((FX / "nse_sample.json").read_text())
RIL = Company("RELIANCE", bse_code="500325")
TODAY = date(2026, 11, 15)


# ---- parsing --------------------------------------------------------------------------------------
def test_parse_bse_maps_fields_and_skips_bad_rows():
    rows, total = A.parse_bse(BSE, "RELIANCE")
    assert total == 4
    assert len(rows) == 3  # the row with an unparseable date is dropped
    r = rows[0]
    assert r["id"] == "bse:a1b2-0001" and r["ticker"] == "RELIANCE" and r["source"] == "bse"
    assert r["ts"] == "2026-11-14T16:32:01+05:30"
    assert r["subject"] == "Outcome of Board Meeting"              # 'Company - 500325 - ' prefix stripped
    assert r["category_raw"] == "Result / Financial Results"
    assert r["pdf_url"] == A.BSE_PDF_BASE + "a1b2-0001.pdf"
    assert "quarter ended" in r["detail"]


def test_parse_bse_optional_fields():
    rows, _ = A.parse_bse(BSE, "RELIANCE")
    no_pdf = rows[1]
    assert no_pdf["pdf_url"] is None and no_pdf["category_raw"] == "Company Update"  # empty subcat omitted
    no_id = rows[2]
    assert no_id["id"].startswith("bse:") and len(no_id["id"]) > 8
    assert no_id["subject"] == "Intimation of analyst meet"
    assert A.parse_bse(BSE, "RELIANCE")[0][2]["id"] == no_id["id"]  # fallback id is deterministic


@pytest.mark.parametrize("bad", [None, [], {"Table": None}, {"nope": []}, "<html>blocked</html>"])
def test_parse_bse_rejects_wrong_shape(bad):
    with pytest.raises(FetchError):
        A.parse_bse(bad, "X")


def test_parse_bse_empty_table_is_valid():
    assert A.parse_bse({"Table": [], "Table1": []}, "X") == ([], None)


def test_parse_nse_maps_fields():
    rows = A.parse_nse(NSE, "RELIANCE")
    assert len(rows) == 2  # the row with no subject is dropped
    a, b = rows
    assert a["id"] == "nse:104500123" and a["ts"] == "2026-11-14T16:32:01+05:30"
    assert a["subject"] == a["category_raw"] == "Outcome of Board Meeting"
    assert a["pdf_url"].startswith("https://nsearchives")
    assert b["pdf_url"] is None                                   # "-" is not a URL
    assert b["ts"] == "2026-11-12T09:00:00+05:30"                  # parsed from an_dt when sort_date is absent
    assert b["id"].startswith("nse:")
    assert A.parse_nse({"data": NSE}, "RELIANCE") == rows         # {"data": [...]} wrapper also accepted
    with pytest.raises(FetchError):
        A.parse_nse({"error": "x"}, "RELIANCE")


def test_clean_subject_and_dates():
    assert A.clean_subject("Tata Steel Ltd - 500470 - Press Release") == "Press Release"
    assert A.clean_subject("No prefix here") == "No prefix here"
    assert A._iso_ist("2026-11-14") == "2026-11-14T00:00:00+05:30"
    assert A._iso_ist("") is None and A._iso_ist("31/12/2026") is None


def test_bse_params():
    p = A.bse_params("500325", date(2026, 10, 16), date(2026, 11, 15), 2)
    assert p["strScrip"] == "500325" and p["strPrevDate"] == "20261016" and p["strToDate"] == "20261115"
    assert p["pageno"] == "2"


# ---- live fetch -----------------------------------------------------------------------------------
def _bse_page(ids, total):
    return {"Table": [{"NEWSID": i, "NEWSSUB": f"s{i}", "NEWS_DT": "2026-11-14T10:00:00"} for i in ids],
            "Table1": [{"ROWCNT": total}]}


def test_fetch_bse_paginates_until_total():
    pages = {"1": _bse_page(["a", "b"], 3), "2": _bse_page(["c"], 3), "3": _bse_page(["zzz"], 3)}
    c = FakeClient(lambda u, p: FakeResp(json_data=pages[p["pageno"]]))
    rows = A.fetch_bse(c, RIL, date(2026, 10, 1), TODAY, sleep=no_sleep)
    assert [r["id"] for r in rows] == ["bse:a", "bse:b", "bse:c"]
    assert len(c.calls) == 2  # stopped at total, never asked for page 3


def test_fetch_bse_stops_on_repeated_page():
    same = _bse_page(["a"], None)
    c = FakeClient(lambda u, p: FakeResp(json_data=same))
    assert len(A.fetch_bse(c, RIL, date(2026, 10, 1), TODAY, sleep=no_sleep)) == 1
    assert len(c.calls) == 2


def test_fetch_bse_non_json_and_missing_scrip():
    c = FakeClient(lambda u, p: FakeResp(text="<html>Access denied</html>"))
    with pytest.raises(FetchError, match="non-JSON"):
        A.fetch_bse(c, RIL, date(2026, 10, 1), TODAY, sleep=no_sleep)
    with pytest.raises(FetchError, match="scrip"):
        A.fetch_bse(c, Company("NOCODE"), date(2026, 10, 1), TODAY, sleep=no_sleep)


def nse_handler(payload):
    def h(url, params):
        return FakeResp(text="home") if url == A.NSE_HOME else FakeResp(json_data=payload)
    return h


def test_fetch_live_bse_then_nse_fallback():
    bse = FakeClient(lambda u, p: FakeResp(503))
    nse = FakeClient(nse_handler(NSE))
    rows, src = A.fetch_live(RIL, date(2026, 10, 1), TODAY, bse_client=bse, nse_client=nse, sleep=no_sleep)
    assert src == "nse" and len(rows) == 2
    assert nse.calls[0][0] == A.NSE_HOME  # cookie warm-up happens first
    assert nse.calls[1][1]["symbol"] == "RELIANCE" and nse.calls[1][1]["from_date"] == "01-10-2026"


def test_fetch_live_empty_bse_is_success_not_fallback():
    bse = FakeClient(lambda u, p: FakeResp(json_data={"Table": [], "Table1": []}))
    nse = FakeClient(nse_handler(NSE))
    rows, src = A.fetch_live(RIL, date(2026, 10, 1), TODAY, bse_client=bse, nse_client=nse, sleep=no_sleep)
    assert (rows, src) == ([], "bse") and not nse.calls


def test_fetch_live_both_fail():
    dead = FakeClient(lambda u, p: FakeResp(500))
    with pytest.raises(FetchError, match="bse.*nse"):
        A.fetch_live(RIL, date(2026, 10, 1), TODAY, bse_client=dead, nse_client=dead, sleep=no_sleep)


# ---- ingest / snapshot ----------------------------------------------------------------------------
def settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path, **kw)


def ok_bse(ids=("a", "b")):
    return FakeClient(lambda u, p: FakeResp(json_data=_bse_page(list(ids), len(ids))))


def dead():
    return FakeClient(lambda u, p: FakeResp(500))


def db_rows(s):
    conn = connect(s.db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM announcements ORDER BY id")]
    finally:
        conn.close()


def test_ingest_is_idempotent_and_preserves_our_classification(tmp_path):
    s = settings(tmp_path)
    kw = dict(bse_client=ok_bse(), nse_client=dead(), sleep=no_sleep, today=TODAY)
    A.ingest_announcements(s, [RIL], 30, **kw)
    conn = connect(s.db_path)
    conn.execute("UPDATE announcements SET category='results', priority=1, summary='mine' WHERE id='bse:a'")
    conn.commit(); conn.close()
    res = A.ingest_announcements(s, [RIL], 30, **kw)
    rows = db_rows(s)
    assert len(rows) == 2 and res[0]["source"] == "bse" and res[0]["rows"] == 2
    a = next(r for r in rows if r["id"] == "bse:a")
    assert (a["category"], a["priority"], a["summary"]) == ("results", 1, "mine")  # untouched by re-ingest
    assert a["symbol"] == "RELIANCE" and a["source"] == "bse"


def test_live_failure_loads_snapshot(tmp_path):
    s = settings(tmp_path)
    A.refresh_snapshot(s, [RIL], 30, bse_client=ok_bse(["a", "b", "c"]), nse_client=dead(), sleep=no_sleep, today=TODAY)
    # fresh DB, dead network:
    s2 = settings(tmp_path / "x")
    (tmp_path / "x" / "snapshots").mkdir(parents=True)
    (tmp_path / "x" / "snapshots" / "announcements.json").write_text(A.snapshot_path(s).read_text())
    res = A.ingest_announcements(s2, [RIL, Company("OTHER")], 30, bse_client=dead(), nse_client=dead(),
                                 sleep=no_sleep, today=TODAY)
    assert res[0]["source"] == "snapshot" and res[0]["rows"] == 3 and not res[0]["error"] or res[0]["source"] == "snapshot"
    assert res[1]["source"] is None and res[1]["error"]  # OTHER: not in snapshot -> reported, not crashed
    assert len(db_rows(s2)) == 3


def test_offline_flag_skips_network(tmp_path):
    s = settings(tmp_path)
    A.refresh_snapshot(s, [RIL], 30, bse_client=ok_bse(), nse_client=dead(), sleep=no_sleep, today=TODAY)
    bse = ok_bse()
    res = A.ingest_announcements(settings(tmp_path, offline=True), [RIL], 30, bse_client=bse, nse_client=dead(),
                                 sleep=no_sleep, today=TODAY)
    assert res[0]["source"] == "snapshot" and not bse.calls


def test_no_snapshot_and_no_network_reports_failure(tmp_path):
    res = A.ingest_announcements(settings(tmp_path), [RIL], 30, bse_client=dead(), nse_client=dead(),
                                 sleep=no_sleep, today=TODAY)
    assert res[0]["error"] and res[0]["source"] is None and db_rows(settings(tmp_path)) == []


def test_refresh_snapshot_merges_and_keeps_old_rows_on_failure(tmp_path):
    s = settings(tmp_path)
    TCS = Company("TCS", bse_code="532540")
    A.refresh_snapshot(s, [RIL, TCS], 30, bse_client=ok_bse(["a"]), nse_client=dead(), sleep=no_sleep, today=TODAY)
    snap = json.loads(A.snapshot_path(s).read_text())
    assert {r["ticker"] for r in snap["announcements"]} == {"RELIANCE", "TCS"}

    def only_ril(u, p):  # TCS (scrip 532540) fails this time; RELIANCE returns different data
        return FakeResp(json_data=_bse_page(["z"], 1)) if p["strScrip"] == "500325" else FakeResp(500)

    res = A.refresh_snapshot(s, [RIL, TCS], 30, bse_client=FakeClient(only_ril), nse_client=dead(),
                             sleep=no_sleep, today=TODAY)
    assert [bool(r["error"]) for r in res] == [False, True]
    snap = json.loads(A.snapshot_path(s).read_text())
    by = {r["ticker"]: r["id"] for r in snap["announcements"]}
    assert by == {"RELIANCE": "bse:z", "TCS": "bse:a"}  # RIL replaced, TCS kept from the old snapshot
    assert set(snap["tickers"]) == {"RELIANCE", "TCS"}


def test_refresh_writes_nothing_when_everything_fails(tmp_path):
    s = settings(tmp_path)
    res = A.refresh_snapshot(s, [RIL], 30, bse_client=dead(), nse_client=dead(), sleep=no_sleep, today=TODAY)
    assert res[0]["error"] and not A.snapshot_path(s).exists()


def test_corrupt_snapshot_is_ignored(tmp_path):
    s = settings(tmp_path)
    p = A.snapshot_path(s)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert A.load_snapshot(p) is None
    res = A.ingest_announcements(s, [RIL], 30, bse_client=ok_bse(), nse_client=dead(), sleep=no_sleep, today=TODAY)
    assert res[0]["source"] == "bse"
