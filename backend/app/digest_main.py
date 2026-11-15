"""Standalone entry for the digest slice: API + static feed. Run: uvicorn app.digest_main:app"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers.digest import router

app = FastAPI(title="FINN digest")
app.include_router(router)
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend" / "feed", html=True))
