#!/usr/bin/env python3
"""Verify the app can reach Neon Postgres + pgvector.

Run after postgres/schema.sql:

    pip install "psycopg[binary]"
    set -a && source .env && set +a
    python3 scripts/check_neon.py
"""

from __future__ import annotations

import os
import sys


def _database_url() -> str | None:
    for key in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL"):
        if os.environ.get(key):
            return os.environ[key]
    return None


def main() -> int:
    try:
        import psycopg  # type: ignore
    except ImportError:
        print('FAIL: pip install "psycopg[binary]"')
        return 1

    url = _database_url()
    if not url:
        print("FAIL: missing DATABASE_URL or NEON_DATABASE_URL")
        return 1

    try:
        conn = psycopg.connect(url)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not connect: {type(e).__name__}: {e}")
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            database, user = cur.fetchone()
            print(f"OK  connected: database={database} user={user}")

            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            if not row:
                print("FAIL: pgvector is not enabled. Run postgres/schema.sql.")
                return 1
            print(f"OK  pgvector enabled: version={row[0]}")

            cur.execute("SELECT COUNT(*) FROM raw.reviews_raw")
            print(f"OK  raw.reviews_raw: {cur.fetchone()[0]} rows")

            cur.execute("SELECT COUNT(*) FROM marts.reviews")
            print(f"OK  marts.reviews: {cur.fetchone()[0]} rows")

            cur.execute("SELECT 1 - ('[1,0,0]'::vector <=> '[1,0,0]'::vector)")
            print(f"OK  vector cosine search works: similarity={float(cur.fetchone()[0]):.3f}")

        print("\nAll Neon Postgres checks passed. You're ready to ingest.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        print("Hints: run postgres/schema.sql, confirm DATABASE_URL includes sslmode=require, "
              "and make sure your Neon project is awake.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
