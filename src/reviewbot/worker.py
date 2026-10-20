"""Cloud worker for Airbyte imports, free local sources, and enrichment."""

from __future__ import annotations

import logging
import os
import time

from . import db
from .airbyte.normalize import normalize_from_config
from .enrich.run import enrich
from .ingestion.run import _load_config, run_source

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("reviewbot.worker")


def _local_sources() -> list[str]:
    raw = os.environ.get("LOCAL_SOURCES", "app_store,google_play")
    return [s.strip() for s in raw.split(",") if s.strip()]


def run_cycle() -> dict[str, int]:
    airbyte = normalize_from_config()
    config = _load_config()
    local = 0
    for source in _local_sources():
        local += run_source(source, config=config)
    enriched = enrich()
    result = {"airbyte": airbyte, "local": local, "enriched": enriched}

    # The QC pass sets `relevant`, and every dashboard, trend, compare, export and
    # RAG query filters on `relevant IS NOT FALSE`. It was only ever wired into
    # `ingestion.run.main()`, which nothing deploys, so on a real deployment the
    # global pass never ran: rows stayed qc_checked = NULL and keyword collisions
    # were never filtered out. Enrichment has to come first, because QC reads the
    # enriched marts rows.
    #
    # It is safe to run unconditionally: it caps its own work (QC_MAX_ROWS), skips
    # cleanly with no API key, and a failed batch leaves rows un-checked rather
    # than mislabelled. Set WORKER_QC=0 to turn it off.
    if os.environ.get("WORKER_QC", "1") != "0":
        try:
            from . import qc as _qc

            verdicts = _qc.qc()
            result["qc_checked"] = verdicts.get("checked", 0)
            result["qc_irrelevant"] = verdicts.get("irrelevant", 0)
        except db.DatabaseConfigError:
            raise
        except Exception:  # noqa: BLE001 - QC is best-effort, collection is not
            log.exception("worker cycle: QC pass failed; rows stay un-checked")

    log.info("worker cycle complete: %s", result)
    return result


def main() -> None:
    interval = int(os.environ.get("WORKER_INTERVAL_SECONDS", "900"))
    run_once = os.environ.get("RUN_ONCE", os.environ.get("WORKER_RUN_ONCE", "0")) == "1"
    while True:
        try:
            run_cycle()
        except db.DatabaseConfigError as exc:
            log.warning("%s Worker is idle until DATABASE_URL is configured.", exc)
        except Exception:  # noqa: BLE001
            log.exception("worker cycle failed; will retry next interval")
        if run_once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
