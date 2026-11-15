import json
from pathlib import Path

import pytest
from fakes import FakeClient, FakeResp, no_sleep

from app.config import Settings
from ingest import fundamentals as F
from ingest.common import FetchError

HTML = (Path(__file__).parent / "fixtures" / "screener_sample.html").read_text()
PDF = b"%PDF-1.7\n" + b"x" * 64


# ---- parsing --------------------------------------------------------------------------------------
def test_parse_annual_reports_sorted_and_labels_cleaned():
    d = F.parse_screener_documents(HTML, "https://www.screener.in/company/X/consolidated/")
    ar = d["annual_reports"]
    assert [a["fy"] for a in ar] == [2025, 2024, 2023, 2022]
    assert ar[0]["label"] == "Financial Year 2025"                       # 'from bse' suffix removed
    assert ar[2]["url"] == "https://www.screener.in/ar_2023.pdf"          # relative URL resolved
    assert not any("rating" in a["url"] for a in ar)                      # other document blocks ignored


def test_parse_concalls_only_transcripts_newest_first():
    cc = F.parse_screener_documents(HTML)["concalls"]
    assert [c["period"] for c in cc] == ["2025-08", "2025-02", "2024-11", "2024-08", "2024-08"]
    assert cc[0]["url"].endswith("cc_2025_08_t.pdf")        # picked the Transcript link, not Notes/PPT
    assert "2025-05" not in [c["period"] for c in cc]        # PPT-only row skipped
    assert cc[3]["label"] == "August 2024"


def test_parse_handles_garbage_and_no_sections():
    assert F.parse_screener_documents("") == {"annual_reports": [], "concalls": []}
    assert F.parse_screener_documents("<div><li><a href='x'>y</a></li></div>") == {"annual_reports": [], "concalls": []}
    unclosed = '<div class="annual-reports"><ul><li><a href="/a.pdf">Financial Year 2024<li><a href="/b.pdf">Financial Year 2023</div>'
    assert [a["fy"] for a in F.parse_screener_documents(unclosed)["annual_reports"]] == [2024, 2023]


def test_period():
    assert F._period("Nov 2024") == "2024-11" and F._period("August 2024") == "2024-08"
    assert F._period("Sept. 2024") == "2024-09" and F._period("Q3 FY25") is None and F._period("Foo 2024") is None


def test_plan_files_limits_names_and_collisions():
    d = F.parse_screener_documents(HTML)
    items = F.plan_files(d, 3, 6)
    names = [i["file"] for i in items]
    assert names[:3] == ["annual_report_FY2025.pdf", "annual_report_FY2024.pdf", "annual_report_FY2023.pdf"]
    assert len([i for i in items if i["kind"] == "concall"]) == 5   # only 5 transcripts exist
    assert len(set(names)) == len(names)                             # the two 2024-08 calls don't collide
    assert sum(n.startswith("concall_2024-08") for n in names) == 2
    assert [i["file"] for i in F.plan_files(d, 1, 0)] == ["annual_report_FY2025.pdf"]


def test_pdf_detection(tmp_path):
    assert F.looks_like_pdf(PDF) and F.looks_like_pdf(b"\n\n%PDF-1.4") and not F.looks_like_pdf(b"<html>")
    good, bad, empty = tmp_path / "g.pdf", tmp_path / "b.pdf", tmp_path / "e.pdf"
    good.write_bytes(PDF); bad.write_bytes(b"<html>"); empty.write_bytes(b"")
    assert F.valid_pdf_file(good) and not F.valid_pdf_file(bad) and not F.valid_pdf_file(empty)
    assert not F.valid_pdf_file(tmp_path / "missing.pdf")


# ---- ingest ---------------------------------------------------------------------------------------
def make_client(broken=(), page_status=200, with_ar_only=False):
    def h(url, params):
        if "/company/" in url and "/api/" not in url:
            if page_status != 200:
                return FakeResp(page_status)
            return FakeResp(text=HTML)
        if any(b in url for b in broken):
            return FakeResp(text="<html>Please enable JS</html>", content=b"<html>", headers={"content-type": "text/html"})
        return FakeResp(content=PDF, headers={"content-type": "application/pdf"})
    return FakeClient(h)


def test_ingest_downloads_validates_and_writes_manifest(tmp_path):
    s = Settings(data_dir=tmp_path)
    c = make_client(broken=["cc_2024_11_t"])
    m = F.ingest_fundamentals(s, "reliance", 3, 6, client=c, sleep=no_sleep)
    d = tmp_path / "docs" / "RELIANCE"
    st = {i["file"]: i["status"] for i in m["items"]}
    assert st["annual_report_FY2025.pdf"] == "downloaded" and (d / "annual_report_FY2025.pdf").read_bytes() == PDF
    assert st["concall_2024-11.pdf"] == "failed" and not (d / "concall_2024-11.pdf").exists()  # HTML never saved as PDF
    assert not list(d.glob("*.part"))
    assert sum(v == "downloaded" for v in st.values()) == 7  # 3 AR + 4 good transcripts
    on_disk = json.loads((d / "manifest.json").read_text())
    assert on_disk["ticker"] == "RELIANCE" and len(on_disk["items"]) == 8
    assert "not a PDF" in next(i for i in on_disk["items"] if i["status"] == "failed")["error"]


def test_second_run_is_idempotent_and_retries_only_failures(tmp_path):
    s = Settings(data_dir=tmp_path)
    F.ingest_fundamentals(s, "RELIANCE", 3, 6, client=make_client(broken=["cc_2024_11_t"]), sleep=no_sleep)
    c2 = make_client()  # the previously broken link works now
    m = F.ingest_fundamentals(s, "RELIANCE", 3, 6, client=c2, sleep=no_sleep)
    st = [i["status"] for i in m["items"]]
    assert st.count("skipped") == 7 and st.count("downloaded") == 1
    pdf_gets = [u for u, _ in c2.calls if u.endswith(".pdf")]
    assert pdf_gets == ["https://example.test/cc_2024_11_t.pdf"]  # nothing already on disk was re-fetched


def test_corrupt_existing_file_is_redownloaded(tmp_path):
    s = Settings(data_dir=tmp_path)
    d = tmp_path / "docs" / "RELIANCE"
    d.mkdir(parents=True)
    (d / "annual_report_FY2025.pdf").write_bytes(b"<html>junk")
    m = F.ingest_fundamentals(s, "RELIANCE", 1, 0, client=make_client(), sleep=no_sleep)
    assert m["items"][0]["status"] == "downloaded" and (d / "annual_report_FY2025.pdf").read_bytes() == PDF


def test_find_documents_falls_back_to_standalone_page(tmp_path):
    def h(url, params):
        if url.endswith("/consolidated/"):
            return FakeResp(404)
        return FakeResp(text=HTML)
    url, docs = F.find_documents(FakeClient(h), "TCS", no_sleep)
    assert url.endswith("/company/TCS/") and docs["annual_reports"]


def test_find_documents_gives_clear_error_when_missing():
    with pytest.raises(FetchError, match="could not find"):
        F.find_documents(FakeClient(lambda u, p: FakeResp(404)), "NOPE", no_sleep)


def test_layout_change_is_reported_clearly(tmp_path):
    c = FakeClient(lambda u, p: FakeResp(text="<html><body>new layout</body></html>") if "/api/" not in u else FakeResp(json_data=[]))
    with pytest.raises(FetchError, match="inspect"):
        F.ingest_fundamentals(Settings(data_dir=tmp_path), "RELIANCE", client=c, sleep=no_sleep)
    assert not (tmp_path / "docs" / "RELIANCE" / "manifest.json").exists()


def test_inspect_cli(tmp_path, capsys):
    f = tmp_path / "page.html"
    f.write_text(HTML)
    assert F.main(["inspect", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["annual_reports"]) == 4
    f.write_text("<html></html>")
    assert F.main(["inspect", str(f)]) == 1
