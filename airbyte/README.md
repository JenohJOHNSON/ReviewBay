# Airbyte Cloud: Apify-backed reviews into Neon

Airbyte Cloud runs the **Extract + Load** for the Apify-backed sources (web
mentions, Google Maps, Yelp, Reddit, TripAdvisor): it reads each Apify **dataset**
and lands it as a table in the Neon `airbyte` schema on a managed, scheduled sync.
A small worker then reshapes those rows into the shared `raw.reviews_raw` table
using the same mappers the direct connectors use, and embeds them into
`marts.reviews`, so Airbyte-fed reviews reach the dashboard and chat exactly like
scraped ones.

App Store and Google Play stay on the free Python connectors, so this path is
opt-in.

```
Apify actor -> dataset -> Airbyte "Apify Dataset" source -> Neon airbyte.<table>
                                                                  |
                                  python -m reviewbot.ingestion.airbyte_sync
                                                                  v
                                     raw.reviews_raw -> marts.reviews -> chat
```

> Airbyte Cloud is a 30-day / 400-credit trial. After it lapses, either pay or run
> Airbyte OSS. The direct Python connectors keep working regardless, so nothing
> breaks when the trial ends.

## Setup (Airbyte Cloud UI)

1. **Sign in** at cloud.airbyte.com and start the trial.
2. **Destination -> Postgres (Neon):** add a *Postgres* destination using the
   pieces of your Neon connection string (host, database, username, password), SSL
   mode `require`, and set **Default Schema = `airbyte`**.
3. **Source -> Apify Dataset:** add the *Apify Dataset* source with your Apify
   token and the dataset id (or actor + run) for a source you want, for example a
   Google Maps or Reddit scrape for a brand.
4. **Connection:** connect that source to the Neon destination and run a sync.
   Note the **table name** Airbyte creates in the `airbyte` schema.
5. **Map it:** add an entry to `config/airbyte_sources.yml`:
   ```yaml
   sources:
     - table: airbyte.<the table Airbyte created>
       brand: Blue Bottle Coffee
       source: google_maps      # google_maps | yelp | tripadvisor | reddit | ...
   ```
6. **Run the worker** after each sync (or schedule it):
   ```bash
   python -m reviewbot.ingestion.airbyte_sync
   ```

## How the mapping works

`airbyte_normalize.py` reads each configured `airbyte.<table>`, pulls the original
Apify item out of the row (a `_airbyte_data` jsonb column, or the typed columns if
the destination expanded them), and passes it to the matching mapper in
`connectors/apify_source.py`. The field mapping is therefore shared with the live
connectors and lives in one place.

## Notes

- **Brand is per-connection.** Each Airbyte connection carries one brand's dataset,
  so `brand` lives in the config entry, not in the data.
- **Idempotent.** The loader upserts on the same stable id, so re-running after a
  sync never duplicates rows.
- **Missing table is a no-op.** If a configured table has not synced yet, the
  worker logs it and skips.

The old Snowflake + Terraform path (App Store / Google Play connectors) is
superseded by this Neon + Apify-dataset design; `snowflake/normalize_airbyte.sql`
is kept only for historical reference.
