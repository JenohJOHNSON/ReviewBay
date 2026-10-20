"""Apply ReviewBay's SQL migrations, without needing `psql` on the PATH.

`postgres/ddl.sql` and `postgres/002_scraping_engines.sql` were written to be run
with psql. That is a fine instruction on a server and an awkward one on a laptop
that has the app's dependencies but no Postgres client, so this runner uses the
psycopg connection the app already depends on and the same DATABASE_URL seam
(`db.connect`), which means it inherits Neon's sslmode and keepalive handling.

    python scripts/migrate.py --dry-run     # connect, report, change nothing
    python scripts/migrate.py               # apply whatever is pending
    python scripts/migrate.py --status      # just list what is applied

Two deliberate constraints:

* **It refuses to run destructive SQL.** Every file is scanned for DROP,
  TRUNCATE, DELETE and type-altering ALTERs before anything executes, and a hit
  aborts the run naming the file and the statement. `--allow-destructive`
  overrides it, which exists so the override has to be typed out loud.
* **Each file runs in one transaction**, so a file either applies completely or
  not at all. Postgres does DDL transactionally, so a failed migration leaves
  nothing half-created.

Applied files are recorded in `public.reviewbay_migrations` with a checksum of
the text that ran. `public` rather than `marts`, because the ledger has to exist
before `ddl.sql` has created any schema. If a file changes after being applied
the runner says so and leaves it alone unless `--force` is passed; every
migration currently in this repository is idempotent, so re-running one is safe,
but that is a property of these files rather than a promise about future ones.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MIGRATIONS_DIR = ROOT / "postgres"

# `ddl.sql` is the base schema and has no number, so ordering purely by filename
# would put it after 002. The order is therefore explicit.
BASE_FILE = "ddl.sql"
NUMBERED = re.compile(r"^(\d+)_.*\.sql$")

LEDGER = "public.reviewbay_migrations"
LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER} (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""

# Matched against SQL with comments and string literals stripped, so the word
# "DROP" inside a -- comment does not trip the guard.
DESTRUCTIVE = (
    (
        re.compile(
            r"\bDROP\s+(TABLE|SCHEMA|DATABASE|COLUMN|INDEX|VIEW|TYPE|EXTENSION)\b", re.I
        ),
        "drops an object",
    ),
    (re.compile(r"\bTRUNCATE\b", re.I), "truncates a table"),
    (re.compile(r"\bDELETE\s+FROM\b", re.I), "deletes rows"),
    (re.compile(r"\bALTER\s+COLUMN\b[^;]*\bTYPE\b", re.I | re.S), "changes a column type"),
    (re.compile(r"\bALTER\s+TABLE\b[^;]*\bRENAME\b", re.I | re.S), "renames an object"),
)


class Migration:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.sql = path.read_text(encoding="utf-8")
        self.checksum = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Migration {self.name} {self.checksum}>"


def discover() -> list[Migration]:
    """Return the migrations in the order they must be applied."""
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"no migrations directory at {MIGRATIONS_DIR}")
    found: list[Migration] = []
    base = MIGRATIONS_DIR / BASE_FILE
    if base.is_file():
        found.append(Migration(base))
    numbered = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = NUMBERED.match(path.name)
        if m:
            numbered.append((int(m.group(1)), path))
    for _, path in sorted(numbered):
        found.append(Migration(path))
    unknown = {p.name for p in MIGRATIONS_DIR.glob("*.sql")} - {m.name for m in found}
    for name in sorted(unknown):
        print(f"  ! ignoring {name}: not {BASE_FILE} and not NNN_name.sql")
    return found


def strip_sql(sql: str) -> str:
    """Remove comments and string literals so the guard reads only real SQL."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"\$\$.*?\$\$", " '' ", sql, flags=re.S)
    sql = re.sub(r"'(?:[^']|'')*'", " '' ", sql)
    return sql


def destructive_findings(migration: Migration) -> list[str]:
    body = strip_sql(migration.sql)
    out = []
    for pattern, what in DESTRUCTIVE:
        for match in pattern.finditer(body):
            snippet = " ".join(match.group(0).split())[:90]
            out.append(f"{what}: {snippet}")
    return out


def applied_map(cur) -> dict[str, str]:
    cur.execute(f"SELECT filename, checksum FROM {LEDGER}")
    return {row[0]: row[1] for row in cur.fetchall()}


def safe_target(url: str) -> str:
    """The host part of a connection URL. Never the password."""
    without_query = url.split("?")[0]
    if "@" in without_query:
        return without_query.rsplit("@", 1)[1]
    return re.sub(r"^\w+://", "", without_query) or "(unparsed)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n")[0])
    ap.add_argument(
        "--dry-run", action="store_true", help="connect and report the plan, execute nothing"
    )
    ap.add_argument(
        "--status", action="store_true", help="list applied and pending migrations, then exit"
    )
    ap.add_argument(
        "--force", action="store_true", help="re-apply files already recorded as applied"
    )
    ap.add_argument(
        "--allow-destructive",
        action="store_true",
        help="permit DROP/TRUNCATE/DELETE/type changes (no migration currently "
        "in this repository needs it)",
    )
    ap.add_argument("--only", metavar="FILENAME", help="apply just this one file")
    args = ap.parse_args(argv)

    from reviewbot import db

    migrations = discover()
    if args.only:
        migrations = [m for m in migrations if m.name == args.only]
        if not migrations:
            print(f"no migration named {args.only}")
            return 2

    print(f"migrations directory : {MIGRATIONS_DIR}")
    print(f"files                : {', '.join(m.name for m in migrations) or 'none'}")

    # The guard runs before the connection is opened, so a destructive file is
    # rejected without the database being touched at all.
    blocked = False
    for m in migrations:
        findings = destructive_findings(m)
        if not findings:
            continue
        if args.allow_destructive:
            print(f"  ! {m.name}: destructive, allowed by --allow-destructive")
            for f in findings:
                print(f"      {f}")
        else:
            blocked = True
            print(f"  REFUSED {m.name}: destructive SQL, and --allow-destructive was not passed")
            for f in findings:
                print(f"      {f}")
    if blocked:
        print(
            "\nNothing was executed. Review the statements above, then re-run with "
            "--allow-destructive if they are genuinely intended."
        )
        return 3

    try:
        url = db.database_url()
    except db.DatabaseConfigError as exc:
        print(f"\n{exc}")
        print("Set it first, for example:")
        print('  $env:DATABASE_URL = "postgresql://user:pw@host/db?sslmode=require"   # PowerShell')
        print('  export DATABASE_URL="postgresql://user:pw@host/db?sslmode=require"   # bash')
        return 2

    print(f"target               : {safe_target(url)}")
    print()

    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 - a connection failure is a normal outcome here
        print(f"could not connect: {type(exc).__name__}: {exc}")
        return 2

    applied_count = 0
    with conn:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
        conn.commit()

        with conn.cursor() as cur:
            applied = applied_map(cur)

        plan: list[tuple[Migration, str]] = []
        for m in migrations:
            previous = applied.get(m.name)
            if previous is None:
                plan.append((m, "pending"))
            elif previous != m.checksum:
                plan.append((m, "changed"))
            else:
                plan.append((m, "applied"))

        width = max((len(m.name) for m, _ in plan), default=10)
        for m, state in plan:
            if state == "pending":
                note = "will apply"
            elif state == "applied":
                note = "already applied" + (", re-running" if args.force else "")
            else:
                note = "file changed since it was applied" + (
                    ", re-running" if args.force else ", pass --force to re-run"
                )
            print(f"  {m.name:<{width}}  {m.checksum}  {note}")

        if args.status:
            return 0

        todo = [
            m
            for m, state in plan
            if state == "pending" or (args.force and state in ("applied", "changed"))
        ]
        if not todo:
            print("\nNothing to do.")
            return 0

        if args.dry_run:
            print(f"\n--dry-run: would apply {len(todo)} file(s), executed nothing.")
            return 0

        print()
        for m in todo:
            print(f"applying {m.name} ...", end=" ", flush=True)
            try:
                with conn.cursor() as cur:
                    cur.execute(m.sql)
                    cur.execute(
                        f"INSERT INTO {LEDGER} (filename, checksum) VALUES (%s, %s) "
                        "ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum, "
                        "applied_at = now()",
                        (m.name, m.checksum),
                    )
                conn.commit()
                applied_count += 1
                print("ok")
            except Exception as exc:  # noqa: BLE001 - reported, then we stop
                conn.rollback()
                print("FAILED")
                print(f"  {type(exc).__name__}: {exc}")
                print(
                    "  This file was rolled back; Postgres applies DDL transactionally, "
                    "so nothing from it remains."
                )
                print(
                    "\nStopped at the first failure. Earlier files that reported ok are applied."
                )
                return 1

    print(f"\nApplied {applied_count} file(s).")
    print("Verify with:  python scripts/migrate.py --status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
