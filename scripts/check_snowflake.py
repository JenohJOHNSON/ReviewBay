#!/usr/bin/env python3
"""Verify the app can reach Snowflake + Cortex, using the SAME env vars and
driver the pipeline uses. Run after the SQL setup:

    pip install snowflake-connector-python
    set -a && source .env && set +a
    python scripts/check_snowflake.py

Exits 0 on success, prints what failed otherwise.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        import snowflake.connector  # type: ignore
    except ImportError:
        print("FAIL: pip install snowflake-connector-python")
        return 1

    required = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"FAIL: missing env vars: {', '.join(missing)} (did you source .env?)")
        return 1

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE", "REVIEWBOT_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "REVIEWBOT_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "REVIEWBOT"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "RAW"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
            acct, role, wh = cur.fetchone()
            print(f"OK  connected: account={acct} role={role} warehouse={wh}")

            cur.execute(
                "SELECT SNOWFLAKE.CORTEX.SENTIMENT('great coffee, friendly staff')"
            )
            score = cur.fetchone()[0]
            print(f"OK  Cortex SENTIMENT works: score={score:.3f}")

            cur.execute(
                "SELECT VECTOR_COSINE_SIMILARITY("
                "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5','great coffee'),"
                "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5','excellent espresso'))"
            )
            sim = cur.fetchone()[0]
            print(f"OK  Cortex EMBED_TEXT_768 + vector search works: similarity={sim:.3f}")

            for tbl in ("RAW.REVIEWS_RAW", "MARTS.REVIEWS"):
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                print(f"OK  {tbl}: {cur.fetchone()[0]} rows")

        print("\nAll Snowflake + Cortex checks passed. You're ready to ingest.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        print("Hints: Cortex needs a supported region + the SNOWFLAKE.CORTEX_USER "
              "grant (00_bootstrap.sql step 4); tables need ddl.sql to have run.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
