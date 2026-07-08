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
from . import insights, onboarding, rag, report, stats

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
    # Public routes exempt from the password gate: health, and the read-only
    # shared-report links (/r/<token> and its data endpoint). Shared links are
    # meant to be opened by people without the app password.
    path = request.url.path
    exempt = path in ("/healthz", "/readyz") or path.startswith("/r/") or path.startswith("/api/shared/")
    if user and pw and not exempt:
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
    brand: str | None = None          # the selected brand (None = "All brands")
    brands: list[str] | None = None   # brands available to compare (this browser's list)


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
    confidence: str = "Low"
    evidence: dict = {}


class AddBrandRequest(BaseModel):
    name: str
    website: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only. INSTANT, no I/O. This is the platform health-check path, so
    it must never block: a slow call here (e.g. a DB round-trip while the embedding
    model is loading) can make the platform judge the container unhealthy and
    restart it mid-work. The database probe lives at /readyz instead.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, object]:
    """Readiness: reports DATABASE_URL config and a trivial DB query, no secrets,
    just booleans, so misconfig is visible without logging in. Kept OFF the
    platform health-check path so it can never trigger a restart."""
    import os

    db_configured = bool(os.environ.get("DATABASE_URL"))
    db = "not_configured"
    if db_configured:
        try:
            from ..db import connect

            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                db = "ok"
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — report, never raise from a readiness check
            db = "unreachable"
    return {"status": "ok", "database_url_set": db_configured, "db": db}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    result = rag.answer(req.question, brand=req.brand, brands=req.brands)
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
    return ChatResponse(
        answer=result.answer,
        sources=sources,
        confidence=result.confidence,
        evidence=result.evidence or {},
    )


@app.get("/api/stats")
def api_stats(brand: str | None = None) -> dict:
    return stats.get_stats(brand)


@app.get("/api/reviews")
def api_reviews(
    brand: str | None = None, sort: str = "recent", limit: int = 40, source: str | None = None
) -> dict:
    return stats.get_reviews(brand, sort, limit, source)


@app.get("/api/trend")
def api_trend(brand: str | None = None, bucket: str = "month") -> dict:
    return stats.get_trend(brand, bucket)


@app.get("/api/sentiment-alert")
def api_sentiment_alert(brand: str | None = None) -> dict:
    return stats.get_sentiment_alert(brand)


@app.get("/api/insights")
def api_insights(brand: str | None = None, refresh: bool = False) -> dict:
    return insights.get_insights(brand, refresh=refresh)


@app.get("/api/report")
def api_report(brand: str | None = None, refresh: bool = False) -> dict:
    return report.get_report(brand, refresh=refresh)


@app.post("/api/report/save")
def api_report_save(brand: str | None = None) -> dict:
    return report.save_report(brand)


@app.get("/api/reports/saved")
def api_reports_saved(brand: str | None = None) -> dict:
    return {"saved": report.list_saved(brand)}


@app.get("/api/reports/saved/{report_id}")
def api_report_saved_one(report_id: int) -> dict:
    return report.get_saved(report_id)


@app.get("/api/shared/{token}")
def api_shared_report(token: str) -> dict:
    return report.get_shared(token)


@app.get("/r/{token}")
def shared_report_page(token: str) -> FileResponse:
    # Public read-only view; the page reads the token from its own URL.
    return FileResponse(_STATIC / "report.html")


@app.get("/api/connectors")
def api_connectors() -> dict:
    from ..sources import connector_statuses

    return {"connectors": connector_statuses()}


@app.get("/api/runs")
def api_runs(brand: str | None = None, limit: int = 50) -> dict:
    from .. import runs

    return {"runs": runs.list_runs(brand, limit)}


@app.get("/api/export/reviews.csv")
def api_export_reviews(brand: str | None = None, source: str | None = None) -> Response:
    from . import export

    return Response(
        content=export.reviews_csv(brand, source=source),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export.reviews_filename(brand)}"'},
    )


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


@app.get("/report")
def report_page() -> FileResponse:
    return FileResponse(_STATIC / "report.html")


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
