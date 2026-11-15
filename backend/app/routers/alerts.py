from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import get_settings
from app.db import connect, init_db
from app.services import alerts as svc

router = APIRouter(prefix="/api")


@router.get("/alerts")
def get_alerts(vol_mult: float = Query(2.0, gt=0, le=100), move_pct: float = Query(3.0, gt=0, le=100)):
    path = get_settings().db_path
    init_db(path)
    conn = connect(path)
    try:
        rows = svc.load_prices(conn)
    finally:
        conn.close()
    return svc.build_alerts(rows, vol_mult, move_pct)
