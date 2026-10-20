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

## Next priorities

**1. Run the migration and turn on the password gate** (minutes, no code)

```
python scripts/migrate.py --dry-run   # see the plan
python scripts/migrate.py             # apply it
```

Unlocks per-engine metrics and the collection-health panel. Then set
`AUTH_USER`/`AUTH_PASS`, which is what currently stands between a public
deployment and an enumerable `GET /api/reports/saved/{id}`. Both are in
[`HANDOFF.md`](HANDOFF.md) section 8.

**2. Create the worker service** (minutes, a deploy decision)
The config and the documentation are now in place; what remains is the decision.
Either add a second Railway service pointed at `railway.worker.json`, or schedule
`RUN_ONCE=1 python -m reviewbot.worker` externally. Doing neither means
collection never refreshes - and now also means the QC pass never runs, which
matters more than it did before F1 was fixed.

**3. Measure against a real brand** (half a day)
The benchmarks are per page *class*, which is the honest way to measure
extraction but says nothing about yield for a given brand. Onboard two or three
real brands with the depth engine on and compare the collection-health panel
against the pre-change numbers.

**4. Browser tier on a worker** (half a day)
The one measured zero is client-rendered review pages. `docker build
--build-arg WITH_BROWSER=1 -f docker/worker.Dockerfile .` closes it. Keep it off
the API image: Chromium is 150-300MB resident.

**5. Per-user ownership** (half a day to a week, depending on the answer)
The real fix for F11, and a **product decision rather than an engineering one**,
which is why it is still open. Three options, in increasing cost:

| Option | What it gives | What it costs |
|---|---|---|
| Leave it, keep the password gate on | nothing new; the hole stays closed by configuration | one forgotten env var reopens it |
| An opaque owner key the browser generates and keeps in `localStorage`, stored on the row | real per-browser scoping, consistent with how the brand list already works | no recovery if the browser's storage is cleared; not an account |
| Real accounts | recoverable, shareable, auditable | a login flow, sessions, and a user table |

The middle option matches the app's existing identity model and needs one
additive nullable column. It is not obviously the right answer, which is exactly
why it should not be chosen by whoever happens to touch the file next.

**6. Site strategies for the domains that matter** (ongoing, cheap)
`config/extraction_strategies.yml` is a config-only change. Add an entry when the
collection-health panel shows a domain the generic extractors read badly.

**7. Still deferred**
A typed `created_at` column so the trend query stops parsing text dates. This is
the one remaining item that touches the database contract, so it wants an
additive column plus a backfill rather than a type change, and it is worth
measuring before doing: the cost is currently unknown because no live database
was reachable.

---

Recommended order: 1 and 2 first - they are configuration, they take minutes, and
until they are done the pipeline is not actually running end to end. Everything
after that is additive and safe to do one item at a time.
