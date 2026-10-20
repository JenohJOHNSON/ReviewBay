# ReviewBay handoff

A living snapshot of where the project stands, why it is built this way, and what
is next. Update the "Last updated" line and the relevant sections whenever
something meaningful changes.

**Last updated:** 2026-10-20

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
| **Depth extraction** | **Scrapling 0.5** | base install is the lxml parser only, no browser. Reads the pages web search finds. On by default |
| **AI extraction** | **ScrapeGraphAI (opt-in) or built-in OpenAI backend** | OFF by default. Only for pages selectors cannot read. Verbatim-verified, spend-capped |
| URL safety | `scraping/urlpolicy.py` | SSRF guard on every server-side fetch, re-checked after each redirect |
| Politeness | `scraping/robots.py` | robots.txt + Crawl-delay honoured; 429/451 quarantine a domain |
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

### 2026-10-20 - Scrapling + ScrapeGraphAI, and an audit-driven cleanup

Full write-up: [`docs/ARCHITECTURE_AUDIT.md`](docs/ARCHITECTURE_AUDIT.md),
[`docs/SCRAPING_ARCHITECTURE.md`](docs/SCRAPING_ARCHITECTURE.md),
[`docs/SCRAPING_BENCHMARKS.md`](docs/SCRAPING_BENCHMARKS.md).

**The problem that shaped everything.** The audit found the bottleneck was depth,
not breadth: `collect_until` hit its ~200 target by running the Tavily search
three times, and a search API returns a ~200-character snippet with no author,
rating or date. Three passes over one forum thread returned three snippets of it.

**What was added**

- `connectors/scrapling_source.py` - reads the pages search already found. Three
  fetch tiers (requests / Scrapling session / browser), chosen per page; a
  browser only when the page produced nothing *and* looks client-rendered.
- `scraping/extract.py` - five strategies in precision order: JSON-LD then
  microdata, per-site config, structural (sibling shape, survives hashed class
  names), whole-page article. Each record is tagged `review` or `mention`.
- `connectors/scrapegraph_source.py` - AI extraction, off by default, offered a
  page only when deterministic extraction found **no reviews** on it. Two
  backends: the library, or the already-configured `openai` client.
- `ingestion/routing.py` - rule-based engine selection with per-domain history in
  `marts.engine_stats`. No LLM decides which scraper runs.
- `quality.py` - validation, provenance, and near-duplicate detection **scoped to
  one (brand, URL)** so two customers writing the same sentence on different
  pages stay two reviews.
- `scraping/{urlpolicy,robots,budget,evidence,metrics}.py` - SSRF guard, robots
  compliance, page/time/spend bounds, verbatim verification, per-engine counters.
- `GET /api/collection-health` plus a dashboard panel; onboarding now reports
  **real** phases from `onboarding.PHASES`.

**Bugs fixed** (severities and failure cases in the audit)

- *Critical*: `qc()` chunked rows across brands and judged each batch against the
  **first row's brand**, marking the rest `relevant = FALSE` - silently deleting
  up to (N-1)/N of QC'd rows from the dashboard, trends, compare and RAG.
- *Critical*: no SSRF validation on the user-supplied `website`, which became a
  live hole the moment an engine fetched it.
- *High*: `brand ILIKE :brand` was unescaped, so `?brand=%` returned every
  brand's data. Seven call sites.
- *High*: stored XSS - `esc()` on three pages escaped only `& < >` while being
  used inside double-quoted `href` attributes.
- *High*: Hacker News text was stored as raw HTML, polluting embeddings and the UI.
- *High*: App Store fell back to `results[0]`, harvesting a **different
  company's** reviews for any brand without an iOS app.
- *High*: the QC relevance filter was missing from insights, the report sample and
  the CSV export, so off-topic rows still shaped themes and cited evidence.
- *High*: enrichment was O(n^2/batch) - roughly 188 full joins and 188 embedding
  requests per 3,000 rows. Now about 32 of each.
- *Medium*: onboarding collections were never recorded, so scrape history was
  always empty for brands added through the product's own front door.
- *Medium*: Apify actor runs had no timeout and could block the collection thread
  (and every queued onboard) indefinitely.
- Plus prompt-injection fencing, CSV formula injection, request size bounds, a
  `metrics.retry()` deadlock, and `SCRAPEGRAPH_MAX_CALLS_PER_RUN=0` meaning
  *unlimited* rather than *none*.

**Measured** (fixed 10-page dataset, `scripts/benchmark_scraping.py --repeat 5`):
2.33x records and 0 to 11 records carrying a star rating versus the previous
snippet-only behaviour; the AI fallback adds 3 attributed reviews on the one
prose-style page and nothing elsewhere. Regressions are reported too: total text
volume drops 7% when AI extraction replaces long whole-page mentions with precise
quotes, and latency is about 29x higher because the depth pass fetches pages and
waits the politeness delay.

**Tests**: 11 to **332** (plus 141 subtests), needing no database, network or
API keys.

**Migration required**: `psql "$DATABASE_URL" -f postgres/002_scraping_engines.sql`
(additive and idempotent; the app degrades gracefully without it).


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

### From the 2026-10-20 engagement

- [ ] **Run the migration** on the live database:
      `psql "$DATABASE_URL" -f postgres/002_scraping_engines.sql`. Until then the
      collection-health panel shows `migration_required` and per-engine metrics
      are not recorded. Everything else works.
- [ ] **Set `AUTH_USER` / `AUTH_PASS`** on any deployment holding real brands.
      `GET /api/reports/saved/{id}` takes a sequential id and is protected only by
      that gate, so without it saved reports are enumerable. Documented rather
      than changed because fixing it properly needs a per-user ownership model,
      which is a product decision.
- [ ] **Decide the worker topology.** `railway.json` builds only the API, so the
      worker loop - scheduled refresh, the Airbyte import, the global `qc()` pass
      - does not run in production, though this file and the README describe one.
      This predates the engagement; it needs a deploy decision, not a code change.
- [ ] Optional: install `requirements-scraping.txt` plus Chromium **on a worker**
      to enable the browser tier for client-rendered review pages. Do not put it
      on the API image; a Chromium process is 150-300MB resident.
- [ ] Optional: `SCRAPEGRAPH_ENABLED=1` for brands whose reviews live in prose
      pages. Start with `SCRAPEGRAPH_MAX_COST_PER_RUN` low and watch the cost
      column in the collection-health panel.
- [ ] Not done: in-process cache eviction for `insights._CACHE` and
      `report._CACHE` (bounded in practice by brand count), and the legacy
      Trustpilot connector still does not consult robots.txt the way the new
      engine does.

### Earlier

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
- `GET /api/reports/saved/{id}` is NOT exempt, but it takes a sequential
  `bigserial`, so with the password gate off it is enumerable. Turn the gate on.
- Any brand filter written as `brand ILIKE %(brand)s` must pass the value through
  `db.like_param()`. Unescaped, `?brand=%` matches every brand in the database.
- Scraped text reaching an LLM must be fenced with `prompting.fence_all()`, and
  the system prompt must carry `prompting.UNTRUSTED_CONTENT_RULE`. A scraped page
  containing "ignore previous instructions" is a real input, not a hypothetical.
- Any server-side fetch of a URL that came from a user or a web page must pass
  `scraping.urlpolicy.check()` first, and **again after every redirect** - a
  validated host can redirect to the cloud metadata endpoint.
- `esc()` in the static pages must escape the quote characters as well as
  `& < >`: the templates interpolate scraped values inside double-quoted
  attributes.
- Scrapling ships Cloudflare-solving and fingerprint-spoofing features. They are
  deliberately unused and `tests/test_scrapling_connector.py::ComplianceTests`
  asserts that against the source. Do not enable them.
- The inline frontend JS has no build step or linter. Run
  `python scripts/check_frontend_js.py` after editing a `<script>` block.
