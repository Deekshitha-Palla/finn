"""Every external data fetch (yfinance, NSE/BSE, screener.in, LinkedIn, ...) goes through `cached_fetch`.

Contract:
  1. Not offline -> run the live fetcher (with a hard timeout). On success, atomically write the result
     to data/snapshots/<name>/<key>.json and return source="live".
  2. Live failed / empty / timed out, or FINN_OFFLINE=1 -> return the last snapshot, source="snapshot".
  3. No snapshot either -> raise SnapshotMissing. Callers decide how to degrade; they never crash the UI.

Snapshots are committed to git (data/snapshots/**), so a fresh deploy with zero network still demos.
Fetchers must return JSON-serialisable data and must not swallow their own errors.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .config import Settings


class SnapshotMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class Fetched:
    data: Any
    source: Literal["live", "snapshot"]
    fetched_at: str            # ISO timestamp of when the data was originally fetched live
    error: str | None = None   # why live failed (only when source == "snapshot" after an attempt)


def _path(settings: Settings, name: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return settings.snapshot_dir / name / f"{safe}.json"


def read_snapshot(settings: Settings, name: str, key: str) -> Fetched | None:
    p = _path(settings, name, key)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        return Fetched(raw["data"], "snapshot", raw["fetched_at"])
    except (json.JSONDecodeError, KeyError):
        return None  # corrupt snapshot == no snapshot


def write_snapshot(settings: Settings, name: str, key: str, data: Any) -> str:
    p = _path(settings, name, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": now, "data": data}, ensure_ascii=False))
    os.replace(tmp, p)  # atomic: a crash never leaves a half-written snapshot
    return now


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=timeout)
    except FutureTimeout as e:
        raise TimeoutError(f"live fetch exceeded {timeout}s") from e
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # a stuck thread is abandoned, not awaited


def cached_fetch(
    settings: Settings,
    name: str,
    key: str,
    fetcher: Callable[[], Any],
    timeout: float | None = None,
) -> Fetched:
    """name = source namespace (e.g. "prices"), key = entity (e.g. "RELIANCE.NS")."""
    error: str | None = None
    if not settings.offline:
        try:
            data = _run_with_timeout(fetcher, timeout or settings.fetch_timeout_s)
            if data in (None, [], {}, ""):
                raise ValueError("live fetch returned empty result")
            return Fetched(data, "live", write_snapshot(settings, name, key, data))
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    snap = read_snapshot(settings, name, key)
    if snap is None:
        raise SnapshotMissing(f"no live data and no snapshot for {name}/{key} (live error: {error})")
    return Fetched(snap.data, "snapshot", snap.fetched_at, error)
