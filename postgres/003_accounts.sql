-- ReviewBay migration 003 — accounts, sessions, and a durable brand registry.
--
-- Apply with:   python scripts/migrate.py
--
-- ADDITIVE and idempotent, like 002: new tables in a new `app` schema, plus one
-- nullable column on marts.saved_reports. Nothing existing is dropped, renamed or
-- retyped, so an older build keeps working against a migrated database.

CREATE SCHEMA IF NOT EXISTS app;   -- accounts and the brand registry

-- ---------------------------------------------------------------------------
-- 1. Users.
--
-- `email_key` is the lowercased, trimmed address and is what uniqueness is
-- enforced on, so Ann@Example.com and ann@example.com are one account. `email`
-- keeps what the person typed, for display.
--
-- `password_hash` is a self-describing scrypt string
-- (scrypt$<n>$<r>$<p>$<salt>$<hash>), so the cost parameters can be raised later
-- and old hashes upgraded on the next successful login.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.users (
    id             bigserial PRIMARY KEY,
    email          text NOT NULL,
    email_key      text NOT NULL,
    password_hash  text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_key_idx ON app.users (email_key);

-- ---------------------------------------------------------------------------
-- 2. Sessions.
--
-- The cookie carries a random token; only its SHA-256 is stored, so a leaked
-- copy of this table cannot be replayed as a login. Deleting a user removes
-- their sessions with them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.sessions (
    token_hash    text PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx    ON app.sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON app.sessions (expires_at);

-- ---------------------------------------------------------------------------
-- 3. The brand registry.
--
-- One row per brand ANYONE tracks, keyed by the lowercased name. This replaces
-- config/brands.dynamic.yml as the source of truth for what the worker
-- refreshes. That file lives on a container's local disk, which on Railway is
-- wiped on every redeploy and is not shared with a separately deployed worker,
-- so a brand added through the website was never refreshed there and was
-- forgotten by the next deploy.
--
-- Keying on the lowercased name also gives every brand ONE canonical spelling:
-- the first person to add it sets it. That matters because review ids are
-- hashed from the brand name case-sensitively (models.stable_id), so "nike"
-- and "Nike" collected separately would store every review twice.
--
-- `active` goes FALSE when the last account tracking a brand removes it, which
-- stops scheduled refresh. The reviews themselves are kept, so adding the brand
-- back shows its history immediately.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.brands (
    brand_key          text PRIMARY KEY,
    name               text NOT NULL,
    website            text,
    keywords           jsonb NOT NULL DEFAULT '[]'::jsonb,
    sources            jsonb NOT NULL DEFAULT '[]'::jsonb,
    fetch_limit        integer,
    active             boolean NOT NULL DEFAULT TRUE,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_collected_at  timestamptz,
    last_rerun_at      timestamptz
);
CREATE INDEX IF NOT EXISTS brands_active_idx ON app.brands (active);

-- ---------------------------------------------------------------------------
-- 4. Which account tracks which brand.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.user_brands (
    user_id    bigint NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
    brand_key  text   NOT NULL REFERENCES app.brands (brand_key),
    added_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, brand_key)
);
CREATE INDEX IF NOT EXISTS user_brands_brand_idx ON app.user_brands (brand_key);

-- ---------------------------------------------------------------------------
-- 5. Saved reports get an owner.
--
-- Before accounts, GET /api/reports/saved/{id} took a sequential id and any
-- caller could walk it (audit finding F11). Reports are now listed and opened
-- only by their owner. Rows saved before this migration have no owner; only
-- accounts listed in ADMIN_EMAILS can see those. Public share links
-- (/r/<token>) are unaffected: they were always meant to be openable by anyone
-- holding the unguessable token.
-- ---------------------------------------------------------------------------
ALTER TABLE marts.saved_reports ADD COLUMN IF NOT EXISTS owner_id bigint;
CREATE INDEX IF NOT EXISTS saved_reports_owner_idx
    ON marts.saved_reports (owner_id, created_at DESC);
