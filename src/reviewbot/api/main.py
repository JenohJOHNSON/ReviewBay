"""FastAPI app: the interactable chatbot new users hit.

POST /chat  -> {answer, sources[]}   the RAG endpoint
GET  /healthz                          liveness
GET  /                                 marketing landing page (static/landing.html)
GET  /start                            add-a-brand onboarding (static/onboarding.html)
GET  /chat                             chat UI (static/index.html)
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import alerts
from . import insights, onboarding, rag, stats

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="ReviewBay", version="0.1.0")
_STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def _basic_auth(request, call_next):
    """Optional password gate. When AUTH_USER and AUTH_PASS are both set, every
    route except /healthz requires HTTP Basic credentials. Off by default, so
    local dev and onboarding stay open. Turn it on before putting the app on a
    public URL (for example behind a Cloudflare tunnel)."""
    user = os.environ.get("AUTH_USER")
    pw = os.environ.get("AUTH_PASS")
    if user and pw and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                u, _, p = decoded.partition(":")
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(
                "Authentication required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ReviewBay"'},
            )
    return await call_next(request)


@app.middleware("http")
async def _no_cache(request, call_next):
    """Always revalidate, so a rebuild shows up immediately with no stale HTML or
    JS left in the browser between deploys. Cheap for a low-traffic demo app."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


class ChatRequest(BaseModel):
    question: str
    brand: str | None = None


class SourceOut(BaseModel):
    n: int
    source: str
    source_url: str
    author: str | None = None
    rating: float | None = None
    brand: str
    sentiment: str | None = None
    excerpt: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut]


class AddBrandRequest(BaseModel):
    name: str
    website: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    result = rag.answer(req.question, brand=req.brand)
    sources = [
        SourceOut(
            n=i,
            source=s.source,
            source_url=s.source_url,
            author=s.author,
            rating=s.rating,
            brand=s.brand,
            sentiment=s.sentiment,
            excerpt=(s.text[:280] + "…") if len(s.text) > 280 else s.text,
        )
        for i, s in enumerate(result.sources, start=1)
    ]
    return ChatResponse(answer=result.answer, sources=sources)


@app.get("/api/stats")
def api_stats(brand: str | None = None) -> dict:
    return stats.get_stats(brand)


@app.get("/api/reviews")
def api_reviews(brand: str | None = None, sort: str = "recent", limit: int = 40) -> dict:
    return stats.get_reviews(brand, sort, limit)


@app.get("/api/insights")
def api_insights(brand: str | None = None, refresh: bool = False) -> dict:
    return insights.get_insights(brand, refresh=refresh)


@app.get("/api/alerts/status")
def api_alerts_status() -> dict:
    return alerts.status()


@app.post("/api/alerts/test")
def api_alerts_test() -> dict:
    return {"ok": alerts.send_test(), "status": alerts.status()}


@app.get("/api/compare")
def api_compare(a: str | None = None, b: str | None = None) -> dict:
    return stats.compare_stats(a, b)


@app.get("/api/brands")
def api_brands() -> dict:
    # The brand list is per-browser (kept in the client's localStorage), so the
    # server intentionally does not hand back a global roster of tracked brands.
    return {"brands": []}


@app.post("/api/brands")
def api_add_brand(req: AddBrandRequest) -> dict:
    cfg = onboarding.add_brand(req.name, req.website)
    onboarding.start_async(cfg)
    return {"ok": True, "brand": cfg["name"], "sources": cfg["sources"]}


@app.get("/api/brands/{brand}/status")
def api_brand_status(brand: str) -> dict:
    return onboarding.status(brand)


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(_STATIC / "dashboard.html")


@app.get("/compare")
def compare() -> FileResponse:
    return FileResponse(_STATIC / "compare.html")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "landing.html")


@app.get("/start")
def start() -> FileResponse:
    return FileResponse(_STATIC / "onboarding.html")


@app.get("/chat")
def chat_ui() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


# Serve any other static assets (kept last so it doesn't shadow the routes).
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
