# Airbyte Cloud configuration-as-code (Terraform).
#
# What this manages:
#   - the Snowflake DESTINATION (fully managed here)
#   - one CONNECTION per source (App Store, Google Play) -> Snowflake AIRBYTE schema
#
# Sources are referenced by ID (var.appstore_source_id / var.googleplay_source_id).
# Why: Airbyte's app-review connectors vary (first-party vs Marketplace/community),
# and their config schemas differ, so the robust path is to create each source once
# in the Airbyte UI (or the connector's own TF resource) and pass its ID in. Example
# managed-source blocks are included below, commented, for when you want them in TF.
#
# Apply:  terraform init && terraform apply
# Auth:   create an Application in Airbyte Cloud (Settings -> Applications) to get
#         client_id / client_secret; put them in terraform.tfvars.

terraform {
  required_providers {
    airbyte = {
      source  = "airbytehq/airbyte"
      version = "~> 0.6"
    }
  }
}

provider "airbyte" {
  # Airbyte Cloud OAuth application credentials.
  client_id     = var.airbyte_client_id
  client_secret = var.airbyte_client_secret
  # server_url defaults to Airbyte Cloud; override for self-hosted.
}

# --- Destination: Snowflake (lands into the AIRBYTE schema) --------------------
resource "airbyte_destination_snowflake" "reviewbot" {
  name         = "reviewbot-snowflake"
  workspace_id = var.airbyte_workspace_id

  configuration = {
    host      = var.snowflake_host          # e.g. xy12345.us-east-1.snowflakecomputing.com
    role      = var.snowflake_role          # role with write on AIRBYTE schema
    warehouse = var.snowflake_warehouse
    database  = var.snowflake_database      # REVIEWBOT
    schema    = "AIRBYTE"
    username  = var.snowflake_username
    credentials = {
      username_and_password = {
        password = var.snowflake_password
      }
    }
  }
}

# --- Connections: each review source -> Snowflake -----------------------------
# Full-refresh|overwrite keeps the landing table a clean mirror; the normalize
# step downstream MERGEs into RAW so history is preserved there. Switch to
# incremental|append_dedup once the connector exposes a cursor you trust.
resource "airbyte_connection" "app_store_reviews" {
  name                                 = "app-store-reviews -> snowflake"
  source_id                            = var.appstore_source_id
  destination_id                       = airbyte_destination_snowflake.reviewbot.destination_id
  namespace_definition                 = "destination"
  non_breaking_schema_changes_preference = "propagate_columns"

  configurations = {
    streams = [{
      name      = var.appstore_stream_name # e.g. "reviews"
      sync_mode = "full_refresh_overwrite"
    }]
  }

  # Airbyte Cloud cron. Tighten toward near-real-time as needed.
  schedule = {
    schedule_type   = "cron"
    cron_expression = var.sync_cron # e.g. "0 */2 * * * ?" (every 2h)
  }
}

resource "airbyte_connection" "google_play_reviews" {
  name                                 = "google-play-reviews -> snowflake"
  source_id                            = var.googleplay_source_id
  destination_id                       = airbyte_destination_snowflake.reviewbot.destination_id
  namespace_definition                 = "destination"
  non_breaking_schema_changes_preference = "propagate_columns"

  configurations = {
    streams = [{
      name      = var.googleplay_stream_name # e.g. "reviews"
      sync_mode = "full_refresh_overwrite"
    }]
  }

  schedule = {
    schedule_type   = "cron"
    cron_expression = var.sync_cron
  }
}

# --- Optional: manage the sources in TF too -----------------------------------
# Uncomment and fill in once you've confirmed the exact connector + config schema
# in the Airbyte registry, then set source_id above to reference these.
#
# resource "airbyte_source_custom" "app_store" {
#   name          = "app-store-reviews"
#   workspace_id  = var.airbyte_workspace_id
#   definition_id = var.appstore_definition_id      # from the connector page
#   configuration = jsonencode({
#     app_id  = var.appstore_app_id                 # numeric Apple app id
#     country = "us"
#     # ...connector-specific fields...
#   })
# }
