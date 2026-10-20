# ReviewBay roadmap

A living checklist, based on `reviewbay_suggestions.md` (2026-07-08). The theme:
make ONE demo bulletproof before adding more connectors. We do phases in order,
one at a time, and tick items as we finish them.

**Last updated:** 2026-10-20

---

## Phase 0 — Stabilize the build (almost done)

The onboarding "Something went wrong" bug is fixed. Three separate causes were
found and fixed:

- [x] `web` was slow and Apify-credit-blocked, switched to Tavily (fast).
- [x] Embedding OOM-killed the container: `ENRICH_BATCH` was 200 in `.env`, lowered to 16.
- [x] Neon connection dropped mid-write: `enrich()` now uses short-lived connections
      (no connection held during the slow embed) plus TCP keepalives.
- [x] Model baked into the Docker images so it never downloads at runtime.
- [x] Verified onboarding reaches `done` and enrich terminates (no loop).
- [x] Committed and pushed these fixes (`9493a24`); Railway redeploying.
- [x] Reset the database to Blue Bottle only: deleted all 13 other brands (1105
      rows each from `raw` and `marts`) and emptied `brands.dynamic.yml`.
- [~] Firecrawl key: user chose NOT to rotate the exposed key (accepted risk;
      it was redacted from the local transcript).

## Phase 1 — One perfect demo brand: Blue Bottle Coffee  [suggestions 4.1]  DONE

- [x] Data: 165 reviews already present (in the 100 to 300 target), no re-scrape needed.
- [x] Verified dashboard: sentiment, source categories (App Reviews / Social / Web & News),
      review list with working source links.
- [x] Verified chat answers with citations that resolve to real reviews.
- [x] Insights polish: filter the brand name and pure-sentiment words from themes,
      so themes read as real topics (App, Ui, Tip, Location) not "Bottle"/"Great".
- [ ] Capture clean dashboard screenshots (yours to grab; Chrome extension was not
      connected for auto-capture). URLs: `/dashboard?brand=Blue Bottle Coffee`, `/chat`.

## Phase 2 — Review Intelligence Report (the hero output)  [suggestions 4.2]  DONE

- [x] Per-brand report at `/report` (+ `GET /api/report`): brand health score,
      top praises/complaints, themes, plus AI-synthesized recurring issues,
      purchase drivers, churn risks, marketing angles, product fixes, all cited.
- [x] Hybrid engine: deterministic facts (health score + scikit-learn insights) +
      ONE OpenAI call for the interpretive sections, grounded in real reviews with
      [n] citations. Cached per brand; facts-only fallback if OpenAI is down.
- [x] Report link added to the dashboard nav (carries the selected brand).
- Note: insights summary avg rating (sample of 60) can differ slightly from the
  header avg rating (all rated reviews). Cosmetic; can align later if desired.

## Phase 3 — Trust and observability in the UI  [suggestions 4.3, 4.4, 4.5]  DONE

- [x] Connector status cards (active / needs verification / disabled), grouped by
      category on the dashboard, from `GET /api/connectors` (env-driven).
- [x] Chat confidence level (High/Medium/Low) + evidence count ("Based on X reviews
      from Y sources"), from retrieval strength, shown under each chat answer.
- [x] Scrape run history: `marts.scrape_runs` table, `runs.record_run` from
      `run_brand`, `GET /api/runs`, and a run-history table on the dashboard
      (when, brand, sources, found, status, duration).

## Phase 4 — Exports and saved reports  [suggestions 5, phase 2]  DONE

- [x] CSV export of reviews (`GET /api/export/reviews.csv`, dashboard "Download CSV").
- [x] PDF export of the report (print stylesheet + "Save as PDF" button, no server lib).
- [x] Saved reports per brand (`marts.saved_reports`, save/list/view endpoints,
      a "Save report" button and a "Saved reports" picker on `/report`).

## Phase 5 — Intelligence v2  [suggestions 5, phase 3]  DONE

- [x] Sentiment trend over time: `GET /api/trend` buckets reviews by their post
      date; a "Sentiment over time" stacked-bar chart on the dashboard. (Blue
      Bottle shows a real decline: 2022 mostly positive, 2025 mostly negative.)
- [x] Sentiment-drop alerts: `GET /api/sentiment-alert` compares recent vs
      baseline negative rate; an in-app banner (ok / watch / drop). Deterministic,
      no external notifications.
- [x] Ad-angle generator: an "Ad ideas" section in the report (punchy taglines
      from real praise), folded into the report's single OpenAI call.
- [x] Competitor comparison: a brand health score (0-100) on each side of the
      `/compare` view, with a verdict line. (Needs a second brand to shine.)
- [x] Shareable report links: saved reports get an unguessable token; a public,
      read-only `/r/<token>` view (exempt from the password gate).
- Note: "topic clustering by source" was not built as a separate view; the
  dashboard insights already cluster topics globally (KMeans over TF-IDF).

## Phase 6 — Positioning and cleanup  [suggestions 8, 9 + housekeeping]  DONE

- [x] README leads with the review-intelligence positioning; added CASESTUDY.md
      (problem / solution / impact / technical proof) for the portfolio.
- [x] Removed legacy warehouse/Cortex references from code docstrings and the
      landing infographic; consolidated ingestion loading under `loader.py`.
- [x] Verified the Trustpilot selectors against a live page: the connector pulled
      8 real reviews (ratings, authors, text) from a real Trustpilot page, not
      blocked. Trustpilot is a working self-hosted source.

## Phase 7 - Extraction depth, data quality and security  (2026-10-20)  DONE

Driven by a full audit ([`docs/ARCHITECTURE_AUDIT.md`](docs/ARCHITECTURE_AUDIT.md)),
whose central finding was that the bottleneck was **depth, not breadth**: the
~200 target was being met by repeating the same web search, and a search API
returns a ~200-character snippet with no author, rating or date.

- [x] **Scrapling depth engine** (`connectors/scrapling_source.py`): reads the
      pages search already found. Three fetch tiers chosen per page, browser only
      for genuinely client-rendered pages. Replaces no existing connector.
- [x] **Five extraction strategies** in precision order (`scraping/extract.py`):
      JSON-LD, microdata, per-site config, structural sibling matching, whole-page
      article. Records tagged `review` or `mention` so a news page never counts as
      a customer review.
- [x] **ScrapeGraphAI AI extraction** (`connectors/scrapegraph_source.py`), off by
      default, offered a page only when deterministic extraction found no reviews.
      Two backends so the capability ships without the library's 74-package tree.
- [x] **Anti-fabrication gate** (`scraping/evidence.py`): AI output must appear
      verbatim in the fetched page or it is discarded. Paraphrases, summaries and
      invented reviews all measurably rejected.
- [x] **Rule-based routing** (`ingestion/routing.py`) with per-domain success
      history. No LLM decides which scraper runs.
- [x] **Validation and deduplication** (`quality.py`), with near-duplicate
      detection scoped to one (brand, URL) so two customers writing the same
      sentence on different pages stay two reviews.
- [x] **SSRF guard** (`scraping/urlpolicy.py`) on every server-side fetch,
      re-validated after each redirect. Tested against 24 attack vectors.
- [x] **robots.txt compliance** including `Crawl-delay`; 403/429/451 stop the
      request rather than triggering harder retries. No anti-bot evasion.
- [x] **Prompt-injection fencing** (`prompting.py`) for the chat, the report and
      the QC judge.
- [x] **Stored-XSS fix**: the `esc()` helper on three pages did not escape quote
      characters while being used inside double-quoted `href` attributes.
- [x] **Critical QC bug fixed**: batches spanned brands and were judged against
      the first row's brand, silently deleting other brands' reviews.
- [x] **Enrichment cost**: ~188 full joins and 188 embedding requests per 3,000
      rows, down to ~32 of each (batch size + three indexes).
- [x] **Collection health**: `GET /api/collection-health`, a dashboard panel, and
      per-engine metrics in `marts.scrape_runs`. Onboarding reports real phases.
- [x] **Reproducible benchmarks** (`scripts/benchmark_scraping.py`) over a fixed
      10-page dataset, with regressions reported alongside improvements:
      [`docs/SCRAPING_BENCHMARKS.md`](docs/SCRAPING_BENCHMARKS.md).
- [x] Tests: 11 to **332** (plus 141 subtests), no database or network needed.

---

## Phase 8 - the follow-up list  (2026-10-20)  PARTLY DONE

Working the priorities above turned up four more problems, all on the background
path rather than the request path, which is why the first audit missed them.
Findings F19-F22 in [`docs/ARCHITECTURE_AUDIT.md`](docs/ARCHITECTURE_AUDIT.md).

- [x] **The QC pass ran in neither deployed entrypoint** (F19, High). `qc()` sets
      `relevant`, which every dashboard, trend, compare, export and RAG query
      filters on. It was wired only into `ingestion.run.main()`, which nothing
      deploys, so on a real deployment the pass never ran and every row stayed
      `qc_checked = NULL`. It fails open, so there was no error to notice - just
      a filter that never engaged. Now in `worker.run_cycle()`, after enrichment
      because QC reads the enriched rows; `WORKER_QC=0` opts out.
- [x] **Bounded the two in-process caches** (F20, Medium). `insights._CACHE` and
      `report._CACHE` were dicts keyed by a query parameter, with TTL checked on
      read and nothing ever removed - one permanent entry per distinct brand
      string ever requested. Now `api/cache.py`, an LRU with real expiry.
- [x] **Brought the Trustpilot connector inside both scraping gates** (F21,
      Medium). It predates `reviewbot.scraping`, so it had no SSRF check on a URL
      derived from user input and no robots.txt check, while sending a spoofed
      Chrome user agent. **Behaviour change:** Trustpilot's robots.txt disallows
      unrecognised crawlers, so it now declines rather than scraping. Use the
      Apify actor source for Trustpilot data.
- [x] **`scripts/migrate.py`** (F22, Low), so the schema step no longer needs
      `psql` installed. Orders `ddl.sql` before the numbered files, records
      checksums, one transaction per file, and refuses DROP/TRUNCATE/DELETE and
      type changes before it even opens a connection.
- [x] **`railway.worker.json`** plus a Railway section in
      [`DEPLOY.md`](DEPLOY.md), so running both services is a documented
      two-service setup rather than a gap.
- [x] Tests: 332 to **399** (plus 143 subtests). Benchmark output unchanged.

---

## Phase 9 - Accounts and the free-model fallback  (2026-10-20)  DONE

Asked for directly: accounts, brands that stay in the account, delete, re-run,
and OpenRouter free models when tokens run out. This also settled the open
ownership question (priority 5 of the previous list) in favour of real accounts.

- [x] **Accounts**: sign up, sign in, sign out; scrypt password hashes; hashed
      session tokens in HttpOnly cookies; per-client and per-address rate limits;
      cross-site write protection; invite-only or closed signup; admin list.
- [x] **Brands belong to accounts**, in a Postgres registry that the worker now
      refreshes (audit F23: the old YAML file lived on a disk Railway wipes).
      One canonical spelling per brand (F24).
- [x] **My brands** page with **Re-run search** (single-flight, cooldown) and
      **Delete** (keeps the reviews; stops refresh when nobody tracks it). Re-run
      on the dashboard too. Import of pre-account browser brand lists.
- [x] **Saved reports owned** by their account (F11 closed).
- [x] **One LLM gateway** for chat, report, QC and extraction, falling back from
      OpenAI to OpenRouter free models with per-model cooldowns. Embeddings fall
      back only to the same model; chat retrieval falls back to full-text search.
- [x] **No paid AI required** (F30): reviews that cannot be embedded are saved
      without a vector and shown at once; a later pass adds the vector, and chat
      reaches those reviews by keyword meanwhile.
- [x] `/api/alerts/test` limited to admins (F25).
- [x] Verified on a real PostgreSQL 16 + pgvector and in real Chrome; the run
      found and fixed F28.

---

## Next priorities

**1. Migrate, then deploy** (minutes, no code)

```
python scripts/migrate.py --dry-run   # see the plan
python scripts/migrate.py             # applies 003 (accounts) and 004 (search index)
```

Then set `ADMIN_EMAILS` to your address and decide who may register:
`SIGNUP_INVITE_CODE` for invite-only, or `SIGNUP_ENABLED=0` once your accounts
exist. Every tracked brand is scraped on a schedule, so open signup on a public
URL is an open tab on the scraping bill. If the code is deployed first, sign-up
says which command to run rather than failing.

**2. Create the worker service** (minutes, a deploy decision)
Either add a second Railway service pointed at `railway.worker.json`, or schedule
`RUN_ONCE=1 python -m reviewbot.worker` externally. The worker now refreshes
every brand any account tracks, from the database registry.

**3. Turn on OpenRouter** (minutes)
Create a key, set `OPENROUTER_API_KEY`, and check `GET /api/llm/status`. Run with
`LLM_PRIMARY=openrouter` for a day to judge free-model answer quality before
relying on it. If every free model fails with a data-policy error, see the
account's privacy settings (https://openrouter.ai/settings/privacy).

**4. Password reset and account deletion** (a day, needs an email sender)
A forgotten password currently means a new account. Both flows need outbound
email, which this deployment does not have; the SMTP settings used for alerts
are the obvious place to start.

**5. Measure against a real brand** (half a day)
Still open from the previous list. The benchmarks are per page class; real yield
per brand is unmeasured.

**6. Browser tier on a worker** (half a day)
`docker build --build-arg WITH_BROWSER=1 -f docker/worker.Dockerfile .` closes
the one measured zero (client-rendered review pages). Keep it off the API image.

**7. Site strategies** (ongoing, cheap)
`config/extraction_strategies.yml`, when the collection-health panel shows a
domain the generic extractors read badly.

**8. Still deferred**
- Merging reviews already stored twice under two spellings of one brand (F24).
  It rewrites ids and deletes copies, so it wants a reviewed, explicit
  migration, not a side effect.
- A typed `created_at` column so the trend query stops parsing text dates.
  Additive column plus backfill; measure the current cost first, which is now
  possible with a local Postgres (see HANDOFF's gotchas).

---

Recommended order: 1 before anything else (the app requires accounts now), then
2 and 3, which are configuration. Everything after that is additive.
