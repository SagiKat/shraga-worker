# Shraga Dev Box Setup Script
# Usage: irm https://raw.githubusercontent.com/SagiKat/shraga-worker/main/setup.ps1 | iex

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host "  Shraga Dev Box Setup" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# Config
$DevCenterEndpoint = "https://72f988bf-86f1-41af-91ab-2d7cd011db47-devcenter-4l24zmpbcslv2-dc.westus3.devcenter.azure.com"
$Project = "PVA"
$Pool = "botdesigner-pool-italynorth"
$ApiVersion = "2024-05-01-preview"

# Step 1: Authenticate
Write-Host "[1/6] Authenticating..." -ForegroundColor Yellow
Write-Host "  A browser window will open. Sign in with your Microsoft account." -ForegroundColor Gray
az login --output none 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Authentication failed. Please try again." -ForegroundColor Red
    exit 1
}

$userEmail = az account show --query "user.name" -o tsv
$userOid = az ad signed-in-user show --query "id" -o tsv
Write-Host "  Signed in as: $userEmail" -ForegroundColor Green

# Step 2: Determine dev box name
$prefix = ($userEmail -split "@")[0] -replace "\.", "-"
$DevBoxName = "shraga-$prefix"
Write-Host ""
Write-Host "[2/6] Provisioning dev box: $DevBoxName" -ForegroundColor Yellow

# Check if already exists
$token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
$headers = @{ "Authorization" = "Bearer $token"; "User-Agent" = "Shraga-Setup/1.0" }

try {
    $existing = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" -Headers $headers -ErrorAction SilentlyContinue
    if ($existing.provisioningState -eq "Succeeded") {
        Write-Host "  Dev box already exists and is ready!" -ForegroundColor Green
        $skip_provision = $true
    } elseif ($existing.provisioningState) {
        Write-Host "  Dev box exists (state: $($existing.provisioningState))" -ForegroundColor Yellow
        $skip_provision = $true
    }
} catch {
    $skip_provision = $false
}

if (-not $skip_provision) {
    # Create dev box
    $body = @{ poolName = $Pool } | ConvertTo-Json
    try {
        Invoke-RestMethod -Method Put -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" `
            -Headers ($headers + @{ "Content-Type" = "application/json" }) -Body $body | Out-Null
        Write-Host "  Provisioning started!" -ForegroundColor Green
    } catch {
        Write-Host "  Failed to create dev box: $_" -ForegroundColor Red
        exit 1
    }

    # Wait for provisioning
    Write-Host "  Waiting for provisioning (this takes ~25 min)..." -ForegroundColor Gray
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        Start-Sleep -Seconds 30
        $token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
        $headers = @{ "Authorization" = "Bearer $token"; "User-Agent" = "Shraga-Setup/1.0" }
        $status = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" -Headers $headers
        $state = $status.provisioningState
        $elapsed = $sw.Elapsed.ToString("mm\:ss")
        Write-Host "  [$elapsed] $state" -ForegroundColor Gray
        if ($state -eq "Succeeded") { Write-Host "  Provisioned!" -ForegroundColor Green; break }
        if ($state -eq "Failed") { Write-Host "  Provisioning failed." -ForegroundColor Red; exit 1 }
    }
}

# Step 3: Apply customizations (tools)
Write-Host ""
Write-Host "[3/6] Installing tools (Git, Claude Code, Python)..." -ForegroundColor Yellow
$token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
$headers = @{ "Authorization" = "Bearer $token"; "Content-Type" = "application/json"; "User-Agent" = "Shraga-Setup/1.0" }

$toolsBody = @{
    tasks = @(
        @{ name = "DevBox.Catalog/winget"; parameters = @{ package = "Git.Git" } },
        @{ name = "DevBox.Catalog/winget"; parameters = @{ package = "Anthropic.ClaudeCode" } },
        @{ name = "DevBox.Catalog/choco"; parameters = @{ package = "python312" } }
    )
} | ConvertTo-Json -Depth 3

try {
    Invoke-RestMethod -Method Put -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-tools`?api-version=2025-04-01-preview" `
        -Headers $headers -Body $toolsBody | Out-Null
    Write-Host "  Tools installation started" -ForegroundColor Green
} catch {
    if ($_.Exception.Response.StatusCode -eq 409) {
        Write-Host "  Tools already installed" -ForegroundColor Green
    } else {
        Write-Host "  Warning: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# Wait for tools
Write-Host "  Waiting for tools installation (~3-5 min)..." -ForegroundColor Gray
while ($true) {
    Start-Sleep -Seconds 15
    $token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
    $headers = @{ "Authorization" = "Bearer $token"; "User-Agent" = "Shraga-Setup/1.0" }
    try {
        $cust = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-tools`?api-version=2025-04-01-preview" -Headers $headers
        if ($cust.status -eq "Succeeded" -or $cust.status -eq "Failed") {
            Write-Host "  Tools: $($cust.status)" -ForegroundColor $(if ($cust.status -eq "Succeeded") { "Green" } else { "Yellow" })
            break
        }
    } catch { }
}

# Step 4: Deploy code + keep-alive + worker
Write-Host ""
Write-Host "[4/6] Deploying code and configuring worker..." -ForegroundColor Yellow
$token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
$headers = @{ "Authorization" = "Bearer $token"; "Content-Type" = "application/json"; "User-Agent" = "Shraga-Setup/1.0" }

$deployCmd = @"
powercfg /change monitor-timeout-ac 0; powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; powercfg /change disk-timeout-ac 0; powercfg /hibernate off; reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' /v fResetBroken /t REG_DWORD /d 0 /f; & 'C:\Program Files\Git\cmd\git.exe' clone --single-branch --depth 1 https://github.com/SagiKat/shraga-worker.git 'C:\Dev\shraga-worker'; & 'C:\Python312\python.exe' -m pip install requests azure-identity azure-core watchdog; `$action = New-ScheduledTaskAction -Execute 'C:\Python312\python.exe' -Argument 'C:\Dev\shraga-worker\integrated_task_worker.py' -WorkingDirectory 'C:\Dev\shraga-worker'; `$trigger = New-ScheduledTaskTrigger -AtStartup; Register-ScheduledTask -TaskName 'ShragaWorker' -Action `$action -Trigger `$trigger -User 'SYSTEM' -RunLevel Highest -Force; Start-ScheduledTask -TaskName 'ShragaWorker'
"@

$deployBody = @{
    tasks = @(
        @{ name = "DevBox.Catalog/powershell"; parameters = @{ command = $deployCmd } }
    )
} | ConvertTo-Json -Depth 3

try {
    Invoke-RestMethod -Method Put -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-deploy`?api-version=2025-04-01-preview" `
        -Headers $headers -Body $deployBody | Out-Null
    Write-Host "  Deployment started" -ForegroundColor Green
} catch {
    if ($_.Exception.Response.StatusCode -eq 409) {
        Write-Host "  Already deployed" -ForegroundColor Green
    } else {
        Write-Host "  Warning: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# Wait for deploy
Write-Host "  Waiting for deployment (~1 min)..." -ForegroundColor Gray
while ($true) {
    Start-Sleep -Seconds 10
    $token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
    $headers = @{ "Authorization" = "Bearer $token"; "User-Agent" = "Shraga-Setup/1.0" }
    try {
        $cust = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-deploy`?api-version=2025-04-01-preview" -Headers $headers
        if ($cust.status -eq "Succeeded" -or $cust.status -eq "Failed") {
            Write-Host "  Deploy: $($cust.status)" -ForegroundColor $(if ($cust.status -eq "Succeeded") { "Green" } else { "Yellow" })
            break
        }
    } catch { }
}

# Step 5: Get RDP URL
Write-Host ""
Write-Host "[5/6] Getting connection info..." -ForegroundColor Yellow
$token = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
$headers = @{ "Authorization" = "Bearer $token"; "User-Agent" = "Shraga-Setup/1.0" }
$conn = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/remoteConnection`?api-version=2024-05-01-preview" -Headers $headers
$webUrl = $conn.webUrl
Write-Host "  Web RDP: $webUrl" -ForegroundColor Green

# Step 6: Auth on the dev box
Write-Host ""
Write-Host "[6/6] Final step: authenticate on your dev box" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Open this link in your browser:" -ForegroundColor White
Write-Host "  $webUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Then in the dev box, open PowerShell and run:" -ForegroundColor White
Write-Host "  az login" -ForegroundColor Cyan
Write-Host "  claude /login" -ForegroundColor Cyan
Write-Host ""
Write-Host "================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "  Your dev box: $DevBoxName" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green
