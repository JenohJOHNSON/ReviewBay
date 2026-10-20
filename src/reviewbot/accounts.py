"""Accounts, sessions, and the durable brand registry.

Until now ReviewBay had no accounts: the brand list lived in each browser's
localStorage, so a brand you searched for existed only on that one browser, and
the server kept no record of who was watching what. This module adds:

* **accounts** — email and password, hashed with scrypt from the standard
  library (no new dependency, and memory-hard, which is what makes a stolen hash
  expensive to crack);
* **sessions** — a random token in an HttpOnly cookie, stored server-side only as
  its SHA-256, so a leaked sessions table cannot be replayed as logins;
* **a brand registry** — one row per brand anyone tracks, plus which accounts
  track it. This is now what the worker refreshes, replacing a YAML file on the
  container's local disk that a Railway redeploy wiped.

Persistence goes through a small `Store` seam. Production uses `PostgresStore`;
the default test suite uses an in-memory store, and one contract test suite runs
against BOTH, so the in-memory fake cannot quietly drift from the real SQL. All
the rules — validation, hashing, session lifetime, the brand limit, what happens
when the last account drops a brand — live here, once, above the seam.

Nothing in this module knows about HTTP. Cookies, rate limits and redirects are
`reviewbot.api.auth`'s job.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# --- configuration ------------------------------------------------------------

#: How long a session lasts without being used.
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
#: A used session's expiry slides forward, but at most this often, so an active
#: user is not a database write on every request.
SESSION_REFRESH = timedelta(hours=24)

PASSWORD_MIN = 8
#: An upper bound keeps a megabyte "password" from being a cheap way to make the
#: server spend memory. Far above anything a password manager produces.
PASSWORD_MAX = 256
EMAIL_MAX = 254  # RFC 5321's practical limit

#: Brands one account may track. Every tracked brand is scraped on a schedule,
#: and scraping costs money, so with open signup this is a cost control as much
#: as a product limit. 0 means unlimited.
MAX_BRANDS_PER_ACCOUNT = int(os.environ.get("MAX_BRANDS_PER_ACCOUNT", "25"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- errors -------------------------------------------------------------------


class AccountError(ValueError):
    """A failure whose message is safe to show the person who caused it."""

    code = "invalid_request"


class EmailTaken(AccountError):
    code = "email_taken"


class InvalidCredentials(AccountError):
    code = "invalid_credentials"


class SignupClosed(AccountError):
    code = "signup_closed"


class InviteRequired(AccountError):
    code = "invite_required"


class BrandLimitReached(AccountError):
    code = "brand_limit"


@dataclass(frozen=True)
class User:
    id: int
    email: str

    @property
    def is_admin(self) -> bool:
        return self.email.strip().lower() in admin_emails()


def admin_emails() -> set[str]:
    """Accounts allowed operator actions, from ADMIN_EMAILS (comma separated).

    Read on every call rather than at import, so changing the variable and
    restarting is the whole procedure, and tests can set it per case.
    """
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# --- validation ---------------------------------------------------------------

# Deliberately loose. The only reliable test of an email address is sending to
# it, and a strict pattern mostly rejects real addresses (plus signs, new TLDs).
# The one thing it does exclude is markup: < > " ' ` are legal only in quoted
# local parts nobody uses, and an address is shown back in the page header, so
# refusing them here means a stored "email" can never be HTML.
_EMAIL = re.compile(r"^[^@\s<>\"'`]+@[^@\s<>\"'`]+\.[^@\s<>\"'`]+$")


def normalize_email(email: str | None) -> tuple[str, str]:
    """(as typed, trimmed; the lowercased key uniqueness is enforced on)."""
    shown = (email or "").strip()
    if not shown:
        raise AccountError("Enter your email address.")
    if len(shown) > EMAIL_MAX or not _EMAIL.match(shown):
        raise AccountError("That does not look like an email address.")
    return shown, shown.lower()


def validate_password(password: str | None) -> str:
    password = password or ""
    if len(password) < PASSWORD_MIN:
        raise AccountError(f"Use at least {PASSWORD_MIN} characters for your password.")
    if len(password) > PASSWORD_MAX:
        raise AccountError(f"Use at most {PASSWORD_MAX} characters for your password.")
    # No composition rules (a digit, a symbol, ...). NIST SP 800-63B recommends
    # against them: they push people toward predictable patterns and add nothing
    # a longer minimum does not.
    return password


# --- password hashing ---------------------------------------------------------

# scrypt with N=2^14, r=8 costs 16 MiB and tens of milliseconds per guess, which
# is the point: cheap for one login, expensive for a billion guesses. The
# parameters are written into each hash so they can be raised later; a hash made
# with old parameters is upgraded on the owner's next successful login.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=_SCRYPT_LEN, maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def _parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            return None
        return int(n), int(r), int(p), _unb64(salt), _unb64(digest)
    except (ValueError, TypeError):
        return None


def verify_password(password: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = _scrypt(password or "", salt, n, r, p)
    except (ValueError, MemoryError):
        return False
    # Constant time, so the comparison does not leak how many bytes matched.
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    parsed = _parse_hash(stored)
    return parsed is None or parsed[:3] != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


_DUMMY_HASH: str | None = None
_DUMMY_LOCK = threading.Lock()


def _dummy_hash() -> str:
    """A real hash to verify against when the email is unknown.

    Without it, "no such account" returns in microseconds while a wrong password
    takes the full scrypt time, and that difference alone tells an attacker
    which addresses have accounts.
    """
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        return _DUMMY_HASH


def token_hash(token: str) -> str:
    """What is stored for a session token. SHA-256 is right here, unlike for
    passwords: the token is 256 random bits, so there is nothing to guess."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# --- signup policy ------------------------------------------------------------


def signup_policy() -> dict:
    """Whether new accounts can be created, and whether that needs a code.

    SIGNUP_ENABLED=0 closes registration. SIGNUP_INVITE_CODE, when set, must be
    supplied to register; that is the simple way to keep a public deployment —
    whose every tracked brand costs scraping money — to the people you invite.
    """
    return {
        "enabled": os.environ.get("SIGNUP_ENABLED", "1") != "0",
        "invite_required": bool(os.environ.get("SIGNUP_INVITE_CODE", "").strip()),
    }


def _check_invite(code: str | None) -> None:
    expected = os.environ.get("SIGNUP_INVITE_CODE", "").strip()
    if not expected:
        return
    if not code or not hmac.compare_digest(code.strip().encode(), expected.encode()):
        raise InviteRequired("A valid invite code is required to create an account.")


# --- brand naming -------------------------------------------------------------


def brand_key(name: str | None) -> str:
    """The registry key for a brand: trimmed and lowercased.

    It matches how every dashboard query compares brands (`brand ILIKE x` with
    wildcards escaped is a case-insensitive equality), so two names with the
    same key are the same brand everywhere in the product.
    """
    return (name or "").strip().lower()


# --- the store seam -----------------------------------------------------------


class Store:
    """Persistence for accounts. Every method is one small, atomic operation;
    the rules that combine them live in the module functions below."""

    # users
    def create_user(self, email: str, email_key: str, password_hash: str) -> User: ...
    def user_by_email(self, email_key: str) -> tuple[User, str] | None: ...
    def set_password_hash(self, user_id: int, password_hash: str) -> None: ...
    def touch_login(self, user_id: int) -> None: ...

    # sessions
    def create_session(self, token_hash: str, user_id: int, expires_at: datetime) -> None: ...
    def session(self, token_hash: str, now: datetime) -> tuple[User, datetime, datetime] | None: ...
    def extend_session(self, token_hash: str, expires_at: datetime, now: datetime) -> None: ...
    def delete_session(self, token_hash: str) -> None: ...
    def delete_expired_sessions(self, now: datetime) -> int: ...

    # brand registry
    def upsert_brand(self, cfg: dict) -> dict: ...
    def get_brand(self, key: str) -> dict | None: ...
    def set_brand_active(self, key: str, active: bool) -> None: ...
    def stamp_brand(self, key: str, column: str) -> None: ...
    def registry(self, include_inactive: bool = False) -> list[dict]: ...

    def ready(self) -> bool:
        """Whether the account tables exist. In-memory stores always are."""
        return True

    # membership
    def add_membership(self, user_id: int, key: str) -> bool: ...
    def remove_membership(self, user_id: int, key: str) -> bool: ...
    def membership_keys(self, user_id: int) -> list[str]: ...
    def memberships(self, user_id: int) -> list[tuple[str, str]]: ...
    def account_brands(self, user_id: int) -> list[dict]: ...
    def tracker_count(self, key: str) -> int: ...


class _UniqueViolation(Exception):
    pass


_BRAND_COLUMNS = (
    "brand_key, name, website, keywords, sources, fetch_limit, active, "
    "created_at, last_collected_at, last_rerun_at"
)
_STAMPABLE = ("last_collected_at", "last_rerun_at")


def _brand_row(row) -> dict:
    key, name, website, keywords, sources, limit, active, created, collected, rerun = row
    return {
        "brand_key": key,
        "name": name,
        "website": website,
        "keywords": list(keywords or []),
        "sources": list(sources or []),
        "limit": limit,
        "active": bool(active),
        "created_at": created,
        "last_collected_at": collected,
        "last_rerun_at": rerun,
    }


class PostgresStore(Store):
    """The production store. Requires migration 003."""

    _ready = False  # a positive answer is cached; tables are never dropped

    @staticmethod
    def _db():
        from . import db

        return db

    def ready(self):
        if self._ready:
            return True
        with self._db().cursor() as cur:
            cur.execute("SELECT to_regclass('app.users') IS NOT NULL")
            ok = bool(cur.fetchone()[0])
        if ok:
            PostgresStore._ready = True
        return ok

    def create_user(self, email, email_key, password_hash):
        from psycopg import errors  # type: ignore

        db = self._db()
        try:
            with db.cursor(commit=True) as cur:
                cur.execute(
                    "INSERT INTO app.users (email, email_key, password_hash) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (email, email_key, password_hash),
                )
                (uid,) = cur.fetchone()
        except errors.UniqueViolation as exc:
            # Two signups racing for one address: the unique index decides, and
            # the loser gets the same answer as if it had checked first.
            raise _UniqueViolation() from exc
        return User(id=int(uid), email=email)

    def user_by_email(self, email_key):
        with self._db().cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash FROM app.users WHERE email_key = %s",
                (email_key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return User(id=int(row[0]), email=row[1]), row[2]

    def set_password_hash(self, user_id, password_hash):
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                "UPDATE app.users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
            )

    def touch_login(self, user_id):
        with self._db().cursor(commit=True) as cur:
            cur.execute("UPDATE app.users SET last_login_at = now() WHERE id = %s", (user_id,))

    def create_session(self, token_hash, user_id, expires_at):
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO app.sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                (token_hash, user_id, expires_at),
            )

    def session(self, token_hash, now):
        with self._db().cursor() as cur:
            cur.execute(
                "SELECT u.id, u.email, s.expires_at, s.last_seen_at "
                "FROM app.sessions s JOIN app.users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > %s",
                (token_hash, now),
            )
            row = cur.fetchone()
        if not row:
            return None
        return User(id=int(row[0]), email=row[1]), row[2], row[3]

    def extend_session(self, token_hash, expires_at, now):
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                "UPDATE app.sessions SET expires_at = %s, last_seen_at = %s WHERE token_hash = %s",
                (expires_at, now, token_hash),
            )

    def delete_session(self, token_hash):
        with self._db().cursor(commit=True) as cur:
            cur.execute("DELETE FROM app.sessions WHERE token_hash = %s", (token_hash,))

    def delete_expired_sessions(self, now):
        with self._db().cursor(commit=True) as cur:
            cur.execute("DELETE FROM app.sessions WHERE expires_at <= %s", (now,))
            return cur.rowcount or 0

    def upsert_brand(self, cfg):
        # ON CONFLICT keeps the existing spelling, keywords and sources: the
        # first account to add a brand sets its canonical form, so a second
        # account typing "NIKE" joins "Nike" rather than forking its data. The
        # website is only filled in if it was missing. Re-adding a brand nobody
        # tracked any more reactivates it.
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                f"""
                INSERT INTO app.brands (brand_key, name, website, keywords, sources, fetch_limit)
                VALUES (%(key)s, %(name)s, %(website)s, %(keywords)s::jsonb,
                        %(sources)s::jsonb, %(limit)s)
                ON CONFLICT (brand_key) DO UPDATE
                   SET active  = TRUE,
                       website = COALESCE(app.brands.website, EXCLUDED.website)
                RETURNING {_BRAND_COLUMNS}
                """,
                {
                    "key": brand_key(cfg["name"]),
                    "name": cfg["name"].strip(),
                    "website": cfg.get("website"),
                    "keywords": json.dumps(cfg.get("keywords") or [cfg["name"].strip()]),
                    "sources": json.dumps(cfg.get("sources") or []),
                    "limit": cfg.get("limit"),
                },
            )
            return _brand_row(cur.fetchone())

    def get_brand(self, key):
        with self._db().cursor() as cur:
            cur.execute(f"SELECT {_BRAND_COLUMNS} FROM app.brands WHERE brand_key = %s", (key,))
            row = cur.fetchone()
        return _brand_row(row) if row else None

    def set_brand_active(self, key, active):
        with self._db().cursor(commit=True) as cur:
            cur.execute("UPDATE app.brands SET active = %s WHERE brand_key = %s", (active, key))

    def stamp_brand(self, key, column):
        if column not in _STAMPABLE:  # a column name cannot be a bound parameter
            raise ValueError(f"cannot stamp {column}")
        with self._db().cursor(commit=True) as cur:
            cur.execute(f"UPDATE app.brands SET {column} = now() WHERE brand_key = %s", (key,))

    def registry(self, include_inactive=False):
        where = "" if include_inactive else "WHERE active"
        with self._db().cursor() as cur:
            cur.execute(f"SELECT {_BRAND_COLUMNS} FROM app.brands {where} ORDER BY created_at")
            return [_brand_row(r) for r in cur.fetchall()]

    def add_membership(self, user_id, key):
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO app.user_brands (user_id, brand_key) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (user_id, key),
            )
            return (cur.rowcount or 0) > 0

    def remove_membership(self, user_id, key):
        with self._db().cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM app.user_brands WHERE user_id = %s AND brand_key = %s", (user_id, key)
            )
            return (cur.rowcount or 0) > 0

    def membership_keys(self, user_id):
        with self._db().cursor() as cur:
            cur.execute(
                "SELECT brand_key FROM app.user_brands WHERE user_id = %s ORDER BY added_at DESC",
                (user_id,),
            )
            return [r[0] for r in cur.fetchall()]

    def memberships(self, user_id):
        # (brand_key, canonical name), newest first. The light query the API's
        # per-request membership check uses; account_brands() is the heavy one.
        with self._db().cursor() as cur:
            cur.execute(
                "SELECT b.brand_key, b.name FROM app.user_brands ub "
                "JOIN app.brands b ON b.brand_key = ub.brand_key "
                "WHERE ub.user_id = %s ORDER BY ub.added_at DESC",
                (user_id,),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def account_brands(self, user_id):
        # The review count uses the same predicate as the dashboard
        # (`relevant IS NOT FALSE`), so the number here matches the one the
        # dashboard shows for the same brand.
        with self._db().cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join("b." + c.strip() for c in _BRAND_COLUMNS.split(","))},
                       ub.added_at,
                       (SELECT COUNT(*) FROM marts.reviews r
                         WHERE lower(r.brand) = b.brand_key AND r.relevant IS NOT FALSE)
                FROM app.user_brands ub
                JOIN app.brands b ON b.brand_key = ub.brand_key
                WHERE ub.user_id = %s
                ORDER BY ub.added_at DESC
                """,
                (user_id,),
            )
            out = []
            for row in cur.fetchall():
                brand = _brand_row(row[:10])
                brand["added_at"] = row[10]
                brand["reviews"] = int(row[11] or 0)
                out.append(brand)
            return out

    def tracker_count(self, key):
        with self._db().cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app.user_brands WHERE brand_key = %s", (key,))
            return int(cur.fetchone()[0] or 0)


class SchemaMissing(RuntimeError):
    """The account tables do not exist yet: migration 003 has not been run."""


def is_schema_missing(exc: BaseException) -> bool:
    """Whether a database error means "the app tables are not there".

    Deploying this code before running the migration is the predictable way to
    get here, and it deserves an instruction rather than a bare 500. SQLSTATE
    42P01 is undefined_table, 3F000 invalid_schema_name.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if getattr(seen, "sqlstate", None) in ("42P01", "3F000"):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


SCHEMA_HELP = (
    "Accounts are not set up on this server yet: run python scripts/migrate.py "
    "to create the account tables (migration 003)."
)


_STORE: Store | None = None
_STORE_LOCK = threading.Lock()


def store() -> Store:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = PostgresStore()
        return _STORE


def set_store(new: Store | None) -> Store | None:
    """Swap the store (tests). Returns the previous one so it can be restored."""
    global _STORE
    with _STORE_LOCK:
        previous, _STORE = _STORE, new
        return previous


# --- accounts -----------------------------------------------------------------


def schema_ready() -> bool:
    return store().ready()


def register(email: str, password: str, invite_code: str | None = None) -> User:
    policy = signup_policy()
    if not policy["enabled"]:
        raise SignupClosed("New accounts are not being created on this server.")
    _check_invite(invite_code)
    shown, key = normalize_email(email)
    validate_password(password)
    try:
        user = store().create_user(shown, key, hash_password(password))
    except _UniqueViolation:
        raise EmailTaken("An account with this email already exists. Sign in instead.") from None
    log.info("accounts: created user id=%s", user.id)
    return user


def authenticate(email: str, password: str) -> User:
    """The account for these credentials, or InvalidCredentials.

    One message for every failure. "No such account" and "wrong password" are
    deliberately indistinguishable, in the response and in the timing.
    """
    failure = InvalidCredentials("Email or password is incorrect.")
    try:
        _shown, key = normalize_email(email)
    except AccountError:
        verify_password(password or "", _dummy_hash())
        raise failure from None
    found = store().user_by_email(key)
    if found is None:
        verify_password(password or "", _dummy_hash())
        raise failure
    user, stored = found
    if not verify_password(password or "", stored):
        raise failure
    if needs_rehash(stored):
        # The password is known to be right at this moment, which is the only
        # time an old hash can be upgraded.
        store().set_password_hash(user.id, hash_password(password))
    store().touch_login(user.id)
    return user


def start_session(user: User) -> str:
    """Create a session and return the raw token for the cookie."""
    token = secrets.token_urlsafe(32)
    now = _now()
    store().create_session(token_hash(token), user.id, now + timedelta(days=SESSION_DAYS))
    # Housekeeping piggybacks on logins rather than needing a scheduled job.
    try:
        store().delete_expired_sessions(now)
    except Exception:  # noqa: BLE001 - never let cleanup fail a login
        log.exception("accounts: expired-session cleanup failed")
    return token


def session_user(token: str | None) -> User | None:
    if not token or len(token) > 512:
        return None
    hashed = token_hash(token)
    now = _now()
    found = store().session(hashed, now)
    if found is None:
        return None
    user, _expires, last_seen = found
    if last_seen is None or now - last_seen >= SESSION_REFRESH:
        store().extend_session(hashed, now + timedelta(days=SESSION_DAYS), now)
    return user


def end_session(token: str | None) -> None:
    if token:
        store().delete_session(token_hash(token))


# --- brands -------------------------------------------------------------------


def add_brand(user: User, cfg: dict) -> tuple[dict, bool]:
    """Track a brand from this account. Returns (canonical config, newly added).

    The returned config is the REGISTRY's: if someone already tracks the brand
    under another spelling, that spelling is what the caller must collect under.
    """
    key = brand_key(cfg.get("name"))
    if not key:
        raise AccountError("Enter a brand name.")
    keys = store().membership_keys(user.id)
    if key not in keys and MAX_BRANDS_PER_ACCOUNT > 0 and len(keys) >= MAX_BRANDS_PER_ACCOUNT:
        raise BrandLimitReached(
            f"Your account can track up to {MAX_BRANDS_PER_ACCOUNT} brands. "
            "Remove one to add another."
        )
    canonical = store().upsert_brand(cfg)
    added = store().add_membership(user.id, key)
    return canonical, added


def remove_brand(user: User, name: str) -> dict:
    """Stop tracking a brand from this account.

    When no account tracks it any more it is marked inactive, which stops the
    worker refreshing it. Its reviews are deliberately kept: deleting shared data
    because one account lost interest would be wrong if anyone re-adds it, and
    keeping it makes re-adding instant.
    """
    key = brand_key(name)
    removed = store().remove_membership(user.id, key)
    orphaned = False
    if removed and store().tracker_count(key) == 0:
        store().set_brand_active(key, False)
        orphaned = True
    return {"removed": removed, "orphaned": orphaned, "brand_key": key}


def brand_keys(user: User) -> list[str]:
    return store().membership_keys(user.id)


def memberships(user: User) -> list[tuple[str, str]]:
    """(brand_key, canonical name) for every brand this account tracks."""
    return store().memberships(user.id)


def account_brands(user: User) -> list[dict]:
    return store().account_brands(user.id)


def get_brand(name: str) -> dict | None:
    return store().get_brand(brand_key(name))


def registry_brands(include_inactive: bool = False) -> list[dict]:
    return store().registry(include_inactive=include_inactive)


def mark_collected(name: str) -> None:
    store().stamp_brand(brand_key(name), "last_collected_at")


def mark_rerun(name: str) -> None:
    store().stamp_brand(brand_key(name), "last_rerun_at")


def as_brand_config(entry: dict) -> dict:
    """A registry row in the shape `ingestion.run` expects from brands.yml."""
    cfg = {
        "name": entry["name"],
        "keywords": entry.get("keywords") or [entry["name"]],
        "sources": entry.get("sources") or [],
    }
    if entry.get("limit"):
        cfg["limit"] = entry["limit"]
    if entry.get("website"):
        cfg["website"] = entry["website"]
    return cfg
