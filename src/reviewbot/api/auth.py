"""HTTP side of accounts: the session cookie, the login wall, and the auth API.

`reviewbot.accounts` owns the rules; this module owns the web concerns around
them:

* **The login wall.** Every page and API route requires a session except a short
  public list: the landing page, the sign-in pages, health checks, static assets,
  and the read-only shared-report links (/r/<token>), which have always been
  meant for people without an account. A page request without a session is
  redirected to /login; an API request gets a 401.
* **The cookie.** HttpOnly (page scripts cannot read it), SameSite=Lax (a
  cross-site POST does not carry it), Secure when the request arrived over
  HTTPS, including behind Railway's TLS-terminating proxy.
* **Cross-site requests.** SameSite=Lax is the main CSRF defence. As a second,
  independent one, a state-changing request whose Origin (or Referer) names a
  different host is refused — OWASP's "verify origin with standard headers".
* **Rate limits** on sign-in and sign-up, per client and per email address,
  because a password form with no limit is a free password-guessing service.
* **Membership.** Every brand-scoped endpoint asks "is this brand in this
  account?". The answer is cached for a few seconds per user, since one
  dashboard load makes about eight such requests.

Session lookups are a database read, and the API is async, so they run in the
threadpool rather than on the event loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import accounts, db
from .cache import TTLCache

log = logging.getLogger(__name__)

SESSION_COOKIE = "rb_session"

# Paths that never require an account. Kept short and explicit: anything not
# listed here is behind the wall, so a new route is protected by default.
_PUBLIC_EXACT = frozenset({"/", "/login", "/signup", "/healthz", "/readyz", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static/", "/r/", "/api/shared/", "/api/auth/")
# Public paths that do not even need the session looked up. /healthz in
# particular must stay instant and I/O-free: it is the platform's liveness probe.
_NO_LOOKUP_EXACT = frozenset({"/healthz", "/readyz", "/favicon.ico"})
_NO_LOOKUP_PREFIXES = ("/static/",)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


def _needs_lookup(path: str) -> bool:
    return not (path in _NO_LOOKUP_EXACT or path.startswith(_NO_LOOKUP_PREFIXES))


# --- caches -------------------------------------------------------------------

# token hash -> User. Thirty seconds bounds how long a session outlives its
# deletion in this process; logout invalidates its own entry immediately.
_SESSIONS = TTLCache(ttl=30, maxsize=4096, name="sessions")
# user id -> [(brand_key, canonical name)]
_MEMBERS = TTLCache(ttl=15, maxsize=4096, name="memberships")


class SessionUnavailable(RuntimeError):
    """The session store could not be reached (as opposed to: no session)."""


def resolve_user(request: Request) -> accounts.User | None:
    """The signed-in user for this request, or None.

    Raises SessionUnavailable when the database is configured but failing, so
    the caller can answer 503 instead of bouncing a signed-in person to the
    login page every time Neon blips.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    key = accounts.token_hash(token)
    cached = _SESSIONS.get(key)
    if cached is not None:
        return cached
    try:
        user = accounts.session_user(token)
    except db.DatabaseConfigError:
        return None  # no database means no accounts, so nobody is signed in
    except Exception as exc:  # noqa: BLE001
        if accounts.is_schema_missing(exc):
            return None  # no account tables yet: nobody can be signed in
        log.warning("auth: session lookup failed: %s", type(exc).__name__)
        raise SessionUnavailable() from exc
    if user is not None:
        _SESSIONS.put(key, user)
    return user


def user_of(request: Request) -> accounts.User | None:
    return getattr(request.state, "user", None)


def memberships(user: accounts.User) -> list[tuple[str, str]]:
    cached = _MEMBERS.get(str(user.id))
    if cached is not None:
        return cached
    found = accounts.memberships(user)
    _MEMBERS.put(str(user.id), found)
    return found


def member_names(user: accounts.User) -> list[str]:
    """Canonical names of this account's brands, newest first."""
    return [name for _key, name in memberships(user)]


def member_brand(user: accounts.User, name: str | None) -> str | None:
    """The canonical name if `name` is one of this account's brands, else None."""
    key = accounts.brand_key(name)
    if not key:
        return None
    for member_key, canonical in memberships(user):
        if member_key == key:
            return canonical
    return None


def forget_memberships(user: accounts.User) -> None:
    _MEMBERS.invalidate(str(user.id))


# --- request helpers ------------------------------------------------------------


def client_ip(request: Request) -> str:
    """The client address, for rate limiting.

    Behind Railway's proxy the socket peer is the proxy, so X-Forwarded-For is
    used. Its LAST entry is the one the proxy appended; earlier entries are
    whatever the client claimed, so the first entry would let anyone pick their
    own rate-limit bucket.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _secure_cookie(request: Request) -> bool:
    mode = os.environ.get("SESSION_COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes"):
        return True
    if mode in ("0", "false", "no"):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _wants_html(request: Request) -> bool:
    return (
        request.method == "GET"
        and not request.url.path.startswith("/api/")
        and "text/html" in request.headers.get("accept", "")
    )


def _origin_host(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return urlsplit(value).netloc.lower() or None
    except ValueError:
        return None


def cross_site(request: Request) -> bool:
    """Whether a state-changing request demonstrably came from another site.

    Requests with neither header (curl, server-to-server, the test client) are
    allowed: browsers always send Origin on a cross-origin POST, so the case
    this check exists for always carries one.
    """
    if request.method not in _UNSAFE_METHODS:
        return False
    origin = request.headers.get("origin")
    if origin == "null":  # sandboxed iframes and file:// pages
        return True
    source = _origin_host(origin) or _origin_host(request.headers.get("referer"))
    if source is None:
        return False
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
    return source != host.split(",")[0].strip()


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message, **extra},
                        status_code=status)


# --- middleware ---------------------------------------------------------------


async def session_middleware(request: Request, call_next):
    """Resolve the session and enforce the login wall. Registered by main.py."""
    path = request.url.path
    request.state.user = None

    if cross_site(request):
        return _error(403, "cross_site", "Cross-site requests are not allowed.")

    if _needs_lookup(path):
        try:
            request.state.user = await run_in_threadpool(resolve_user, request)
        except SessionUnavailable:
            if not is_public(path):
                return _error(503, "session_unavailable",
                              "Could not check your sign-in just now. Try again shortly.")

    if request.state.user is None and not is_public(path):
        if _wants_html(request):
            target = path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
        return _error(401, "auth_required", "Sign in to continue.")

    return await call_next(request)


# --- rate limiting --------------------------------------------------------------


class SlidingWindowLimiter:
    """At most `limit` hits per `window` seconds per key, in this process.

    Memory is bounded: past `max_keys` the least recently used keys are dropped.
    A restart forgets everything, which is acceptable for slowing down guessing;
    it is not an audit log.
    """

    def __init__(self, limit: int, window: float, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, seconds until one is allowed)."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            self._hits.move_to_end(key)
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(self.window - (now - hits[0])) + 1)
            hits.append(now)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


LOGIN_PER_IP = SlidingWindowLimiter(limit=20, window=15 * 60)
LOGIN_PER_EMAIL = SlidingWindowLimiter(limit=10, window=15 * 60)
SIGNUP_PER_IP = SlidingWindowLimiter(limit=5, window=60 * 60)


def _limited(retry_after: int) -> JSONResponse:
    resp = _error(429, "rate_limited", "Too many attempts. Wait a few minutes and try again.",
                  retry_after=retry_after)
    resp.headers["Retry-After"] = str(retry_after)
    return resp


# --- routes ---------------------------------------------------------------------

router = APIRouter()

_STATUS = {
    "email_taken": 409,
    "invalid_credentials": 401,
    "signup_closed": 403,
    "invite_required": 403,
    "brand_limit": 409,
}


def account_error(exc: accounts.AccountError) -> JSONResponse:
    return _error(_STATUS.get(exc.code, 400), exc.code, str(exc))


def _setup_required() -> JSONResponse:
    return _error(503, "setup_required",
                  "Accounts need the database. Set DATABASE_URL and run "
                  "python scripts/migrate.py.", setup_required="database")


def _schema_missing() -> JSONResponse:
    return _error(503, "setup_required", accounts.SCHEMA_HELP, setup_required="migration")


class SignInRequest(BaseModel):
    # Generous outer bounds; accounts.py applies the real rules and messages.
    email: str = Field(max_length=accounts.EMAIL_MAX + 16)
    password: str = Field(max_length=accounts.PASSWORD_MAX + 16)


class SignUpRequest(SignInRequest):
    invite_code: str | None = Field(default=None, max_length=200)


def _with_session(user: accounts.User, request: Request) -> JSONResponse:
    token = accounts.start_session(user)
    resp = JSONResponse({"ok": True, "user": {"email": user.email, "is_admin": user.is_admin}})
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=accounts.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=_secure_cookie(request), path="/",
    )
    return resp


@router.get("/api/auth/config")
def auth_config() -> dict:
    """What the sign-in page needs to render: no secrets, no account data."""
    try:
        db.database_url()
        database = True
    except db.DatabaseConfigError:
        database = False
    ready = False
    if database:
        try:
            ready = accounts.schema_ready()
        except Exception:  # noqa: BLE001 - unreachable database: report not ready
            ready = False
    policy = accounts.signup_policy()
    return {
        "signup_enabled": policy["enabled"],
        "invite_required": policy["invite_required"],
        "password_min": accounts.PASSWORD_MIN,
        "database": database,
        "accounts": ready,
    }


@router.post("/api/auth/signup")
def signup(req: SignUpRequest, request: Request):
    allowed, retry = SIGNUP_PER_IP.hit("signup:" + client_ip(request))
    if not allowed:
        return _limited(retry)
    try:
        user = accounts.register(req.email, req.password, req.invite_code)
        response = _with_session(user, request)
    except accounts.AccountError as exc:
        return account_error(exc)
    except db.DatabaseConfigError:
        return _setup_required()
    except Exception as exc:  # noqa: BLE001
        if accounts.is_schema_missing(exc):
            return _schema_missing()
        raise
    return response


@router.post("/api/auth/login")
def login(req: SignInRequest, request: Request):
    ip_ok, ip_retry = LOGIN_PER_IP.hit("login:" + client_ip(request))
    email_key = (req.email or "").strip().lower()
    email_ok, email_retry = LOGIN_PER_EMAIL.hit("login:" + email_key)
    if not (ip_ok and email_ok):
        return _limited(max(ip_retry, email_retry))
    try:
        user = accounts.authenticate(req.email, req.password)
        response = _with_session(user, request)
    except accounts.AccountError as exc:
        return account_error(exc)
    except db.DatabaseConfigError:
        return _setup_required()
    except Exception as exc:  # noqa: BLE001
        if accounts.is_schema_missing(exc):
            return _schema_missing()
        raise
    # A correct password clears this address's counter, so someone who mistyped
    # a few times is not still carrying those strikes.
    LOGIN_PER_EMAIL.reset("login:" + email_key)
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _SESSIONS.invalidate(accounts.token_hash(token))
        try:
            accounts.end_session(token)
        except Exception:  # noqa: BLE001 - the cookie is cleared either way
            log.exception("auth: could not delete session on logout")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/api/auth/me")
def me(request: Request):
    """Who is signed in. "Nobody" is a normal answer for a public page asking,
    so it is a 200 with user null rather than an error status."""
    user = user_of(request)
    if user is None:
        return {"ok": True, "user": None}
    return {"ok": True, "user": {"email": user.email, "is_admin": user.is_admin}}
