.DEFAULT_GOAL := help
PY       ?= python3
VENV     := backend/.venv
VPY      := $(VENV)/bin/python
PORT     ?= 8000

.PHONY: help setup setup-backend setup-frontend dev backend frontend test lint smoke snapshot cache-export build serve freeze docker clean ingest-prices ingest-announcements refresh-announcements ingest-fundamentals

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n",$$1,$$2}'

setup: setup-backend setup-frontend  ## install everything (needs network)
	@test -f .env || (cp .env.example .env && echo "created .env -- add your API keys")

setup-backend:  ## venv + python deps
	$(PY) -m venv $(VENV)
	$(VPY) -m pip install -U pip
	$(VPY) -m pip install -r backend/requirements-dev.txt

setup-frontend:  ## npm install
	cd frontend && npm install

dev:  ## backend (:8000) + Vite (:5173) together; open :5173 on your phone via LAN IP
	$(MAKE) -j2 backend frontend

backend:  ## FastAPI with reload
	cd backend && ../$(VPY) -m uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

frontend:  ## Vite dev server (proxies /api -> :8000)
	cd frontend && npm run dev

test:  ## backend unit tests (no network, no keys needed)
	cd backend && ../$(VPY) -m pytest -q

lint:  ## ruff
	cd backend && ../$(VPY) -m ruff check app tests scripts

smoke:  ## one real LLM call; run twice -- 2nd must say cached=True
	cd backend && ../$(VPY) -m scripts.smoke

ingest-prices:  ## EOD prices for portfolio.yaml (last 90d) -> SQLite; T="TCS INFY" to limit
	cd backend && ../$(VPY) -m ingest.prices $(if $(T),--tickers $(T))

ingest-announcements:  ## BSE -> NSE -> snapshot, into SQLite; T="TCS" to limit
	cd backend && ../$(VPY) -m ingest.announcements $(if $(T),--tickers $(T))

refresh-announcements:  ## live fetch -> data/snapshots/announcements.json (run locally, commit it)
	cd backend && ../$(VPY) -m scripts.refresh_announcements_snapshot $(if $(T),--tickers $(T))

ingest-fundamentals:  ## ARs + concall PDFs from screener.in: make ingest-fundamentals T=RELIANCE (or T=--all)
	@test -n "$(T)" || (echo "usage: make ingest-fundamentals T=RELIANCE   (or T=--all)"; exit 2)
	cd backend && ../$(VPY) -m ingest.fundamentals $(T)

snapshot: refresh-announcements  ## refresh live-source snapshots (prices/fundamentals snapshot themselves on every fetch)

cache-export:  ## dump LLM cache to backend/data/seed/llm_cache.jsonl (commit it)
	cd backend && ../$(VPY) -m scripts.cache_export

build:  ## production build of the PWA -> frontend/dist
	cd frontend && npm run build

serve: build  ## production-like: FastAPI serves the built PWA on one port
	cd backend && ../$(VPY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

freeze:  ## pin python deps after a known-good install
	$(VPY) -m pip freeze > backend/requirements.lock.txt

docker:  ## build + run the deploy image locally on :7860
	docker build -t finn . && docker run --rm -p 7860:7860 --env-file .env finn

clean:  ## remove build artefacts (keeps data/)
	rm -rf frontend/dist frontend/node_modules $(VENV)
