"""Database connection helpers for Neon Postgres.

The app expects a hosted Postgres database with the pgvector extension enabled.
Use Neon by setting DATABASE_URL or NEON_DATABASE_URL to the connection string
from the Neon console, typically including sslmode=require.

Three things live here.

`connect()` is the one place a socket is opened. Everything else goes through it,
so it stays the single seam for tests and for configuration.

`connection()` / `cursor()` reuse idle connections. Neon charges a TLS handshake
(and sometimes a compute wake) per connect, and the previous code opened a fresh
one per loader call, per enrich batch, per QC write and per API request handler.
This is a deliberately small LIFO cache of idle connections rather than a
third-party pool: it needs no new dependency and it keeps `connect()` as the
seam. Set `DB_POOL=0` to disable it and get the old connect-per-call behaviour.

`like_param()` escapes a value for the right-hand side of LIKE/ILIKE. Every brand
filter in this codebase is `brand ILIKE %(brand)s`, which treats `%` and `_` in
the *value* as wildcards: unescaped, `?brand=%` matches every brand in the
database, which defeats the deliberate decision to keep each browser's brand list
private.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)


class DatabaseConfigError(RuntimeError):
    """Raised when ReviewBay has no database URL configured."""


def database_url() -> str:
    for key in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL"):
        value = os.environ.get(key)
        if value:
            return value
    raise DatabaseConfigError(
        "DATABASE_URL is not set. Add your Neon Postgres URL with sslmode=require."
    )


def connect(**kwargs):
    """Open a NEW connection. The only place this module opens a socket.

    Keepalives matter on Neon: an idle connection gets dropped by the proxy,
    which surfaced as "SSL error: unexpected eof" during long enrichment passes.
    """
    import psycopg  # type: ignore

    defaults = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    defaults.update(kwargs)
    return psycopg.connect(database_url(), **defaults)


# --- idle-connection reuse ---------------------------------------------------

def _pool_size() -> int:
    if os.environ.get("DB_POOL", "1") == "0":
        return 0
    try:
        return max(0, int(os.environ.get("DB_POOL_MAX_IDLE", "4")))
    except ValueError:
        return 4


_IDLE: list = []
_IDLE_LOCK = threading.Lock()


def _take_idle():
    """An idle connection that is still usable, or None."""
    while True:
        with _IDLE_LOCK:
            if not _IDLE:
                return None
            conn = _IDLE.pop()
        if getattr(conn, "closed", False):
            continue  # dropped by the server while idle; try the next one
        return conn


def _give_back(conn) -> bool:
    """Return a clean connection to the idle set. True if it was kept."""
    limit = _pool_size()
    if limit <= 0 or getattr(conn, "closed", False):
        return False
    try:
        # Clear any aborted-transaction state so the next borrower starts fresh.
        conn.rollback()
    except Exception:  # noqa: BLE001
        return False
    with _IDLE_LOCK:
        if len(_IDLE) >= limit:
            return False
        _IDLE.append(conn)
        return True


@contextmanager
def connection():
    """A connection, reused when one is idle, always released on exit.

    Commit semantics are the caller's, exactly as with `connect()`: psycopg
    connections are not autocommit, so a writer still calls `conn.commit()`.
    A connection that raised is closed rather than reused.
    """
    conn = _take_idle() if _pool_size() > 0 else None
    if conn is None:
        conn = connect()
    try:
        yield conn
    except BaseException:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        if not _give_back(conn):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def cursor(commit: bool = False):
    """The common case: borrow a connection, hand back a cursor."""
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()


def close_idle() -> None:
    """Close every cached connection (API shutdown, tests)."""
    with _IDLE_LOCK:
        conns, _IDLE[:] = list(_IDLE), []
    for conn in conns:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# --- query helpers -----------------------------------------------------------

def like_param(value: str | None) -> str | None:
    r"""Escape a value for use on the right-hand side of LIKE/ILIKE.

    `%` and `_` are wildcards inside a LIKE pattern even when the pattern is a
    bound parameter, so a brand name has to be escaped to be matched literally.
    Uses backslash, Postgres's default LIKE escape character.

    >>> like_param("100% Pure")
    '100\\% Pure'
    >>> like_param("Nik_")
    'Nik\\_'
    >>> like_param(None) is None
    True
    """
    if value is None:
        return None
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
