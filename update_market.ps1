[CmdletBinding()]
param(
    [switch]$SkipPull,
    [switch]$NoGit,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$CommitSha = "-"
$PushStatus = "NOT RUN"

function Stop-WithMessage([string]$Step, [string]$Message, [int]$Code = 1) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "A-share market update FAILED" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "Failed step: $Step"
    Write-Host $Message
    Write-Host "No bad market data will be committed."
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $Code
}

function Invoke-Git([string[]]$Arguments) {
    $output = & git @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw (($output | Out-String).Trim()) }
    return $output
}

Set-Location -LiteralPath $ProjectRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

if (-not $NoGit) {
    try {
        & git --version *> $null
        if ($LASTEXITCODE -ne 0) { throw "Git is not installed or is not in PATH." }
    } catch { Stop-WithMessage "Git check" $_.Exception.Message }
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonArgs = @()
if (-not (Test-Path -LiteralPath $Python)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $Python = $launcher.Source
        $PythonArgs = @("-3")
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if (-not $command) { Stop-WithMessage "Python check" "Python 3.11+ was not found. Install Python and enable Add Python to PATH." }
        $Python = $command.Source
    }
}

& $Python @PythonArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Python check" "Python 3.11 or newer is required." }

& $Python @PythonArgs -c "import requests, yaml, chinese_calendar, tzdata" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Missing Python packages. Installing requirements.txt..." -ForegroundColor Yellow
    & $Python @PythonArgs -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Dependency install" "pip install failed. Check the network and requirements.txt." }
}

if (-not $SkipPull -and -not $NoGit) {
    Write-Host "[Git] git pull --ff-only..."
    try { Invoke-Git @("pull", "--ff-only") | Write-Host }
    catch { Stop-WithMessage "git pull --ff-only" "Remote and local history cannot fast-forward. Resolve Git synchronization manually.`n$($_.Exception.Message)" }
}

Write-Host "[Market] Fetching current data and rebuilding rolling history windows..."
& $Python @PythonArgs (Join-Path $ProjectRoot "update_market.py")
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Market collection or validation" "The Python updater failed. See the source/network/validation error above." $LASTEXITCODE }

if (-not $NoGit) {
    try {
        Invoke-Git @("add", "--", "data/") | Out-Null
        & git diff --cached --quiet -- data/
        $diffExit = $LASTEXITCODE
        if ($diffExit -eq 1) {
            $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm 'CST'"
            Invoke-Git @("commit", "-m", "market snapshot: $timestamp", "--", "data/") | Write-Host
            $CommitSha = ((Invoke-Git @("rev-parse", "--short", "HEAD")) | Select-Object -First 1).Trim()
        } elseif ($diffExit -eq 0) {
            Write-Host "Market data has no material change; no new commit is needed." -ForegroundColor Yellow
        } else { throw "git diff --cached returned unexpected code $diffExit" }

        $branch = ((Invoke-Git @("branch", "--show-current")) | Select-Object -First 1).Trim()
        if (-not $branch) { throw "Detached HEAD: automatic push is disabled." }
        $ahead = 0
        & git rev-parse --abbrev-ref "@{upstream}" *> $null
        if ($LASTEXITCODE -eq 0) {
            $aheadText = ((Invoke-Git @("rev-list", "--count", "@{upstream}..HEAD")) | Select-Object -First 1).Trim()
            $ahead = [int]$aheadText
        } elseif ($diffExit -eq 1) { $ahead = 1 }
        if ($ahead -gt 0) {
            Invoke-Git @("push", "origin", $branch) | Write-Host
            $PushStatus = "SUCCESS"
        } else { $PushStatus = "SKIPPED (NO UNPUSHED COMMIT)" }
    } catch { Stop-WithMessage "git commit/push" $_.Exception.Message }
} else { $PushStatus = "SKIPPED (TEST MODE)" }

try {
    $Snapshot = Get-Content -Raw -Encoding UTF8 (Join-Path $ProjectRoot "data\latest.json") | ConvertFrom-Json
    $Breadth = $Snapshot.market_breadth
    $Shanghai = $Snapshot.indices | Where-Object { $_.symbol -eq "000001" } | Select-Object -First 1
    $ChiNext = $Snapshot.indices | Where-Object { $_.symbol -eq "399006" } | Select-Object -First 1
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "A-share market update COMPLETE" -ForegroundColor Green
    Write-Host "========================================"
    Write-Host "Market time:       $($Snapshot.market_time)"
    Write-Host "Generated at:      $($Snapshot.generated_at)"
    Write-Host "SSE Composite:     $($Shanghai.last)"
    Write-Host "ChiNext:           $($ChiNext.last)"
    Write-Host "Advancers:         $($Breadth.up)"
    Write-Host "Decliners:         $($Breadth.down)"
    Write-Host ("Market turnover:   {0:N2} CNY 100m" -f ([double]$Breadth.turnover_cny / 100000000))
    Write-Host "Sources:           $($Snapshot.sources -join ' / ')"
    foreach ($name in @("latest.json", "latest_intraday.json", "latest_rotation.json", "latest_rotation.md")) {
        $status = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "data\$name")) { "OK" } else { "MISSING" }
        Write-Host ("{0,-30} {1}" -f $name, $status)
    }
    Write-Host "Git Commit:        $CommitSha"
    Write-Host "Git Push:          $PushStatus"
    Write-Host "========================================"
    Write-Host "The latest repository data is ready for ChatGPT." -ForegroundColor Green
    Write-Host "========================================"
} catch { Stop-WithMessage "Result display" $_.Exception.Message }

if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
exit 0
