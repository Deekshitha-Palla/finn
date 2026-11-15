# FINN — financial investment agent (mobile-first PWA)

> Status: **skeleton**. No features yet. Pieces that exist and are tested: the single LLM module
> (`backend/app/llm.py`) and the snapshot-fallback fetcher (`backend/app/fetch.py`).

## Quickstart

```bash
make setup        # venv + pip + npm install; copies .env.example -> .env
# edit .env: set LLM_CHEAP_* (and LLM_STRONG_*) -- optional, tests and the UI shell run without keys
make test         # 68 backend tests, no network or keys needed
make dev          # API :8000 + Vite :5173 (open http://<your-LAN-ip>:5173 on a phone)
make smoke        # one real LLM call; run twice, second must print cached=True
make serve        # production-like: built PWA served by FastAPI on :8000
make help         # everything else
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
| 12 | | _Add: interaction model chosen (chat vs. feed vs. voice) and why_ | |
| 13 | | _Add: priority map Category → alert/high/normal/collapsed_ | |
| 14 | | _Add: what the cheap vs. strong tier does_ | |

## Known risks / unverified

- **Ingest endpoints are UNVERIFIED against live servers.** BSE/NSE URLs, params and JSON field names and screener.in's HTML structure are from memory (the build sandbox had no internet); parsers are tested only on hand-written samples in `backend/tests/fixtures/`. Passing tests prove the logic, not compatibility. First action on a networked machine: `make refresh-announcements T=RELIANCE` with `-v`, `make ingest-fundamentals T=RELIANCE`, and fix field names if output is empty.
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

## Submission checklist (from the brief)

- [ ] Deployed link that works on a phone
- [ ] GitHub repo
- [ ] AI build history (export chats / share links)
- [ ] Covering-email writeup: what was built, key decisions & trade-offs, what was left out, next steps
- [ ] Email to assessment@enam.com, subject: `IIT Delhi – AI Engineer - 2027`
- [ ] Deadline: 15 Nov 2026
