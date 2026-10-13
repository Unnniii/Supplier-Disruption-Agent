# Daily live scan for Windows Task Scheduler.
# Register once (PowerShell), changing the path to where the project lives:
#   schtasks /Create /SC DAILY /ST 07:30 /TN "SupplierDisruptionAgent" `
#     /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\supplier-disruption-agent\scripts\run_daily.ps1"
# Remove:   schtasks /Delete /TN "SupplierDisruptionAgent" /F
# The laptop must be on (or set the task to run when available).
# Use this OR the cloud scan (scripts/deploy_gcp.ps1), not both: they share one
# Gemini daily quota, and each keeps its own event memory (data/events.json here).

Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path logs | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
"===== $stamp =====" | Out-File -Append -Encoding utf8 logs\daily.log
uv run python -m agent.run --live --max-live 15 2>&1 | Out-File -Append -Encoding utf8 logs\daily.log
