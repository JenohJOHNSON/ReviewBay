"""An in-memory `accounts.Store`, for tests that must not need a database.

It mirrors PostgresStore's semantics, including the parts that are easy to get
subtly wrong: the unique index on email_key, the foreign key from memberships to
brands, "first spelling wins" on brand upsert, and newest-first ordering.
`tests/test_accounts_contract.py` runs one suite against this AND against a real
Postgres, which is what keeps the two honest.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone

from reviewbot import accounts


class MemoryStore(accounts.Store):
    def __init__(self):
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._tick = itertools.count(1)  # stands in for added_at ordering
        self.users: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.brands: dict[str, dict] = {}
        self.member_rows: dict[tuple[int, str], int] = {}
        #: brand_key -> review count, so account_brands() has something to report
        self.review_counts: dict[str, int] = {}

    # users -------------------------------------------------------------------
    def create_user(self, email, email_key, password_hash):
        with self._lock:
            if any(u["email_key"] == email_key for u in self.users.values()):
                raise accounts._UniqueViolation()
            uid = next(self._ids)
            self.users[uid] = {
                "id": uid, "email": email, "email_key": email_key,
                "password_hash": password_hash, "last_login_at": None,
            }
            return accounts.User(id=uid, email=email)

    def user_by_email(self, email_key):
        with self._lock:
            for u in self.users.values():
                if u["email_key"] == email_key:
                    return accounts.User(id=u["id"], email=u["email"]), u["password_hash"]
            return None

    def set_password_hash(self, user_id, password_hash):
        with self._lock:
            self.users[user_id]["password_hash"] = password_hash

    def touch_login(self, user_id):
        with self._lock:
            self.users[user_id]["last_login_at"] = datetime.now(timezone.utc)

    # sessions ----------------------------------------------------------------
    def create_session(self, token_hash, user_id, expires_at):
        with self._lock:
            if user_id not in self.users:
                raise KeyError("foreign key: no such user")
            now = datetime.now(timezone.utc)
            self.sessions[token_hash] = {
                "user_id": user_id, "expires_at": expires_at,
                "created_at": now, "last_seen_at": now,
            }

    def session(self, token_hash, now):
        with self._lock:
            s = self.sessions.get(token_hash)
            if not s or not s["expires_at"] > now:
                return None
            u = self.users[s["user_id"]]
            return accounts.User(id=u["id"], email=u["email"]), s["expires_at"], s["last_seen_at"]

    def extend_session(self, token_hash, expires_at, now):
        with self._lock:
            if token_hash in self.sessions:
                self.sessions[token_hash].update(expires_at=expires_at, last_seen_at=now)

    def delete_session(self, token_hash):
        with self._lock:
            self.sessions.pop(token_hash, None)

    def delete_expired_sessions(self, now):
        with self._lock:
            dead = [k for k, s in self.sessions.items() if s["expires_at"] <= now]
            for k in dead:
                del self.sessions[k]
            return len(dead)

    # brand registry ------------------------------------------------------------
    def upsert_brand(self, cfg):
        with self._lock:
            key = accounts.brand_key(cfg["name"])
            existing = self.brands.get(key)
            if existing:
                existing["active"] = True
                if not existing["website"]:
                    existing["website"] = cfg.get("website")
                return dict(existing)
            row = {
                "brand_key": key,
                "name": cfg["name"].strip(),
                "website": cfg.get("website"),
                "keywords": list(cfg.get("keywords") or [cfg["name"].strip()]),
                "sources": list(cfg.get("sources") or []),
                "limit": cfg.get("limit"),
                "active": True,
                "created_at": datetime.now(timezone.utc),
                "last_collected_at": None,
                "last_rerun_at": None,
                "_order": next(self._tick),
            }
            self.brands[key] = row
            return self._public(row)

    def get_brand(self, key):
        with self._lock:
            row = self.brands.get(key)
            return self._public(row) if row else None

    def set_brand_active(self, key, active):
        with self._lock:
            if key in self.brands:
                self.brands[key]["active"] = bool(active)

    def stamp_brand(self, key, column):
        if column not in accounts._STAMPABLE:
            raise ValueError(f"cannot stamp {column}")
        with self._lock:
            if key in self.brands:
                self.brands[key][column] = datetime.now(timezone.utc)

    def registry(self, include_inactive=False):
        with self._lock:
            rows = sorted(self.brands.values(), key=lambda r: r["_order"])
            return [self._public(r) for r in rows if include_inactive or r["active"]]

    # membership --------------------------------------------------------------
    def add_membership(self, user_id, key):
        with self._lock:
            if key not in self.brands:
                raise KeyError("foreign key: no such brand")
            if (user_id, key) in self.member_rows:
                return False
            self.member_rows[(user_id, key)] = next(self._tick)
            return True

    def remove_membership(self, user_id, key):
        with self._lock:
            return self.member_rows.pop((user_id, key), None) is not None

    def membership_keys(self, user_id):
        with self._lock:
            mine = [(order, key) for (uid, key), order in self.member_rows.items() if uid == user_id]
            return [key for _order, key in sorted(mine, reverse=True)]

    def memberships(self, user_id):
        with self._lock:
            return [(key, self.brands[key]["name"]) for key in self.membership_keys(user_id)]

    def account_brands(self, user_id):
        with self._lock:
            out = []
            for key in self.membership_keys(user_id):
                row = self._public(self.brands[key])
                row["added_at"] = None
                row["reviews"] = self.review_counts.get(key, 0)
                out.append(row)
            return out

    def tracker_count(self, key):
        with self._lock:
            return sum(1 for (_uid, k) in self.member_rows if k == key)

    @staticmethod
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}
