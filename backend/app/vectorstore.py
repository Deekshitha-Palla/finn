"""Chroma wrapper (lazy import: chromadb is heavy and only the pull side needs it).

Only primary sources (annual reports, concall transcripts) go in here -- see the brief's pull-side rule.
Chunks must carry metadata: symbol, doc_type ("annual_report" | "concall"), period, source_url, page.

RISK: chromadb's default embedding function downloads an ONNX model on first use. On a cold deploy with
no network that fails. Either pre-warm it in the Docker build or pass an explicit embedding function.
"""
from __future__ import annotations

from .config import Settings


def get_collection(settings: Settings, name: str = "filings"):
    import chromadb  # noqa: PLC0415

    client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return client.get_or_create_collection(name)
