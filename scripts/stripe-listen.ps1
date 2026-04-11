param(
    [string]$ForwardTo = "http://localhost:8000/billing/webhook"
)

Write-Host "Starting Stripe webhook forwarder..." -ForegroundColor Cyan
Write-Host "Forward target: $ForwardTo" -ForegroundColor Yellow
Write-Host "If needed, run: stripe login" -ForegroundColor Gray

stripe listen --forward-to $ForwardTo
