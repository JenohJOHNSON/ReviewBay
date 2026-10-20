"""Test helpers for the login wall.

Most API tests are about what an endpoint does, not about signing in, so they
mix in `SignedIn` to run as an already-authenticated user. `test_auth_api.py`
and `test_brand_api.py` exercise the real sign-in flow end to end instead.
"""

from __future__ import annotations

import os
import unittest.mock

from reviewbot import accounts
from reviewbot.api import auth

_DB_VARS = ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL")


def reset_auth_state() -> None:
    """Clear the per-process caches and rate limiters between tests."""
    auth._SESSIONS.clear()
    auth._MEMBERS.clear()
    for limiter in (auth.LOGIN_PER_IP, auth.LOGIN_PER_EMAIL, auth.SIGNUP_PER_IP):
        limiter.clear()


def isolate_environment(testcase) -> None:
    """Never let a test reach a real database through the developer's shell."""
    env = unittest.mock.patch.dict(os.environ, {}, clear=False)
    env.start()
    testcase.addCleanup(env.stop)
    for key in _DB_VARS:
        os.environ.pop(key, None)


class SignedIn:
    """Mixin: every request in the test is made by `self.user`.

    With no store installed, membership checks reach the (unconfigured)
    database and pass the brand through, so endpoint behaviour is tested exactly
    as before accounts. Set `self.store` in a subclass's setUp, before calling
    super().setUp(), to use the in-memory store instead.
    """

    store: accounts.Store | None = None

    def setUp(self):
        super().setUp()
        isolate_environment(self)
        reset_auth_state()
        if self.store is not None:
            previous = accounts.set_store(self.store)
            self.addCleanup(accounts.set_store, previous)
        self.user = accounts.User(id=1, email="tester@example.com")
        patcher = unittest.mock.patch.object(auth, "resolve_user", return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_auth_state)
