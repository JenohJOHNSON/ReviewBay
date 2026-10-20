"""Deterministic extraction: the strategies, their precision order, and edges.

These run against the fixture pages in `tests/fixtures/`, with no network and no
HTTP server, so they stay fast and are the first place a selector regression
shows up.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from reviewbot.scraping import extract as ex

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class JsonLdTests(unittest.TestCase):
    """The highest-precision path: the page labelled its own reviews."""

    def setUp(self) -> None:
        self.result = ex.extract(fixture("jsonld_reviews.html"), "https://shop.example/widget")

    def test_jsonld_wins_over_the_other_strategies(self) -> None:
        self.assertEqual(self.result.strategy, "jsonld")

    def test_fields_come_through_exactly(self) -> None:
        first = self.result.candidates[0]
        self.assertEqual(first.author, "Dana R.")
        self.assertEqual(first.rating, 5.0)
        self.assertEqual(first.created_at, "2026-02-11")
        self.assertEqual(first.kind, "review")
        self.assertIn("Arrived two days early", first.text)

    def test_jsonld_records_are_reviews_not_mentions(self) -> None:
        self.assertTrue(all(c.kind == "review" for c in self.result.candidates))

    def test_too_short_reviews_are_dropped(self) -> None:
        """The fixture's third review is the word "ok"."""
        self.assertEqual(len(self.result.candidates), 2)
        self.assertNotIn("ok", [c.text for c in self.result.candidates])

    def test_script_content_is_read_despite_ignore_tags(self) -> None:
        """Regression: Selector.get_all_text() ignores <script> by default.

        Using it on the JSON-LD block returned "" and silently disabled the whole
        highest-precision strategy, so this asserts the block is actually read.
        """
        self.assertGreater(len(self.result.candidates), 0)


class StructuralTests(unittest.TestCase):
    """Cards found by shape, which is what survives hashed class names."""

    def setUp(self) -> None:
        self.result = ex.extract(
            fixture("structural_reviews.html"), "https://www.trustpilot.com/review/x.example"
        )

    def test_all_three_cards_are_found(self) -> None:
        self.assertEqual(len(self.result.candidates), 3)

    def test_ratings_authors_and_dates_are_extracted(self) -> None:
        ratings = sorted(c.rating for c in self.result.candidates)
        self.assertEqual(ratings, [2.0, 4.0, 5.0])
        self.assertEqual(
            sorted(c.author for c in self.result.candidates), ["Lena", "Marco", "Priya K"]
        )
        dated = [c for c in self.result.candidates if c.created_at]
        self.assertEqual(len(dated), 2)

    def test_byline_and_date_are_not_left_in_the_body(self) -> None:
        """A card's text includes its own byline; those duplicate the fields."""
        for candidate in self.result.candidates:
            self.assertNotIn("4 Mar 2026", candidate.text)
            first_line = candidate.text.split("\n")[0]
            self.assertNotEqual(first_line, candidate.author)

    def test_pagination_link_is_found_and_canonicalized(self) -> None:
        self.assertEqual(
            self.result.next_urls, ["https://www.trustpilot.com/review/northwind?page=2"]
        )


class ArticleFallbackTests(unittest.TestCase):
    def test_a_news_page_is_a_mention_not_a_review(self) -> None:
        """Counting a news article as a customer review would pad the quota."""
        result = ex.extract(fixture("article_only.html"), "https://news.example/story")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].kind, "mention")
        self.assertIsNone(result.candidates[0].rating)

    def test_boilerplate_lines_are_stripped(self) -> None:
        result = ex.extract(fixture("article_only.html"), "https://news.example/story")
        self.assertNotIn("Cookie policy", result.candidates[0].text)


class IrregularAndPaginatedTests(unittest.TestCase):
    def test_irregular_prose_yields_only_a_page_level_mention(self) -> None:
        """This is the case AI extraction exists for, and this is why.

        Blockquoted reviews in prose have no repeated card structure and no
        review markup, so deterministic extraction can only offer the whole page
        as a single mention.
        """
        result = ex.extract(fixture("irregular.html"), "https://blog.example/harbour")
        self.assertTrue(all(c.kind == "mention" for c in result.candidates))
        self.assertLessEqual(len(result.candidates), 1)

    def test_paginated_page_declares_its_next_page(self) -> None:
        result = ex.extract(fixture("paginated_page1.html"), "https://shop.example/reviews")
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(result.next_urls, ["https://shop.example/paginated_page2.html"])

    def test_last_page_declares_no_next(self) -> None:
        result = ex.extract(fixture("paginated_page3.html"), "https://shop.example/p3")
        self.assertEqual(result.next_urls, [])


class EdgeCaseTests(unittest.TestCase):
    def test_empty_page_yields_nothing_and_says_why(self) -> None:
        result = ex.extract(fixture("empty.html"), "https://e.example/empty")
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.error, "no extractable content")

    def test_empty_body_string(self) -> None:
        result = ex.extract("", "https://e.example/x")
        self.assertEqual(result.candidates, [])
        self.assertIn("empty", result.error)

    def test_malformed_html_does_not_raise(self) -> None:
        result = ex.extract(fixture("malformed.html"), "https://e.example/broken")
        self.assertEqual(result.error, "")
        self.assertGreaterEqual(len(result.candidates), 1)

    def test_a_page_of_only_navigation_is_not_a_review(self) -> None:
        html = """<html><body><nav><a href="/a">Sign in</a><a href="/b">Cookie policy</a>
                  </nav><div class="review">Accept all cookies</div></body></html>"""
        result = ex.extract(html, "https://e.example/nav")
        self.assertEqual(result.candidates, [])

    def test_record_count_is_capped(self) -> None:
        cards = "".join(
            f'<div class="review"><p>Customer number {i} found the product genuinely '
            f'useful and said the delivery was quick and well packaged.</p></div>'
            for i in range(200)
        )
        result = ex.extract(f"<html><body>{cards}</body></html>", "https://e.example/many")
        self.assertLessEqual(len(result.candidates), ex.MAX_RECORDS_PER_PAGE)

    def test_long_text_is_truncated(self) -> None:
        body = "Great product. " * 2000
        html = f'<html><body><div class="review"><p>{body}</p></div></body></html>'
        result = ex.extract(html, "https://e.example/long")
        self.assertTrue(result.candidates)
        self.assertLessEqual(len(result.candidates[0].text), ex.MAX_TEXT_CHARS + 10)


class RatingParsingTests(unittest.TestCase):
    def test_common_shapes(self) -> None:
        cases = {
            "4.5 out of 5": (4.5, 5.0),
            "Rated 4 out of 5 stars": (4.0, 5.0),
            "3/5": (3.0, 5.0),
            "5 stars": (5.0, None),
            "4,5 out of 5": (4.5, 5.0),
            4: (4.0, None),
            2.5: (2.5, None),
            "2": (2.0, None),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ex.parse_rating(raw), expected)

    def test_other_scales_are_rescaled_to_five(self) -> None:
        """models.py documents rating as 1-5, so an 8/10 must become 4.0."""
        self.assertEqual(ex.parse_rating("8 out of 10"), (4.0, 5.0))
        self.assertEqual(ex.parse_rating(90, 100), (4.5, 5.0))

    def test_unparseable_values_return_nothing_rather_than_a_guess(self) -> None:
        for raw in ("great", "", None, "N/A", "★★★", "lots"):
            with self.subTest(raw=raw):
                self.assertEqual(ex.parse_rating(raw), (None, None))

    def test_out_of_range_values_are_refused(self) -> None:
        self.assertEqual(ex.parse_rating(11), (None, None))
        self.assertEqual(ex.parse_rating(-2), (None, None))


class DateHandlingTests(unittest.TestCase):
    def test_iso_dates_are_kept_verbatim(self) -> None:
        for raw in ("2026-03-04", "2026-03-04T08:12:00Z", "2026-03-04T08:12:00+02:00"):
            with self.subTest(raw=raw):
                self.assertEqual(ex._clean_date(raw), raw)

    def test_relative_dates_are_dropped_not_converted(self) -> None:
        """Converting "6 years ago" would invent a precision the page never gave."""
        for raw in ("6 years ago", "yesterday", "last week", "Mar 4", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(ex._clean_date(raw))


class SiteStrategyTests(unittest.TestCase):
    def test_strategies_load_from_the_shipped_config(self) -> None:
        strategies = ex.site_strategies("config/extraction_strategies.yml")
        self.assertTrue(strategies)
        self.assertTrue(any(s.name == "trustpilot" for s in strategies))

    def test_a_strategy_matches_its_domain_and_subdomains(self) -> None:
        strategy = next(
            s for s in ex.site_strategies("config/extraction_strategies.yml")
            if s.name == "trustpilot"
        )
        self.assertTrue(strategy.matches("https://www.trustpilot.com/review/x"))
        self.assertTrue(strategy.matches("https://uk.trustpilot.com/review/x"))
        self.assertFalse(strategy.matches("https://notrustpilot.example/review/x"))

    def test_a_missing_config_file_disables_site_strategies_quietly(self) -> None:
        self.assertEqual(ex.site_strategies("config/does-not-exist.yml"), [])

    def test_a_malformed_config_does_not_raise(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8") as fh:
            fh.write("strategies: [{name: broken}]\n")  # no domains, no container
            path = fh.name
        self.assertEqual(ex.site_strategies(path), [])

    def test_the_site_strategy_is_used_when_it_matches(self) -> None:
        result = ex.extract(
            fixture("structural_reviews.html"),
            "https://www.trustpilot.com/review/northwind.example",
            strategies=ex.site_strategies("config/extraction_strategies.yml"),
        )
        self.assertEqual(result.strategy, "site:trustpilot")
        self.assertEqual(len(result.candidates), 3)


if __name__ == "__main__":
    unittest.main()
