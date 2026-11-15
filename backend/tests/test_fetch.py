import time

import pytest

from app.config import Settings
from app.fetch import SnapshotMissing, cached_fetch


def S(tmp_path, **kw):
    return Settings(data_dir=tmp_path, **kw)


def test_live_writes_snapshot_then_falls_back(tmp_path):
    s = S(tmp_path)
    r = cached_fetch(s, "prices", "RELIANCE.NS", lambda: [{"close": 1}])
    assert r.source == "live"

    def down():
        raise ConnectionError("blocked")

    r2 = cached_fetch(s, "prices", "RELIANCE.NS", down)
    assert r2.source == "snapshot" and r2.data == [{"close": 1}]
    assert "ConnectionError" in r2.error
    assert r2.fetched_at == r.fetched_at


def test_offline_never_calls_fetcher(tmp_path):
    cached_fetch(S(tmp_path), "p", "k", lambda: {"a": 1})
    called = []
    r = cached_fetch(S(tmp_path, offline=True), "p", "k", lambda: called.append(1))
    assert r.source == "snapshot" and not called


def test_empty_live_result_uses_snapshot(tmp_path):
    s = S(tmp_path)
    cached_fetch(s, "p", "k", lambda: [1])
    assert cached_fetch(s, "p", "k", lambda: []).source == "snapshot"


def test_missing_everything_raises(tmp_path):
    with pytest.raises(SnapshotMissing):
        cached_fetch(S(tmp_path), "p", "nope", lambda: None)


def test_timeout_falls_back(tmp_path):
    s = S(tmp_path)
    cached_fetch(s, "p", "k", lambda: [1])

    def slow():
        time.sleep(0.5)
        return [2]

    t0 = time.perf_counter()
    r = cached_fetch(s, "p", "k", slow, timeout=0.05)
    assert r.source == "snapshot" and time.perf_counter() - t0 < 0.4


def test_unsafe_key_is_sanitised(tmp_path):
    s = S(tmp_path)
    cached_fetch(s, "p", "../../etc/passwd", lambda: [1])
    assert not (tmp_path.parent / "etc").exists()
    assert list((tmp_path / "snapshots" / "p").glob("*.json"))
