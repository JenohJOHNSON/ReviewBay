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
    log.info("worker cycle complete: %s", result)
    return result


def main() -> None:
    interval = int(os.environ.get("WORKER_INTERVAL_SECONDS", os.environ.get("POLL_INTERVAL_SECONDS", "900")))
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
