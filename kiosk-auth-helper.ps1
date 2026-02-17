# Kiosk Authentication Helper for Dev Box
# This script manages Claude Code authentication in kiosk mode

param(
    [Parameter(Mandatory=$false)]
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Start'
)

$ErrorActionPreference = 'Stop'

# Configuration
$KIOSK_LOCK_FILE = "C:\ProgramData\shraga\kiosk-auth.lock"
$CLAUDE_AUTH_CHECK = "C:\Users\$env:USERNAME\.config\claude-code\auth.json"

function Start-KioskAuth {
    """Start Claude Code authentication in kiosk mode"""

    Write-Host "=== Starting Kiosk Authentication ===" -ForegroundColor Cyan

    # Check if already running
    if (Test-Path $KIOSK_LOCK_FILE) {
        Write-Host "⚠️  Kiosk authentication already running" -ForegroundColor Yellow
        return
    }

    # Create lock file
    New-Item -ItemType Directory -Force -Path (Split-Path $KIOSK_LOCK_FILE) | Out-Null
    Set-Content -Path $KIOSK_LOCK_FILE -Value (Get-Date).ToString()

    try {
        # Kill any existing Chrome kiosk instances
        Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*--kiosk*" } | Stop-Process -Force

        # Start Claude Code auth in background
        Write-Host "Starting Claude Code authentication server..." -ForegroundColor Yellow
        Start-Process powershell -ArgumentList "-WindowStyle Hidden -Command `"claude /login`"" -PassThru

        # Wait for server to start
        Start-Sleep -Seconds 3

        # Check if localhost:8080 is responding
        $maxRetries = 10
        $retry = 0
        while ($retry -lt $maxRetries) {
            try {
                $response = Invoke-WebRequest -Uri "http://localhost:8080" -TimeoutSec 2 -UseBasicParsing
                Write-Host "✓ Claude Code auth server is ready" -ForegroundColor Green
                break
            } catch {
                $retry++
                Write-Host "Waiting for auth server... ($retry/$maxRetries)" -ForegroundColor Gray
                Start-Sleep -Seconds 2
            }
        }

        if ($retry -eq $maxRetries) {
            throw "Claude Code auth server failed to start"
        }

        # Start Chrome in kiosk mode
        Write-Host "Launching kiosk browser..." -ForegroundColor Yellow

        $chromeArgs = @(
            "--kiosk",
            "--app=http://localhost:8080",
            "--no-first-run",
            "--disable-translate",
            "--disable-infobars",
            "--disable-features=TranslateUI",
            "--disable-save-password-bubble"
        )

        Start-Process chrome.exe -ArgumentList $chromeArgs

        Write-Host "✓ Kiosk browser launched" -ForegroundColor Green
        Write-Host ""
        Write-Host "🌐 User should now connect via browser RDP to complete authentication" -ForegroundColor Cyan
        Write-Host "Press ESC in the browser to exit kiosk mode" -ForegroundColor Gray

    } catch {
        Write-Host "✗ Error: $_" -ForegroundColor Red
        Remove-Item -Path $KIOSK_LOCK_FILE -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Stop-KioskAuth {
    """Stop kiosk authentication"""

    Write-Host "=== Stopping Kiosk Authentication ===" -ForegroundColor Cyan

    # Kill Chrome kiosk instances
    Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*--kiosk*" } | Stop-Process -Force

    # Remove lock file
    Remove-Item -Path $KIOSK_LOCK_FILE -Force -ErrorAction SilentlyContinue

    Write-Host "✓ Kiosk authentication stopped" -ForegroundColor Green
}

function Get-AuthStatus {
    """Check Claude Code authentication status"""

    Write-Host "=== Claude Code Authentication Status ===" -ForegroundColor Cyan

    # Check if auth file exists
    if (Test-Path $CLAUDE_AUTH_CHECK) {
        Write-Host "✓ Authenticated" -ForegroundColor Green

        # Get auth file details
        $authFile = Get-Item $CLAUDE_AUTH_CHECK
        Write-Host "Auth file created: $($authFile.CreationTime)" -ForegroundColor Gray
        Write-Host "Last modified: $($authFile.LastWriteTime)" -ForegroundColor Gray

        return $true
    } else {
        Write-Host "✗ Not authenticated" -ForegroundColor Red

        # Check if kiosk is running
        if (Test-Path $KIOSK_LOCK_FILE) {
            Write-Host "⏳ Kiosk authentication in progress" -ForegroundColor Yellow
        }

        return $false
    }
}

# Main execution
switch ($Action) {
    'Start' { Start-KioskAuth }
    'Stop' { Stop-KioskAuth }
    'Status' { Get-AuthStatus }
}
