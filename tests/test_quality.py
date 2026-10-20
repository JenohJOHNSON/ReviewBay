"""Validation, deduplication and provenance.

The deduplication tests carry the most weight here. Over-merging silently
deletes genuine customer feedback and understates how often a sentiment appears,
which is worse than storing a duplicate — so the scoping rule has tests that
assert the *negative* case as firmly as the positive one.
"""

from __future__ import annotations

import unittest

from reviewbot import quality
from reviewbot.models import NormalizedReview

LONG = (
    "Harbour Tools delivered the drill set next day and the case is genuinely "
    "sturdy, not the flimsy plastic I expected for the price."
)
BRAND = "Harbour Tools"


def review(
    text: str = LONG,
    url: str = "https://site.example/r/1",
    *,
    brand: str = BRAND,
    source: str = "web",
    author: str | None = None,
    rating=None,
    created_at=None,
    kind: str = "review",
) -> NormalizedReview:
    return NormalizedReview(
        brand=brand,
        source=source,
        source_url=url,
        text=text,
        author=author,
        rating=rating,
        created_at=created_at,
        extra={"extraction": {"engine": "test", "kind": kind}},
    )


class BrandRelevanceTests(unittest.TestCase):
    def test_a_text_naming_the_brand_is_relevant(self) -> None:
        self.assertTrue(quality.mentions_brand("Love my Harbour Tools drill", BRAND))

    def test_a_generic_word_alone_is_not_a_mention(self) -> None:
        """The reason the generic-word list exists: "coffee" is not a brand."""
        self.assertFalse(quality.mentions_brand("I love coffee in the morning", "Blue Bottle Coffee"))
        self.assertTrue(quality.mentions_brand("Blue Bottle is my favourite", "Blue Bottle Coffee"))

    def test_run_together_spellings_match(self) -> None:
        self.assertTrue(quality.mentions_brand("harbourtools shipped fast", BRAND))

    def test_configured_keywords_also_count(self) -> None:
        self.assertTrue(
            quality.mentions_brand("HTools sent the wrong item", BRAND, keywords=["HTools"])
        )

    def test_a_brand_of_only_generic_words_still_matches_on_them(self) -> None:
        self.assertTrue(
            quality.mentions_brand("The Coffee Company was great", "The Coffee Company")
        )


class ValidationTests(unittest.TestCase):
    def test_a_good_record_passes(self) -> None:
        self.assertTrue(quality.validate(quality.clean_review(review())))

    def test_short_text_is_rejected(self) -> None:
        verdict = quality.validate(quality.clean_review(review("Too short")))
        self.assertFalse(verdict)
        self.assertIn("shorter than", verdict.reason)

    def test_missing_url_is_rejected_because_it_cannot_be_cited(self) -> None:
        verdict = quality.validate(quality.clean_review(review(url="")))
        self.assertFalse(verdict)
        self.assertIn("could not be cited", verdict.reason)

    def test_a_non_http_url_is_rejected(self) -> None:
        self.assertFalse(quality.validate(quality.clean_review(review(url="javascript:alert(1)"))))

    def test_boilerplate_is_rejected(self) -> None:
        verdict = quality.validate(
            quality.clean_review(
                review("This site uses cookies to improve your experience and we need consent.")
            )
        )
        self.assertFalse(verdict)
        self.assertIn("boilerplate", verdict.reason)

    def test_degenerate_repeated_text_is_rejected(self) -> None:
        verdict = quality.validate(quality.clean_review(review("spam " * 20)))
        self.assertFalse(verdict)
        self.assertIn("degenerate", verdict.reason)

    def test_off_brand_text_is_rejected(self) -> None:
        verdict = quality.validate(
            quality.clean_review(
                review("The weather has been lovely all week and the garden is finally growing.")
            ),
            keywords=[BRAND],
        )
        self.assertFalse(verdict)
        self.assertIn("does not mention the brand", verdict.reason)

    def test_trusted_sources_skip_the_brand_check(self) -> None:
        """An App Store review rarely repeats the app's name."""
        verdict = quality.validate(
            quality.clean_review(
                review(
                    "Crashes every time I open the camera tab and loses my draft entirely.",
                    source="app_store",
                )
            )
        )
        self.assertTrue(verdict, verdict.reason)


class NormalizationTests(unittest.TestCase):
    def test_ratings_outside_one_to_five_are_dropped_not_clamped(self) -> None:
        """A wrong rating is worse than a missing one."""
        self.assertEqual(quality.normalize_rating(9)[0], None)
        self.assertEqual(quality.normalize_rating(-1)[0], None)
        self.assertEqual(quality.normalize_rating(0)[0], None)
        self.assertEqual(quality.normalize_rating(4)[0], 4.0)
        self.assertEqual(quality.normalize_rating(float("nan"))[0], None)

    def test_relative_dates_are_dropped(self) -> None:
        self.assertEqual(quality.normalize_created_at("6 years ago")[0], None)
        self.assertEqual(quality.normalize_created_at("2026-01-02")[0], "2026-01-02")

    def test_html_and_entities_are_cleaned_from_text(self) -> None:
        dirty = (
            "<p>The <b>Harbour Tools</b> drill&#x27;s case is sturdy &amp; the bits "
            "fit properly, which was the whole point of buying the set.</p>"
        )
        cleaned = quality.clean_review(review(dirty))
        self.assertNotIn("<p>", cleaned.text)
        self.assertNotIn("&#x27;", cleaned.text)
        self.assertIn("drill's case", cleaned.text)

    def test_tracking_parameters_are_stripped_from_the_citation_url(self) -> None:
        cleaned = quality.clean_review(review(url="https://s.example/r/1?utm_source=x&id=2"))
        self.assertEqual(cleaned.source_url, "https://s.example/r/1?id=2")

    def test_an_overlong_author_is_dropped_rather_than_stored(self) -> None:
        cleaned = quality.clean_review(review(author="x" * 400))
        self.assertIsNone(cleaned.author)
        self.assertTrue(cleaned.extra["extraction"]["validation"]["author_dropped"])

    def test_the_validation_trail_only_records_what_was_actually_lost(self) -> None:
        """An int 4 becoming 4.0 is not a dropped value."""
        cleaned = quality.clean_review(review(rating=4, created_at="2026-01-02"))
        validation = cleaned.extra["extraction"]["validation"]
        self.assertNotIn("rating_dropped", validation)
        self.assertNotIn("created_at_dropped", validation)
        self.assertEqual(cleaned.rating, 4.0)

    def test_unicode_is_normalized_so_recaptures_hash_identically(self) -> None:
        a = quality.clean_review(review("Harbour Tools drill set arrived early and well packed."))
        b = quality.clean_review(review("Harbour Tools drill set arrived early and well packed."))
        self.assertEqual(a.id, b.id)


class DeduplicationTests(unittest.TestCase):
    def test_identical_captures_collapse(self) -> None:
        dedup = quality.Deduplicator()
        kept = quality.prepare([review(), review()], keywords=[BRAND], dedup=dedup)
        self.assertEqual(len(kept), 1)

    def test_the_same_review_read_twice_with_minor_differences_collapses(self) -> None:
        dedup = quality.Deduplicator()
        kept = quality.prepare(
            [review(LONG), review(LONG + " Read more")], keywords=[BRAND], dedup=dedup
        )
        self.assertEqual(len(kept), 1)

    def test_a_truncated_copy_on_the_same_url_collapses(self) -> None:
        dedup = quality.Deduplicator()
        kept = quality.prepare([review(LONG), review(LONG[:80])], keywords=[BRAND], dedup=dedup)
        self.assertEqual(len(kept), 1)

    def test_tracking_parameter_variants_collapse(self) -> None:
        dedup = quality.Deduplicator()
        kept = quality.prepare(
            [
                review(url="https://site.example/r/1"),
                review(url="https://site.example/r/1?utm_source=newsletter"),
            ],
            keywords=[BRAND],
            dedup=dedup,
        )
        self.assertEqual(len(kept), 1)

    # --- the negative cases, which matter at least as much ------------------

    def test_identical_text_on_DIFFERENT_urls_is_kept_as_two_reviews(self) -> None:
        """Two customers can independently write the same sentence.

        Merging them would delete genuine feedback and understate how often that
        sentiment appears, which is why near-duplicate detection is scoped to one
        URL rather than applied globally.
        """
        dedup = quality.Deduplicator()
        kept = quality.prepare(
            [
                review(LONG, url="https://a.example/r/1"),
                review(LONG, url="https://b.example/r/9"),
                review(LONG, url="https://c.example/r/4"),
            ],
            keywords=[BRAND],
            dedup=dedup,
        )
        self.assertEqual(len(kept), 3)

    def test_the_same_text_under_different_brands_is_kept(self) -> None:
        """A "Brand A vs Brand B" page is two correctly-attributed rows."""
        dedup = quality.Deduplicator()
        kept = quality.prepare(
            [
                review(LONG, brand="Harbour Tools"),
                review(LONG.replace("Harbour Tools", "Rival Tools"), brand="Rival Tools"),
            ],
            keywords=None,
            dedup=dedup,
            require_brand_mention=False,
        )
        self.assertEqual(len(kept), 2)

    def test_a_review_quote_is_not_swallowed_by_a_page_level_mention(self) -> None:
        """A whole-page mention contains every quote on that page by definition.

        Without the kind guard, the article-level fallback would swallow the
        specific reviews an engine later extracted from the same page, deleting
        the better data in favour of the worse.
        """
        page_text = (
            "What people say about Harbour Tools. " + LONG + " And another customer "
            "said the Harbour Tools delivery estimates are pure fiction every time."
        )
        dedup = quality.Deduplicator()
        kept = quality.prepare(
            [
                review(page_text, url="https://blog.example/x", kind="mention"),
                review(LONG, url="https://blog.example/x", kind="review"),
            ],
            keywords=[BRAND],
            dedup=dedup,
        )
        self.assertEqual(len(kept), 2)

    def test_different_reviews_on_the_same_url_are_both_kept(self) -> None:
        other = (
            "Harbour Tools customer service never answered my email about a cracked "
            "handle, so I ended up gluing it back together myself."
        )
        dedup = quality.Deduplicator()
        kept = quality.prepare([review(LONG), review(other)], keywords=[BRAND], dedup=dedup)
        self.assertEqual(len(kept), 2)

    def test_forget_location_allows_a_better_record_to_replace_one(self) -> None:
        dedup = quality.Deduplicator()
        mention = quality.clean_review(review(LONG, kind="mention"))
        dedup.add(mention)
        self.assertEqual(len(dedup), 1)
        dedup.forget_location(mention)
        self.assertEqual(len(dedup), 0)
        again = quality.prepare([review(LONG, kind="mention")], keywords=[BRAND], dedup=dedup)
        self.assertEqual(len(again), 1)


class SimilarityTests(unittest.TestCase):
    def test_identical_text_scores_one(self) -> None:
        self.assertEqual(quality.similarity(LONG, LONG), 1.0)

    def test_unrelated_text_scores_zero(self) -> None:
        self.assertEqual(quality.similarity(LONG, "Completely different words here now"), 0.0)

    def test_empty_input_is_safe(self) -> None:
        self.assertEqual(quality.similarity("", LONG), 0.0)


class ReportTests(unittest.TestCase):
    def test_the_report_explains_every_drop(self) -> None:
        report = quality.QualityReport()
        quality.prepare(
            [
                review(),                                  # kept
                review(),                                  # duplicate
                review("short"),                           # rejected
                review("The weather is lovely this week and the garden is growing."),  # off brand
            ],
            keywords=[BRAND],
            report=report,
        )
        summary = report.as_dict()
        self.assertEqual(summary["kept"], 1)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["rejected"], 2)
        self.assertTrue(summary["reasons"])

    def test_a_record_that_cannot_be_cleaned_is_counted_not_raised(self) -> None:
        class Broken:
            brand = BRAND
            source = "web"
            source_url = "https://e.example/1"
            extra: dict = {}

            @property
            def text(self):
                raise RuntimeError("boom")

        report = quality.QualityReport()
        kept = quality.prepare([Broken()], report=report)
        self.assertEqual(kept, [])
        self.assertEqual(report.rejected, 1)


if __name__ == "__main__":
    unittest.main()
