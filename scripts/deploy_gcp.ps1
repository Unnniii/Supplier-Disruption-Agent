# Deploy the daily live scan to Google Cloud (free tier). Windows PowerShell.
#
# One-time setup:
#   winget install Google.CloudSDK
#   winget install Hashicorp.Terraform
#   (close and reopen PowerShell)
#   gcloud auth login
#   gcloud auth application-default login   (optional; if it fails, the script uses your gcloud login)
#
# Deploy (re-run any time; it only changes what changed):
#   .\scripts\deploy_gcp.ps1 -ProjectId my-project-id -BillingAccount XXXXXX-XXXXXX-XXXXXX
#
# What it does:
#   0. Checks billing is linked and switches on the APIs Terraform needs first
#   1. Terraform creates the APIs, image registry, empty secret and build identity
#   2. Adds your Gemini key (from .env) to Secret Manager, if not there yet
#   3. Cloud Build builds the container image
#   4. Terraform creates the job, schedule, BigQuery tables (runs, events) and budget alert
#   5. Runs the job once and shows what landed in BigQuery

param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "us-central1",
    [string]$BillingAccount = "",
    [switch]$SkipRun
)

# "Continue", not "Stop": Windows PowerShell 5.1 turns gcloud's normal progress
# messages on stderr into errors under "Stop". Failures are caught by exit code.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..") -ErrorAction Stop

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE). See the messages above." } }

foreach ($tool in "gcloud", "terraform") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is not installed. See the setup lines at the top of this script."
    }
}
# Terraform needs credentials. Prefer Application Default Credentials; if that
# login is missing (its consent screen often fails), hand Terraform a short-lived
# token from the normal gcloud login instead. Valid ~1 hour: enough for a deploy.
$useAdc = $true
gcloud auth application-default print-access-token *> $null
if ($LASTEXITCODE -ne 0) {
    $useAdc = $false
    $token = gcloud auth print-access-token
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw "Not logged in. Run: gcloud auth login" }
    $env:GOOGLE_OAUTH_ACCESS_TOKEN = $token
    Write-Host "Using your gcloud login for Terraform (application-default login not found)."
}

gcloud config set project $ProjectId | Out-Null; Check "gcloud config set project"
$image = "$Region-docker.pkg.dev/$ProjectId/sda/agent"
$tfvars = @("-var", "project_id=$ProjectId", "-var", "region=$Region", "-var", "billing_account=$BillingAccount")

Step "0/5 Project checks"
# Terraform itself needs these APIs before it can enable anything else.
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com cloudbilling.googleapis.com
Check "gcloud services enable"
$billing = gcloud billing projects describe $ProjectId --format="value(billingEnabled)"
if ($billing -ne "True") {
    throw "Billing is not enabled on $ProjectId. Link a billing account (free tier still needs one): gcloud billing projects link $ProjectId --billing-account=XXXXXX-XXXXXX-XXXXXX"
}
# Terraform runs as you; bill its API quota to this project (the budgets API insists).
if ($useAdc) { gcloud auth application-default set-quota-project $ProjectId | Out-Null; Check "set ADC quota project" }

Step "1/5 Terraform: APIs, registry, secret, build identity"
Push-Location infra
try {
    terraform init -input=false; Check "terraform init"
    terraform apply -input=false -auto-approve @tfvars `
        "-target=google_project_service.apis" `
        "-target=google_artifact_registry_repository.sda" `
        "-target=google_secret_manager_secret.gemini" `
        "-target=google_project_iam_member.build"
    Check "terraform apply (phase 1)"
} finally { Pop-Location }

Step "2/5 Gemini key -> Secret Manager"
$existing = gcloud secrets versions list gemini-api-key --filter="state:ENABLED" --format="value(name)" --limit=1
Check "gcloud secrets versions list"
if ($existing) {
    Write-Host "Secret already has a key (version $existing). To replace it: gcloud secrets versions add gemini-api-key --data-file=<file>"
} else {
    $key = ""
    if (Test-Path .env) {
        $line = Get-Content .env | Where-Object { $_ -match '^\s*GEMINI_API_KEY\s*=' } | Select-Object -First 1
        if ($line) { $key = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
    }
    if (-not $key) {
        $secure = Read-Host "Paste your Gemini API key" -AsSecureString
        $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    # Written without a trailing newline: a newline inside an API key breaks the request header.
    $tmp = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($tmp.FullName, $key)
        gcloud secrets versions add gemini-api-key --data-file="$($tmp.FullName)" | Out-Null; Check "gcloud secrets versions add"
    } finally { Remove-Item $tmp.FullName -Force }
    Write-Host "Key stored in Secret Manager (never in Terraform state or the image)."
}

Step "3/5 Cloud Build: container image"
# New service accounts and a just-enabled Cloud Build API can take a few minutes
# to be recognised everywhere, so the first build may get PERMISSION_DENIED.
for ($try = 1; $try -le 4; $try++) {
    gcloud builds submit --config cloudbuild.yaml --substitutions "_IMAGE=${image}:latest" .
    if ($LASTEXITCODE -eq 0) { break }
    if ($try -eq 4) { Check "gcloud builds submit" }
    Write-Host "Build failed (attempt $try of 4). New permissions can take a few minutes to apply; waiting 90s and retrying..." -ForegroundColor Yellow
    Start-Sleep -Seconds 90
}
$digest = gcloud artifacts docker images describe "${image}:latest" --format="value(image_summary.digest)"
Check "read image digest"
Write-Host "Image: $image@$digest"

Step "4/5 Terraform: job, schedule, BigQuery, budget"
Push-Location infra
try {
    terraform apply -input=false -auto-approve @tfvars "-var" "image=$image@$digest"
    Check "terraform apply (phase 2)"
    $table = terraform output -raw bigquery_table
} finally { Pop-Location }

if ($SkipRun) { Write-Host "`nDeployed. The scan runs daily; start one now with: gcloud run jobs execute sda-daily-scan --region $Region --wait"; exit 0 }

Step "5/5 Run once now (takes a few minutes)"
gcloud run jobs execute sda-daily-scan --region $Region --wait
Check "gcloud run jobs execute"
$dataset = "${ProjectId}.supplier_disruption"
Write-Host "`nWhat landed in BigQuery:"
bq query --use_legacy_sql=false --format=pretty "SELECT * FROM ``$dataset.daily_summary`` LIMIT 7"
bq query --use_legacy_sql=false --format=pretty "SELECT news_id, status, event_type, ARRAY_LENGTH(supplier_ids) AS suppliers, LEFT(headline, 70) AS headline FROM ``$table`` ORDER BY run_at DESC LIMIT 15"
Write-Host "`nDone. Logs: https://console.cloud.google.com/run/jobs/details/$Region/sda-daily-scan/executions?project=$ProjectId"
