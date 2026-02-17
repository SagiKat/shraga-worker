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
$CustomApiVersion = "2025-04-01-preview"

# Pre-check: az CLI must be installed
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Host "  Azure CLI (az) is required but not installed." -ForegroundColor Red
    Write-Host "  Install from: https://aka.ms/installazurecli" -ForegroundColor Yellow
    exit 1
}

# Helper: get fresh token
function Get-DevCenterToken {
    $t = az account get-access-token --resource "https://devcenter.azure.com" --query "accessToken" -o tsv
    return @{ "Authorization" = "Bearer $t"; "User-Agent" = "Shraga-Setup/1.0" }
}

# Step 1: Authenticate
Write-Host "[1/6] Authenticating..." -ForegroundColor Yellow
Write-Host "  A browser window will open. Sign in with your Microsoft account." -ForegroundColor Gray
az login --output none 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Authentication failed. Please try again." -ForegroundColor Red
    exit 1
}
$userEmail = az account show --query "user.name" -o tsv
Write-Host "  Signed in as: $userEmail" -ForegroundColor Green

# Step 2: Find next available dev box name (shraga-box-01, 02, 03...)
Write-Host ""
Write-Host "[2/6] Finding next available dev box..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$existingBoxes = (Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes?api-version=$ApiVersion" -Headers $headers).value
$shragaBoxes = @($existingBoxes | Where-Object { $_.name -match "^shraga-box-\d+$" })
$usedNumbers = @($shragaBoxes | ForEach-Object { [int]($_.name -replace "shraga-box-", "") })

$nextNum = 1
while ($usedNumbers -contains $nextNum) { $nextNum++ }
$DevBoxName = "shraga-box-{0:D2}" -f $nextNum

Write-Host "  Existing shraga boxes: $($shragaBoxes.Count)" -ForegroundColor Gray
Write-Host "  Creating: $DevBoxName" -ForegroundColor Green

# Step 3: Provision dev box
Write-Host ""
Write-Host "[3/6] Provisioning dev box..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$body = @{ poolName = $Pool } | ConvertTo-Json
try {
    Invoke-RestMethod -Method Put `
        -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" `
        -Headers ($headers + @{ "Content-Type" = "application/json" }) -Body $body | Out-Null
    Write-Host "  Provisioning started" -ForegroundColor Green
} catch {
    Write-Host "  Failed: $_" -ForegroundColor Red
    exit 1
}

Write-Host "  This takes ~25 minutes. Progress:" -ForegroundColor Gray
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($true) {
    Start-Sleep -Seconds 30
    $headers = Get-DevCenterToken
    $status = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName`?api-version=$ApiVersion" -Headers $headers
    $state = $status.provisioningState
    $elapsed = $sw.Elapsed.ToString("mm\:ss")
    Write-Host "  [$elapsed] $state" -ForegroundColor Gray
    if ($state -eq "Succeeded") { Write-Host "  Done!" -ForegroundColor Green; break }
    if ($state -eq "Failed") { Write-Host "  Provisioning failed." -ForegroundColor Red; exit 1 }
}

# Step 4: Install tools (customization group 1)
Write-Host ""
Write-Host "[4/6] Installing tools (Git, Claude Code, Python)..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$toolsBody = @{
    tasks = @(
        @{ name = "DevBox.Catalog/winget"; parameters = @{ package = "Git.Git" } },
        @{ name = "DevBox.Catalog/winget"; parameters = @{ package = "Anthropic.ClaudeCode" } },
        @{ name = "DevBox.Catalog/choco"; parameters = @{ package = "python312" } }
    )
} | ConvertTo-Json -Depth 3

try {
    Invoke-RestMethod -Method Put `
        -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-tools?api-version=$CustomApiVersion" `
        -Headers ($headers + @{ "Content-Type" = "application/json" }) -Body $toolsBody | Out-Null
    Write-Host "  Started" -ForegroundColor Green
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 409) { Write-Host "  Already done" -ForegroundColor Green }
    else { Write-Host "  Warning: $($_.Exception.Message)" -ForegroundColor Yellow }
}

Write-Host "  Waiting (~3-5 min)..." -ForegroundColor Gray
while ($true) {
    Start-Sleep -Seconds 15
    try {
        $headers = Get-DevCenterToken
        $cust = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-tools?api-version=$CustomApiVersion" -Headers $headers
        $s = $cust.status
        if ($s -eq "Succeeded" -or $s -eq "Failed") { Write-Host "  Tools: $s" -ForegroundColor $(if ($s -eq "Succeeded") {"Green"} else {"Yellow"}); break }
        if ($s -ne "NotStarted") { Write-Host "  [$s]" -NoNewline -ForegroundColor Gray }
    } catch { }
}

# Step 5: Deploy code + keep-alive + auth shortcut (customization group 2)
Write-Host ""
Write-Host "[5/6] Deploying code and worker..." -ForegroundColor Yellow
$headers = Get-DevCenterToken

$deployCmd = "Write-Output 'Keep-alive...'; " +
    "powercfg /change monitor-timeout-ac 0; powercfg /change standby-timeout-ac 0; " +
    "powercfg /change hibernate-timeout-ac 0; powercfg /change disk-timeout-ac 0; " +
    "powercfg /hibernate off; " +
    "reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services' /v fResetBroken /t REG_DWORD /d 0 /f; " +
    "Write-Output 'Cloning repo...'; " +
    "& 'C:\Program Files\Git\cmd\git.exe' clone --single-branch --depth 1 https://github.com/SagiKat/shraga-worker.git 'C:\Dev\shraga-worker'; " +
    "Write-Output 'Installing packages...'; " +
    "& 'C:\Python312\python.exe' -m pip install requests azure-identity azure-core watchdog; " +
    "Write-Output 'Registering worker...'; " +
    "`$action = New-ScheduledTaskAction -Execute 'C:\Python312\python.exe' -Argument 'C:\Dev\shraga-worker\integrated_task_worker.py' -WorkingDirectory 'C:\Dev\shraga-worker'; " +
    "`$trigger = New-ScheduledTaskTrigger -AtStartup; " +
    "Register-ScheduledTask -TaskName 'ShragaWorker' -Action `$action -Trigger `$trigger -User 'SYSTEM' -RunLevel Highest -Force; " +
    "Write-Output 'Creating auth shortcut...'; " +
    "Set-Content -Path 'C:\Users\Public\Desktop\Shraga-Authenticate.ps1' -Value @'" + "`n" +
    "Write-Host ''`n" +
    "Write-Host '=== Shraga Authentication ===' -ForegroundColor Cyan`n" +
    "Write-Host ''`n" +
    "Write-Host 'Step 1: Azure login...' -ForegroundColor Yellow`n" +
    "az login`n" +
    "Write-Host ''`n" +
    "Write-Host 'Step 2: Claude Code login...' -ForegroundColor Yellow`n" +
    "claude /login`n" +
    "Write-Host ''`n" +
    "Write-Host 'All done! You can close this window.' -ForegroundColor Green`n" +
    "Read-Host 'Press Enter to close'`n" +
    "'@; " +
    "`$ws = New-Object -ComObject WScript.Shell; " +
    "`$sc = `$ws.CreateShortcut('C:\Users\Public\Desktop\Shraga - Click to Authenticate.lnk'); " +
    "`$sc.TargetPath = 'powershell.exe'; " +
    "`$sc.Arguments = '-ExecutionPolicy Bypass -File C:\Users\Public\Desktop\Shraga-Authenticate.ps1'; " +
    "`$sc.Save(); " +
    "Write-Output 'Done'"

$deployBody = @{
    tasks = @(
        @{ name = "DevBox.Catalog/powershell"; parameters = @{ command = $deployCmd } }
    )
} | ConvertTo-Json -Depth 3

try {
    Invoke-RestMethod -Method Put `
        -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-deploy?api-version=$CustomApiVersion" `
        -Headers ($headers + @{ "Content-Type" = "application/json" }) -Body $deployBody | Out-Null
    Write-Host "  Started" -ForegroundColor Green
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 409) { Write-Host "  Already done" -ForegroundColor Green }
    else { Write-Host "  Warning: $($_.Exception.Message)" -ForegroundColor Yellow }
}

Write-Host "  Waiting (~1-2 min)..." -ForegroundColor Gray
while ($true) {
    Start-Sleep -Seconds 10
    try {
        $headers = Get-DevCenterToken
        $cust = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/customizationGroups/shraga-deploy?api-version=$CustomApiVersion" -Headers $headers
        $s = $cust.status
        if ($s -eq "Succeeded" -or $s -eq "Failed") { Write-Host "  Deploy: $s" -ForegroundColor $(if ($s -eq "Succeeded") {"Green"} else {"Yellow"}); break }
        if ($s -ne "NotStarted") { Write-Host "  [$s]" -NoNewline -ForegroundColor Gray }
    } catch { }
}

# Step 6: Show connection info
Write-Host ""
Write-Host "[6/6] Getting connection info..." -ForegroundColor Yellow
$headers = Get-DevCenterToken
$conn = Invoke-RestMethod -Uri "$DevCenterEndpoint/projects/$Project/users/me/devboxes/$DevBoxName/remoteConnection?api-version=$ApiVersion" -Headers $headers
$webUrl = $conn.webUrl

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Your dev box is ready: $DevBoxName" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Final step — authenticate on the dev box:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Open: $webUrl" -ForegroundColor Cyan
Write-Host '  2. Double-click "Shraga - Click to Authenticate" on the desktop' -ForegroundColor White
Write-Host ""
