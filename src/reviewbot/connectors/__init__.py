"""Source connectors.

Each connector implements BaseConnector.fetch() and yields NormalizedReview
objects. Adding source #N is a new plugin here — nothing else changes.
"""

from .base import BaseConnector
from .apify_source import (
    ApifyConnector,
    REDDIT,
    GOOGLE_MAPS,
    YELP,
    TRIPADVISOR,
    INSTAGRAM,
    FACEBOOK,
)
from .google_search import GoogleSearchConnector
from .app_store import AppStoreConnector
from .google_play import GooglePlayConnector

__all__ = [
    "BaseConnector",
    "ApifyConnector",
    "GoogleSearchConnector",
    "AppStoreConnector",
    "GooglePlayConnector",
    "REDDIT",
    "GOOGLE_MAPS",
    "YELP",
    "TRIPADVISOR",
    "INSTAGRAM",
    "FACEBOOK",
]
