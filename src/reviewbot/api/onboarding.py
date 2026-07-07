"""Brand onboarding — the product's front door.

Add a brand (website optional) and immediately kick off collection, reporting
live progress, then hand off to the dashboard. New brands are written to a
machine-managed `brands.dynamic.yml` (separate from the hand-written brands.yml)
that the ingestion loop also reads, so an onboarded brand keeps getting refreshed
on every future poll.
"""

from __future__ import annotations

import logging
import os
import threading

import yaml

from ..ingestion.run import CONFIG_PATH, DYNAMIC_CONFIG, run_brand

log = logging.getLogger(__name__)

# Onboarding defaults to the sources we've VALIDATED end-to-end, so a new brand
# lights up with real data fast and mostly free. Users can enable more later.
ONBOARD_SOURCES = ["web", "app_store", "google_play"]
ONBOARD_LIMIT = int(os.environ.get("ONBOARD_LIMIT", "50"))

# In-memory progress for the "collecting…" screen. Fine for the single-process
# API; swap for a shared store if this ever runs multi-worker.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _norm(name: str) -> str:
    return (name or "").strip()


def list_brands() -> list[str]:
    names: list[str] = []
    for path in (CONFIG_PATH, DYNAMIC_CONFIG):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
            for b in doc.get("brands") or []:
                n = _norm(b.get("name", ""))
                if n and n not in names:
                    names.append(n)
        except Exception:  # noqa: BLE001
            log.exception("could not read %s", path)
    return names


def _persist_brand(brand_cfg: dict) -> None:
    """Append the brand to the dynamic (machine-managed) config file."""
    doc = {"brands": []}
    if os.path.exists(DYNAMIC_CONFIG):
        try:
            with open(DYNAMIC_CONFIG) as fh:
                doc = yaml.safe_load(fh) or {"brands": []}
        except Exception:  # noqa: BLE001
            log.exception("could not read %s — recreating", DYNAMIC_CONFIG)
            doc = {"brands": []}
    brands = doc.get("brands") or []
    key = _norm(brand_cfg["name"]).lower()
    if any(_norm(b.get("name", "")).lower() == key for b in brands):
        return  # already tracked
    brands.append(brand_cfg)
    doc["brands"] = brands
    os.makedirs(os.path.dirname(DYNAMIC_CONFIG), exist_ok=True)
    tmp = DYNAMIC_CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# Machine-managed: brands added via the onboarding UI. Safe to edit.\n")
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, DYNAMIC_CONFIG)


def add_brand(name: str, website: str | None = None, keywords: list[str] | None = None) -> dict:
    name = _norm(name)
    if not name:
        raise ValueError("brand name is required")
    brand_cfg = {
        "name": name,
        "keywords": keywords or [name],
        "sources": ONBOARD_SOURCES,
        "limit": ONBOARD_LIMIT,
    }
    if website and website.strip():
        brand_cfg["website"] = website.strip()
    _persist_brand(brand_cfg)
    return brand_cfg


def start_collection(brand_cfg: dict) -> None:
    """Background job: scrape the brand, enrich, updating progress as it goes."""
    name = brand_cfg["name"]
    with _LOCK:
        _JOBS[name] = {"status": "collecting", "phase": "scraping", "collected": 0, "sources": {}}

    def _progress(source: str, written: int, total: int) -> None:
        with _LOCK:
            job = _JOBS.setdefault(name, {"status": "collecting", "collected": 0, "sources": {}})
            job["sources"][source] = written
            job["collected"] = total

    try:
        run_brand(brand_cfg, on_progress=_progress)
        with _LOCK:
            _JOBS[name]["phase"] = "analyzing"  # embeddings + sentiment
        from ..enrich.run import enrich

        enrich()
        with _LOCK:
            _JOBS[name].update(status="done", phase="done")
    except Exception:  # noqa: BLE001
        log.exception("onboarding collection failed for %s", name)
        with _LOCK:
            _JOBS.setdefault(name, {})["status"] = "error"


def start_async(brand_cfg: dict) -> None:
    threading.Thread(target=start_collection, args=(brand_cfg,), daemon=True).start()


def status(name: str) -> dict:
    with _LOCK:
        job = _JOBS.get(_norm(name))
        return dict(job) if job else {"status": "unknown", "phase": None, "collected": 0, "sources": {}}
