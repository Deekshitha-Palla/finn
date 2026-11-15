"""Proves the LLM path end to end: key -> provider -> cache -> cost log. Run twice: 2nd call is a cache hit."""
from app.config import get_settings
from app.llm import LLMUnavailable, get_llm
from app.prompts import SMOKE

try:
    r = get_llm().run(SMOKE, "ping")
except LLMUnavailable as e:
    raise SystemExit(f"LLM unavailable: {e}")
print(f"text={r.text!r} cached={r.cached} model={r.model} tokens={r.usage} cost=${r.cost_usd:.6f}")
print(get_llm().usage_summary())
