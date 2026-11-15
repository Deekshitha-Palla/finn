"""Central settings. Plain dataclasses + os.environ so tests need no extra deps.

Env is loaded from <repo>/.env (if present) without overriding real env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TierConfig:
    """One LLM 'tier' (cheap = bulk extraction, strong = reasoning)."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    price_in_per_m: float = 0.0   # USD per 1M input tokens (shadow price for free tiers)
    price_out_per_m: float = 0.0  # USD per 1M output tokens


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    offline: bool = False          # FINN_OFFLINE: never hit the network for data; snapshots only
    llm_cache_only: bool = False   # LLM_CACHE_ONLY: never call a model; cache only
    fetch_timeout_s: float = 8.0
    tiers: dict[str, TierConfig] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finn.db"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def seed_dir(self) -> Path:
        return self.data_dir / "seed"


def _tier(prefix: str) -> TierConfig:
    g = os.environ.get
    return TierConfig(
        base_url=g(f"LLM_{prefix}_BASE_URL", ""),
        api_key=g(f"LLM_{prefix}_API_KEY", ""),
        model=g(f"LLM_{prefix}_MODEL", ""),
        price_in_per_m=float(g(f"LLM_{prefix}_PRICE_IN_PER_M", "0") or 0),
        price_out_per_m=float(g(f"LLM_{prefix}_PRICE_OUT_PER_M", "0") or 0),
    )


@lru_cache
def get_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    data_dir = Path(os.environ.get("FINN_DATA_DIR", REPO_ROOT / "backend" / "data"))
    return Settings(
        data_dir=data_dir,
        offline=_bool("FINN_OFFLINE"),
        llm_cache_only=_bool("LLM_CACHE_ONLY"),
        fetch_timeout_s=float(os.environ.get("FETCH_TIMEOUT_S", "8")),
        tiers={"cheap": _tier("CHEAP"), "strong": _tier("STRONG")},
    )
