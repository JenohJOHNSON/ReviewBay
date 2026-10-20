"""The login wall and the auth API, end to end through the real routes.

Uses the in-memory account store (held to the same contract as Postgres by
test_accounts_contract.py), the real middleware, real cookies and real password
hashing. Nothing is mocked between the HTTP request and the store.
"""

from __future__ import annotations

import os
import unittest
import unittest.mock

from auth_helpers import isolate_environment, reset_auth_state
from fastapi.testclient import TestClient
from memory_store import MemoryStore

from reviewbot import accounts
from reviewbot.api import auth, main

PASSWORD = "correct horse battery"


class AuthApiTestCase(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        reset_auth_state()
        self.addCleanup(reset_auth_state)
        os.environ.update({"SIGNUP_ENABLED": "1", "SIGNUP_INVITE_CODE": "", "ADMIN_EMAILS": "",
                           "SESSION_COOKIE_SECURE": "auto"})
        self.store = MemoryStore()
        previous = accounts.set_store(self.store)
        self.addCleanup(accounts.set_store, previous)
        for target, value in (("database_url", "postgresql://test@localhost/reviewbay_test"),):
            p = unittest.mock.patch.object(main.db, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app, follow_redirects=False)

    def signup(self, client=None, email="ann@example.com", **extra):
        client = client or self.client
        resp = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD, **extra})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp


class LoginWallTests(AuthApiTestCase):
    PROTECTED_PAGES = ("/dashboard", "/brands", "/start", "/chat", "/report", "/compare")

    def test_protected_pages_redirect_to_login_with_a_return_path(self):
        for path in self.PROTECTED_PAGES:
            with self.subTest(path=path):
                resp = self.client.get(path + "?brand=Acme", headers={"accept": "text/html"})
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(resp.headers["location"],
                                 "/login?next=" + path.replace("/", "%2F") + "%3Fbrand%3DAcme")

    def test_the_api_answers_401_rather_than_redirecting(self):
        for method, path in (("GET", "/api/stats?brand=Acme"), ("GET", "/api/me/brands"),
                             ("POST", "/chat"), ("DELETE", "/api/brands/Acme"),
                             ("GET", "/api/llm/status")):
            with self.subTest(path=path):
                resp = self.client.request(method, path, json={"question": "hi"})
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.json()["error"], "auth_required")

    def test_public_pages_stay_public(self):
        for path in ("/", "/login", "/signup", "/healthz", "/r/sometoken", "/static/theme.css"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_healthz_never_touches_the_session_store(self):
        self.signup()
        with unittest.mock.patch.object(accounts, "session_user",
                                        side_effect=AssertionError("I/O on /healthz")):
            self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_a_database_outage_is_a_503_not_a_trip_to_the_login_page(self):
        self.signup()
        auth._SESSIONS.clear()
        with unittest.mock.patch.object(accounts, "session_user",
                                        side_effect=ConnectionError("neon is down")):
            resp = self.client.get("/api/me/brands")
        self.assertEqual(resp.status_code, 503)

    def test_signed_in_visitors_to_login_go_to_their_brands(self):
        self.signup()
        resp = self.client.get("/login")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/brands"))


class SignupLoginTests(AuthApiTestCase):
    def test_signup_signs_you_in_with_a_hardened_cookie(self):
        resp = self.signup()
        cookie = resp.headers["set-cookie"]
        self.assertIn(f"{auth.SESSION_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)  # plain http in the test client
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["email"], "ann@example.com")

    def test_the_cookie_is_secure_behind_an_https_proxy(self):
        resp = self.client.post(
            "/api/auth/signup", json={"email": "ann@example.com", "password": PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        self.assertIn("Secure", resp.headers["set-cookie"])

    def test_the_cookie_is_not_what_the_database_stores(self):
        self.signup()
        token = self.client.cookies.get(auth.SESSION_COOKIE)
        self.assertNotIn(token, self.store.sessions)
        self.assertIn(accounts.token_hash(token), self.store.sessions)

    def test_a_second_signup_with_the_same_email_is_refused(self):
        self.signup()
        resp = TestClient(main.app).post(
            "/api/auth/signup", json={"email": "ANN@example.com", "password": PASSWORD})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "email_taken")

    def test_weak_input_is_explained(self):
        for body, needle in (({"email": "not-an-email", "password": PASSWORD}, "email"),
                             ({"email": "a@b.co", "password": "short"}, "8 characters")):
            with self.subTest(body=body):
                resp = self.client.post("/api/auth/signup", json=body)
                self.assertEqual(resp.status_code, 400)
                self.assertIn(needle, resp.json()["message"])

    def test_login_and_logout(self):
        self.signup()
        other = TestClient(main.app, follow_redirects=False)
        resp = other.post("/api/auth/login", json={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(other.get("/api/auth/me").status_code, 200)
        token = other.cookies.get(auth.SESSION_COOKIE)
        other.post("/api/auth/logout")
        # The session is gone server-side, not just forgotten by this browser.
        self.assertNotIn(accounts.token_hash(token), self.store.sessions)
        other.cookies.set(auth.SESSION_COOKIE, token)  # replaying the old cookie
        self.assertEqual(other.get("/api/me/brands").status_code, 401)

    def test_wrong_password_and_unknown_email_get_the_same_answer(self):
        self.signup()
        wrong = self.client.post("/api/auth/login",
                                 json={"email": "ann@example.com", "password": "nope nope nope"})
        unknown = self.client.post("/api/auth/login",
                                   json={"email": "nobody@example.com", "password": PASSWORD})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())

    def test_signup_can_be_closed(self):
        os.environ["SIGNUP_ENABLED"] = "0"
        resp = self.client.post("/api/auth/signup",
                                json={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual((resp.status_code, resp.json()["error"]), (403, "signup_closed"))
        self.assertFalse(self.client.get("/api/auth/config").json()["signup_enabled"])

    def test_an_invite_code_can_be_required(self):
        os.environ["SIGNUP_INVITE_CODE"] = "letmein"
        self.assertTrue(self.client.get("/api/auth/config").json()["invite_required"])
        for code in (None, "wrong"):
            with self.subTest(code=code):
                resp = self.client.post("/api/auth/signup", json={
                    "email": "ann@example.com", "password": PASSWORD, "invite_code": code})
                self.assertEqual(resp.status_code, 403)
        self.signup(invite_code="letmein")

    def test_auth_config_reveals_nothing_secret(self):
        os.environ["SIGNUP_INVITE_CODE"] = "letmein"
        body = self.client.get("/api/auth/config").text
        self.assertNotIn("letmein", body)


class AbuseTests(AuthApiTestCase):
    def test_password_guessing_is_rate_limited_per_email(self):
        self.signup()
        guesser = TestClient(main.app)
        statuses = [
            guesser.post("/api/auth/login",
                         json={"email": "ann@example.com", "password": f"guess-{i}-xxxx"}).status_code
            for i in range(12)
        ]
        self.assertEqual(statuses[:10], [401] * 10)
        self.assertEqual(statuses[10], 429)
        limited = guesser.post("/api/auth/login",
                               json={"email": "ann@example.com", "password": PASSWORD})
        # Even the right password waits out the window: otherwise the limit
        # would only slow down guessers who are wrong.
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in limited.headers})

    def test_a_correct_login_clears_the_strikes_for_that_address(self):
        self.signup()
        for i in range(5):
            self.client.post("/api/auth/login",
                             json={"email": "ann@example.com", "password": f"typo-{i}-xxxx"})
        self.assertEqual(self.client.post("/api/auth/login", json={
            "email": "ann@example.com", "password": PASSWORD}).status_code, 200)
        for i in range(9):
            self.assertEqual(self.client.post("/api/auth/login", json={
                "email": "ann@example.com", "password": f"typo2-{i}-xx"}).status_code, 401)

    def test_signups_are_rate_limited_per_client(self):
        codes = [self.client.post("/api/auth/signup", json={
            "email": f"user{i}@example.com", "password": PASSWORD}).status_code for i in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    def test_the_rate_limit_key_cannot_be_chosen_by_the_client(self):
        # The proxy appends the real address LAST; anything before it is
        # whatever the client claimed.
        request = unittest.mock.Mock(headers={"x-forwarded-for": "1.2.3.4, 203.0.113.9"},
                                     client=None)
        self.assertEqual(auth.client_ip(request), "203.0.113.9")

    def test_cross_site_writes_are_refused(self):
        self.signup()
        for origin in ("https://evil.example", "null"):
            with self.subTest(origin=origin):
                resp = self.client.post("/api/brands", json={"name": "Acme"},
                                        headers={"origin": origin})
                self.assertEqual((resp.status_code, resp.json()["error"]), (403, "cross_site"))

    def test_login_csrf_is_refused_too(self):
        resp = self.client.post("/api/auth/login",
                                json={"email": "ann@example.com", "password": PASSWORD},
                                headers={"origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_writes_are_allowed(self):
        self.signup()
        with unittest.mock.patch.object(main.onboarding, "start_async", return_value=True), \
                unittest.mock.patch.object(main.onboarding, "_persist_brand"):
            resp = self.client.post("/api/brands", json={"name": "Acme"},
                                    headers={"origin": "http://testserver"})
        self.assertEqual(resp.status_code, 200)

    def test_a_referer_from_another_site_counts_as_cross_site(self):
        self.signup()
        resp = self.client.post("/api/auth/logout",
                                headers={"referer": "https://evil.example/page"})
        self.assertEqual(resp.status_code, 403)


class UnmigratedDatabaseTests(AuthApiTestCase):
    """Deployed before `python scripts/migrate.py`: say so, do not 500."""

    class Undefined(Exception):
        sqlstate = "42P01"  # what psycopg reports for a missing table

    def test_signup_explains_the_missing_migration(self):
        with unittest.mock.patch.object(self.store, "create_user",
                                        side_effect=self.Undefined("app.users missing")):
            resp = self.client.post("/api/auth/signup",
                                    json={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["setup_required"], "migration")
        self.assertIn("migrate.py", resp.json()["message"])

    def test_the_sign_in_page_is_told_before_anyone_types(self):
        with unittest.mock.patch.object(self.store, "ready", return_value=False):
            self.assertFalse(self.client.get("/api/auth/config").json()["accounts"])
        self.assertTrue(self.client.get("/api/auth/config").json()["accounts"])

    def test_a_stale_cookie_is_signed_out_not_an_outage(self):
        self.client.cookies.set(auth.SESSION_COOKIE, "from-before-the-deploy")
        with unittest.mock.patch.object(self.store, "session",
                                        side_effect=self.Undefined("app.sessions missing")):
            resp = self.client.get("/brands", headers={"accept": "text/html"})
        self.assertEqual(resp.status_code, 303)

    def test_a_wrapped_schema_error_is_still_recognised(self):
        try:
            try:
                raise self.Undefined("inner")
            except Exception as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as outer:
            self.assertTrue(accounts.is_schema_missing(outer))
        self.assertFalse(accounts.is_schema_missing(RuntimeError("unrelated")))


class AdminTests(AuthApiTestCase):
    def test_sending_a_test_alert_needs_an_admin(self):
        self.signup()
        with unittest.mock.patch.object(main.alerts, "send_test", return_value=True) as send:
            self.assertEqual(self.client.post("/api/alerts/test").status_code, 403)
            send.assert_not_called()
            os.environ["ADMIN_EMAILS"] = "someone@example.com, Ann@Example.com"
            auth._SESSIONS.clear()
            self.assertEqual(self.client.post("/api/alerts/test").status_code, 200)
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
