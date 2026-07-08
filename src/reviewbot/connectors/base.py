"""The connector interface — the key design bet of the whole system.

Every source (Reddit, Google Maps, Yelp, Instagram, ...) implements this one
method. The ingestion loop never knows or cares which source it is talking to;
it just calls fetch() and gets NormalizedReview objects back. That is what keeps
"add another source" a plugin instead of a rewrite.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Iterator

from ..models import NormalizedReview

log = logging.getLogger(__name__)


class BaseConnector(abc.ABC):
    #: short, stable identifier stored on every row (e.g. "reddit")
    source_name: str = "base"

    @abc.abstractmethod
    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        """Yield normalized reviews mentioning `brand`.

        `website` is the brand's optional home-page URL. Connectors that can use
        it (the web search) may scope results to that domain to disambiguate
        common names; others ignore it.

        Implementations should be resilient: log and skip a bad record rather
        than aborting the whole fetch, so one malformed item can't starve the
        pipeline of everything else.
        """
        raise NotImplementedError
