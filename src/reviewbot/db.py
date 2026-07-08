"""Database connection helpers for Neon Postgres.

The app expects a hosted Postgres database with the pgvector extension enabled.
Use Neon by setting DATABASE_URL or NEON_DATABASE_URL to the connection string
from the Neon console, typically including sslmode=require.
"""

from __future__ import annotations

import os


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
    import psycopg  # type: ignore

    defaults = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    defaults.update(kwargs)
    return psycopg.connect(database_url(), **defaults)
