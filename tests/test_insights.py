from __future__ import annotations

import unittest
from unittest.mock import patch

from reviewbot.api import insights


ROWS = [
    {"text": "The app is fast and pickup is easy", "source": "app_store", "rating": 5, "sentiment": "positive"},
    {"text": "Fast pickup and friendly staff", "source": "google_maps", "rating": 5, "sentiment": "positive"},
    {"text": "The app crashes and support is slow", "source": "google_play", "rating": 1, "sentiment": "negative"},
    {"text": "Slow support after the app crashes", "source": "reddit", "rating": None, "sentiment": "negative"},
]


class InsightsTests(unittest.TestCase):
    def test_generate_returns_dashboard_shape(self) -> None:
        data = insights._generate(ROWS)

        self.assertIn("summary", data)
        self.assertIn("pros", data)
        self.assertIn("cons", data)
        self.assertIn("themes", data)
        self.assertIn("behavior", data)
        self.assertEqual(data["reviews_analyzed"], len(ROWS))
        self.assertIsInstance(data["themes"], list)

    def test_empty_get_insights_shape(self) -> None:
        with patch.object(insights, "_sample_reviews", return_value=[]):
            data = insights.get_insights("No Reviews", refresh=True)

        self.assertEqual(data["reviews_analyzed"], 0)
        self.assertEqual(data["pros"], [])
        self.assertEqual(data["cons"], [])


if __name__ == "__main__":
    unittest.main()
