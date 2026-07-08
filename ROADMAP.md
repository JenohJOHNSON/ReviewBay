# ReviewBay roadmap

A living checklist, based on `reviewbay_suggestions.md` (2026-07-08). The theme:
make ONE demo bulletproof before adding more connectors. We do phases in order,
one at a time, and tick items as we finish them.

**Last updated:** 2026-07-08

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

---

Recommended order: finish Phase 0 (commit + cleanup), then Phase 1. Everything
after that is additive product value, safe to do one phase at a time.
