"""Database connection helpers for Neon Postgres.

The app expects a hosted Postgres database with the pgvector extension enabled.
Use Neon by setting DATABASE_URL or NEON_DATABASE_URL to the connection string
from the Neon console, typically including sslmode=require.
"""

from __future__ import annotations

import os


def database_url() -> str:
    for key in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL"):
        value = os.environ.get(key)
        if value:
            return value
    raise KeyError("DATABASE_URL")


def connect(**kwargs):
    import psycopg  # type: ignore

    return psycopg.connect(database_url(), **kwargs)
