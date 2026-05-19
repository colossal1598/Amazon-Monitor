$ErrorActionPreference = "Stop"

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:EcosystemFile = Join-Path $script:ProjectRoot "ecosystem.config.cjs"
$script:StackApps = @("amazon-monitor", "wa-server", "monitor-healthcheck")

function Ensure-Pm2Available {
    if (-not (Get-Command pm2 -ErrorAction SilentlyContinue)) {
        throw "PM2 is not installed or not in PATH. Install it with: npm install -g pm2"
    }
}

function Invoke-Pm2 {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args,
        [string]$FailureMessage = "PM2 command failed."
    )

    & pm2 @Args
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage Command: pm2 $($Args -join ' ')"
    }
}

function Get-Pm2ProcessNames {
    Ensure-Pm2Available

    $json = & pm2 jlist
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($json)) {
        return @()
    }

    try {
        $items = $json | ConvertFrom-Json
    } catch {
        return @()
    }

    if ($items -isnot [System.Array]) {
        $items = @($items)
    }

    return @(
        $items |
            ForEach-Object { $_.name } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

function Start-Stack {
    Ensure-Pm2Available

    if (-not (Test-Path $script:EcosystemFile)) {
        throw "ecosystem.config.cjs not found at: $script:EcosystemFile"
    }

    $currentNames = Get-Pm2ProcessNames
    $missingApps = $script:StackApps | Where-Object { $_ -notin $currentNames }

    if ($missingApps.Count -gt 0) {
        Write-Host "Starting PM2 stack from ecosystem file..."
        Invoke-Pm2 -Args @("start", $script:EcosystemFile) -FailureMessage "Could not start PM2 stack."
        return
    }

    Write-Host "All stack apps already registered in PM2. Restarting stack apps..."
    foreach ($app in $script:StackApps) {
        Invoke-Pm2 -Args @("restart", $app) -FailureMessage "Could not restart PM2 app '$app'."
    }
}

function Start-Monitor {
    Ensure-Pm2Available
    $exists = (Get-Pm2ProcessNames) -contains "amazon-monitor"

    if ($exists) {
        Invoke-Pm2 -Args @("start", "amazon-monitor") -FailureMessage "Could not start amazon-monitor."
        return
    }

    Write-Host "amazon-monitor is not registered in PM2. Starting full ecosystem..."
    Start-Stack
}

function Stop-Monitor {
    Ensure-Pm2Available
    $exists = (Get-Pm2ProcessNames) -contains "amazon-monitor"

    if (-not $exists) {
        Write-Host "amazon-monitor is not registered in PM2."
        return
    }

    Invoke-Pm2 -Args @("stop", "amazon-monitor") -FailureMessage "Could not stop amazon-monitor."
}

function Restart-Monitor {
    Ensure-Pm2Available
    $exists = (Get-Pm2ProcessNames) -contains "amazon-monitor"

    if ($exists) {
        Invoke-Pm2 -Args @("restart", "amazon-monitor") -FailureMessage "Could not restart amazon-monitor."
        return
    }

    Write-Host "amazon-monitor is not registered in PM2. Starting full ecosystem..."
    Start-Stack
}

function Stop-All {
    Ensure-Pm2Available
    Invoke-Pm2 -Args @("stop", "all") -FailureMessage "Could not stop PM2 apps."
}

function Save-Pm2State {
    Ensure-Pm2Available
    Invoke-Pm2 -Args @("save") -FailureMessage "Could not save PM2 process list."
}
