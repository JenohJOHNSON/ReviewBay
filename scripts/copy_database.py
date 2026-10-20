"""Copy ReviewBay's data from one Postgres database into another.

Written for moving off Neon onto a Railway Postgres, and meant to run INSIDE
Railway with both connection strings supplied as variable references, so
neither password passes through a laptop, a shell history or a chat:

    SOURCE_DATABASE_URL = ${{ReviewBay.DATABASE_URL}}          # the database you are leaving
    DATABASE_URL        = ${{pgvector.DATABASE_URL_PRIVATE}}    # the one you are moving to

    python scripts/migrate.py && python scripts/copy_database.py

DEPLOY.md shows the temporary Railway service that runs it. It works from a
laptop too, with both variables set.

What it does and does not do:

* Reads the source; never writes to it.
* Never deletes or overwrites a row on the target. Rows go in with
  ON CONFLICT DO NOTHING, so a second run copies only what is missing, which is
  how rows written to the old database during the switch-over get picked up.
* A table with no primary key or unique index cannot be deduplicated that way,
  so it is copied only when the target's copy is still empty.
* Copies the columns both sides have, so a source that never ran migration 002
  still copies; target-only columns take their defaults.
* Moves sequences past the copied ids, so new saved reports and run records do
  not collide with copied ones.
* Refuses to run when source and target are the same database.
* Prints hosts and row counts, never a connection string.

    python scripts/copy_database.py --dry-run    # count only, change nothing
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.parse import urlsplit

# Copy order matters only for the account tables (user_brands references both
# users and brands). Every other table stands alone.
TABLES = (
    "airbyte.app_brand_map",
    "raw.reviews_raw",
    "marts.reviews",
    "marts.scrape_runs",
    "marts.saved_reports",
    "marts.extraction_cache",
    "marts.engine_stats",
    "app.users",
    "app.brands",
    "app.user_brands",
)
# Sessions are deliberately not copied: signing in again after a move is the
# safe default, and a session table full of stale tokens is no loss.
SCHEMAS = ("raw", "marts", "airbyte", "app")


def where(url: str) -> str:
    """host[:port]/database, never the credentials."""
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}{parts.path or ''}"


def same_database(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return ((pa.hostname or "").lower(), pa.port or 5432, pa.path) == (
        (pb.hostname or "").lower(), pb.port or 5432, pb.path)


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return bool(cur.fetchone()[0])


def columns(cur, table: str) -> list[str]:
    schema, name = table.split(".", 1)
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, name),
    )
    return [r[0] for r in cur.fetchall()]


def has_unique_key(cur, table: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_index WHERE indrelid = %s::regclass AND indisunique)",
        (table,),
    )
    return bool(cur.fetchone()[0])


def count(cur, table: str) -> int:
    from psycopg import sql

    schema, name = table.split(".", 1)
    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(schema, name)))
    return int(cur.fetchone()[0])


def serial_columns(cur, table: str) -> list[str]:
    """Columns fed by an owned sequence (bigserial ids)."""
    out = []
    for col in columns(cur, table):
        cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, col))
        if cur.fetchone()[0]:
            out.append(col)
    return out


def advance_sequences(cur, table: str) -> None:
    from psycopg import sql

    schema, name = table.split(".", 1)
    for col in serial_columns(cur, table):
        # setval(seq, max, true) makes the next id max + 1; on an empty table,
        # setval(seq, 1, false) makes it 1.
        cur.execute(
            sql.SQL(
                "SELECT setval(pg_get_serial_sequence(%s, %s), "
                "COALESCE((SELECT MAX({col}) FROM {tbl}), 1), "
                "(SELECT MAX({col}) FROM {tbl}) IS NOT NULL)"
            ).format(col=sql.Identifier(col), tbl=sql.Identifier(schema, name)),
            (table, col),
        )


def copy_table(src, dst, table: str, dry_run: bool) -> dict:
    from psycopg import sql

    schema, name = table.split(".", 1)
    ident = sql.Identifier(schema, name)
    with src.cursor() as scur, dst.cursor() as dcur:
        if not table_exists(scur, table):
            return {"table": table, "status": "not in source"}
        if not table_exists(dcur, table):
            return {"table": table, "status": "MISSING ON TARGET (run scripts/migrate.py first)"}
        shared = [c for c in columns(dcur, table) if c in set(columns(scur, table))]
        source_rows = count(scur, table)
        before = count(dcur, table)
        result = {"table": table, "source": source_rows, "target_before": before}
        if dry_run or not source_rows:
            result["status"] = "dry run" if dry_run else "empty in source"
            return result
        if not has_unique_key(dcur, table) and before:
            # Nothing to deduplicate on: copying again would duplicate rows.
            result["status"] = "skipped: target already has rows and the table has no unique key"
            return result

        cols = sql.SQL(", ").join(sql.Identifier(c) for c in shared)
        started = time.monotonic()
        dcur.execute(
            sql.SQL("CREATE TEMP TABLE _incoming (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP")
            .format(ident)
        )
        # Stream source rows straight into the target: memory stays flat no
        # matter how many reviews (and 768-float embeddings) there are.
        with scur.copy(sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT").format(cols, ident)) as out:
            with dcur.copy(sql.SQL("COPY _incoming ({}) FROM STDIN").format(cols)) as inp:
                for block in out:
                    inp.write(block)
        dcur.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM _incoming ON CONFLICT DO NOTHING")
            .format(ident, cols, cols)
        )
        inserted = dcur.rowcount or 0
        advance_sequences(dcur, table)
        dst.commit()
        result.update(
            status="copied",
            inserted=inserted,
            already_present=source_rows - inserted,
            seconds=round(time.monotonic() - started, 1),
            columns_not_in_source=[c for c in columns(dcur, table) if c not in shared],
        )
        return result


def uncovered_tables(src) -> list[str]:
    """Source tables in ReviewBay's schemas that this script does not copy."""
    with src.cursor() as cur:
        cur.execute(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s) ORDER BY 1",
            (list(SCHEMAS),),
        )
        found = [r[0] for r in cur.fetchall()]
    return [t for t in found if t not in TABLES and t != "app.sessions"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Copy ReviewBay's data between databases.")
    ap.add_argument("--dry-run", action="store_true", help="count rows, change nothing")
    args = ap.parse_args(argv)

    source = os.environ.get("SOURCE_DATABASE_URL", "").strip()
    target = os.environ.get("DATABASE_URL", "").strip()
    if not source or not target:
        print("Set SOURCE_DATABASE_URL (copy from) and DATABASE_URL (copy to).")
        return 2
    print(f"source : {where(source)}")
    print(f"target : {where(target)}")
    if same_database(source, target):
        print("Source and target are the same database; nothing to copy.")
        return 2

    import psycopg  # type: ignore

    try:
        src = psycopg.connect(source, connect_timeout=20)
        dst = psycopg.connect(target, connect_timeout=20)
    except Exception as exc:  # noqa: BLE001 - reported without the URL
        print(f"could not connect: {type(exc).__name__}")
        return 2

    failed = False
    with src, dst:
        src.read_only = True  # belt and braces: this script never writes there
        print()
        for table in TABLES:
            try:
                r = copy_table(src, dst, table, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - report, roll back, carry on
                dst.rollback()
                print(f"  {table:<26} FAILED: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
                failed = True
                continue
            if r.get("status") == "copied":
                extra = ""
                if r["columns_not_in_source"]:
                    extra = f" (defaults for: {', '.join(r['columns_not_in_source'])})"
                print(f"  {table:<26} {r['source']:>7} in source, {r['inserted']:>7} copied, "
                      f"{r['already_present']:>7} already there, {r['seconds']}s{extra}")
            elif "source" in r:
                print(f"  {table:<26} {r['source']:>7} in source, target has "
                      f"{r['target_before']:>7}: {r['status']}")
            else:
                print(f"  {table:<26} {r['status']}")

        extra_tables = uncovered_tables(src)
        if extra_tables:
            print("\nNot copied (not part of ReviewBay's schema; copy by hand if you need them):")
            for t in extra_tables:
                print(f"  {t}")

    print("\nDry run: nothing was changed." if args.dry_run else
          ("\nFinished with errors; rows already copied are kept. Re-running is safe."
           if failed else "\nDone. Re-running is safe: it only copies rows that are missing."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
