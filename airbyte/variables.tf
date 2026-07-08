variable "airbyte_client_id" {
  type        = string
  description = "Airbyte Cloud application client_id (Settings -> Applications)."
  sensitive   = true
}

variable "airbyte_client_secret" {
  type      = string
  sensitive = true
}

variable "airbyte_workspace_id" {
  type        = string
  description = "Airbyte Cloud workspace UUID."
}

# --- Snowflake destination ---
variable "snowflake_host" {
  type        = string
  description = "e.g. xy12345.us-east-1.snowflakecomputing.com"
}
variable "snowflake_role" { type = string }
variable "snowflake_warehouse" { type = string }
variable "snowflake_database" {
  type    = string
  default = "REVIEWBOT"
}
variable "snowflake_username" { type = string }
variable "snowflake_password" {
  type      = string
  sensitive = true
}

# --- Sources (create in the Airbyte UI, paste IDs here) ---
variable "appstore_source_id" {
  type        = string
  description = "Airbyte source ID for the Apple App Store reviews connector."
}
variable "appstore_stream_name" {
  type    = string
  default = "reviews"
}
variable "googleplay_source_id" {
  type        = string
  description = "Airbyte source ID for the Google Play reviews connector."
}
variable "googleplay_stream_name" {
  type    = string
  default = "reviews"
}

variable "sync_cron" {
  type        = string
  description = "Airbyte cron (Quartz format). Default: every 2 hours."
  default     = "0 0 */2 * * ?"
}
