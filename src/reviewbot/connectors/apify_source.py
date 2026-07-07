"""Generic Apify-actor connector.

One class drives many sources — Google Maps reviews, Yelp, TripAdvisor, etc. —
because Apify actors all share the run→dataset shape. A source is just an
ActorSpec: which actor to run, how to build its input, and how to map a dataset
item to our NormalizedReview. Adding a new Apify-backed source = one ActorSpec,
no new class.

Set APIFY_TOKEN in the environment.

⚠️ The review-actor field mappings below (Reddit / Google Maps / Yelp /
TripAdvisor / Facebook) are based on each actor's documented output, NOT verified
against a live run — do a one-off `.call()` and inspect the dataset once before
trusting them in prod (actor schemas drift). By contrast, google_search.py (web)
AND the Instagram mapping/input here ARE validated against live output (2026-07).

Instagram/Facebook are BEST-EFFORT: Meta actively blocks scraping (ToS
violation, fragile). They fail gracefully — if the actor errors or returns
nothing, the pass continues and the always-on `web` source is the fallback.
Instagram needs directUrls (a hashtag/profile page), not hashtag search, to get
real posts — auto-built from the brand, or pin env APIFY_IG_URL_<BRAND>.
Facebook needs a page URL pinned via env APIFY_FB_URL_<BRAND>; without it the
source skips itself (no run, no credits).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Callable

from ..models import NormalizedReview
from .apify_client import ordered_tokens, run_with_fallback
from .base import BaseConnector

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActorSpec:
    source_name: str
    actor_id: str  # Apify actor, e.g. "compass/google-maps-reviews-scraper"
    build_input: Callable[[str, list[str], int], dict[str, Any]]
    map_item: Callable[[str, dict[str, Any]], NormalizedReview | None]


def _gmaps_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    return {"searchStringsArray": keywords or [brand], "maxReviews": limit, "language": "en"}


def _gmaps_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("text") or item.get("reviewText")
    if not text:
        return None
    return NormalizedReview(
        brand=brand,
        source="google_maps",
        source_url=item.get("reviewUrl") or item.get("url") or "",
        text=text,
        author=item.get("name") or item.get("reviewerName"),
        rating=_as_float(item.get("stars") or item.get("rating")),
        created_at=item.get("publishedAtDate") or item.get("publishAt"),
        extra={"place": item.get("title") or item.get("placeName")},
    )


def _yelp_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    return {"searchTerms": keywords or [brand], "maxReviews": limit}


def _yelp_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("text") or item.get("comment")
    if not text:
        return None
    return NormalizedReview(
        brand=brand,
        source="yelp",
        source_url=item.get("url") or item.get("reviewUrl") or "",
        text=text,
        author=item.get("userName") or item.get("author"),
        rating=_as_float(item.get("rating") or item.get("stars")),
        created_at=item.get("date"),
        extra={"business": item.get("bizName")},
    )


def _tripadvisor_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    return {"query": (keywords or [brand])[0], "maxItemsPerQuery": limit}


def _tripadvisor_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("text") or item.get("review")
    if not text:
        return None
    return NormalizedReview(
        brand=brand,
        source="tripadvisor",
        source_url=item.get("url") or "",
        text=text,
        author=item.get("user", {}).get("username") if isinstance(item.get("user"), dict) else item.get("userName"),
        rating=_as_float(item.get("rating")),
        created_at=item.get("publishedDate"),
        extra={"place": item.get("placeName")},
    )


def _reddit_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    # trudax/reddit-scraper-lite style input: search terms + item cap.
    return {
        "searches": keywords or [brand],
        "type": "posts",
        "sort": "NEW",
        "maxItems": limit,
        "maxPostCount": limit,
    }


def _reddit_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    # Works for posts (title + body) and comments (body only).
    title = item.get("title")
    body = item.get("body") or item.get("text")
    text = "\n\n".join(x for x in (title, body) if x)
    url = item.get("url") or item.get("link")
    if not text or not url:
        return None
    return NormalizedReview(
        brand=brand,
        source="reddit",
        source_url=url,
        text=text,
        author=item.get("username") or item.get("author"),
        rating=None,  # Reddit has no stars; sentiment fills this role
        created_at=item.get("createdAt"),
        extra={
            "subreddit": item.get("communityName") or item.get("parsedCommunityName"),
            "up_votes": item.get("upVotes"),
            "num_comments": item.get("numberOfComments"),
            "data_type": item.get("dataType"),  # "post" or "comment"
        },
    )


def _instagram_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    # Point the scraper at a specific page via directUrls — that's what returns
    # flat posts WITH captions (verified 2026-07). Hashtag *search* only returns a
    # tag-summary object, not posts. Default to the brand's English hashtag page;
    # override per brand with env APIFY_IG_URL_<BRAND> (a profile or hashtag URL).
    slug = "".join(ch if ch.isalnum() else "_" for ch in (brand or "").upper())
    url = os.environ.get("APIFY_IG_URL_" + slug)
    if not url:
        tag = "".join(ch for ch in (keywords or [brand])[0] if ch.isalnum()).lower()
        url = f"https://www.instagram.com/explore/tags/{tag}/"
    return {"directUrls": [url], "resultsType": "posts", "resultsLimit": limit}


def _instagram_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("caption") or item.get("text")
    url = item.get("url") or item.get("postUrl")
    if not text or not url:
        return None
    return NormalizedReview(
        brand=brand,
        source="instagram",
        source_url=url,
        text=text,
        author=item.get("ownerUsername") or item.get("ownerFullName"),
        rating=None,  # no stars; sentiment fills this role
        created_at=item.get("timestamp") or item.get("takenAtDate"),
        extra={
            "likes": item.get("likesCount"),
            "comments": item.get("commentsCount"),
            "hashtag": item.get("hashtag") or item.get("inputUrl"),
            "type": item.get("type"),
        },
    )


def _facebook_input(brand: str, keywords: list[str], limit: int) -> dict[str, Any]:
    # FB has no clean keyword search — scrape a specific page's posts, pinned per
    # brand via env APIFY_FB_URL_<BRAND>. No URL -> empty input -> source skips.
    slug = "".join(ch if ch.isalnum() else "_" for ch in (brand or "").upper())
    page_url = os.environ.get("APIFY_FB_URL_" + slug)
    if not page_url:
        return {}
    return {"startUrls": [{"url": page_url}], "resultsLimit": limit, "maxPosts": limit}


def _facebook_map(brand: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("text") or item.get("message") or item.get("postText")
    url = item.get("url") or item.get("postUrl") or item.get("topLevelUrl")
    if not text or not url:
        return None
    user = item.get("user")
    author = item.get("authorName") or item.get("pageName")
    if not author and isinstance(user, dict):
        author = user.get("name")
    return NormalizedReview(
        brand=brand,
        source="facebook",
        source_url=url,
        text=text,
        author=author,
        rating=None,
        created_at=item.get("time") or item.get("date") or item.get("timestamp"),
        extra={
            "likes": item.get("likes"),
            "comments": item.get("comments"),
            "shares": item.get("shares"),
        },
    )


# Ready-made specs. Actor ids are the common Apify Store actors; swap freely.
GOOGLE_MAPS = ActorSpec(
    source_name="google_maps",
    actor_id=os.environ.get("APIFY_GMAPS_ACTOR", "compass/google-maps-reviews-scraper"),
    build_input=_gmaps_input,
    map_item=_gmaps_map,
)
YELP = ActorSpec(
    source_name="yelp",
    actor_id=os.environ.get("APIFY_YELP_ACTOR", "tri_angle/yelp-reviews-scraper"),
    build_input=_yelp_input,
    map_item=_yelp_map,
)
TRIPADVISOR = ActorSpec(
    source_name="tripadvisor",
    actor_id=os.environ.get("APIFY_TRIPADVISOR_ACTOR", "maxcopell/tripadvisor-reviews"),
    build_input=_tripadvisor_input,
    map_item=_tripadvisor_map,
)
REDDIT = ActorSpec(
    source_name="reddit",
    actor_id=os.environ.get("APIFY_REDDIT_ACTOR", "trudax/reddit-scraper-lite"),
    build_input=_reddit_input,
    map_item=_reddit_map,
)
INSTAGRAM = ActorSpec(
    source_name="instagram",
    actor_id=os.environ.get("APIFY_INSTAGRAM_ACTOR", "apify/instagram-scraper"),
    build_input=_instagram_input,
    map_item=_instagram_map,
)
FACEBOOK = ActorSpec(
    source_name="facebook",
    actor_id=os.environ.get("APIFY_FACEBOOK_ACTOR", "apify/facebook-posts-scraper"),
    build_input=_facebook_input,
    map_item=_facebook_map,
)


class ApifyConnector(BaseConnector):
    def __init__(self, spec: ActorSpec) -> None:
        self.spec = spec
        self.source_name = spec.source_name
        ordered_tokens()  # validate ≥1 token now, so a tokenless source disables cleanly

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        run_input = self.spec.build_input(brand, keywords, limit)
        if not run_input:
            log.info(
                "%s: no run input for brand=%s — skipping (source needs config)",
                self.source_name, brand,
            )
            return
        client, dataset_id = run_with_fallback(self.spec.actor_id, run_input)
        if not client:
            return

        for item in client.dataset(dataset_id).iterate_items():
            try:
                review = self.spec.map_item(brand, item)
                if review and review.source_url and review.text.strip():
                    yield review
            except Exception:  # noqa: BLE001
                log.exception("skipping malformed %s item", self.spec.source_name)
                continue


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
