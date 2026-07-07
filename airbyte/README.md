# Airbyte Cloud — App Store + Google Play reviews

> **STATUS: OPTIONAL — built but not deployed (as of 2026-07).**
> App Store + Google Play reviews are already handled **for free** by the built-in
> connectors — `src/reviewbot/connectors/app_store.py` (Apple's official RSS feed)
> and `google_play.py` (the google-play-scraper library) — no Airbyte account
> needed, and they're **live in the pipeline today**. This Terraform is kept as an
> optional **managed-EL** path: reach for it only if you outgrow hand-written
> connectors or want Airbyte's scheduling/monitoring and its hundreds of other
> connectors. Until you actually `terraform apply` this, the Airflow
> `normalize_airbyte` task is a harmless no-op (0 rows).

Airbyte can pull **public app-store reviews** (Apple App Store + Google Play) into
Snowflake with managed, scheduled, schema-aware syncs and **no scraper code**.

```
Airbyte Cloud ──(sync)──► Snowflake AIRBYTE.APP_STORE_REVIEWS
                                     AIRBYTE.GOOGLE_PLAY_REVIEWS
                                          │  normalize_airbyte.sql
                                          ▼
                                 RAW.REVIEWS_RAW  ──► MARTS ──► chatbot
```

Airbyte owns **Extract + Load**; the `normalize_airbyte.sql` step reshapes its
output into the shared `NormalizedReview` contract, so the reviews land in the
exact same table (`RAW.REVIEWS_RAW`) the scrapers feed. Nothing downstream
changes.

## One-time setup

1. **Snowflake:** run `../snowflake/ddl.sql` (creates the `AIRBYTE` schema) and
   grant Airbyte's role write access:
   ```sql
   GRANT USAGE ON DATABASE REVIEWBOT TO ROLE REVIEWBOT_ROLE;
   GRANT USAGE ON SCHEMA REVIEWBOT.AIRBYTE TO ROLE REVIEWBOT_ROLE;
   GRANT CREATE TABLE ON SCHEMA REVIEWBOT.AIRBYTE TO ROLE REVIEWBOT_ROLE;
   ```
2. **Airbyte Cloud:**
   - Create an **Application** (Settings → Applications) → `client_id` / `client_secret`.
   - Create the two **sources** in the UI (App Store reviews, Google Play reviews)
     — pick the connectors from the registry, set the app IDs, and copy each
     source's ID. (Sources live in the UI because app-review connectors and their
     config schemas vary; the destination + connections are managed in Terraform.)
3. **Terraform:**
   ```bash
   cd airbyte
   cp terraform.tfvars.example terraform.tfvars   # fill in creds + source IDs
   terraform init
   terraform apply
   ```
   This creates the Snowflake destination and both connections (App Store → SF,
   Google Play → SF) on the schedule in `sync_cron`.

## After each sync

Airbyte writes `AIRBYTE.APP_STORE_REVIEWS` / `AIRBYTE.GOOGLE_PLAY_REVIEWS`. Then:

```bash
# 1) confirm the connector's real column names, adjust the SELECT if needed:
#    DESC TABLE AIRBYTE.APP_STORE_REVIEWS;
snowsql -f ../snowflake/normalize_airbyte.sql   # AIRBYTE.* -> RAW.REVIEWS_RAW
snowsql -f ../snowflake/transform.sql           # RAW -> MARTS (embed + sentiment)
```
Schedule these two as Snowflake Tasks (or Airflow) so app reviews flow through
to the chatbot automatically after every Airbyte sync.

## Notes / gotchas

- **`brand` mapping:** app-review streams are per-app, not per-brand. See the
  note at the bottom of `normalize_airbyte.sql` — for more than one or two apps,
  add an `AIRBYTE.APP_BRAND_MAP(app_id, brand)` table and JOIN it.
- **Connector schemas vary.** The column names in `normalize_airbyte.sql` are the
  typical shape; verify with `DESC TABLE` and adjust.
- **State:** don't commit `terraform.tfstate` or `terraform.tfvars` (they hold
  IDs/secrets).
- **Sync mode:** connections use full-refresh|overwrite for a clean mirror; the
  MERGE in `normalize_airbyte.sql` preserves history in `RAW`. Move to
  incremental|append_dedup once you trust the connector's cursor field.
