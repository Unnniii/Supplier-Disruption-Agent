variable "project_id" {
  description = "GCP project id (not the name)"
  type        = string
}

variable "region" {
  description = "Cloud Run, Scheduler and Artifact Registry region. us-central1 is in the Cloud Run free tier; a real UK deployment would use europe-west2 and pay for it."
  type        = string
  default     = "us-central1"
}

variable "image" {
  description = "Container image to run. The deploy script passes the digest it just built; empty means <region>-docker.pkg.dev/<project>/sda/agent:latest"
  type        = string
  default     = ""
}

variable "schedule" {
  description = "Cron for the daily scan"
  type        = string
  default     = "47 6 * * *"
}

variable "time_zone" {
  type    = string
  default = "Europe/London"
}

variable "max_live" {
  description = "Live headlines per run. Each costs 1 to ~9 model calls."
  type        = number
  default     = 15
}

variable "max_llm_calls" {
  description = "Hard cap on model calls per run (the Gemini free tier allows ~500 a day per model)"
  type        = number
  default     = 150
}

variable "bq_location" {
  type    = string
  default = "US"
}

variable "protect_data" {
  description = "Stop terraform destroy from deleting the BigQuery table. Set false to tear everything down."
  type        = bool
  default     = true
}

variable "billing_account" {
  description = "Billing account id (XXXXXX-XXXXXX-XXXXXX) for the budget alert. Empty skips the budget."
  type        = string
  default     = ""
}

variable "budget_amount" {
  description = "Monthly budget in the billing account's currency. Alerts at 50%, 90% and 100%; it does not stop spending."
  type        = number
  default     = 1
}
