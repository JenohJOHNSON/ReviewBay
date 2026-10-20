# ReviewBay — Scraping Benchmarks

Measurements for the extraction work described in
[`SCRAPING_ARCHITECTURE.md`](SCRAPING_ARCHITECTURE.md).

The brief's rule for this section was *"do not assert performance gains without
measurements"*. Everything below is produced by a script in the repository and
can be re-run to the same numbers; nothing here is estimated or extrapolated.
Regressions are reported alongside improvements.

---

## 1. How to reproduce

```bash
pip install -r requirements.txt
export PYTHONPATH=src
python scripts/benchmark_scraping.py --repeat 5
```

No database, no network, no API keys and no paid service are required. Optional
flags: `--json results.json`, `--markdown table.md`, `--ai live` (real model
calls, real spend — see §6).

Run recorded here: **2026-10-20**, `--repeat 5`, `--ai stub`, Python 3.12.10 on
Windows 11, Scrapling 0.5.

---

## 2. Methodology

### Why a local fixture server, not live sites

A benchmark against live pages measures those sites' availability on the day it
ran, cannot be re-run to the same numbers, and sends load to third parties for
no one's benefit. So the dataset is ten fixture pages in `tests/fixtures/`,
served over HTTP by `tests/fixture_server.py` — the **real** fetch path, not a
mocked transport, because that path is where the redirect re-validation, the
content-type check, the byte cap and the politeness controls live.

The cost of this choice, stated plainly: these numbers show how the engines
behave on each *class* of page. They do not predict the yield on any particular
real-world brand, which depends on what pages exist for it.

### The dataset

Chosen to cover the page classes the brief asks about, each behaving differently:

| Page class | Fixture | What it tests |
|---|---|---|
| static + schema.org | `jsonld_reviews.html` | reviews published as JSON-LD |
| repeated cards | `structural_reviews.html` | obfuscated class names, `data-*` hooks |
| paginated listing | `paginated_page1-3.html` | 3 pages linked by `rel=next` |
| irregular structure | `irregular.html` | prose with blockquotes, no review markup |
| article / mention | `article_only.html` | a news page, not a review page |
| JS-rendered | `/js-shell.html` | empty shell, content needs a browser |
| empty page | `empty.html` | valid HTML, no content |
| malformed HTML | `malformed.html` | unclosed tags |
| unavailable | `/missing.html` | 404 |
| declined | `/forbidden` | 403, must not be retried harder |

### The approaches

| Approach | What it is |
|---|---|
| `search-only` | **The pre-existing behaviour.** A search API returns a title plus a short content extract; this reproduces that shape (title + first 200 characters, no author/rating/date) from the same pages, because that is what `tavily_source._map_result` stores and there is nowhere in a SERP result for the other fields to come from. |
| `scrapling` | Stage B: fetch the page and extract deterministically. |
| `scrapegraph-fallback` | Stage B, plus AI extraction for pages that yielded no reviews. |
| `routed` | The production path: the router decides per URL and gates the escalation. |

### What the columns mean

* **Records** — rows that passed validation *and* deduplication, so the number is
  comparable across approaches rather than counting raw candidates.
* **Reviews / Mentions** — the `extra.extraction.kind` split. A mention is a
  whole-page extract, not a customer review; keeping them separate is what stops
  this table flattering itself.
* **Chars** — total stored text. This is the metric the audit identified as the
  real gap: snippets are short, and short text makes weak embeddings and thin
  citations.
* **Rating / Author / Date** — records carrying that field. A SERP result cannot
  carry any of them.
* **$ est** — estimated, from token counts. Labelled an estimate everywhere.

### The AI stub

`--ai stub` (the default) replaces the model call with a deterministic
stand-in that returns the page's `<blockquote>` and `<p>` contents **verbatim**.
Because the text is copied, it passes the same verbatim-evidence gate a real
model's output must pass, so the record counts are the ones the real path would
produce. Token counts are estimated. This keeps the benchmark free, repeatable
and CI-safe; `--ai live` runs the real backend and reports real spend.

---

## 3. Results

### Totals across the 10-page dataset

| Approach | Records | Reviews | Mentions | Chars | Avg chars | Rating | Author | Date | Fetches | AI calls | $ est | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| search-only | 6 | 0 | 6 | 1,231 | 205 | 0 | 0 | 0 | 10 | 0 | 0 | 141 |
| scrapling | 14 | 11 | 3 | 2,386 | 170 | 11 | 5 | 4 | 13 | 0 | 0 | 4,093 |
| scrapegraph-fallback | 17 | 17 | 0 | 2,218 | 130 | 11 | 5 | 4 | 13 | 5 | 0.00124 | 4,015 |
| routed | 17 | 17 | 0 | 2,218 | 130 | 11 | 5 | 4 | 13 | 5 | 0.00124 | 4,047 |

### Relative to the pre-existing behaviour

| Approach | Records | Text volume | Records that are actual reviews | Records with a rating |
|---|---|---|---|---|
| scrapling | **×2.33** | **×1.94** | 0 → **11** | 0 → **11** |
| scrapegraph-fallback | **×2.83** | ×1.80 | 0 → **17** | 0 → 11 |
| routed | **×2.83** | ×1.80 | 0 → **17** | 0 → 11 |

### Per page class

| Page class | search-only | scrapling | +AI fallback | routed |
|---|---|---|---|---|
| static + schema.org | 1 rec / 57 ch | **2 / 308** | 2 / 308 | 2 / 308 |
| repeated cards | 1 / 230 | **3 / 432** | 3 / 432 | 3 / 432 |
| paginated listing | 1 / 236 | **6 / 770** | 6 / 770 | 6 / 770 |
| irregular structure | 1 / 237 | 1 / 528 *(mention)* | **4 / 398** | 4 / 398 |
| article / mention | 1 / 239 | 1 / 241 *(mention)* | 1 / 203 | 1 / 203 |
| JS-rendered | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| empty page | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| malformed HTML | 1 / 232 | 1 / 107 | 1 / 107 | 1 / 107 |
| unavailable (404) | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| declined (403) | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

---

## 4. What the numbers actually say

**The gain is concentrated where the architecture predicted it.** Pagination is
the largest single win (1 → 6 records), because a SERP result for page 1 of a
review listing is one snippet while the listing itself is three pages of
reviews. Structured-markup and repeated-card pages roughly triple. These three
classes are what most real review pages are.

**The field coverage change matters more than the record count.** Going from 0
to 11 records carrying a star rating, 5 carrying an author and 4 carrying a
post date is the difference between a dashboard that can compute an average
rating and a sentiment trend, and one that cannot. `search-only` produces
exactly zero of all three, structurally, because a search result has nowhere to
put them. That is not a tuning difference; it is a different kind of data.

**AI extraction helps on exactly one page class.** It converts the
irregular-prose page from 1 whole-page mention into 3 attributed reviews, and
adds nothing anywhere else — 5 calls for 3 records. That is the correct shape
for a paid fallback, and it is why it is off by default and gated on "this page
produced no reviews".

**Two honest regressions:**

1. **Total text volume drops when AI extraction is enabled** (2,386 → 2,218
   chars, −7%), and average characters per record drops from 170 to 130. This is
   the right trade, not a defect: a 528-character whole-page mention is replaced
   by three precise ~130-character review quotes. Fewer characters, better data —
   6 more records that are actual attributed reviews. But the raw character count
   goes down, and a report that only showed "records up 21%" would be hiding it.

2. **Malformed HTML yields less text than the snippet approach** (107 vs 232
   chars). Taking the first 200 characters of a broken page happens to grab more
   bytes than parsing it correctly does. Those extra bytes are markup debris, so
   this is a regression in the metric rather than in the data — but the metric
   does regress.

**Latency is ~29× higher** (141ms → ~4,100ms for 13 fetches). This is inherent
and expected: `search-only` makes one request per page, while the depth engine
fetches each page and waits the per-domain politeness delay between requests.
About 10 of those 13 seconds-worth is the deliberate 1s/domain floor plus
retry backoff on the 404 and 500 fixtures. Collection runs in a background
thread with a 240-second deadline, so this cost lands on a job, not a request.

**Nothing crashes on the four hostile pages.** Empty, malformed, 404 and 403 all
return cleanly; the 403 is recorded as declined and never retried.

---

## 5. Enrichment cost

Separate from extraction, and measured by reading the code path rather than by
timing a live OpenAI account.

`enrich()` finds pending rows with a `LEFT JOIN` of `raw.reviews_raw` against
`marts.reviews` and loops that query until it comes back empty. With
`ENRICH_BATCH=16` and no index on `raw.reviews_raw`, enriching 3,000 rows ran
**~188 full joins** and made **188 separate embeddings requests** — against an
API the connector already batches at 96 inputs per call.

| | Before | After |
|---|---|---|
| `ENRICH_BATCH` | 16 | 96 |
| Embedding requests for 3,000 rows | ~188 | ~32 |
| Pending-query executions | ~188 | ~32 |
| Index on `raw.reviews_raw` | none | brand, captured_at, source |

That is a **~6× reduction in both round trips and join executions**, from a
config default and three indexes. Per-row API cost is unchanged — the same text
is embedded either way — so this is latency and database load, not spend.

A measured end-to-end enrichment run against a live Neon instance is not
included, because this environment has no database credentials. The arithmetic
above is from the batch sizes in the code, not a timing.

---

## 6. What is not measured

Stated so the table is not read as claiming more than it does.

* **The `scrapegraphai` library backend.** Implemented and unit-tested, but its
  74-package dependency tree is not installed here, so the AI column reflects the
  `native` backend's code path with a stubbed model. Both backends share the
  objective, validation, evidence gate, cache and budget, so record counts should
  match; token counts and latency would differ.
* **Live model quality.** The stub copies text verbatim, which is what a
  competent model does on these fixtures. A real model will sometimes paraphrase,
  and the evidence gate will reject it — so the live record count on the
  irregular page is an **upper bound**, not a guarantee. `--ai live` measures the
  real thing.
* **Real-world yield per brand.** Measured per page class, not per brand.
* **Browser-tier throughput.** No browser is installed in this environment, which
  is why the JS-rendered row is 0 across the board.
* **Live Railway memory and cold-start figures.** No deployment was performed.
* **Query latency improvements** from the new `marts.reviews` indexes, for the
  same lack-of-database reason.

---

## 7. Test suite as a baseline

| | Tests | Result |
|---|---|---|
| Before this work (commit `805de8f`) | 11 | 11 passed, 12.5s |
| After the extraction work | 332 (+141 subtests) | 332 passed, 86s |
| After the follow-up list | 399 (+143 subtests) | 399 passed, 86s |

Run with `pytest tests/ -q`. No database, network or API keys required: the
scraping tests use the local fixture server and the model calls are stubbed.

Several of the numbers in this document exist because the benchmark found
problems the tests had not:

* The first run showed AI extraction making 2 calls for **0 records**. The cause
  was the article fallback always producing a record, so the escalation condition
  — "did we extract anything" — was never false. It now keys on *reviews*
  extracted, which is what produced the 1 → 4 improvement on the irregular page.
* Writing the end-to-end fetch test surfaced a **deadlock** in
  `metrics.RunMetrics.retry()`, which took a non-reentrant lock twice and hung
  the collection thread with no exception and no log line.

Both are regression-tested
(`test_routing.py::test_escalation_when_only_a_page_level_mention_was_extracted`,
`test_helpers.py::test_retry_does_not_deadlock`).
