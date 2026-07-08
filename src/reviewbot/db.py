"""Shared Neon Postgres connection.

Reads ``DATABASE_URL`` (a standard ``postgresql://`` URL, e.g. from Neon) and
returns a psycopg (v3) connection. Queries use ``%(name)s`` named placeholders.
Embeddings are written and searched as pgvector values via an explicit
``%(...)s::vector`` cast in the SQL (``json.dumps`` of a float list is already
valid pgvector text input), so no extra vector adapter is needed here.
"""

from __future__ import annotations

import os


def connect():
    """Open a psycopg connection to DATABASE_URL. The lazy import keeps this
    module inert until a caller actually needs the database.

    TCP keepalives are enabled so a connection that goes briefly idle (for
    example while embeddings are computed) is less likely to be dropped by Neon's
    proxy with an "SSL error: unexpected eof". The enrich step also uses
    short-lived connections so it never holds one idle across the slow embed."""
    import psycopg  # type: ignore

    return psycopg.connect(
        os.environ["DATABASE_URL"],
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
