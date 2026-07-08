# ReviewBay handoff

A living snapshot of where the project stands, why it is built this way, and what
is next. Update the "Last updated" line and the relevant sections whenever
something meaningful changes.

**Last updated:** 2026-07-08

---

## 1. What this is

ReviewBay is a brand-reputation ETL pipeline plus RAG chatbot. It scrapes public
reviews and mentions of a brand, stores them in Neon Postgres with pgvector,
embeds them through the OpenAI API, and serves an OpenAI-powered chat that
answers with links back to the original reviews, plus a dashboard with sentiment
and a local-ML review summary. See `README.md` for the full description.

## 2. Current stack (as running today)

| Layer | Choice | Notes |
|-------|--------|-------|
| Storage | Neon Postgres + pgvector | `raw.reviews_raw`, `marts.reviews` (vector(768), HNSW cosine) |
| Embeddings | OpenAI `text-embedding-3-small` (768-dim) | API call; local fastembed removed (it OOM/crashed the cloud container) |
| Sentiment | vaderSentiment | local |
| Chat | OpenAI Responses API, `gpt-4o-mini` by default | extractive fallback |
| Insights | scikit-learn (TF-IDF + KMeans) | no LLM, deterministic |
| Web search | Tavily (preferred) or Apify google-search | `web` source |
| Deep scrape | Firecrawl | opt-in per brand |
| Trustpilot | Playwright + Selectolax (self-hosted) | opt-in; no per-page fee; needs chromium |
| App reviews | Apple RSS + google-play-scraper | free |
| Other sites | Apify actors | Google Maps / Yelp / TripAdvisor / IG / FB |
| API host | Railway | serves the app and runs onboarding-time collection |
| Worker host | docker compose / optional Railway service | Airbyte import, recurring local sources, enrichment |

The previous warehouse stack was fully re-platformed to Neon/Postgres, pgvector,
and OpenAI. Legacy warehouse and orchestration resources were removed from the
repo so the running path is the only path documented here.

## 3. Where things live / are deployed

- **Code**: GitHub `javidjmg28/reviewbay`, branch `main`.
- **API**: Railway (built from `docker/api.Dockerfile` via `railway.json`).
  Needs `DATABASE_URL`, `OPENAI_API_KEY`, and optional `AUTH_USER`/`AUTH_PASS`
  in Variables. Add `TAVILY_API_KEY` for better onboarding-time web coverage.
- **Database**: Neon (connection string in local `.env` as `DATABASE_URL`).
- **Worker**: can run via `docker compose` or a separate Railway worker service
  where recurring scraper keys live. Keep high-cost keys off the public API
  service unless onboarding needs them.
- **Data present**: Blue Bottle Coffee sample data in Neon from earlier passes.

## 4. Secrets and where keys go (important)

- **Never paste secrets in chat.** Only into local `.env` or Railway Variables.
- **API keys (Railway needs)**: `DATABASE_URL`, `OPENAI_API_KEY`, and optionally
  `AUTH_USER`, `AUTH_PASS`, `TAVILY_API_KEY`. These belong in Railway Variables
  (and local `.env`).
- **Recurring scraper keys (worker needs)**: `FIRECRAWL_API_KEY`,
  `APIFY_TOKEN(S)`, and any source-specific overrides. Put them on the worker
  service or local `.env`, not necessarily on the public API service.
- `.env` and `config/brands.dynamic.yml` are gitignored and must stay so.

## 5. How to run

Local, full app:
```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker compose up --build           # api on :8000 + worker loop
```

One scrape + enrich pass only (no continuous cost):
```bash
docker compose build worker
RUN_ONCE=1 docker compose run --rm worker
```

Deploy API changes: push to GitHub `main`; Railway rebuilds automatically.

Database schema (once): `psql "$DATABASE_URL" -f postgres/ddl.sql`.

## 6. Conventions (please keep)

- **No em dashes anywhere.** Use commas, periods, or parentheses.
- **No emoji used as icons.** Use inline SVG line icons or the ASCII/mono motifs.
- Design system (as of 2026-07-08 rebrand): terminal-editorial. Near-black
  surfaces (`#090909` / `#121210` / `#1A1A17`), lime-chartreuse accent (`#CBFB5E`),
  square corners. Fonts: Funnel Display (headings), Public Sans (body), Martian
  Mono (labels/buttons/stats). All tokens live in `static/theme.css`; the legacy
  token names (`--accent`, `--bg`, `--ink`, `--muted`, `--pos/--neu/--neg` ...)
  are kept and remapped, so pages re-skin without markup changes. The previous
  cream + brick/orange + Poppins system is retired.
- Adding a source = new connector implementing `connectors/base.py`, plus one
  branch in `ingestion/run.py:build_connectors`, plus a category in `sources.py`.
  Connectors should disable cleanly (raise `KeyError` on a missing key) so a
  missing credential turns the source off instead of crashing the run.

## 7. Recent work (most recent first)

- **Deployed app works end to end + scrape-anywhere + OpenAI embeddings (2026-07-08)**:
  the live Railway app now onboards any brand, scrapes from across the web and
  social, saves to Neon, enriches, and serves a live dashboard. Getting there:
  (1) added open/free connectors (Reddit JSON, Hacker News, Mastodon) and a
  `collect_until` orchestrator targeting ~200 samples, cloud-safe first with Apify last,
  plus "Social SEO" discovery (X/LinkedIn/IG/FB/YouTube tagged from web search);
  (2) brand-keyed review id so brands never mix; (3) diagnosed the live failures:
  first a missing `DATABASE_URL` on Railway (variable-scoping trap), then the local
  fastembed model OOM/crashing the container during enrichment; (4) the real fix,
  replaced local embeddings with the **OpenAI embeddings API** (`text-embedding-3-small`
  @ 768 dims), removing the crashing model entirely and shrinking the image.
  Verified live: a fresh brand collected 168 reviews and enriched with no crash.
  Auth gate currently OFF on Railway (app is public).
- **Full visual rebrand (2026-07-08)**: adapted a ReviewBay landing template from
  Claude Design into a terminal-editorial system (near-black + lime-chartreuse,
  Funnel Display / Public Sans / Martian Mono, square corners). Rewrote
  `static/theme.css` to the new palette while keeping the legacy token names, so
  every page (landing, onboarding, chat, dashboard, report, compare) re-skinned at
  once. Rebuilt `landing.html` from the template with corrected copy: the template
  now says Neon + pgvector / OpenAI embeddings / AI answers, and pointed the nav
  and CTAs at the real routes. Fabricated marketing
  claims (SOC 2, "+48K/day") were replaced with honest stats. Verified all pages
  render in a local preview.
- **Roadmap complete: Phase 5 shipped (2026-07-08)**: Intelligence v2, the
  sentiment trend chart (by review post date), an in-app sentiment-drop alert
  banner, "Ad ideas" in the report, a brand health score on `/compare`, and
  shareable read-only report links at a public `/r/<token>`. All six roadmap
  phases (0-6) are now done. New: `marts.saved_reports.token` column.
- **Phases 2 to 6 shipped (2026-07-08)** (see `ROADMAP.md` for the checklist):
  Review Intelligence Report at `/report` (facts + OpenAI synthesis, cited);
  trust/observability (connector status cards simplified to Active/Inactive, chat
  confidence + evidence, scrape run history); exports (reviews CSV, print-to-PDF
  report, saved report snapshots); and cleanup (renamed loader.py, removed
  legacy warehouse references, review-intelligence positioning + CASESTUDY.md).
  Trustpilot selectors VERIFIED against a live page (pulled real reviews, not
  blocked), so it is a working self-hosted source. New Neon tables:
  marts.scrape_runs, marts.saved_reports (created in the shared DB).
- **Onboarding fixed + roadmap (2026-07-08)**: the onboarding "Something went
  wrong" bug was three issues: `web` on credit-blocked Apify (fixed by Tavily),
  embedding OOM from `ENRICH_BATCH=200` (lowered to 16), and Neon dropping the
  connection held idle during the embed (fixed with short-lived connections in
  `enrich()` + TCP keepalives in `db.py`). Model is now baked into the images.
  Verified end to end. Turned `reviewbay_suggestions.md` into `ROADMAP.md` (phased
  checklist). NOTE: these code fixes are not committed yet.
- **Ship + secret cleanup (2026-07-08)**: committed and pushed the session's work
  to `origin/main` (`362ebe5`), triggering a Railway redeploy. A Firecrawl API key
  was accidentally pasted into chat; redacted it from the local transcript (still
  needs rotating, see pending). Decided to keep `FirecrawlConnector` on the v1
  endpoint for now (v2 exists but was intentionally not changed).
- **Trustpilot via Playwright (2026-07-08)**: added `TrustpilotConnector`
  (`connectors/playwright_source.py`), a self-hosted Playwright + Selectolax
  scraper. Opt-in via listing `trustpilot` in a brand's sources; derives the
  Trustpilot page from the brand `website` (or `TRUSTPILOT_URL_<BRAND>`).
  Playwright/Selectolax are optional install-time dependencies, not part of the
  base Railway requirements. Parser and graceful-disable (missing deps/browser ->
  no-op) verified.
- **Scraping upgrade (2026-07-08)**:
  - Reddit API is closed to self-service, so `reddit` was dropped as a default
    source. The `web` search now tags reddit.com / youtube.com hits so Reddit and
    YouTube coverage is free and lands in the Social category. `reddit_api.py`
    remains as a dormant fallback if API keys are ever approved.
  - Added source categories (App Reviews / Review Sites / Social / Web & News),
    surfaced on the dashboard "Where reviews come from" card.
  - Added `TavilyConnector` (preferred `web` search when `TAVILY_API_KEY` set,
    else Apify google-search) and `FirecrawlConnector` (opt-in deep scrape,
    enabled by listing `firecrawl` in a brand's sources).
- **Re-platform (earlier)**: warehouse storage moved to Neon + pgvector; Claude
  moved to OpenAI Responses API; LLM insights moved to local scikit-learn;
  removed old warehouse connector deps; Airbyte path retargeted to Neon.
- **Web UI + deploy (earlier)**: landing page, FAQ, per-browser brand history,
  "coming soon" alerts panel, chat system prompt, clickable theme chips; deployed
  the API to Railway.

## 8. Pending / next steps

- [x] Committed the session's changes (categories, Tavily, Firecrawl,
      Reddit-via-web, Trustpilot/Playwright, README, this handoff) as `362ebe5`
      and pushed to `origin/main`. Railway is redeploying the API.
- [~] Firecrawl key `fc-a331...`: user chose NOT to rotate it (accepted the risk).
      It was redacted from the local transcript. Left as-is by request.
- [x] User pasted `TAVILY_API_KEY` and `FIRECRAWL_API_KEY` into local `.env`.
      Still to do: run a `RUN_ONCE` pass to pull data via Tavily and verify.
- [x] Built the Playwright + Selectolax Trustpilot connector.
- [x] Verified the Trustpilot selectors against a live page (pulled real reviews).
      To use it in Docker, install `playwright`, `selectolax`, and Chromium in the
      worker/API image that will run it, then add `trustpilot` to a brand.
- [ ] Optional: activate Airbyte Cloud (UI step) and add entries to
      `config/airbyte_sources.yml`.
- [ ] Verify the live Railway URL end to end (health, login, dashboard, chat)
      once the URL is shared.
- [x] Housekeeping: removed legacy warehouse/Airflow resources and scrubbed stale
      references from docs and code comments.

## 9. Known gotchas

- Postgres needs `%(brand)s::text IS NULL` casts (not bare `%(brand)s IS NULL`)
  or it raises `AmbiguousParameter`.
- pgvector: a `json.dumps(list_of_floats)` string is valid vector input; write and
  search with a `%(...)s::vector` cast.
- Code is baked into the Docker image via COPY, so code changes need a rebuild.
- Keep `.env.example` on a broadly available OpenAI model; verify model ids live
  before changing deployment variables.
- `/r/<token>` and `/api/shared/<token>` are PUBLIC (exempt from the AUTH_USER/
  AUTH_PASS gate) by design, so a share link opens a saved report without the app
  password. The token is unguessable and the data is public reviews, but be aware
  the link exposes that brand's report to anyone who has it.
