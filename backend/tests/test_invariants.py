"""Architecture guardrails: cheap static checks so the two core rules can't silently rot."""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
INGEST = BACKEND / "ingest"

LLM_PAT = re.compile(
    r"chat/completions|import openai|from openai|import anthropic|from anthropic|google\.generativeai|import groq"
)
NET_PAT = re.compile(r"\b(import requests|import yfinance|import httpx|from httpx|import urllib\.request)\b")


def _py(root: Path, exclude: set[str] = frozenset()):
    return [p for p in root.rglob("*.py") if p.name not in exclude]


def test_only_llm_module_talks_to_model_providers():
    offenders = [str(p) for p in _py(APP, {"llm.py"}) + _py(INGEST) if LLM_PAT.search(p.read_text())]
    assert not offenders, f"LLM provider access outside app/llm.py: {offenders}"


def test_only_ingest_and_llm_touch_the_network():
    offenders = [str(p) for p in _py(APP, {"llm.py"}) if NET_PAT.search(p.read_text())]
    assert not offenders, f"network access outside ingest/ + app/llm.py: {offenders}"
