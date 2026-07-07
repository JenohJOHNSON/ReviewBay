"""Ingestion entrypoint: read brand config, run every enabled connector, load.

Runs each source on its own cadence in a single loop. With RUN_ONCE=1 it does a
single pass and exits (handy for cron / Airflow tasks / local testing);
otherwise it polls forever, which is the "near-real-time" mode. When freshness
needs to beat the poll interval, this is the component that grows an event bus
in front of the loader — the connector interface above stays unchanged.
"""

from __future__ import annotations

import logging
import os
import time

import yaml

from ..connectors import (
    FACEBOOK,
    GOOGLE_MAPS,
    INSTAGRAM,
    REDDIT,
    TRIPADVISOR,
    YELP,
    ApifyConnector,
    AppStoreConnector,
    GooglePlayConnector,
    GoogleSearchConnector,
)
from . import postgres_loader

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("reviewbot.ingestion")

CONFIG_PATH = os.environ.get("BRANDS_CONFIG", "/app/config/brands.yml")
# Brands added at runtime via the onboarding UI land here (machine-managed), kept
# separate so the hand-written brands.yml keeps its comments. Both files merge.
DYNAMIC_CONFIG = os.environ.get(
    "BRANDS_DYNAMIC_CONFIG", os.path.join(os.path.dirname(CONFIG_PATH), "brands.dynamic.yml")
)

# Which connectors to instantiate, gated by the env vars they need so a missing
# credential disables that source instead of crashing the whole run. Reddit,
# Google Maps, Yelp, and TripAdvisor all run through Apify (one APIFY_TOKEN).
_APIFY_SPECS = {
    "reddit": REDDIT,
    "google_maps": GOOGLE_MAPS,
    "yelp": YELP,
    "tripadvisor": TRIPADVISOR,
    "instagram": INSTAGRAM,
    "facebook": FACEBOOK,
}


def build_connectors(enabled: list[str]) -> list:
    connectors = []
    for name in enabled:
        try:
            if name == "web":
                connectors.append(GoogleSearchConnector())
            elif name == "app_store":
                connectors.append(AppStoreConnector())
            elif name == "google_play":
                connectors.append(GooglePlayConnector())
            elif name in _APIFY_SPECS:
                connectors.append(ApifyConnector(_APIFY_SPECS[name]))
            else:
                log.warning("unknown source '%s' — skipping", name)
        except KeyError as missing:
            log.warning("source '%s' disabled: missing env var %s", name, missing)
        except Exception:  # noqa: BLE001
            log.exception("could not init source '%s' — skipping", name)
    return connectors


def _load_config() -> dict:
    with open(CONFIG_PATH) as fh:
        config = yaml.safe_load(fh) or {}
    brands = list(config.get("brands") or [])
    seen = {(b.get("name") or "").strip().lower() for b in brands}
    if os.path.exists(DYNAMIC_CONFIG):
        try:
            with open(DYNAMIC_CONFIG) as fh:
                dyn = yaml.safe_load(fh) or {}
            for b in dyn.get("brands") or []:
                key = (b.get("name") or "").strip().lower()
                if key and key not in seen:
                    brands.append(b)
                    seen.add(key)
        except Exception:  # noqa: BLE001
            log.exception("could not read dynamic brands file %s", DYNAMIC_CONFIG)
    config["brands"] = brands
    return config


def run_source(source_name: str, config: dict | None = None) -> int:
    """Run a single source across every brand that enables it.

    This is the granular entrypoint Airflow calls — one task per source, so each
    source's cadence, retries, and failures are independent of the others.
    """
    config = config or _load_config()
    default_limit = int(os.environ.get("FETCH_LIMIT", "50"))
    connectors = build_connectors([source_name])
    if not connectors:
        log.warning("source '%s' unavailable (missing creds?) — skipping", source_name)
        return 0
    connector = connectors[0]
    total = 0
    for brand_cfg in config.get("brands", []):
        enabled = brand_cfg.get("sources", config.get("default_sources", []))
        if source_name not in enabled:
            continue
        brand = brand_cfg["name"]
        keywords = brand_cfg.get("keywords", [brand])
        limit = int(brand_cfg.get("limit", default_limit))
        reviews = list(connector.fetch(brand, keywords, limit, website=brand_cfg.get("website")))
        total += postgres_loader.load(reviews)
    log.info("source=%s upserted %d reviews", source_name, total)
    return total


def run_brand(brand_cfg: dict, on_progress=None) -> int:
    """Scrape ONE brand across its enabled sources into raw. Returns rows written.

    The onboarding flow calls this to collect a just-added brand immediately. The
    optional on_progress(source_name, written, running_total) callback lets the
    caller show live progress. One source failing never aborts the others.
    """
    default_limit = int(os.environ.get("FETCH_LIMIT", "50"))
    brand = brand_cfg["name"]
    keywords = brand_cfg.get("keywords", [brand])
    enabled = brand_cfg.get("sources") or []
    limit = int(brand_cfg.get("limit", default_limit))
    total = 0
    for connector in build_connectors(enabled):
        try:
            reviews = list(connector.fetch(brand, keywords, limit, website=brand_cfg.get("website")))
            written = postgres_loader.load(reviews)
        except Exception:  # noqa: BLE001
            log.exception("brand=%s source=%s failed — skipping", brand, connector.source_name)
            written = 0
        total += written
        if on_progress:
            try:
                on_progress(connector.source_name, written, total)
            except Exception:  # noqa: BLE001
                log.exception("on_progress callback failed")
    log.info("run_brand: brand=%s upserted %d reviews", brand, total)
    return total


def run_once(config: dict) -> int:
    total = 0
    default_limit = int(os.environ.get("FETCH_LIMIT", "50"))
    for brand_cfg in config.get("brands", []):
        brand = brand_cfg["name"]
        keywords = brand_cfg.get("keywords", [brand])
        enabled = brand_cfg.get("sources", config.get("default_sources", []))
        limit = int(brand_cfg.get("limit", default_limit))

        for connector in build_connectors(enabled):
            log.info("fetching brand=%s source=%s", brand, connector.source_name)
            reviews = list(connector.fetch(brand, keywords, limit, website=brand_cfg.get("website")))
            written = postgres_loader.load(reviews)
            total += written
    return total


def main() -> None:
    config = _load_config()

    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "900"))  # 15 min default
    run_once_only = os.environ.get("RUN_ONCE", "0") == "1"

    while True:
        try:
            n = run_once(config)
            log.info("scrape pass complete: %d reviews upserted", n)
            # Embed + score the new rows into MARTS so the chatbot can see them.
            # (In the Airflow setup this is a separate task instead.)
            from ..enrich.run import enrich

            enrich()
        except Exception:  # noqa: BLE001
            log.exception("ingestion pass failed; will retry next interval")

        if run_once_only:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
