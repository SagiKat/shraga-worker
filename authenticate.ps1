Write-Host ""
Write-Host "=== Shraga Authentication ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Step 1: Azure login..." -ForegroundColor Yellow
az login
Write-Host ""
Write-Host "Step 2: Claude Code login..." -ForegroundColor Yellow
claude /login
Write-Host ""
Write-Host "All done! You can close this window." -ForegroundColor Green
Read-Host "Press Enter to close"
