"""FastAPI app: the interactable chatbot new users hit.

POST /chat  -> {answer, sources[]}   the RAG endpoint
GET  /healthz                          liveness
GET  /                                 marketing landing page (static/landing.html)
GET  /login, /signup                   accounts (static/auth.html)
GET  /brands                           your brands: open, re-run, delete (static/brands.html)
GET  /start                            add-a-brand onboarding (static/onboarding.html)
GET  /chat                             chat UI (static/index.html)

Everything except the landing page, the sign-in pages, health checks, static
assets and shared-report links requires an account; see `api/auth.py`. Every
brand-scoped endpoint also checks that the brand is one of the signed-in
account's brands, so one account cannot read another's brand list or reports.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import accounts, alerts, db, llm
from . import auth, health, insights, onboarding, rag, report, stats

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

app = FastAPI(title="ReviewBay", version="0.2.0")
app.include_router(auth.router)
_STATIC = Path(__file__).parent / "static"


# Middleware runs outermost-last-registered: _no_cache wraps _basic_auth, which
# wraps the session check. So the optional site-wide password gate still answers
# first, exactly as before accounts existed.
@app.middleware("http")
async def _session(request, call_next):
    return await auth.session_middleware(request, call_next)


@app.middleware("http")
async def _basic_auth(request, call_next):
    """Optional password gate. When AUTH_USER and AUTH_PASS are both set, every
    route except /healthz requires HTTP Basic credentials. Off by default.

    With accounts this is an extra outer wall rather than the only one — useful
    for keeping a staging deployment private — and no longer what protects
    saved reports (that is ownership now)."""
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


# --- errors and scoping -----------------------------------------------------------


class ApiError(Exception):
    """An error with a status, a stable code for the frontend, and a message."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@app.exception_handler(ApiError)
async def _api_error(_request, exc: ApiError):
    return JSONResponse({"ok": False, "error": exc.code, "message": exc.message, **exc.extra},
                        status_code=exc.status)


def _user(request: Request) -> accounts.User:
    user = auth.user_of(request)
    if user is None:  # the middleware normally answers first; this is the backstop
        raise ApiError(401, "auth_required", "Sign in to continue.")
    return user


def _brand(request: Request, brand: str | None, *, required: bool = True) -> str | None:
    """The canonical name of one of THIS account's brands.

    Brand-scoped endpoints used to accept any name, and an omitted name meant
    every brand in the database. With accounts the brand has to be one you track:
    otherwise a request could read another account's brands, and probing a name
    would reveal that someone tracks it.
    """
    user = _user(request)
    name = (brand or "").strip()
    if not name:
        if required:
            raise ApiError(400, "brand_required", "Choose one of your brands.")
        return None
    try:
        canonical = auth.member_brand(user, name)
    except db.DatabaseConfigError:
        # No database means no data to scope; the endpoint itself will answer
        # with its usual "set DATABASE_URL" payload.
        return name
    except Exception:  # noqa: BLE001
        log.exception("could not check brand membership")
        raise ApiError(503, "unavailable", "Could not load your brands just now.") from None
    if canonical is None:
        raise ApiError(404, "not_in_account", f"{name} is not one of your brands.", brand=name)
    return canonical


def _member_names(request: Request) -> list[str]:
    try:
        return auth.member_names(_user(request))
    except db.DatabaseConfigError:
        return []


def _require_admin(request: Request) -> accounts.User:
    user = _user(request)
    if not user.is_admin:
        raise ApiError(403, "admin_only", "Only an administrator can do that.")
    return user


# Request bounds. Without them `question` was an unbounded string and `brands`
# an unbounded list that fans out to ONE OpenAI embedding call plus one pgvector
# query PER BRAND (rag.py), so a single request naming 500 brands was 500
# embedding calls. Pydantic rejects an over-long request with a 422 before any
# of that work starts.
MAX_QUESTION_CHARS = 2000
MAX_BRANDS_PER_REQUEST = 25
MAX_BRAND_CHARS = 120

BrandQuery = Query(default=None, max_length=MAX_BRAND_CHARS)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    # the selected brand (None = "All my brands")
    brand: str | None = Field(default=None, max_length=MAX_BRAND_CHARS)
    # brands to compare; ignored unless they are the account's own
    brands: list[str] | None = Field(default=None, max_length=MAX_BRANDS_PER_REQUEST)


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
    # Which model wrote the answer, so the UI can say when a free fallback did.
    generated_by: dict | None = None


class AddBrandRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_BRAND_CHARS)
    website: str | None = Field(default=None, max_length=2000)


class ImportBrandsRequest(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=MAX_BRANDS_PER_REQUEST)


# --- health -------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only. INSTANT, no I/O. This is the platform health-check path, so
    it must never block: a slow call here (e.g. a DB round-trip while the embedding
    model is loading) can make the platform judge the container unhealthy and
    restart it mid-work. The database probe lives at /readyz instead.
    """
    return {"status": "ok"}


@app.get("/readyz", response_model=None)
def readyz():
    """Readiness probe with setup diagnostics. It is deliberately separate from
    /healthz so slow DB checks never affect platform liveness."""
    checks: dict[str, str] = {}
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            checks["database"] = "ok"
        finally:
            conn.close()
    except db.DatabaseConfigError as exc:
        checks["database"] = str(exc)
        return JSONResponse({"status": "error", "checks": checks}, status_code=503)
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse({"status": "error", "checks": checks}, status_code=503)

    checks["openai"] = "configured" if os.environ.get("OPENAI_API_KEY") else "fallback"
    checks["llm"] = llm.summary()
    airbyte_path = Path(os.environ.get("AIRBYTE_SOURCE_MAP", "config/airbyte_sources.yml"))
    checks["airbyte_source_map"] = "configured" if airbyte_path.exists() else "missing"
    return {"status": "ok", "checks": checks}


# --- chat -----------------------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    mine = _member_names(request)
    brand = _brand(request, req.brand, required=False)
    # Only the account's own brands can be pulled into an answer. "All my brands"
    # used to mean every brand in the database whenever the browser's list was
    # empty; now it means exactly this account's list.
    lookup = {accounts.brand_key(b): b for b in mine}
    wanted = [lookup[k] for k in (accounts.brand_key(b) for b in (req.brands or [])) if k in lookup]
    brands = list(dict.fromkeys(wanted)) or mine
    if not brand and not brands:
        return ChatResponse(
            answer="Add a brand first, then I can tell you what its customers are saying.",
            sources=[],
        )
    result = rag.answer(req.question, brand=brand, brands=brands)
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
        generated_by=getattr(result, "generated_by", None),
    )


# --- brand-scoped data -------------------------------------------------------------------


@app.get("/api/stats")
def api_stats(request: Request, brand: str | None = BrandQuery) -> dict:
    return stats.get_stats(_brand(request, brand))


@app.get("/api/reviews")
def api_reviews(
    request: Request,
    brand: str | None = BrandQuery,
    sort: str = "recent",
    limit: int = 40,
    source: str | None = None,
) -> dict:
    return stats.get_reviews(_brand(request, brand), sort, limit, source)


@app.get("/api/trend")
def api_trend(request: Request, brand: str | None = BrandQuery, bucket: str = "month") -> dict:
    return stats.get_trend(_brand(request, brand), bucket)


@app.get("/api/sentiment-alert")
def api_sentiment_alert(request: Request, brand: str | None = BrandQuery) -> dict:
    return stats.get_sentiment_alert(_brand(request, brand))


@app.get("/api/insights")
def api_insights(request: Request, brand: str | None = BrandQuery, refresh: bool = False) -> dict:
    return insights.get_insights(_brand(request, brand), refresh=refresh)


@app.get("/api/report")
def api_report(request: Request, brand: str | None = BrandQuery, refresh: bool = False) -> dict:
    return report.get_report(_brand(request, brand), refresh=refresh)


@app.post("/api/report/save")
def api_report_save(request: Request, brand: str | None = BrandQuery) -> dict:
    return report.save_report(_brand(request, brand), owner_id=_user(request).id)


@app.get("/api/reports/saved")
def api_reports_saved(request: Request, brand: str | None = BrandQuery) -> dict:
    user = _user(request)
    scoped = _brand(request, brand, required=False)
    return {"saved": report.list_saved(scoped, owner_id=user.id, is_admin=user.is_admin)}


@app.get("/api/reports/saved/{report_id}")
def api_report_saved_one(report_id: int, request: Request) -> dict:
    user = _user(request)
    found = report.get_saved(report_id, owner_id=user.id, is_admin=user.is_admin)
    if found is None:
        # The same answer for "not yours" and "does not exist", so sequential
        # ids cannot be probed (audit finding F11).
        raise ApiError(404, "not_found", "That saved report was not found.")
    return found


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


@app.get("/api/llm/status")
def api_llm_status() -> dict:
    """Which models are configured, which are cooling down, and why. No keys."""
    return llm.status()


@app.get("/api/runs")
def api_runs(request: Request, brand: str | None = BrandQuery, limit: int = 50) -> dict:
    from .. import runs

    return {"runs": runs.list_runs(_brand(request, brand), limit)}


@app.get("/api/export/reviews.csv")
def api_export_reviews(
    request: Request, brand: str | None = BrandQuery, source: str | None = None
) -> Response:
    from . import export

    scoped = _brand(request, brand)
    return Response(
        content=export.reviews_csv(scoped, source=source),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export.reviews_filename(scoped)}"'},
    )


@app.get("/api/alerts/status")
def api_alerts_status() -> dict:
    return alerts.status()


@app.post("/api/alerts/test")
def api_alerts_test(request: Request) -> dict:
    # Sends a real message to the operator's Slack/Discord webhook or inbox, so
    # with open signup it must not be something any account can trigger.
    _require_admin(request)
    return {"ok": alerts.send_test(), "status": alerts.status()}


@app.get("/api/compare")
def api_compare(request: Request, a: str | None = BrandQuery, b: str | None = BrandQuery) -> dict:
    return stats.compare_stats(_brand(request, a), _brand(request, b, required=False))


@app.get("/api/collection-health")
def api_collection_health(request: Request, brand: str | None = BrandQuery) -> dict:
    """Per-source and per-engine collection health for the dashboard panel."""
    return health.collection_health(_brand(request, brand))


# --- your brands ---------------------------------------------------------------------------


def _brand_entry(row: dict) -> dict:
    name = row["name"]
    job = onboarding.status(name)
    running = job.get("status") == "collecting"
    collected = row.get("last_collected_at")
    return {
        "name": name,
        "website": row.get("website"),
        "reviews": row.get("reviews", 0),
        "added_at": row["added_at"].isoformat() if row.get("added_at") else None,
        "last_collected_at": collected.isoformat() if collected else None,
        "running": running,
        "phase_label": job.get("phase_label") if running else None,
        "cooldown_seconds": 0 if running else onboarding.cooldown_remaining(name),
    }


@app.get("/api/brands")
def api_brands(request: Request) -> dict:
    """This account's brand names, newest first (what every page's picker shows)."""
    return {"brands": _member_names(request)}


@app.get("/api/me/brands")
def api_my_brands(request: Request) -> dict:
    """This account's brands with review counts and collection state."""
    user = _user(request)
    try:
        rows = accounts.account_brands(user)
    except db.DatabaseConfigError as exc:
        return {"brands": [], "setup_required": "database", "message": str(exc)}
    return {
        "brands": [_brand_entry(r) for r in rows],
        "limit": accounts.MAX_BRANDS_PER_ACCOUNT,
        "rerun_cooldown_seconds": onboarding.RERUN_COOLDOWN_SECONDS,
    }


@app.post("/api/brands")
def api_add_brand(req: AddBrandRequest, request: Request):
    """Add a brand to this account and start collecting it.

    "Search a brand" in the UI: the brand is recorded on the account (so it is
    still there from any browser, after any deploy) before collection starts.
    """
    user = _user(request)
    # Validate the REQUEST before checking infrastructure. A rejected website is
    # a 400 about the input, and whether it is acceptable does not depend on
    # whether the database happens to be configured.
    try:
        website = onboarding.normalize_website(req.website)
        cfg = onboarding.build_brand_config(req.name, website)
    except onboarding.InvalidWebsite as exc:
        # This URL is one the server will later fetch, so a rejected one is a
        # 400 carrying the policy reason, not a 500.
        return JSONResponse({"ok": False, "error": "invalid_website", "message": str(exc)},
                            status_code=400)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "invalid_request", "message": str(exc)},
                            status_code=400)

    try:
        db.database_url()
    except db.DatabaseConfigError as exc:
        return JSONResponse(
            {"ok": False, "setup_required": "database", "message": str(exc)},
            status_code=503,
        )

    try:
        canonical, _added = accounts.add_brand(user, cfg)
    except accounts.AccountError as exc:
        return auth.account_error(exc)
    finally:
        auth.forget_memberships(user)

    brand_cfg = accounts.as_brand_config(canonical)
    onboarding._persist_brand(brand_cfg)  # the YAML copy, for setups without a database
    started = False
    reused = False
    if onboarding.is_running(brand_cfg["name"]):
        pass  # someone (maybe another account) is collecting it right now: attach
    elif onboarding.cooldown_remaining(brand_cfg["name"]) > 0:
        reused = True  # collected moments ago: show that data instead of re-scraping
    else:
        started = onboarding.start_async(brand_cfg)
    return {
        "ok": True,
        "brand": brand_cfg["name"],
        "sources": brand_cfg["sources"],
        "started": started,
        "reused": reused,
    }


@app.post("/api/me/brands/import")
def api_import_brands(req: ImportBrandsRequest, request: Request) -> dict:
    """Bring the brands this browser remembered (from before accounts) into the
    account. Membership only: nothing is collected until you ask for it."""
    user = _user(request)
    imported, skipped = [], []
    try:
        for raw in req.names:
            try:
                cfg = onboarding.build_brand_config(raw)
                canonical, _added = accounts.add_brand(user, cfg)
                imported.append(canonical["name"])
            except (ValueError, accounts.AccountError) as exc:
                skipped.append({"name": str(raw)[:MAX_BRAND_CHARS], "reason": str(exc)})
    except db.DatabaseConfigError as exc:
        raise ApiError(503, "setup_required", str(exc), setup_required="database") from None
    finally:
        auth.forget_memberships(user)
    return {"ok": True, "imported": imported, "skipped": skipped}


@app.delete("/api/brands/{brand}")
def api_delete_brand(brand: str, request: Request) -> dict:
    """Remove a brand from this account.

    Its reviews are kept (other accounts may track it, and re-adding is then
    instant). If no account tracks it any more, scheduled refresh stops.
    """
    user = _user(request)
    canonical = _brand(request, brand)
    try:
        result = accounts.remove_brand(user, canonical)
    finally:
        auth.forget_memberships(user)
    if result["orphaned"]:
        onboarding.forget_brand(canonical)
    return {"ok": True, "brand": canonical, "removed": result["removed"],
            "refresh_stopped": result["orphaned"]}


@app.post("/api/brands/{brand}/rerun")
def api_rerun_brand(brand: str, request: Request):
    """Collect a brand again now, with the same pipeline as adding it."""
    canonical = _brand(request, brand)
    if onboarding.is_running(canonical):
        return {"ok": True, "brand": canonical, "started": False, "already_running": True}
    wait = onboarding.cooldown_remaining(canonical)
    if wait > 0:
        resp = JSONResponse(
            {"ok": False, "error": "cooldown", "brand": canonical, "retry_after": wait,
             "message": f"{canonical} was collected moments ago. Try again in {wait} seconds."},
            status_code=429,
        )
        resp.headers["Retry-After"] = str(wait)
        return resp
    entry = accounts.get_brand(canonical) if _db_ready() else None
    brand_cfg = accounts.as_brand_config(entry) if entry else onboarding.build_brand_config(canonical)
    started = onboarding.start_async(brand_cfg)
    if started:
        try:
            accounts.mark_rerun(canonical)
        except Exception:  # noqa: BLE001 - the in-memory job already enforces the cooldown
            log.debug("could not stamp last_rerun_at for %s", canonical)
    return {"ok": True, "brand": canonical, "started": started, "already_running": not started}


def _db_ready() -> bool:
    try:
        db.database_url()
        return True
    except db.DatabaseConfigError:
        return False


@app.get("/api/brands/{brand}/status")
def api_brand_status(brand: str, request: Request) -> dict:
    canonical = _brand(request, brand)
    job = onboarding.status(canonical)
    if job.get("status") == "unknown" and _db_ready():
        # No job in this process (restart, or the brand was reused rather than
        # re-collected): report what is stored instead of an empty "unknown".
        try:
            rows = {r["name"]: r for r in accounts.account_brands(_user(request))}
            stored = rows.get(canonical, {}).get("reviews", 0)
        except Exception:  # noqa: BLE001
            stored = 0
        job.update(status="done" if stored else "empty", phase="done", collected=stored,
                   collected_new=0, reused=True)
    return job


# --- pages -----------------------------------------------------------------------------------


def _safe_next(value: str | None, default: str = "/brands") -> str:
    """A same-site path to continue to. Anything else (an absolute URL, or the
    protocol-relative "//evil.example") would make this an open redirect."""
    value = value or ""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default


@app.get("/login")
def login_page(request: Request, next: str | None = None):  # noqa: A002 - the query name
    if auth.user_of(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return FileResponse(_STATIC / "auth.html")


@app.get("/signup")
def signup_page(request: Request, next: str | None = None):  # noqa: A002 - the query name
    if auth.user_of(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return FileResponse(_STATIC / "auth.html")


@app.get("/brands")
def brands_page() -> FileResponse:
    return FileResponse(_STATIC / "brands.html")


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
