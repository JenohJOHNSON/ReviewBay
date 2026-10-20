"""Brand onboarding — the product's front door.

Add a brand (website optional) and immediately kick off collection, reporting
live progress, then hand off to the dashboard. New brands are written to a
machine-managed `brands.dynamic.yml` (separate from the hand-written brands.yml)
that the ingestion loop also reads, so an onboarded brand keeps getting refreshed
on every future poll.

**Progress here is real, never simulated.** Each phase is set when the work for
it actually begins, and `sources` carries per-source outcomes as they land, so
the UI can show which sources produced data and which were unavailable rather
than a bar that moves on a timer. The phase list is fixed and published as
`PHASES` so the frontend and the API cannot drift apart.

**The website field is the SSRF entry point.** It is a URL from an untrusted
user that the server will later fetch, so it is validated here, at the boundary,
before it is ever persisted — see `reviewbot.scraping.urlpolicy`.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import yaml

from .. import db
from ..ingestion.collect import FREE_PLAN, collect_until
from ..ingestion.run import CONFIG_PATH, DYNAMIC_CONFIG
from ..scraping import urlpolicy

log = logging.getLogger(__name__)

# Onboarding collects from cloud-safe free sources, then broadens and (last
# resort) reaches walled gardens via Apify, all handled by collect_until. The
# persisted source list is the same plan so future poll passes stay quiet on
# Railway while still refreshing broadly.
ONBOARD_SOURCES = list(FREE_PLAN)
ONBOARD_LIMIT = int(os.environ.get("ONBOARD_LIMIT", "50"))

#: The phases a collection moves through, in order, with the label the UI shows.
#: Published so the frontend renders the real sequence instead of hard-coding it.
PHASES: tuple[tuple[str, str], ...] = (
    ("queued", "Waiting for a collection slot"),
    ("discovering", "Discovering sources"),
    ("collecting", "Collecting reviews"),
    ("reading", "Reading review pages"),
    ("validating", "Validating records"),
    ("deduplicating", "Removing duplicates"),
    ("enriching", "Enriching data"),
    ("checking", "Generating intelligence"),
    ("done", "Completed"),
)
PHASE_LABELS = dict(PHASES)

# In-memory progress for the "collecting…" screen. Fine for the single-process
# API; swap for a shared store if this ever runs multi-worker.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
#: Keep the job map from growing for the life of the process.
_MAX_JOBS = int(os.environ.get("ONBOARD_JOB_HISTORY", "64"))

# Serialize collections so the API container never runs multiple scrapes and
# enrichment passes at the same time. Extra onboards queue and run one at a time. Raise
# MAX_CONCURRENT_COLLECTIONS only on a host with RAM to spare.
_COLLECT_GATE = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT_COLLECTIONS", "1")))


def _norm(name: str) -> str:
    return (name or "").strip()


class InvalidWebsite(ValueError):
    """The supplied website is not a URL the server may fetch."""


def normalize_website(website: str | None) -> str | None:
    """Validate and canonicalize a user-supplied website URL.

    Returns None when nothing was supplied (the field is optional). Raises
    `InvalidWebsite` when something was supplied that the server must not fetch:
    this value reaches the scraping engines, so an unvalidated `http://localhost`
    or `http://169.254.169.254` here is a server-side request forgery with the
    user holding the trigger.

    A bare domain is accepted and upgraded to https, because that is what people
    type.
    """
    raw = (website or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    verdict = urlpolicy.check(raw)
    if not verdict.allowed:
        # The reason is safe to show: it describes the policy, not the network.
        raise InvalidWebsite(
            f"That website cannot be used: {verdict.reason}. "
            "Enter a public http(s) address, or leave the field empty."
        )
    return urlpolicy.canonical(raw) or raw


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


#: Longest brand name we will accept. Long enough for any real brand, short
#: enough that it cannot be used to bloat the config file or a query.
MAX_BRAND_NAME = 120


def add_brand(name: str, website: str | None = None, keywords: list[str] | None = None) -> dict:
    name = _norm(name)
    if not name:
        raise ValueError("brand name is required")
    if len(name) > MAX_BRAND_NAME:
        raise ValueError(f"brand name must be {MAX_BRAND_NAME} characters or fewer")
    brand_cfg = {
        "name": name,
        "keywords": keywords or [name],
        "sources": ONBOARD_SOURCES,
        "limit": ONBOARD_LIMIT,
    }
    validated = normalize_website(website)  # raises InvalidWebsite on a bad URL
    if validated:
        brand_cfg["website"] = validated
    _persist_brand(brand_cfg)
    return brand_cfg


def _set(name: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.setdefault(
            name, {"status": "collecting", "phase": "queued", "collected": 0, "sources": {}}
        )
        job.update(fields)


def _trim_jobs() -> None:
    with _LOCK:
        if len(_JOBS) <= _MAX_JOBS:
            return
        finished = [
            (job.get("finished_at") or 0, key)
            for key, job in _JOBS.items()
            if job.get("status") in ("done", "error")
        ]
        for _ts, key in sorted(finished)[: len(_JOBS) - _MAX_JOBS]:
            _JOBS.pop(key, None)


def start_collection(brand_cfg: dict) -> None:
    """Background job: scrape the brand, enrich, updating progress as it goes."""
    name = brand_cfg["name"]
    started = time.time()
    with _LOCK:
        _JOBS[name] = {
            "status": "collecting",
            "phase": "queued",
            "collected": 0,
            "collected_new": 0,
            "sources": {},
            "started_at": started,
        }
    _trim_jobs()

    def _progress(source: str, written: int, total: int) -> None:
        with _LOCK:
            job = _JOBS.setdefault(
                name, {"status": "collecting", "collected": 0, "sources": {}}
            )
            sources = job.setdefault("sources", {})
            # Accumulate: a source can report more than once across stages, and
            # overwriting made the last (often empty) pass hide earlier results.
            sources[source] = sources.get(source, 0) + written
            job["collected"] = total
            # The phase genuinely changes once pages start being read, so reflect
            # it rather than showing "collecting" for the whole run.
            if source in ("scrapling", "scrapegraphai") and job.get("phase") == "collecting":
                job["phase"] = "reading"

    # Wait our turn so concurrent onboards don't scrape at once (memory spike).
    # Extra onboards sit in "queued" until a slot frees.
    _COLLECT_GATE.acquire()
    try:
        _set(name, phase="discovering")

        # collect_until never raises and never returns nothing just because one
        # source (or the brand website) failed, so onboarding degrades to a
        # partial result instead of a dead-end error.
        _set(name, phase="collecting")
        result = collect_until(brand_cfg, on_progress=_progress)

        # Validation and deduplication happen inside collect_until as records
        # arrive, so by here they are done; report their real outcomes.
        quality = result.get("quality") or {}
        _set(
            name,
            phase="validating",
            collected=result["total"],
            collected_new=result.get("collected_new", 0),
            validated=quality.get("kept", 0),
            rejected=quality.get("rejected", 0),
            duplicates=quality.get("duplicates", 0),
        )

        # Enrichment (embeddings + sentiment) is a separate step. If it fails we
        # still finish "done" with what we collected; the poll loop's enrich will
        # catch it up. We never flip to a scary "nothing" error over this.
        _set(name, phase="enriching")
        analyzed = True
        try:
            from ..enrich.run import enrich

            enrich()
        except Exception:  # noqa: BLE001
            log.exception("onboarding: enrich failed for %s (loop will retry)", name)
            analyzed = False

        # Quality-control pass: an LLM checks each review for brand relevance and
        # sentiment. Best-effort; a failure just leaves rows un-checked (still
        # shown) rather than blocking the hand-off to the dashboard.
        _set(name, phase="checking")
        qc_result = None
        try:
            from .. import qc as _qc

            qc_result = _qc.qc(brand=name)
        except Exception:  # noqa: BLE001
            log.exception("onboarding: qc failed for %s", name)

        # "Partial" is a real outcome and is reported as one: the floor was not
        # met, or enrichment did not finish. Saying "done" either way is what
        # makes a progress display untrustworthy.
        status = "done"
        if not result["total"]:
            status = "empty"
        elif not result.get("floor_met") or not analyzed:
            status = "partial"

        _set(
            name,
            status=status,
            phase="done",
            collected=result["total"],
            collected_new=result.get("collected_new", 0),
            analyzed=analyzed,
            qc=qc_result,
            result=result,
            finished_at=time.time(),
            duration_ms=int((time.time() - started) * 1000),
        )
    except db.DatabaseConfigError as exc:
        log.warning("onboarding collection paused for %s: %s", name, exc)
        _set(name, status="error", phase="done", message=str(exc), finished_at=time.time())
    except Exception:  # noqa: BLE001 — should be unreachable; collect_until is resilient
        log.exception("onboarding collection failed for %s", name)
        _set(
            name,
            status="error",
            phase="done",
            message="Collection failed. Check the API logs for details.",
            finished_at=time.time(),
        )
    finally:
        _COLLECT_GATE.release()


def start_async(brand_cfg: dict) -> None:
    threading.Thread(target=start_collection, args=(brand_cfg,), daemon=True).start()


def status(name: str) -> dict:
    with _LOCK:
        job = _JOBS.get(_norm(name))
        if not job:
            return {
                "status": "unknown",
                "phase": None,
                "phase_label": None,
                "collected": 0,
                "sources": {},
                "phases": [{"key": k, "label": v} for k, v in PHASES],
            }
        out = dict(job)
    out["phase_label"] = PHASE_LABELS.get(out.get("phase") or "", None)
    out["phases"] = [{"key": k, "label": v} for k, v in PHASES]
    # Which sources produced data and which came back empty, so the UI can say so
    # instead of implying everything worked.
    sources = out.get("sources") or {}
    out["sources_succeeded"] = sorted(k for k, v in sources.items() if v)
    out["sources_empty"] = sorted(k for k, v in sources.items() if not v)
    return out
