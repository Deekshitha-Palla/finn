from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from app.config import get_settings
from app.db import connect, init_db
from app.services import digest as svc

router = APIRouter(prefix="/api")


@router.get("/digest")
def get_digest(window: Literal["24h", "3d", "7d", "30d"] = "24h"):
    path = get_settings().db_path
    init_db(path)
    conn = connect(path)  # opened inside the handler: sqlite connections are thread-bound
    try:
        rows = svc.classify_and_store(conn)
    finally:
        conn.close()
    return svc.build_digest(rows, window)
