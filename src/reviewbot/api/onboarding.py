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

**One collection per brand at a time.** Jobs are keyed by the brand's registry
key (trimmed, lowercased), and claiming a job is a check-and-set under a lock.
Before accounts, two people adding "nike" and "Nike" started two concurrent
scrapes of the same brand with two progress records; now the second request
attaches to the first. Re-runs are additionally held to RERUN_COOLDOWN_SECONDS,
so a re-run button (or delete-then-re-add) cannot be used to hammer the scrapers.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import yaml

from .. import accounts, db
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

# In-memory progress for the "collecting…" screen, keyed by brand key. Fine for
# the single-process API; swap for a shared store if this ever runs multi-worker.
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
# Serializes read-modify-write of brands.dynamic.yml. Two onboards finishing
# together used to both read the file, each append its brand, and the second
# write dropped the first.
_FILE_LOCK = threading.Lock()

#: Minimum gap between collections of one brand started by a re-run (or by
#: adding a brand someone collected moments ago). 0 disables the cooldown.
RERUN_COOLDOWN_SECONDS = int(os.environ.get("RERUN_COOLDOWN_SECONDS", "300"))
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


def _read_dynamic() -> dict:
    if not os.path.exists(DYNAMIC_CONFIG):
        return {"brands": []}
    try:
        with open(DYNAMIC_CONFIG) as fh:
            return yaml.safe_load(fh) or {"brands": []}
    except Exception:  # noqa: BLE001
        log.exception("could not read %s — recreating", DYNAMIC_CONFIG)
        return {"brands": []}


def _write_dynamic(doc: dict) -> None:
    os.makedirs(os.path.dirname(DYNAMIC_CONFIG), exist_ok=True)
    tmp = DYNAMIC_CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# Machine-managed: brands added via the onboarding UI. Safe to edit.\n")
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, DYNAMIC_CONFIG)


def _persist_brand(brand_cfg: dict) -> None:
    """Append the brand to the dynamic (machine-managed) config file.

    With a database the registry in `app.brands` is the source of truth and this
    file is a convenience copy for setups without one; on a deploy whose disk is
    ephemeral it is simply lost, which no longer matters.
    """
    try:
        with _FILE_LOCK:
            doc = _read_dynamic()
            brands = doc.get("brands") or []
            key = accounts.brand_key(brand_cfg["name"])
            if any(accounts.brand_key(b.get("name", "")) == key for b in brands):
                return  # already tracked
            brands.append(brand_cfg)
            doc["brands"] = brands
            _write_dynamic(doc)
    except OSError:
        # A read-only config directory must not fail an onboard that the
        # database has already recorded.
        log.warning("could not write %s; relying on the database registry", DYNAMIC_CONFIG)


def forget_brand(name: str) -> bool:
    """Remove a brand from the dynamic config file, so the worker's YAML path
    stops refreshing it too. Returns whether it was there."""
    key = accounts.brand_key(name)
    try:
        with _FILE_LOCK:
            doc = _read_dynamic()
            brands = doc.get("brands") or []
            kept = [b for b in brands if accounts.brand_key(b.get("name", "")) != key]
            if len(kept) == len(brands):
                return False
            doc["brands"] = kept
            _write_dynamic(doc)
            return True
    except OSError:
        log.warning("could not update %s while removing %s", DYNAMIC_CONFIG, name)
        return False


#: Longest brand name we will accept. Long enough for any real brand, short
#: enough that it cannot be used to bloat the config file or a query.
MAX_BRAND_NAME = 120


def build_brand_config(
    name: str, website: str | None = None, keywords: list[str] | None = None
) -> dict:
    """Validate an onboarding request into a brand config, persisting nothing."""
    name = " ".join(_norm(name).split())
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
    return brand_cfg


def add_brand(name: str, website: str | None = None, keywords: list[str] | None = None) -> dict:
    """Validate and persist to the YAML file (the no-database path)."""
    brand_cfg = build_brand_config(name, website, keywords)
    _persist_brand(brand_cfg)
    return brand_cfg


def _set(name: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.setdefault(
            accounts.brand_key(name),
            {"status": "collecting", "phase": "queued", "collected": 0, "sources": {}},
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


def _fresh_job(name: str) -> dict:
    return {
        "status": "collecting",
        "phase": "queued",
        "brand": name,
        "collected": 0,
        "collected_new": 0,
        "sources": {},
        "started_at": time.time(),
    }


def _claim(name: str) -> bool:
    """Atomically reserve the brand's job slot. False if one is already running."""
    key = accounts.brand_key(name)
    with _LOCK:
        job = _JOBS.get(key)
        if job and job.get("status") == "collecting":
            return False
        _JOBS[key] = _fresh_job(name)
    _trim_jobs()
    return True


def is_running(name: str) -> bool:
    with _LOCK:
        job = _JOBS.get(accounts.brand_key(name))
        return bool(job and job.get("status") == "collecting")


def seconds_since_collection(name: str) -> float | None:
    """How long ago this brand was last collected or re-run, if known.

    Combines this process's job history with the registry's durable timestamps,
    so the cooldown survives a restart.
    """
    now = time.time()
    seen: list[float] = []
    with _LOCK:
        job = _JOBS.get(accounts.brand_key(name))
        if job:
            for field in ("finished_at", "started_at"):
                if job.get(field):
                    seen.append(now - float(job[field]))
    try:
        entry = accounts.get_brand(name)
    except Exception:  # noqa: BLE001 - no database, or no migration: memory only
        entry = None
    if entry:
        for field in ("last_collected_at", "last_rerun_at"):
            stamp = entry.get(field)
            if stamp is not None and hasattr(stamp, "timestamp"):
                seen.append(now - stamp.timestamp())
    return min(seen) if seen else None


def cooldown_remaining(name: str) -> int:
    """Seconds until this brand may be collected again; 0 when it may."""
    if RERUN_COOLDOWN_SECONDS <= 0:
        return 0
    since = seconds_since_collection(name)
    if since is None:
        return 0
    return max(0, int(RERUN_COOLDOWN_SECONDS - since + 0.999))


def start_collection(brand_cfg: dict, _claimed: bool = False) -> None:
    """Background job: scrape the brand, enrich, updating progress as it goes."""
    name = brand_cfg["name"]
    key = accounts.brand_key(name)
    if not _claimed:
        with _LOCK:
            _JOBS[key] = _fresh_job(name)
        _trim_jobs()
    with _LOCK:
        started = _JOBS[key].get("started_at") or time.time()

    def _progress(source: str, written: int, total: int) -> None:
        with _LOCK:
            job = _JOBS.setdefault(
                key, {"status": "collecting", "collected": 0, "sources": {}}
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
        _after_collection(name)
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


def _after_collection(name: str) -> None:
    """Make fresh data visible: stamp the registry and drop cached summaries.

    Without the cache invalidation, a re-run would finish and the dashboard would
    keep serving the pre-run insights and report for up to their TTL, which reads
    as "re-run did nothing".
    """
    try:
        accounts.mark_collected(name)
    except Exception:  # noqa: BLE001 - no database or no migration 003
        log.debug("onboarding: could not stamp last_collected_at for %s", name)
    from . import insights, report

    key = accounts.brand_key(name)
    insights._CACHE.invalidate(key)
    report._CACHE.invalidate(key)


def start_async(brand_cfg: dict) -> bool:
    """Start collecting in the background. False if this brand is already being
    collected, in which case the caller should report that job's progress."""
    if not _claim(brand_cfg["name"]):
        return False
    threading.Thread(
        target=start_collection, args=(brand_cfg,), kwargs={"_claimed": True}, daemon=True
    ).start()
    return True


def status(name: str) -> dict:
    with _LOCK:
        job = _JOBS.get(accounts.brand_key(name))
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
