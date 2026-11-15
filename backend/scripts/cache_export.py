"""Export the LLM cache to backend/data/seed/llm_cache.jsonl (commit it; imported at startup)."""
from app.config import get_settings
from app.llm import get_llm

s = get_settings()
n = get_llm().export_cache(s.seed_dir / "llm_cache.jsonl")
print(f"exported {n} cached responses -> {s.seed_dir / 'llm_cache.jsonl'}")
