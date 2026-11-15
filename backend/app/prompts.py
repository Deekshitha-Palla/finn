"""Prompt registry. Every LLM call is a `Prompt` with a version.

Bump `version` whenever you change `system` (or the output contract): the version is part of the
cache key, so a bump invalidates cached answers for that prompt and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    tier: str = "cheap"        # "cheap" (bulk extraction) | "strong" (reasoning)
    json_mode: bool = False
    max_tokens: int = 512
    temperature: float = 0.0   # deterministic by default -> cache is meaningful


# Used by `make smoke` to prove keys + cache + cost logging work end to end.
SMOKE = Prompt(
    name="smoke",
    version="1",
    system="Reply with the single word: pong",
    max_tokens=8,
)

# Real prompts get added here as features land, e.g.:
#   CLASSIFY_ANNOUNCEMENT = Prompt("classify_announcement", "1", "...", tier="cheap", json_mode=True)
#   RESULTS_EXTRACT       = Prompt("results_extract", "1", "...", tier="cheap", json_mode=True)
#   NUMBERS_VS_NARRATIVE  = Prompt("numbers_vs_narrative", "1", "...", tier="strong")
