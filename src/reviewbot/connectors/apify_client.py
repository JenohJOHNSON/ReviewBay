"""Apify token pool: sequential drain + automatic fallback.

Reads one or more Apify tokens and runs an actor against them strictly IN ORDER:
the first token handles everything until it runs out of credits (its run fails),
then it falls through to the second, then the third, and so on. Both the
ApifyConnector and the web connector go through run_with_fallback, so this is
centralized.

Tokens come from (first that is set):
  APIFY_TOKENS = "tok_a, tok_b, tok_c, tok_d"   # comma/space separated, in order
  APIFY_TOKEN  = "tok_a"                         # single token

Note: the dataset produced by a run belongs to the account whose token ran it,
so we return the *same* client that succeeded — the caller must read the dataset
with it.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _all_tokens() -> list[str]:
    raw = os.environ.get("APIFY_TOKENS") or os.environ.get("APIFY_TOKEN", "")
    return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]


def ordered_tokens() -> list[str]:
    """Tokens in the exact order given — first is used until it's exhausted.

    Raises KeyError if none are configured, so a source with no token is
    disabled cleanly by build_connectors (same behavior as before).
    """
    tokens = _all_tokens()
    if not tokens:
        raise KeyError("APIFY_TOKEN")
    return tokens


def _run_dataset_id(run) -> str | None:
    """Read the run's default dataset id across apify-client versions.

    Older apify-client returns a dict (`run["defaultDatasetId"]`); v3+ returns a
    typed Run object with a `.default_dataset_id` attribute. Handle both.
    """
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    for attr in ("default_dataset_id", "defaultDatasetId"):
        v = getattr(run, attr, None)
        if v:
            return v
    try:
        return run["defaultDatasetId"]
    except Exception:  # noqa: BLE001
        pass
    for dumper in ("model_dump", "to_dict", "dict"):
        f = getattr(run, dumper, None)
        if callable(f):
            try:
                d = f()
                if isinstance(d, dict):
                    return d.get("defaultDatasetId") or d.get("default_dataset_id")
            except Exception:  # noqa: BLE001
                pass
    return None


def run_with_fallback(actor_id: str, run_input: dict):
    """Run an Apify actor, falling back to the next token only if the RUN fails.

    Returns (client, dataset_id) from the token that ran it, or (None, None).
    Never raises. Crucially, a failure to *run* (quota, network) advances to the
    next token, but a failure to *parse* an otherwise-successful run does NOT —
    otherwise a code bug would burn every account's credits.
    """
    from apify_client import ApifyClient  # type: ignore

    tokens = ordered_tokens()
    last_err: object = None
    for i, token in enumerate(tokens, start=1):
        client = ApifyClient(token)
        try:
            run = client.actor(actor_id).call(run_input=run_input)
        except Exception as err:  # noqa: BLE001 — the run failed: try the next token
            last_err = err
            log.warning(
                "apify token #%d/%d failed to run %s (%s) — trying next",
                i, len(tokens), actor_id, err,
            )
            continue
        # The actor ran (and cost credits). A parse problem here must NOT trigger
        # another token's run.
        dataset_id = _run_dataset_id(run)
        if dataset_id:
            if i > 1:
                log.info("apify: token #%d ran %s after fallback", i, actor_id)
            return client, dataset_id
        log.error(
            "apify: %s ran on token #%d but no dataset id found (run type=%s)",
            actor_id, i, type(run).__name__,
        )
        return None, None
    log.error("apify: all %d token(s) failed to run %s: %s", len(tokens), actor_id, last_err)
    return None, None
