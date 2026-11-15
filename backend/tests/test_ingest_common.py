import pytest
from fakes import FakeClient, FakeResp

from ingest.common import FetchError, get_with_retries, load_portfolio


def run(handler, **kw):
    sleeps = []
    c = FakeClient(handler)
    try:
        r = get_with_retries(c, "http://x", sleep=sleeps.append, **kw)
    except FetchError:
        return c, sleeps, None
    return c, sleeps, r


def test_retries_then_succeeds():
    seq = iter([FakeResp(503), FakeResp(429), FakeResp(200)])
    c, sleeps, r = run(lambda u, p: next(seq))
    assert r.status_code == 200 and len(c.calls) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential


def test_non_retryable_4xx_fails_fast():
    c, sleeps, r = run(lambda u, p: FakeResp(404))
    assert r is None and len(c.calls) == 1 and not sleeps


def test_gives_up_after_retries():
    c, sleeps, r = run(lambda u, p: FakeResp(500), retries=2)
    assert r is None and len(c.calls) == 3 and len(sleeps) == 2


def test_network_exceptions_are_retried():
    n = {"i": 0}

    def h(u, p):
        n["i"] += 1
        if n["i"] < 3:
            raise ConnectionError("reset")
        return FakeResp(200)

    c, _, r = run(h)
    assert r is not None and n["i"] == 3


def test_retry_after_header_is_honoured_and_capped():
    seq = iter([FakeResp(429, headers={"Retry-After": "7"}), FakeResp(429, headers={"Retry-After": "999"}), FakeResp(200)])
    _, sleeps, r = run(lambda u, p: next(seq))
    assert sleeps == [7.0, 30.0] and r is not None


def test_real_portfolio_file_is_valid():
    ps = load_portfolio()
    assert len(ps) == 20
    assert len({p.ticker for p in ps}) == 20
    assert all(p.bse_code and len(p.bse_code) == 6 for p in ps)
    assert {"large", "mid", "small"} <= {p.cap for p in ps}
    assert ps[0].yahoo == "RELIANCE.NS"


def test_adhoc_ticker_outside_portfolio():
    (c,) = load_portfolio(tickers=["zomato"])
    assert c.ticker == "ZOMATO" and c.bse_code is None
    (r,) = load_portfolio(tickers=["tcs"])
    assert r.bse_code == "532540"


def test_bad_portfolio_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("companies:\n  - {ticker: A, bse_code: '12'}\n")
    with pytest.raises(ValueError):
        load_portfolio(f)
    f.write_text("companies:\n  - {ticker: A}\n  - {ticker: a}\n")
    with pytest.raises(ValueError):
        load_portfolio(f)
