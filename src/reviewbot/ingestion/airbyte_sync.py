"""Airbyte worker step: normalize the Airbyte-landed rows, then embed into marts.

Run this after an Airbyte Cloud sync lands data in the Neon `airbyte` schema (on a
schedule, a webhook, or by hand):

    python -m reviewbot.ingestion.airbyte_sync

It reshapes the staging rows into raw.reviews_raw (see airbyte_normalize) and then
runs the existing local embeddings + sentiment into marts.reviews, so Airbyte-fed
reviews flow through to the dashboard and chat exactly like scraped ones.
"""

from __future__ import annotations

import logging
import os


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from ..enrich.run import enrich
    from .airbyte_normalize import normalize

    n = normalize()
    logging.getLogger("reviewbot.airbyte").info(
        "airbyte sync: normalized %d rows into raw.reviews_raw; enriching", n
    )
    enrich()


if __name__ == "__main__":
    main()
