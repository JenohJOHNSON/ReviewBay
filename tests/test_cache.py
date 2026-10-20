"""Tests for the bounded TTL cache and for the two modules that use it."""

from __future__ import annotations

import threading
import time
import unittest
import unittest.mock

from reviewbot.api import cache


class TTLCacheTests(unittest.TestCase):
    def test_round_trip(self):
        c = cache.TTLCache(ttl=60)
        c.put("a", {"v": 1})
        self.assertEqual(c.get("a"), {"v": 1})

    def test_miss_returns_none(self):
        self.assertIsNone(cache.TTLCache(ttl=60).get("nope"))

    def test_expired_entry_is_a_miss_and_is_removed(self):
        c = cache.TTLCache(ttl=60)
        c.put("a", 1)
        # Reach past the TTL without sleeping for it.
        with unittest.mock.patch.object(cache.time, "time", return_value=time.time() + 61):
            self.assertIsNone(c.get("a"))
        # The old implementation left expired entries in the dict forever; the
        # point of this one is that the entry is gone, not merely ignored.
        self.assertEqual(len(c), 0)

    def test_entry_within_ttl_survives(self):
        c = cache.TTLCache(ttl=60)
        c.put("a", 1)
        with unittest.mock.patch.object(cache.time, "time", return_value=time.time() + 59):
            self.assertEqual(c.get("a"), 1)

    def test_capacity_is_bounded(self):
        c = cache.TTLCache(ttl=600, maxsize=3)
        for i in range(50):
            c.put(f"brand-{i}", {"i": i})
        self.assertEqual(len(c), 3)
        self.assertEqual(c.stats()["evictions"], 47)

    def test_eviction_is_least_recently_used_not_oldest_written(self):
        c = cache.TTLCache(ttl=600, maxsize=3)
        c.put("a", 1)
        c.put("b", 2)
        c.put("c", 3)
        c.get("a")          # 'a' becomes the most recently used
        c.put("d", 4)       # forces one eviction
        self.assertEqual(c.get("a"), 1)     # kept, because it was just read
        self.assertIsNone(c.get("b"))       # evicted, least recently used
        self.assertEqual(c.get("c"), 3)
        self.assertEqual(c.get("d"), 4)

    def test_rewriting_a_key_does_not_grow_the_cache(self):
        c = cache.TTLCache(ttl=600, maxsize=4)
        for _ in range(20):
            c.put("same", 1)
        self.assertEqual(len(c), 1)

    def test_invalidate_removes_one_entry_and_reports_whether_it_existed(self):
        c = cache.TTLCache(ttl=600)
        c.put("a", 1)
        self.assertTrue(c.invalidate("a"))
        self.assertFalse(c.invalidate("a"))
        self.assertIsNone(c.get("a"))

    def test_clear_empties_the_cache(self):
        c = cache.TTLCache(ttl=600)
        c.put("a", 1)
        c.put("b", 2)
        c.clear()
        self.assertEqual(len(c), 0)

    def test_maxsize_below_one_is_floored_so_caching_is_not_silently_disabled(self):
        c = cache.TTLCache(ttl=600, maxsize=0)
        c.put("a", 1)
        self.assertEqual(c.get("a"), 1)

    def test_zero_ttl_means_nothing_is_ever_served_from_cache(self):
        c = cache.TTLCache(ttl=0)
        c.put("a", 1)
        self.assertIsNone(c.get("a"))

    def test_negative_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            cache.TTLCache(ttl=-1)

    def test_hit_and_miss_counters(self):
        c = cache.TTLCache(ttl=600)
        c.put("a", 1)
        c.get("a")
        c.get("a")
        c.get("b")
        s = c.stats()
        self.assertEqual((s["hits"], s["misses"]), (2, 1))

    def test_concurrent_writes_do_not_exceed_maxsize(self):
        c = cache.TTLCache(ttl=600, maxsize=8)
        errors: list[BaseException] = []

        def hammer(offset: int):
            try:
                for i in range(200):
                    c.put(f"k{offset}-{i}", i)
                    c.get(f"k{offset}-{i}")
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(c), 8)


class ModuleCacheWiringTests(unittest.TestCase):
    """The two call sites, which is where the leak actually was."""

    def test_insights_cache_is_bounded(self):
        from reviewbot.api import insights

        self.assertIsInstance(insights._CACHE, cache.TTLCache)
        self.assertGreaterEqual(insights._CACHE.maxsize, 1)

    def test_report_cache_is_bounded(self):
        from reviewbot.api import report

        self.assertIsInstance(report._CACHE, cache.TTLCache)
        self.assertGreaterEqual(report._CACHE.maxsize, 1)

    def test_insights_serves_a_second_call_from_cache(self):
        from reviewbot.api import insights

        insights._CACHE.clear()
        calls = []

        def fake_sample(brand):
            calls.append(brand)
            return [{"text": "good", "sentiment": "positive", "source": "web"}]

        with unittest.mock.patch.object(insights, "_sample_reviews", fake_sample):
            with unittest.mock.patch.object(
                insights, "_generate", lambda b, r: {"summary": "s", "pros": [], "cons": [],
                                                     "themes": [], "behavior": ""}
            ):
                first = insights.get_insights("Acme")
                second = insights.get_insights("Acme")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second call should have been served from cache")

    def test_insights_refresh_bypasses_the_cache(self):
        from reviewbot.api import insights

        insights._CACHE.clear()
        calls = []

        def fake_sample(brand):
            calls.append(brand)
            return [{"text": "good", "sentiment": "positive", "source": "web"}]

        with unittest.mock.patch.object(insights, "_sample_reviews", fake_sample):
            with unittest.mock.patch.object(
                insights, "_generate", lambda b, r: {"summary": "s", "pros": [], "cons": [],
                                                     "themes": [], "behavior": ""}
            ):
                insights.get_insights("Acme")
                insights.get_insights("Acme", refresh=True)
        self.assertEqual(len(calls), 2)

    def test_many_distinct_brands_do_not_grow_the_insights_cache_without_bound(self):
        from reviewbot.api import insights

        insights._CACHE.clear()
        with unittest.mock.patch.object(insights, "_sample_reviews", lambda b: []):
            for i in range(insights._CACHE.maxsize * 3):
                insights.get_insights(f"brand-{i}")
        # This is the leak: brand is a request parameter, so an unbounded dict
        # grew one permanent entry per distinct string ever asked for.
        self.assertLessEqual(len(insights._CACHE), insights._CACHE.maxsize)


if __name__ == "__main__":
    unittest.main()
