# Supplier Disruption Agent on GCP, designed to stay inside the free tier.
#
#   Cloud Scheduler (daily) --> Cloud Run Job (container: live scan) --> BigQuery (one row per headline)
#                                   |                                          |
#                          Secret Manager (Gemini key)            views: alerts, daily_summary
#
# The Gemini key is NOT in this file or in Terraform state: Terraform creates an
# empty secret and the deploy script adds the key as a secret version.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # The budgets API needs a quota project when you run Terraform with your own login.
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {}

locals {
  image    = var.image != "" ? var.image : "${var.region}-docker.pkg.dev/${var.project_id}/sda/agent:latest"
  bq_table = "${var.project_id}.${google_bigquery_dataset.sda.dataset_id}.${google_bigquery_table.runs.table_id}"
  apis = [
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "bigquery.googleapis.com",
    "billingbudgets.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# --- Image registry ------------------------------------------------------------

resource "google_artifact_registry_repository" "sda" {
  repository_id = "sda"
  location      = var.region
  format        = "DOCKER"
  description   = "Supplier Disruption Agent images"

  # Free tier is 0.5 GB: keep the 2 newest images, delete the rest.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-newest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      older_than = "86400s"
    }
  }
  depends_on = [google_project_service.apis]
}

# --- Secret (value added outside Terraform) ------------------------------------

resource "google_secret_manager_secret" "gemini" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# --- Identities: one per job, least privilege ----------------------------------

resource "google_service_account" "job" {
  account_id   = "sda-job"
  display_name = "Supplier Disruption Agent - runs the scan"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "scheduler" {
  account_id   = "sda-scheduler"
  display_name = "Supplier Disruption Agent - starts the job"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "build" {
  account_id   = "sda-build"
  display_name = "Supplier Disruption Agent - builds the image"
  depends_on   = [google_project_service.apis]
}

# Cloud Build runs as sda-build (see cloudbuild.yaml), not the project's default
# account, whose permissions differ between new and old projects.
resource "google_project_iam_member" "build" {
  for_each = toset([
    "roles/logging.logWriter",       # build logs
    "roles/storage.objectViewer",    # read the uploaded source
    "roles/artifactregistry.writer", # push the image
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

resource "google_secret_manager_secret_iam_member" "job_reads_key" {
  secret_id = google_secret_manager_secret.gemini.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.job.email}"
}

resource "google_bigquery_dataset_iam_member" "job_writes_rows" {
  dataset_id = google_bigquery_dataset.sda.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.job.email}"
}

resource "google_project_iam_member" "job_runs_load_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.job.email}"
}

# --- BigQuery ------------------------------------------------------------------

resource "google_bigquery_dataset" "sda" {
  dataset_id  = "supplier_disruption"
  location    = var.bq_location
  description = "Run records from the Supplier Disruption Agent, one row per headline"
  depends_on  = [google_project_service.apis]
}

resource "google_bigquery_table" "runs" {
  dataset_id          = google_bigquery_dataset.sda.dataset_id
  table_id            = "runs"
  description         = "One row per headline per run. Written by a free batch load job at the end of each run."
  schema              = file("${path.module}/bigquery_schema.json")
  deletion_protection = var.protect_data

  time_partitioning {
    type  = "DAY"
    field = "run_at"
  }
  clustering = ["status", "event_type"]
}

resource "google_bigquery_table" "alerts" {
  dataset_id          = google_bigquery_dataset.sda.dataset_id
  table_id            = "alerts"
  description         = "Headlines that touched our suppliers, newest first"
  deletion_protection = false
  view {
    use_legacy_sql = false
    query          = <<-SQL
      SELECT run_at, news_id, headline, url, event_type, supplier_ids,
             high_risk_suppliers, min_days_of_cover, backup_warnings, status
      FROM `${local.bq_table}`
      WHERE status IN ('approved', 'needs_review')
      ORDER BY run_at DESC
    SQL
  }
}

resource "google_bigquery_table" "daily_summary" {
  dataset_id          = google_bigquery_dataset.sda.dataset_id
  table_id            = "daily_summary"
  description         = "One row per day: what was read, what was flagged, what it cost"
  deletion_protection = false
  view {
    use_legacy_sql = false
    query          = <<-SQL
      SELECT DATE(run_at, 'Europe/London') AS day,
             COUNT(*) AS headlines,
             COUNTIF(is_disruption) AS disruptions,
             COUNTIF(status IN ('approved', 'needs_review')) AS alerts,
             COUNTIF(status = 'error') AS errors,
             SUM(backup_warnings) AS backup_warnings,
             SUM(input_tokens + output_tokens) AS tokens,
             ROUND(SUM(cost_usd), 4) AS cost_usd
      FROM `${local.bq_table}`
      GROUP BY day
      ORDER BY day DESC
    SQL
  }
}

# --- The job -------------------------------------------------------------------

resource "google_cloud_run_v2_job" "scan" {
  name                = "sda-daily-scan"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.job.email
      timeout         = "1800s"
      max_retries     = 0 # a retry would spend the free model quota twice

      containers {
        image = local.image
        args  = ["--live", "--max-live", tostring(var.max_live)]

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }

        env {
          name  = "SDA_PROVIDER"
          value = "gemini"
        }
        env {
          name  = "SDA_MAX_LLM_CALLS"
          value = tostring(var.max_llm_calls)
        }
        env {
          name  = "SDA_BQ_TABLE"
          value = local.bq_table
        }
        env {
          name = "GEMINI_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.gemini.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.job_reads_key,
    google_bigquery_dataset_iam_member.job_writes_rows,
    google_project_iam_member.job_runs_load_jobs,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_job" {
  name     = google_cloud_run_v2_job.scan.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# --- The schedule (3 Scheduler jobs per billing account are free) ---------------

resource "google_cloud_scheduler_job" "daily" {
  name             = "sda-daily"
  region           = var.region
  description      = "Start the Supplier Disruption Agent live scan"
  schedule         = var.schedule
  time_zone        = var.time_zone
  attempt_deadline = "60s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.scan.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_job]
}

# --- Budget alert (emails billing admins; it does not cap spend) ----------------

resource "google_billing_budget" "monthly" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "Supplier Disruption Agent"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      units = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.apis]
}
