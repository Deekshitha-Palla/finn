import pytest

from app.config import Settings, TierConfig
from app.db import connect
from app.llm import LLM, LLMUnavailable, Usage
from app.prompts import Prompt

P1 = Prompt("t", "1", "sys")
P2 = Prompt("t", "2", "sys")


class Fake:
    def __init__(self):
        self.calls = 0

    def chat(self, *, model, system, user, max_tokens, json_mode, temperature):
        self.calls += 1
        return f"echo:{user}", Usage(100, 20)


class Boom:
    def chat(self, **kw):
        raise RuntimeError("429")


def mk(tmp_path, providers, **kw):
    tiers = {"cheap": TierConfig("http://x", "k", "m1", price_in_per_m=1.0, price_out_per_m=2.0)}
    return LLM(Settings(data_dir=tmp_path, tiers=tiers, **kw), providers=providers, sleep=lambda s: None)


def logs(llm):
    c = connect(llm.settings.db_path)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM llm_log ORDER BY id")]
    finally:
        c.close()


def test_cache_hit_and_logging(tmp_path):
    f = Fake()
    llm = mk(tmp_path, {"cheap": f})
    a = llm.run(P1, {"b": 1, "a": 2})
    b = llm.run(P1, {"a": 2, "b": 1})  # same payload, different key order -> same cache key
    assert (a.cached, b.cached) == (False, True)
    assert f.calls == 1
    assert a.text == b.text
    assert a.cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)
    assert b.cost_usd == 0.0
    rows = logs(llm)
    assert [r["cached"] for r in rows] == [0, 1]
    assert rows[0]["in_tokens"] == 100 and rows[0]["out_tokens"] == 20


def test_version_bump_invalidates(tmp_path):
    f = Fake()
    llm = mk(tmp_path, {"cheap": f})
    llm.run(P1, "x")
    llm.run(P2, "x")
    assert f.calls == 2


def test_model_change_invalidates(tmp_path):
    f = Fake()
    llm = mk(tmp_path, {"cheap": f})
    llm.run(P1, "x")
    llm.settings.tiers["cheap"] = TierConfig("http://x", "k", "m2")
    llm.run(P1, "x")
    assert f.calls == 2


def test_cache_only_mode(tmp_path):
    f = Fake()
    mk(tmp_path, {"cheap": f}).run(P1, "x")  # warm
    llm = mk(tmp_path, {"cheap": f}, llm_cache_only=True)
    assert llm.run(P1, "x").cached is True
    with pytest.raises(LLMUnavailable):
        llm.run(P1, "never-seen")
    assert f.calls == 1


def test_no_provider_raises_and_logs_error(tmp_path):
    llm = mk(tmp_path, {})
    with pytest.raises(LLMUnavailable):
        llm.run(P1, "x")
    assert logs(llm)[-1]["error"]


def test_provider_failure_retries_then_raises(tmp_path):
    llm = mk(tmp_path, {"cheap": Boom()})
    with pytest.raises(LLMUnavailable):
        llm.run(P1, "x")
    assert llm.usage_summary()["errors"] == 1


def test_export_import_roundtrip(tmp_path):
    a = mk(tmp_path / "a", {"cheap": Fake()})
    a.run(P1, "x")
    out = tmp_path / "seed.jsonl"
    assert a.export_cache(out) == 1
    b = mk(tmp_path / "b", {}, llm_cache_only=True)
    assert b.import_cache(out) == 1
    assert b.run(P1, "x").cached is True
