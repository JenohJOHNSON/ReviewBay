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
from .reddit_api import RedditApiConnector
from .reddit_json import RedditJsonConnector
from .hackernews import HackerNewsConnector
from .mastodon_source import MastodonConnector
from .tavily_source import TavilyConnector
from .firecrawl_source import FirecrawlConnector
from .playwright_source import TrustpilotConnector

__all__ = [
    "BaseConnector",
    "ApifyConnector",
    "GoogleSearchConnector",
    "AppStoreConnector",
    "GooglePlayConnector",
    "RedditApiConnector",
    "RedditJsonConnector",
    "HackerNewsConnector",
    "MastodonConnector",
    "TavilyConnector",
    "FirecrawlConnector",
    "TrustpilotConnector",
    "REDDIT",
    "GOOGLE_MAPS",
    "YELP",
    "TRIPADVISOR",
    "INSTAGRAM",
    "FACEBOOK",
]
