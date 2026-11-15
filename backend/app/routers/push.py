from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/digest")
def digest(hours: int = 24):
    """Windowed digest across the portfolio, prioritised. TODO."""
    raise HTTPException(501, "not implemented")


@router.get("/alerts")
def alerts():
    """Triggered alerts (announcement-type + EOD price/volume thresholds). TODO."""
    raise HTTPException(501, "not implemented")
