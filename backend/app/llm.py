"""THE single entry point for every model call in FINN.

Rules (enforced by tests/test_invariants.py):
  * Nothing outside this module talks to an LLM provider.
  * Every call is a versioned `Prompt` run through `LLM.run`.
  * Cache key = "<prompt>:<version>:<sha256(model + canonical_input)>". Model is hashed in so
    switching models never serves another model's answer; bumping Prompt.version invalidates.
  * Every call (hit, miss, or failure) is logged to llm_log with tokens and cost.

Providers are OpenAI-compatible chat endpoints (Groq, Gemini's OpenAI-compat endpoint, OpenRouter,
Ollama, ...) configured purely through env: see .env.example.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Settings, TierConfig, get_settings
from .db import connect, init_db
from .prompts import Prompt


class LLMUnavailable(RuntimeError):
    """No cached answer and no usable provider (cache-only mode, missing key, or provider down).

    Callers must catch this and degrade (show the raw filing, a stale summary, a snapshot).
    """


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    cached: bool
    model: str
    tier: str
    usage: Usage
    cost_usd: float
    cache_key: str

    def json(self) -> Any:
        return json.loads(self.text)


class Provider(Protocol):
    def chat(
        self, *, model: str, system: str, user: str, max_tokens: int, json_mode: bool, temperature: float
    ) -> tuple[str, Usage]: ...


class OpenAICompatProvider:
    """Minimal OpenAI-compatible /chat/completions client (httpx imported lazily)."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, *, model, system, user, max_tokens, json_mode, temperature):
        import httpx

        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        u = data.get("usage") or {}
        return text, Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))


def _canonical(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _cost(tier: TierConfig, u: Usage) -> float:
    return (u.input_tokens * tier.price_in_per_m + u.output_tokens * tier.price_out_per_m) / 1_000_000


class LLM:
    def __init__(
        self,
        settings: Settings,
        providers: dict[str, Provider] | None = None,
        retries: int = 2,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.retries = retries
        self._sleep = sleep
        init_db(settings.db_path)
        if providers is None:
            providers = {
                name: OpenAICompatProvider(t.base_url, t.api_key)
                for name, t in settings.tiers.items()
                if t.api_key and t.base_url and t.model
            }
        self.providers = providers

    # ---- public API -------------------------------------------------------------------------
    def run(self, prompt: Prompt, payload: Any) -> LLMResult:
        tier_cfg = self.settings.tiers.get(prompt.tier) or TierConfig()
        model = tier_cfg.model or "unconfigured"
        user = _canonical(payload)
        digest = hashlib.sha256(f"{model}\n{user}".encode()).hexdigest()
        key = f"{prompt.name}:{prompt.version}:{digest}"

        hit = self._cache_get(key)
        if hit is not None:
            text, usage = hit
            self._log(prompt, prompt.tier, model, cached=True, usage=usage, cost=0.0, latency_ms=0)
            return LLMResult(text, True, model, prompt.tier, usage, 0.0, key)

        if self.settings.llm_cache_only:
            raise self._fail(prompt, model, "cache-only mode and no cached answer")
        provider = self.providers.get(prompt.tier)
        if provider is None:
            raise self._fail(prompt, model, f"no provider configured for tier '{prompt.tier}'")

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                text, usage = provider.chat(
                    model=model,
                    system=prompt.system,
                    user=user,
                    max_tokens=prompt.max_tokens,
                    json_mode=prompt.json_mode,
                    temperature=prompt.temperature,
                )
            except Exception as e:  # network, 429, 5xx, bad payload
                last = e
                if attempt < self.retries:
                    self._sleep(0.5 * 2**attempt)
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            cost = _cost(tier_cfg, usage)
            self._cache_put(key, prompt, model, text, usage)
            self._log(prompt, prompt.tier, model, cached=False, usage=usage, cost=cost, latency_ms=ms)
            return LLMResult(text, False, model, prompt.tier, usage, cost, key)

        raise self._fail(prompt, model, f"provider error after retries: {last!r}")

    def usage_summary(self) -> dict[str, Any]:
        conn = connect(self.settings.db_path)
        try:
            rows = conn.execute(
                """SELECT prompt, version, model, COUNT(*) calls, SUM(cached) cache_hits,
                          SUM(in_tokens) in_tok, SUM(out_tokens) out_tok, SUM(cost_usd) cost
                   FROM llm_log WHERE error IS NULL GROUP BY prompt, version, model"""
            ).fetchall()
            errs = conn.execute("SELECT COUNT(*) FROM llm_log WHERE error IS NOT NULL").fetchone()[0]
        finally:
            conn.close()
        by_prompt = [dict(r) for r in rows]
        return {
            "total_cost_usd": sum(r["cost"] or 0 for r in by_prompt),
            "calls": sum(r["calls"] for r in by_prompt),
            "cache_hits": sum(r["cache_hits"] or 0 for r in by_prompt),
            "errors": errs,
            "by_prompt": by_prompt,
        }

    # Ship the cache with the repo so a cold, ephemeral deploy still has every demo answer.
    def export_cache(self, path: Path) -> int:
        conn = connect(self.settings.db_path)
        try:
            rows = conn.execute("SELECT * FROM llm_cache ORDER BY cache_key").fetchall()
        finally:
            conn.close()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
        return len(rows)

    def import_cache(self, path: Path) -> int:
        path = Path(path)
        if not path.exists():
            return 0
        conn = connect(self.settings.db_path)
        n = 0
        try:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO llm_cache
                       (cache_key,prompt,version,model,response,in_tokens,out_tokens,created_at)
                       VALUES (:cache_key,:prompt,:version,:model,:response,:in_tokens,:out_tokens,:created_at)""",
                    r,
                )
                n += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return n

    # ---- internals --------------------------------------------------------------------------
    def _cache_get(self, key: str) -> tuple[str, Usage] | None:
        conn = connect(self.settings.db_path)
        try:
            r = conn.execute(
                "SELECT response,in_tokens,out_tokens FROM llm_cache WHERE cache_key=?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return (r["response"], Usage(r["in_tokens"], r["out_tokens"])) if r else None

    def _cache_put(self, key: str, p: Prompt, model: str, text: str, u: Usage) -> None:
        conn = connect(self.settings.db_path)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO llm_cache(cache_key,prompt,version,model,response,in_tokens,out_tokens)
                   VALUES (?,?,?,?,?,?,?)""",
                (key, p.name, p.version, model, text, u.input_tokens, u.output_tokens),
            )
            conn.commit()
        finally:
            conn.close()

    def _log(self, p: Prompt, tier: str, model: str, *, cached: bool, usage: Usage, cost: float,
             latency_ms: int, error: str | None = None) -> None:
        conn = connect(self.settings.db_path)
        try:
            conn.execute(
                """INSERT INTO llm_log(prompt,version,tier,model,cached,in_tokens,out_tokens,cost_usd,latency_ms,error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (p.name, p.version, tier, model, int(cached), usage.input_tokens, usage.output_tokens,
                 cost, latency_ms, error),
            )
            conn.commit()
        finally:
            conn.close()

    def _fail(self, p: Prompt, model: str, msg: str) -> LLMUnavailable:
        self._log(p, p.tier, model, cached=False, usage=Usage(), cost=0.0, latency_ms=0, error=msg)
        return LLMUnavailable(msg)


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM(get_settings())
    return _llm
