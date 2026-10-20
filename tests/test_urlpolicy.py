"""The SSRF guard. These are the tests that must never be allowed to regress.

`POST /api/brands` takes a URL from whoever is using the product and the
scraping engines then fetch it, so a hole here is a server-side request forgery
with the user holding the trigger. On Railway that reaches the container's
loopback, the platform's internal service mesh, and on providers that expose one
the metadata endpoint that hands out credentials.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from reviewbot.scraping import urlpolicy


class BlockedTargetTests(unittest.TestCase):
    """Everything in here must be refused."""

    def test_loopback_is_refused(self) -> None:
        for url in (
            "http://127.0.0.1/",
            "http://127.0.0.1:8000/admin",
            "https://127.255.255.254/",
            "http://[::1]/",
            "http://localhost/",
            "http://LOCALHOST/",
            "http://localhost.localdomain/",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_cloud_metadata_endpoints_are_refused(self) -> None:
        for url in (
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://100.100.100.200/latest/meta-data/",
            "http://192.0.0.192/opc/v1/instance/",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_private_ranges_are_refused(self) -> None:
        for url in (
            "http://10.0.0.1/", "http://10.255.255.255/",
            "http://172.16.0.1/", "http://172.31.255.255/",
            "http://192.168.0.1/", "http://192.168.255.255/",
            "http://0.0.0.0/",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_ipv6_embedded_ipv4_is_refused(self) -> None:
        """A mapped or translated IPv6 address reaching a private IPv4 host.

        `::ffff:127.0.0.1` is not `is_loopback` on the IPv6 object in every
        Python version, so this is checked by unwrapping the embedded address.
        """
        for url in (
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:10.0.0.1]/",
            "http://[::ffff:192.168.1.1]/",
            "http://[fc00::1]/",
            "http://[fe80::1]/",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_internal_hostnames_are_refused(self) -> None:
        for url in (
            "http://api.railway.internal/",
            "http://db.internal/",
            "http://service.cluster.local/",
            "http://printer.local/",
            "http://thing.home.arpa/",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_non_http_schemes_are_refused(self) -> None:
        for url in (
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/",
            "dict://example.com:11211/",
            "ldap://example.com/",
            "jar:http://example.com!/",
            "data:text/html,<script>1</script>",
            "//example.com/no-scheme",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_credentials_in_url_are_refused(self) -> None:
        self.assertFalse(urlpolicy.check("https://user:pass@example.com/").allowed)
        self.assertFalse(urlpolicy.check("https://admin@169.254.169.254/").allowed)

    def test_non_web_ports_are_refused(self) -> None:
        for url in ("https://example.com:22/", "https://example.com:6379/",
                    "https://example.com:5432/", "https://example.com:11211/"):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_request_splitting_characters_are_refused(self) -> None:
        for url in (
            "https://example.com/\r\nHost: evil.com",
            "https://example.com/\npath",
            "https://exa mple.com/",
            "https://example.com/\x00",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_html_injection_characters_are_refused(self) -> None:
        """A stored source_url becomes an href on the dashboard and in the chat.

        An unencoded quote in it is an attribute-injection primitive, so these
        are refused at the boundary as well as escaped at render time.
        """
        for url in (
            'https://e.example/r/1"onmouseover="alert(1)',
            "https://e.example/r/1'onclick='x",
            "https://e.example/<script>",
            "https://e.example/`backtick`",
        ):
            with self.subTest(url=url):
                verdict = urlpolicy.check(url)
                self.assertFalse(verdict.allowed, url)
                self.assertIn("percent-encoded", verdict.reason)

    def test_injection_characters_with_whitespace_are_also_refused(self) -> None:
        """Whichever check fires first, the URL must not be allowed."""
        for url in (
            'https://e.example/r/1" onmouseover="alert(1)',
            "https://e.example/r/1' onclick='x",
        ):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)

    def test_properly_encoded_urls_are_still_allowed(self) -> None:
        with patch("socket.getaddrinfo", return_value=DnsRebindingTests._addrinfo("93.184.216.34")):
            self.assertTrue(
                urlpolicy.check("https://e.example/search?q=%22quoted%22&a=%3Cb%3E").allowed
            )

    def test_unresolvable_host_is_refused(self) -> None:
        """Deny by default: a host whose wishes we cannot even look up."""
        verdict = urlpolicy.check("https://nope-zzz-9q8w7e6r5t.invalid/")
        self.assertFalse(verdict.allowed)
        self.assertIn("does not resolve", verdict.reason)

    def test_empty_and_garbage_input(self) -> None:
        for url in ("", "   ", "not a url", "http://", "https://:80/"):
            with self.subTest(url=url):
                self.assertFalse(urlpolicy.check(url).allowed, url)


class DnsRebindingTests(unittest.TestCase):
    """A name that resolves to a private address must be refused.

    Validating the *name* is not enough: `evil.com` can have an A record
    pointing at 127.0.0.1, and a name can resolve to one public and one private
    address. Every resolved address has to be public.
    """

    @staticmethod
    def _addrinfo(*addresses):
        import socket

        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]

    def test_name_resolving_to_private_address_is_refused(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._addrinfo("127.0.0.1")):
            verdict = urlpolicy.check("https://evil.example/")
        self.assertFalse(verdict.allowed)
        self.assertIn("loopback", verdict.reason)

    def test_name_resolving_to_metadata_address_is_refused(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._addrinfo("169.254.169.254")):
            verdict = urlpolicy.check("https://innocent.example/")
        self.assertFalse(verdict.allowed)

    def test_mixed_public_and_private_addresses_are_refused(self) -> None:
        """One bad address is enough: the connect might pick it."""
        with patch("socket.getaddrinfo", return_value=self._addrinfo("93.184.216.34", "10.0.0.5")):
            verdict = urlpolicy.check("https://mixed.example/")
        self.assertFalse(verdict.allowed)
        self.assertIn("private", verdict.reason)

    def test_all_public_addresses_are_allowed(self) -> None:
        with patch("socket.getaddrinfo", return_value=self._addrinfo("93.184.216.34", "93.184.216.35")):
            verdict = urlpolicy.check("https://fine.example/")
        self.assertTrue(verdict.allowed, verdict.reason)
        self.assertEqual(verdict.addresses, ("93.184.216.34", "93.184.216.35"))


class AllowedTargetTests(unittest.TestCase):
    def test_ordinary_public_pages_are_allowed(self) -> None:
        addrinfo = DnsRebindingTests._addrinfo("93.184.216.34")
        for url in (
            "https://www.trustpilot.com/review/example.com",
            "http://example.com/reviews?page=2",
            "https://shop.example.co.uk/product/1#reviews",
            "https://example.com:8443/reviews",
        ):
            with self.subTest(url=url), patch("socket.getaddrinfo", return_value=addrinfo):
                self.assertTrue(urlpolicy.check(url).allowed, url)

    def test_syntactic_only_check_skips_dns(self) -> None:
        """`resolve=False` is the cheap pre-filter over a big list of links."""
        with patch("socket.getaddrinfo", side_effect=AssertionError("must not resolve")):
            self.assertTrue(urlpolicy.check("https://example.com/x", resolve=False).allowed)


class DevModeTests(unittest.TestCase):
    """The local-development escape hatch needs BOTH variables."""

    def test_one_variable_alone_does_not_open_the_guard(self) -> None:
        with patch.dict("os.environ", {"ALLOW_PRIVATE_FETCH": "1"}, clear=False), \
             patch.dict("os.environ", {"REVIEWBAY_ENV": "production"}, clear=False):
            self.assertFalse(urlpolicy.check("http://127.0.0.1:8000/").allowed)

    def test_both_variables_allow_loopback(self) -> None:
        with patch.dict(
            "os.environ",
            {"ALLOW_PRIVATE_FETCH": "1", "REVIEWBAY_ENV": "dev"},
            clear=False,
        ):
            self.assertTrue(urlpolicy.check("http://127.0.0.1:54321/x").allowed)

    def test_metadata_endpoint_is_refused_even_in_dev(self) -> None:
        """Dev mode relaxes private ranges; it never unlocks metadata."""
        with patch.dict(
            "os.environ",
            {"ALLOW_PRIVATE_FETCH": "1", "REVIEWBAY_ENV": "dev"},
            clear=False,
        ):
            self.assertFalse(
                urlpolicy.check("http://169.254.169.254/latest/meta-data/").allowed
            )


class RequireTests(unittest.TestCase):
    def test_require_raises_on_a_blocked_url(self) -> None:
        with self.assertRaises(urlpolicy.UrlNotAllowed):
            urlpolicy.require("http://169.254.169.254/")

    def test_require_returns_the_url_when_allowed(self) -> None:
        with patch("socket.getaddrinfo", return_value=DnsRebindingTests._addrinfo("93.184.216.34")):
            self.assertEqual(
                urlpolicy.require("https://example.com/a"), "https://example.com/a"
            )


class CanonicalTests(unittest.TestCase):
    def test_tracking_parameters_are_dropped(self) -> None:
        self.assertEqual(
            urlpolicy.canonical(
                "https://site.example/r/1?utm_source=news&utm_campaign=x&fbclid=y&id=7"
            ),
            "https://site.example/r/1?id=7",
        )

    def test_host_and_scheme_are_lowercased_and_default_port_dropped(self) -> None:
        self.assertEqual(
            urlpolicy.canonical("HTTPS://WWW.Example.COM:443/Path/"),
            "https://www.example.com/Path",
        )

    def test_query_order_does_not_change_identity(self) -> None:
        self.assertEqual(
            urlpolicy.canonical("https://e.example/p?b=2&a=1"),
            urlpolicy.canonical("https://e.example/p?a=1&b=2"),
        )

    def test_review_anchors_are_preserved(self) -> None:
        """app_store.py relies on the fragment to make each citation unique."""
        url = "https://apps.apple.com/us/app/id1?see-all=reviews#review-9876"
        self.assertEqual(urlpolicy.canonical(url), url)

    def test_other_fragments_are_dropped(self) -> None:
        self.assertEqual(
            urlpolicy.canonical("https://e.example/p#section-2"), "https://e.example/p"
        )

    def test_path_case_is_preserved(self) -> None:
        """Paths are case-sensitive on most servers; only the host is folded."""
        self.assertEqual(
            urlpolicy.canonical("https://e.example/CaseSensitive"),
            "https://e.example/CaseSensitive",
        )


class RegistrableDomainTests(unittest.TestCase):
    def test_subdomains_fold_to_the_registrable_domain(self) -> None:
        self.assertEqual(urlpolicy.registrable_domain("https://www.reddit.com/r/x"), "reddit.com")
        self.assertEqual(
            urlpolicy.registrable_domain("https://news.ycombinator.com/item"), "ycombinator.com"
        )

    def test_bare_host_input(self) -> None:
        self.assertEqual(urlpolicy.registrable_domain("shop.example.com"), "example.com")

    def test_ip_is_its_own_key(self) -> None:
        self.assertEqual(urlpolicy.registrable_domain("http://127.0.0.1:8000/x"), "127.0.0.1")

    def test_empty_input(self) -> None:
        self.assertEqual(urlpolicy.registrable_domain(""), "")


if __name__ == "__main__":
    unittest.main()
