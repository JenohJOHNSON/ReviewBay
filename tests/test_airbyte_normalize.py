from __future__ import annotations

import unittest

from reviewbot.airbyte.normalize import map_airbyte_row


class AirbyteNormalizeTests(unittest.TestCase):
    def test_google_maps_row_maps_to_stable_review(self) -> None:
        row = {
            "text": "Friendly staff and fast pickup.",
            "reviewUrl": "https://maps.example/review/1",
            "stars": 5,
            "name": "Ava",
        }

        first = map_airbyte_row("Blue Bottle", "google_maps", "google_maps", row)[0]
        second = map_airbyte_row("Blue Bottle", "google_maps", "google_maps", row)[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.brand, "Blue Bottle")
        self.assertEqual(first.source, "google_maps")
        self.assertEqual(first.rating, 5.0)

    def test_yelp_and_reddit_rows_map(self) -> None:
        yelp = map_airbyte_row(
            "Blue Bottle",
            "yelp",
            "yelp",
            {"text": "Great espresso.", "url": "https://yelp.example/r/1", "rating": 4},
        )
        reddit = map_airbyte_row(
            "Blue Bottle",
            "reddit",
            "reddit",
            {"title": "Worth it?", "body": "The queue is long.", "url": "https://reddit.example/p/1"},
        )

        self.assertEqual(yelp[0].source, "yelp")
        self.assertIn("Worth it?", reddit[0].text)

    def test_web_serp_row_expands_organic_results(self) -> None:
        reviews = map_airbyte_row(
            "Blue Bottle",
            "web",
            "web",
            {
                "organicResults": [
                    {"url": "https://example.com/a", "title": "Review A", "description": "Loved it"},
                    {"url": "https://example.com/b", "title": "Review B", "description": "Too slow"},
                ]
            },
        )

        self.assertEqual(len(reviews), 2)
        self.assertEqual({r.source for r in reviews}, {"web"})


if __name__ == "__main__":
    unittest.main()
