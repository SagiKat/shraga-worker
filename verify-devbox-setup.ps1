# Dev Box Setup Verification Script
# Run this after Dev Box provisioning to verify everything is configured correctly

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "Dev Box Setup Verification" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

$allPassed = $true

# Check Git
Write-Host "[1/6] Checking Git..." -ForegroundColor Yellow
try {
    $gitVersion = git --version
    Write-Host "  ✓ Git installed: $gitVersion" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Git not found" -ForegroundColor Red
    $allPassed = $false
}

# Check Claude Code
Write-Host "`n[2/6] Checking Claude Code..." -ForegroundColor Yellow
try {
    $claudeVersion = claude --version
    Write-Host "  ✓ Claude Code installed: $claudeVersion" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Claude Code not found" -ForegroundColor Red
    $allPassed = $false
}

# Check Node.js
Write-Host "`n[3/6] Checking Node.js..." -ForegroundColor Yellow
try {
    $nodeVersion = node --version
    Write-Host "  ✓ Node.js installed: $nodeVersion" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Node.js not found (optional)" -ForegroundColor Yellow
}

# Check Python
Write-Host "`n[4/6] Checking Python..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version
    Write-Host "  ✓ Python installed: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Python not found (optional)" -ForegroundColor Yellow
}

# Check OneDrive Silent Configuration
Write-Host "`n[5/6] Checking OneDrive Silent Account Configuration..." -ForegroundColor Yellow
try {
    $regPath = 'HKLM:\SOFTWARE\Policies\Microsoft\OneDrive'
    $silentConfig = Get-ItemProperty -Path $regPath -Name 'SilentAccountConfig' -ErrorAction SilentlyContinue

    if ($silentConfig.SilentAccountConfig -eq 1) {
        Write-Host "  ✓ OneDrive Silent Account Configuration: ENABLED" -ForegroundColor Green
    } else {
        Write-Host "  ✗ OneDrive Silent Account Configuration: DISABLED" -ForegroundColor Red
        $allPassed = $false
    }
} catch {
    Write-Host "  ✗ Unable to check OneDrive configuration" -ForegroundColor Red
    $allPassed = $false
}

# Check Primary Refresh Token (PRT) for SSO
Write-Host "`n[6/6] Checking SSO Status (Primary Refresh Token)..." -ForegroundColor Yellow
try {
    $dsregOutput = dsregcmd /status
    $azureAdPrt = $dsregOutput | Select-String "AzureAdPrt"

    if ($azureAdPrt -like "*YES*") {
        Write-Host "  ✓ Primary Refresh Token: PRESENT" -ForegroundColor Green
        Write-Host "  ✓ SSO to Microsoft 365 services should work" -ForegroundColor Green
    } else {
        Write-Host "  ! Primary Refresh Token: NOT FOUND" -ForegroundColor Yellow
        Write-Host "  ! SSO may not be fully configured (sign out/in may be needed)" -ForegroundColor Yellow
    }

    $azureAdJoined = $dsregOutput | Select-String "AzureAdJoined"
    if ($azureAdJoined -like "*YES*") {
        Write-Host "  ✓ Device is Azure AD Joined" -ForegroundColor Green
    }
} catch {
    Write-Host "  ! Unable to check SSO status" -ForegroundColor Yellow
}

# Summary
Write-Host "`n========================================" -ForegroundColor Cyan
if ($allPassed) {
    Write-Host "✓ All critical checks PASSED" -ForegroundColor Green
    Write-Host "`nNext Steps:" -ForegroundColor Yellow
    Write-Host "1. Sign out and back in to activate SSO" -ForegroundColor White
    Write-Host "2. Run 'claude /login' to authenticate Claude Code" -ForegroundColor White
    Write-Host "3. Check OneDrive is syncing automatically" -ForegroundColor White
} else {
    Write-Host "✗ Some checks FAILED" -ForegroundColor Red
    Write-Host "`nTroubleshooting:" -ForegroundColor Yellow
    Write-Host "- Verify customization tasks completed successfully" -ForegroundColor White
    Write-Host "- Check Windows Event Viewer for errors" -ForegroundColor White
    Write-Host "- Re-run customization or manually install missing components" -ForegroundColor White
}
Write-Host "========================================`n" -ForegroundColor Cyan

# Show Claude Code authentication status
Write-Host "`nChecking Claude Code authentication..." -ForegroundColor Yellow
try {
    claude auth status
} catch {
    Write-Host "Run 'claude /login' to authenticate" -ForegroundColor Yellow
}
