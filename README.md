\# FINN: a filtering layer for investors


> Most of what a listed company files doesn't matter. FINN's job is to bury it and put the few things that do in front of you, with the reason attached.



\*\*Live demo:\*\* https://finn-sepk.onrender.com. (free tier, first load can take about 30s to wake)


\*\*Built for:\*\* ENAM AI Engineer Build Challenge



\---



\## What this is?



FINN is a mobile-first investor feed. This submission ships the \*\*push side\*\*: a ranked digest of corporate filings over a window you choose (24h, 3d, 7d, 30d).



\- \*\*Material items lead.\*\* Results and fund-raises rank at the top, each with a score and a one-line reason.

\- \*\*Routine items collapse.\*\* Investor-meet intimations and similar paperwork sit in a collapsed group, so nothing is hidden but nothing competes for attention.

\- \*\*Duplicates merge.\*\* A results filing and its board-meeting outcome describe one event, so they appear as one card, with the other PDFs listed under "related".

\- \*\*Every score is explainable.\*\* Example: the 30 Oct RELIANCE results card scores \*\*80 = 60 (category) + 20 (results bonus)\*\*, and the card says so.



\## Try it



\### Live

Open the link above on your phone. It opens on the 30-day view, where the real content is.



\### Locally (Windows PowerShell)



```powershell

cd backend

python -m venv .venv

.\\.venv\\Scripts\\Activate.ps1

python -m pip install -r requirements-dev.txt

uvicorn app.digest\_main:app --port 8000

```



Open `http://localhost:8000`, then use your browser's device toolbar at 390px, or open it on your phone via your PC's LAN IP.



\### Locally (macOS / Linux)



```bash

cd backend \&\& python3 -m venv .venv \&\& source .venv/bin/activate

pip install -r requirements-dev.txt

uvicorn app.digest\_main:app --port 8000

```



\### API



```

GET /api/digest?window=24h|3d|7d|30d

\-> { "ranked": \[...], "routine": \[...], "now": "..." }

```



Each item has `ticker, ts, subject, category, score, why\_it\_matters, pdf\_url, related`.



\### Tests



```

cd backend \&\& python -m pytest -q

```



Covers the ingest parsing, retries and fallbacks, plus the digest's classification, collapsing, ranking and window filtering.



\---



\## How it works



```

BSE / NSE filings ─┐

&#x20;                  ├─► ingest (live → fallback → committed snapshot) ─► SQLite

yfinance EOD ──────┘                                                      │

&#x20;                                                                         ▼

screener.in PDFs ─► ingest/fundamentals ─► data/docs/<TICKER>/     digest service

&#x20;                                                                 (classify → collapse

&#x20;                                                                  → score → group)

&#x20;                                                                         │

&#x20;                                                                         ▼

&#x20;                                                             FastAPI  ─►  mobile feed

```



| Layer | What it does |

|---|---|

| `backend/ingest/` | Prices (yfinance), announcements (BSE → NSE → snapshot), annual reports and concall PDFs (screener.in). Idempotent, with retries and fallbacks. |

| `backend/app/services/digest.py` | Classifies filings into the 17 reference categories, collapses duplicates, scores, and splits ranked from routine. |

| `backend/app/routers/digest.py` | The `/api/digest` endpoint. |

| `frontend/feed/` | Single-file mobile page: window picker, score chips, expandable routine group, results card. |



\---



\## Decisions and trade-offs



| Decision | Why | What it costs |

|---|---|---|

| \*\*No LLM in the digest path\*\* | Rules are fast, free and auditable. An investor can see exactly why something scored 80. | Rules won't catch unusual wording. Fine for an alert feed. |

| \*\*"Now" anchors to the newest filing, not the wall clock\*\* | A 24h window on a quiet day would be empty and the demo would look broken. | Freshness depends on the last snapshot. |

| \*\*Duplicate filings collapse into one card\*\* | A results filing and its board outcome are one event. Showing both is noise. | The lead card is the longest subject. Nothing is dropped, since the rest sit under "related". |

| \*\*Routine group, collapsed\*\* | Most filings are noise. Hiding them in one tap beats ranking them equally. | Category weights are hand-set (`WEIGHT` and `BONUS` at the top of the service) and easy to tune. |

| \*\*Committed snapshot as the demo data path\*\* | BSE and NSE returned 403 from my build network, and exchange feeds often block cloud IPs. A demo shouldn't depend on a live scrape. | The demo data is a snapshot of real RELIANCE filings, not a live feed. |

| \*\*Ingest and digest are separate\*\* | The digest reads SQLite and never touches the network, so it's fast and testable. | Refreshing data is two steps: ingest, then reload. |

| \*\*Opens on 30 days\*\* | With the anchored "now", shorter windows are sparse. | Tight windows need a manual tap. |



\---



\## What's built, and what isn't



I'd rather you see the gaps from me than find them.



\*\*Working\*\*

\- Push digest with windows, ranking, collapsing, routine grouping and rule-based reasons

\- EOD price and volume ingest for 19 NSE tickers, with a volume baseline (20-day average, excluding the current day)

\- Announcement ingest with live → fallback → snapshot logic

\- Annual report and concall PDF download from screener.in (tested live on RELIANCE: 9 documents)

\- A single LLM module with caching and cost logging, ready for the pull side. It isn't used in the digest.

## Why I built it this way

**Filtering is the product, so the digest uses rules, not an LLM.**
Scoring, grouping and "why it matters" are deterministic. That makes every ranking explainable (80 = 60 category + 20 results bonus), free to run, and testable. I'd spend LLM calls where rules can't reach: reading a results PDF, and judging promises against outcomes.

**Spend is routed by how hard the task is.**
`llm.py` has a cheap tier for bulk extraction and a strong tier for reasoning, with a cache keyed on prompt version, model and input. Every call, including cache hits and failures, is logged with tokens and cost. The digest makes zero model calls, so a browse costs nothing.

**Request handlers never touch the network.**
Only ingest jobs and `llm.py` do, and a guard test fails the build if anything else does. A dead exchange can slow a refresh, never a page load.

**The demo can't be broken by someone else's server.**
BSE and NSE blocked my requests, which is common for exchange feeds. Instead of fighting it, the app falls back to a committed snapshot of real filings, and the ingest tells you which source it used.

**Ingest is built to fail safely.**
It's idempotent. A refresh that fails for one ticker keeps that ticker's old rows. Nothing is written if every ticker fails. A non-PDF response (like a block page) is never saved as a PDF. The 20-day volume baseline excludes the current day, so "today vs average" isn't compared with itself.

**Plain, boring tools on purpose.**
FastAPI, SQLite and one container. No ORM, no vendor SDK, an OpenAI-compatible HTTP client so I can swap providers by env var. It deploys free and a reviewer can run it in two minutes.

**Feed first, chat second.**
Investors shouldn't have to know what to ask. The feed shows what changed, and research starts from a tap on a card.

**I stopped at one ticker done properly.**
A thin, working slice with honest gaps over a broad one that breaks.

\*\*Not built\*\*

\- \*\*Only RELIANCE's filings are loaded.\*\* The brief asks for 15 to 20 companies. I went deep on one ticker rather than thin across many.

\- \*\*Results numbers (Revenue, EBITDA, PBT, PAT with QoQ and YoY) aren't extracted yet.\*\* The results card shows the layout with placeholders, so the design target is visible but the numbers aren't there.

\- \*\*Price and volume aren't wired into the score or alerts yet.\*\* The data is ingested, but triggers and thresholds are still to do.

\- \*\*The pull side isn't built\*\*, including the promise-vs-delivered analysis, which I consider the most valuable piece of the brief.

\- \*\*Management-change enrichment\*\* isn't built.

\- \*\*Live exchange feeds\*\* couldn't be reached from my build network.



\## Next steps



1\. \*\*Results extractor\*\* from the 30 Oct filing: rules and table parsing first, an LLM only as a fallback, with a PBT − tax ≈ PAT check and low-confidence flags instead of guesses.

2\. \*\*More tickers\*\*, using a browser-assisted snapshot refresh for the exchange feeds.

3\. \*\*Threshold alerts\*\* on volume and price move, with user-set thresholds.

4\. \*\*Promise vs delivered\*\* on the concall transcripts, grounded in primary sources only, with a page citation for every claim.

5\. \*\*Copilot sheet\*\* scoped to the open card, so a tap leads straight into research.



\---



\## Repo layout



```

backend/

&#x20; app/            config, db, classify, llm, digest service + router

&#x20; ingest/         prices, announcements, fundamentals

&#x20; scripts/        snapshot builders, smoke test, cache export

&#x20; tests/

&#x20; data/           SQLite DB, committed snapshots

frontend/feed/    mobile digest page

docs/

```



\## Built with



Python, FastAPI, SQLite, yfinance, httpx. Built with AI coding assistants. The conversation history is attached to the submission email.



Full architecture notes, invariants and ingest commands: \[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)



\---



\*Built by Palla Deekshitha(2023CE11034), IIT Delhi.\*





