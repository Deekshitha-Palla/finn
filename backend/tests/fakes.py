"""Tiny fakes for httpx so network logic is testable offline."""
from __future__ import annotations

from typing import Any, Callable

_MISSING = object()


class FakeResp:
    def __init__(self, status: int = 200, json_data: Any = _MISSING, text: str = "", content: bytes = b"",
                 headers: dict[str, str] | None = None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json is _MISSING:
            raise ValueError("no json")
        return self._json


class FakeClient:
    """handler(url, params) -> FakeResp, or raises. Records every call in .calls."""

    def __init__(self, handler: Callable[[str, dict | None], FakeResp]):
        self.handler = handler
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.handler(url, params)

    def close(self):
        self.closed = True


def no_sleep(_s: float) -> None:
    return None
