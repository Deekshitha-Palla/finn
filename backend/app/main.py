from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import init_db
from .llm import get_llm
from .routers import pull, push


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    init_db(s.db_path)
    # Seed the LLM cache from the committed export so ephemeral deploys keep every demo answer.
    get_llm().import_cache(s.seed_dir / "llm_cache.jsonl")
    yield


app = FastAPI(title="FINN", lifespan=lifespan)
app.include_router(push.router)
app.include_router(pull.router)


@app.get("/api/health")
def health():
    s = get_settings()
    return {
        "ok": True,
        "offline": s.offline,
        "llm_cache_only": s.llm_cache_only,
        "llm_tiers": {k: bool(t.api_key and t.model) for k, t in s.tiers.items()},
    }


@app.get("/api/usage")
def usage():
    """Token / cost / cache-hit summary: show this in the writeup."""
    return get_llm().usage_summary()


# Production: one container serves the built PWA. Mounted last so /api/* wins.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
