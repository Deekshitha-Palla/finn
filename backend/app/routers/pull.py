from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/pull", tags=["pull"])


@router.post("/research")
def research(symbol: str):
    """On-demand analysis for any NSE ticker, grounded in annual reports + concalls only. TODO."""
    raise HTTPException(501, "not implemented")
