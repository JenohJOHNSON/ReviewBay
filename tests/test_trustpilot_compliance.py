"""The Trustpilot connector's SSRF and robots.txt gates.

This connector predates `reviewbot.scraping`, so neither the URL policy nor the
robots check applied to it: it derived a URL from user-supplied input, sent a
spoofed desktop-Chrome user agent, and fetched. The Scrapling engine does all
three correctly, which made the project's stated robots compliance depend on
which code path happened to run.

These tests stop before any browser is launched, which is also where the gates
themselves sit — a disallowed page should cost nothing, not a Chromium start-up.
Neither playwright nor selectolax is needed to run them.
"""

from __future__ import annotations

import os
import unittest
import unittest.mock

from reviewbot.connectors import playwright_source as tp
from reviewbot.scraping import urlpolicy


def allow(url: str, **_kw) -> urlpolicy.UrlVerdict:
    return urlpolicy.UrlVerdict(allowed=True, url=url, host="www.trustpilot.com")


def deny(reason: str):
    def _deny(url: str, **_kw) -> urlpolicy.UrlVerdict:
        return urlpolicy.UrlVerdict(allowed=False, url=url, reason=reason)

    return _deny


class UrlDerivationTests(unittest.TestCase):
    def test_domain_is_taken_from_the_website(self):
        self.assertEqual(
            tp._page_url("Acme", "https://www.acme.com/pricing"),
            "https://www.trustpilot.com/review/acme.com",
        )

    def test_no_website_means_no_url(self):
        self.assertIsNone(tp._page_url("Acme", None))

    def test_env_override_wins(self):
        with unittest.mock.patch.dict(
            os.environ, {"TRUSTPILOT_URL_ACME": "https://www.trustpilot.com/review/other.com"}
        ):
            self.assertEqual(
                tp._page_url("Acme", "https://acme.com"),
                "https://www.trustpilot.com/review/other.com",
            )


class GateTests(unittest.TestCase):
    def _fetch(self, brand="Acme", website="https://acme.com", limit=10):
        return list(tp.TrustpilotConnector().fetch(brand, [], limit, website=website))

    def test_no_url_yields_nothing(self):
        self.assertEqual(self._fetch(website=None), [])

    def test_url_policy_rejection_stops_the_fetch(self):
        with unittest.mock.patch.object(tp.urlpolicy, "check", deny("private address")):
            with unittest.mock.patch.object(tp.robots, "can_fetch") as can_fetch:
                with self.assertLogs("reviewbot.connectors.playwright_source", "WARNING") as logs:
                    self.assertEqual(self._fetch(), [])
        self.assertIn("refusing", " ".join(logs.output))
        # The policy check must come first; robots is not even consulted for a
        # URL we are not allowed to resolve.
        can_fetch.assert_not_called()

    def test_an_env_override_pointing_at_localhost_is_refused(self):
        # The real policy, not a stub: this is the SSRF case the gate exists for.
        with unittest.mock.patch.dict(
            os.environ, {"TRUSTPILOT_URL_ACME": "http://127.0.0.1:8000/admin"}
        ):
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ALLOW_PRIVATE_FETCH", None)
                with self.assertLogs("reviewbot.connectors.playwright_source", "WARNING"):
                    self.assertEqual(self._fetch(), [])

    def test_an_env_override_pointing_at_cloud_metadata_is_refused(self):
        with unittest.mock.patch.dict(
            os.environ, {"TRUSTPILOT_URL_ACME": "http://169.254.169.254/latest/meta-data/"}
        ):
            os.environ.pop("ALLOW_PRIVATE_FETCH", None)
            with self.assertLogs("reviewbot.connectors.playwright_source", "WARNING"):
                self.assertEqual(self._fetch(), [])

    def test_robots_disallow_stops_the_fetch_before_a_browser_starts(self):
        with unittest.mock.patch.object(tp.urlpolicy, "check", allow):
            with unittest.mock.patch.object(tp.robots, "requested_delay_exceeds_limit",
                                            lambda _u: False):
                with unittest.mock.patch.object(tp.robots, "can_fetch", lambda _u: False):
                    with self.assertLogs(
                        "reviewbot.connectors.playwright_source", "WARNING"
                    ) as logs:
                        self.assertEqual(self._fetch(), [])
        joined = " ".join(logs.output)
        self.assertIn("robots.txt disallows", joined)
        # The message has to say what to do instead, or the next person reads it
        # as a breakage and removes the check.
        self.assertIn("Apify", joined)

    def test_an_excessive_crawl_delay_stops_the_fetch(self):
        with unittest.mock.patch.object(tp.urlpolicy, "check", allow):
            with unittest.mock.patch.object(tp.robots, "requested_delay_exceeds_limit",
                                            lambda _u: True):
                with unittest.mock.patch.object(tp.robots, "can_fetch") as can_fetch:
                    with self.assertLogs(
                        "reviewbot.connectors.playwright_source", "WARNING"
                    ) as logs:
                        self.assertEqual(self._fetch(), [])
        self.assertIn("crawl delay", " ".join(logs.output))
        can_fetch.assert_not_called()

    def test_passing_both_gates_reaches_the_dependency_check(self):
        # playwright and selectolax are not installed in this environment, so
        # getting as far as that warning proves both gates were cleared.
        with unittest.mock.patch.object(tp.urlpolicy, "check", allow):
            with unittest.mock.patch.object(tp.robots, "requested_delay_exceeds_limit",
                                            lambda _u: False):
                with unittest.mock.patch.object(tp.robots, "can_fetch", lambda _u: True):
                    with unittest.mock.patch.object(tp.robots, "crawl_delay", lambda _u: 0.0):
                        with self.assertLogs(
                            "reviewbot.connectors.playwright_source", "WARNING"
                        ) as logs:
                            self.assertEqual(self._fetch(), [])
        self.assertIn("optional deps missing", " ".join(logs.output))


class UserAgentTests(unittest.TestCase):
    def test_the_connector_identifies_itself_honestly(self):
        # A spoofed browser UA is how a crawler evades rules written for
        # crawlers, and it would make the robots check above evaluate a token we
        # do not actually send.
        self.assertEqual(tp._UA, tp.robots.USER_AGENT)
        self.assertIn("ReviewBay", tp._UA)
        self.assertNotIn("Mozilla", tp._UA)


if __name__ == "__main__":
    unittest.main()
