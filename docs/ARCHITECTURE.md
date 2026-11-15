# FINN — financial investment agent (mobile-first PWA)

> Status: push digest and price/volume alerts working on committed snapshots (RELIANCE filings, 20 tickers of prices). Pull side (results extractor, promise-vs-delivered) not built yet.
> (`backend/app/llm.py`) and the snapshot-fallback fetcher (`backend/app/fetch.py`).

## Quickstart

### Windows (PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cd ..
Copy-Item .env.example .env      # LLM keys are optional; the digest runs without them

cd backend
python -m pytest -q              # backend tests, no network or keys needed
python -m ingest.prices
python -m ingest.announcements --tickers RELIANCE
python -m ingest.fundamentals RELIANCE
uvicorn app.digest_main:app --port 8000
```

Open `http://localhost:8000`. For a phone, use `http://<your-LAN-ip>:8000` on the same Wi-Fi.

```

The Makefile targets map one-to-one to the PowerShell commands above, so Windows users can read the Makefile and run the same lines by hand.
```

## Architecture

```
frontend/ (React + Vite + vite-plugin-pwa)  ──/api──▶  backend/ (FastAPI)
                                                         ├─ routers/   push.py, pull.py
                                                         ├─ (reads SQLite + data/docs; never fetches at request time)
                                                         ├─ llm.py     ONE module, versioned prompts, SQLite cache + cost log
                                                         ├─ db.py      SQLite (finn.db)
                                                         └─ vectorstore.py  Chroma (primary-source chunks only)

backend/ingest/   (CLI batch jobs; the only code that touches the network, besides llm.py)
   prices.py         yfinance EOD → SQLite, 20-session volume baseline   (snapshot via fetch.cached_fetch)
   announcements.py  BSE API → NSE → data/snapshots/announcements.json    (own snapshot, see below)
   fundamentals.py   screener.in → data/docs/<TICKER>/*.pdf + manifest.json
   portfolio.yaml    the 20 demo tickers (+ BSE scrip codes)
```

Two invariants, enforced by `tests/test_invariants.py`:

1. **Only `app/llm.py` talks to a model provider.** Everything else calls `get_llm().run(Prompt, payload)`.
2. **Only `ingest/*` (and `llm.py`) touch the network.** Request handlers read local state, so the UI can never hang on a dead exchange.

### LLM module
- Cache key = `<prompt>:<version>:<sha256(model + canonical_input)>`. Bump `Prompt.version` to invalidate.
- Two tiers via env: `cheap` (bulk extraction/classification) and `strong` (reasoning). Any OpenAI-compatible endpoint.
- Every call, including cache hits and failures, is a row in `llm_log` (tokens, cost, latency, error). `GET /api/usage` summarises it.
- `LLM_CACHE_ONLY=1` → never calls a model. Misses raise `LLMUnavailable`; callers must degrade gracefully.
- `make cache-export` writes `backend/data/seed/llm_cache.jsonl`; it is imported at startup so ephemeral hosts keep the demo answers.

### Fetch fallback
`app/fetch.py::cached_fetch` (used by prices): live (with timeout) → write snapshot → return; on failure/empty/timeout/`FINN_OFFLINE=1` → last snapshot; nothing → `SnapshotMissing`.
Announcements use one shared file, `data/snapshots/announcements.json`, refreshed by `make refresh-announcements`.
Snapshots live in `backend/data/snapshots/` and are **committed**.

### Ingest (run from the repo root)

```bash
make ingest-prices                          # 20 tickers, last 90 days; T="TCS INFY" to limit
make ingest-announcements                   # BSE → NSE → snapshot; options: python -m ingest.announcements --help
make refresh-announcements                  # live → data/snapshots/announcements.json  (then git add it)
make ingest-fundamentals T=RELIANCE         # 3 annual reports + 6 concall transcripts → backend/data/docs/RELIANCE/
python -m ingest.fundamentals inspect page.html   # debug the screener parser on a saved page
```

All are idempotent (upserts / skip-if-valid-PDF) and exit non-zero if any ticker failed.
Notes: `avg_vol_20` on a price row = mean volume of the 20 sessions *before* that date (NULL until 20 exist).
Ingest never overwrites our own `category / priority / summary` columns on announcements.
`data/docs/` is git-ignored (large PDFs); extract/chunk text into the snapshot layer before deploying.

## Repo layout

```
Makefile  Dockerfile  .env.example
backend/app/{main,config,db,llm,prompts,fetch,classify,vectorstore}.py
backend/app/routers/
backend/ingest/{prices,announcements,fundamentals,common}.py   backend/portfolio.yaml
backend/scripts/{smoke,refresh_announcements_snapshot,cache_export}.py
backend/tests/
backend/data/{snapshots,seed}/     committed demo state
frontend/src/{App,api,main}.tsx|ts
backend/app/services/{digest,alerts}.py
backend/app/routers/{digest,alerts}.py   backend/app/digest_main.py
```

## Decisions log

| # | Date | Decision | Why / trade-off |
|---|------|----------|-----------------|
| 1 | 2026-11-15 | FastAPI + SQLite + Chroma; React/Vite PWA | Fast to build, zero infra, free to host. PWA = installable on a phone with no app-store step. |
| 2 | 2026-11-15 | One container serves API + built PWA | One deploy, no CORS, one URL to send reviewers. Cost: slower rebuilds than split hosting. |
| 3 | 2026-11-15 | All LLM calls through `llm.py`; versioned `Prompt` objects | Single place for caching, cost logging, tiering, and kill-switches. Guard test prevents bypass. |
| 4 | 2026-11-15 | Model is hashed into the cache key (spec was hash(input)+version) | Changing model must not serve another model's answer. Version still invalidates per-prompt. |
| 5 | 2026-11-15 | Provider-agnostic OpenAI-compatible HTTP via httpx, no vendor SDK | Swap Groq/Gemini/Ollama by env only; fewer deps. Cost: no provider-specific features. |
| 6 | 2026-11-15 | Snapshots committed to git | Demo survives dead APIs, blocked IPs, ephemeral disks. Cost: repo carries some data files. |
| 7 | 2026-11-15 | Stdlib `sqlite3`, no ORM; stdlib config instead of pydantic-settings | Fewer moving parts; tests run without installs. |
| 8 | 2026-11-15 | Service worker never caches `/api` | Freshness and fallback are the backend's job; avoids stale-UI confusion. |
| 9 | 2026-11-15 | Ingest is offline batch CLIs, not request-time fetches | Demo latency/failure never depends on BSE/NSE/Yahoo; cost: data is as fresh as the last run. |
| 10 | 2026-11-15 | BSE first, NSE fallback, then snapshot; empty result ≠ failure | A quiet company legitimately has no filings; only errors trigger fallback. |
| 11 | 2026-11-15 | Stdlib HTMLParser for screener, no bs4 | One less dependency; parser is small and unit-tested. Cost: more code than bs4 selectors. |
| 12 | 2026-11-15 | Feed-first interaction: a ranked digest, with research planned as a tap-through from a card | The brief is about filtering, so the feed shows what matters without a question. Chat comes after a tap. Cost: the copilot sheet isn't built yet. |
| 13 | 2026-11-15 | Priority map: results 80, management_change 65, M&A 55, fund_raise 45. The `priority` column stores 3/2/1/0 (alert/high/normal/collapsed), derived from the score | Hand-set weights in one place (`WEIGHT`, `BONUS`) are easy to explain and tune. Cost: weights are judgment calls, not learned. |
| 14 | 2026-11-15 | Cheap tier for bulk extraction and classification fallback; strong tier for promise-vs-delivered reasoning | Routes spend to where reasoning matters. Cost: designed in `llm.py` but not used yet, since the digest is rule-based. |
| 15 | 2026-11-15 | No LLM in the digest path: classification, scoring and "why it matters" are rules | Fast, free, auditable; an investor can see why something scored 80. Cost: rules miss unusual wording. |
| 16 | 2026-11-15 | Digest "now" anchors to the newest filing, not the wall clock | A quiet week would otherwise give an empty feed. Cost: freshness depends on the last snapshot. |
| 17 | 2026-11-15 | Same ticker + same IST date + same category collapses into one card; other PDFs go under "related" | A results filing and its board outcome are one event. Cost: lead card is the longest subject, so a terse but better title can lose. |
| 18 | 2026-11-15 | Hand-built snapshot of real RELIANCE filings as the demo data path | BSE/NSE returned 403 from the build network. Cost: one ticker, six filings, not a live feed. |
| 19 | 2026-11-15 | Alerts read the stored prices table only; thresholds are request parameters | No network or LLM on the request path, and users define "unusual". Cost: alerts are pulled by the page, not pushed. |
| 20 | 2026-11-15 | Volume rule skipped when `avg_vol_20` is NULL or 0; a price move can still fire | Avoids divide-by-zero and false alerts on young data. Cost: new listings get fewer alerts. |

## Known risks / unverified

- **Ingest endpoints are UNVERIFIED against live servers.** Live BSE/NSE returned 403 from the build network. The snapshot path is the tested demo path. The screener.in and BSE PDF downloads were verified live for (20 tickers, RELIANCE documents). The BSE/NSE parsers are tested against hand-written samples only.
- **`bse_code` values in `portfolio.yaml` are from memory.** A wrong code silently fetches another company's filings. Check each against bseindia.com (or the BSE `SLONGNAME` field).
- BSE PDF links use the `AttachLive` path; older filings may live under `AttachHis`.
- **Not verified in this scaffold:** `npm install`, `npm run build`, `make setup`, Docker build, and booting FastAPI were *not* run (no network when scaffolded). Python tests were run and pass. Expect small version fixes on first install.
- Frontend dependency versions are caret ranges chosen from memory; `vite-plugin-pwa` must support the installed Vite major.
- Python deps are unpinned; run `make freeze` after the first good install.
- **NSE endpoints often block cloud/datacenter IPs and need cookies.** Plan on committed snapshots as the demo path.
- **Chroma's default embedder downloads a model on first use** (fails on a cold, offline host). Pre-warm in the Docker build or supply an explicit embedding function before relying on it.
- Free-tier model names and rate limits change; confirm in the provider console. Base URLs in `.env.example` are examples to verify.
- Free hosts (Render/HF Spaces) may sleep or have ephemeral disks; hence snapshots + cache seed in git.
- PWA icons are placeholders.

## Submission checklist

- Deployed link that works on a phone ✅
- GitHub repo ✅
- AI build history (export chats / share links) ✅
- Covering-email writeup: what was built, key decisions & trade-offs, what was left out, next steps ✅
- Email to assessment@enam.com ✅
