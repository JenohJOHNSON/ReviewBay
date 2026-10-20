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

## Next priorities

Ordered by value over effort. The first two are operational and need no code.

**1. Run the migration and turn on the password gate** (minutes, no code)
`psql "$DATABASE_URL" -f postgres/002_scraping_engines.sql` unlocks per-engine
metrics and the collection-health panel. Setting `AUTH_USER`/`AUTH_PASS` closes
the enumerable `GET /api/reports/saved/{id}` endpoint. Both are called out in
[`HANDOFF.md`](HANDOFF.md) section 8.

**2. Decide the worker topology** (a deploy decision)
`railway.json` builds only the API, so scheduled refresh, the Airbyte import and
the global `qc()` pass do not run in production even though the docs describe a
worker. Either deploy the worker service or stop documenting one.

**3. Measure against a real brand** (half a day)
The benchmarks are per page *class*, which is the honest way to measure
extraction but says nothing about yield for a given brand. Onboard two or three
real brands with the depth engine on and compare the collection-health panel
against the pre-change numbers.

**4. Browser tier on a worker** (half a day)
The measured gap is client-rendered review pages, which currently yield zero.
`requirements-scraping.txt` plus Chromium on the worker closes it. Keep it off
the API image.

**5. Per-user ownership** (the real fix for finding F11)
Saved reports and the brand list are currently per-browser `localStorage` with no
server-side ownership. A minimal account model would let the saved-report
endpoint be scoped properly instead of relying on a shared password.

**6. Site strategies for the domains that matter** (ongoing, cheap)
`config/extraction_strategies.yml` is a config-only change. Add an entry when the
collection-health panel shows a domain the generic extractors read badly.

**7. Deferred from this engagement**
In-process cache eviction for `insights._CACHE` / `report._CACHE`; teaching the
legacy Trustpilot connector the robots.txt check the new engine does; and a
`created_at` column type that would let the trend query stop parsing text dates.

---

Recommended order: the operational items (1 and 2) first, since they are
configuration rather than code. Everything after that is additive, and safe to do
one item at a time.
