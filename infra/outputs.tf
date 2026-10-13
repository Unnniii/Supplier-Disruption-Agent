output "job" {
  value = google_cloud_run_v2_job.scan.name
}

output "bigquery_table" {
  value = local.bq_table
}

output "run_now" {
  description = "Start a scan now and wait for it"
  value       = "gcloud run jobs execute ${google_cloud_run_v2_job.scan.name} --region ${var.region} --wait"
}

output "see_alerts" {
  value = "bq query --use_legacy_sql=false 'SELECT * FROM `${var.project_id}.${google_bigquery_dataset.sda.dataset_id}.alerts` LIMIT 20'"
}

output "console_job" {
  value = "https://console.cloud.google.com/run/jobs/details/${var.region}/${google_cloud_run_v2_job.scan.name}/executions?project=${var.project_id}"
}

output "console_bigquery" {
  value = "https://console.cloud.google.com/bigquery?project=${var.project_id}&ws=!1m4!1m3!3m2!1s${var.project_id}!2s${google_bigquery_dataset.sda.dataset_id}"
}

output "see_events" {
  value = "bq query --use_legacy_sql=false 'SELECT event_id, status, event_type, headline_count, first_seen, last_seen, title FROM `${local.bq_events}` ORDER BY last_seen DESC'"
}
