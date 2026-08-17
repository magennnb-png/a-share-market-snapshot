[CmdletBinding()]
param(
    [switch]$SkipPull,
    [switch]$NoGit,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ProjectRoot "scripts\git_native.ps1")

$MarketStatus = "NOT RUN"
$PullStatus = "NOT RUN"
$CommitStatus = "NOT RUN"
$CommitSha = "-"
$PushStatus = "NOT RUN"
$PublishDetail = ""
$FinalExitCode = 0

function Stop-BeforeMarket([string]$Step, [string]$Message, [int]$Code = 1) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "A-share market update STOPPED" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "Failed step: $Step"
    Write-Host $Message
    Write-Host "No market update was published."
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $Code
}

Set-Location -LiteralPath $ProjectRoot
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

if (-not $NoGit) {
    $version = Invoke-NativeGit @("--version")
    if ($version.ExitCode -ne 0) { Stop-BeforeMarket "Git check" $version.StdErr }
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
        if (-not $command) { Stop-BeforeMarket "Python check" "Python 3.11+ was not found. Install Python and enable Add Python to PATH." }
        $Python = $command.Source
    }
}

& $Python @PythonArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { Stop-BeforeMarket "Python check" "Python 3.11 or newer is required." }

& $Python @PythonArgs -c "import requests, yaml, chinese_calendar, tzdata" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Missing Python packages. Installing requirements.txt..." -ForegroundColor Yellow
    & $Python @PythonArgs -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Stop-BeforeMarket "Dependency install" "pip install failed. Check the network and requirements.txt." }
}

$branch = ""
if (-not $NoGit) {
    $branchResult = Invoke-NativeGit @("branch", "--show-current")
    if ($branchResult.ExitCode -ne 0 -or -not $branchResult.StdOut.Trim()) {
        Stop-BeforeMarket "Git branch check" "Detached HEAD or branch lookup failed. Automatic publishing is disabled."
    }
    $branch = $branchResult.StdOut.Trim()

    if (-not $SkipPull) {
        Write-Host "[Git] git pull --ff-only..."
        $pull = Invoke-NativeGit @("pull", "--ff-only")
        Write-GitResult $pull
        if ($pull.ExitCode -eq 0) {
            $PullStatus = "SUCCESS"
        } else {
            $pullDetail = (($pull.StdOut, $pull.StdErr) -join "`n").Trim()
            $pullKind = Get-GitFailureKind $pullDetail
            if ($pullKind -eq "CONFLICT") {
                Stop-BeforeMarket "git pull --ff-only" "Remote and local Git history diverged. Resolve synchronization manually. No merge, reset, or force push was attempted.`n$pullDetail"
            }
            $PullStatus = "FAILED - $pullKind (CONTINUING LOCALLY)"
            Write-Host "Git pull is temporarily unavailable. Market collection will continue locally." -ForegroundColor Yellow
        }
    } else { $PullStatus = "SKIPPED" }

    # Retry commits left ahead by an earlier network outage before collecting
    # new data. Network/auth failures do not block market collection.
    $pending = Invoke-PendingGitPush $branch $false
    if ($pending.Status -eq "SUCCESS") {
        $PushStatus = "SUCCESS (PREVIOUS LOCAL COMMIT)"
    } elseif ($pending.Status -eq "FAILED") {
        if ($pending.FailureKind -eq "CONFLICT") {
            Stop-BeforeMarket "push pending commit" "Local and remote Git history conflict. Resolve synchronization manually.`n$($pending.Detail)"
        }
        $PushStatus = "FAILED - $($pending.FailureKind) (WILL RETRY AFTER UPDATE)"
    }
}

Write-Host "[Market] Fetching current data and rebuilding rolling history windows..."
& $Python @PythonArgs (Join-Path $ProjectRoot "update_market.py")
if ($LASTEXITCODE -ne 0) { Stop-BeforeMarket "Market collection or validation" "The Python updater failed. See the source/network/validation error above." $LASTEXITCODE }
$MarketStatus = "SUCCESS"

if (-not $NoGit) {
    $createdCommit = $false
    try {
        Invoke-Git @("add", "--", "data/") | Out-Null
        $diff = Invoke-NativeGit @("diff", "--cached", "--quiet", "--", "data/")
        if ($diff.ExitCode -eq 1) {
            $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm 'CST'"
            Invoke-Git @("commit", "-m", "market snapshot: $timestamp", "--", "data/") | Out-Null
            $sha = Invoke-Git @("rev-parse", "--short", "HEAD")
            $CommitSha = $sha.StdOut.Trim()
            $CommitStatus = "SUCCESS"
            $createdCommit = $true
        } elseif ($diff.ExitCode -eq 0) {
            $CommitStatus = "SKIPPED (NO DATA CHANGE)"
        } else {
            throw "git diff --cached failed with exit code $($diff.ExitCode): $($diff.StdErr)"
        }
    } catch {
        $CommitStatus = "FAILED - LOCAL GIT"
        $PushStatus = "SKIPPED (COMMIT FAILED)"
        $PublishDetail = $_.Exception.Message
    }

    if ($CommitStatus -notlike "FAILED*") {
        # Always retry once after a pull/network problem. The remote-tracking
        # ref can be stale when pull could not reach GitHub, so an ahead count
        # of zero is not authoritative in that case.
        $forcePushAttempt = $createdCommit -or ($PullStatus -like "FAILED*") -or ($PushStatus -like "FAILED*")
        $push = Invoke-PendingGitPush $branch $forcePushAttempt
        if ($push.Status -eq "SUCCESS") {
            $PushStatus = "SUCCESS"
        } elseif ($push.Status -eq "SKIPPED") {
            if ($PushStatus -notlike "SUCCESS*") {
                $PushStatus = "SKIPPED (NO UNPUSHED COMMIT)"
            }
        } else {
            $PushStatus = "FAILED - $($push.FailureKind)"
            $PublishDetail = $push.Detail
            if ($push.FailureKind -eq "CONFLICT") { $FinalExitCode = 3 }
        }
    }
} else {
    $PullStatus = "SKIPPED (TEST MODE)"
    $CommitStatus = "SKIPPED (TEST MODE)"
    $PushStatus = "SKIPPED (TEST MODE)"
}

try {
    $Snapshot = Get-Content -Raw -Encoding UTF8 (Join-Path $ProjectRoot "data\latest.json") | ConvertFrom-Json
    $Breadth = $Snapshot.market_breadth
    $Shanghai = $Snapshot.indices | Where-Object { $_.symbol -eq "000001" } | Select-Object -First 1
    $ChiNext = $Snapshot.indices | Where-Object { $_.symbol -eq "399006" } | Select-Object -First 1
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "A-share market update COMPLETE" -ForegroundColor Green
    Write-Host "========================================"
    Write-Host "Market update:     $MarketStatus"
    Write-Host "Git pull:          $PullStatus"
    Write-Host "Git commit:        $CommitStatus"
    Write-Host "Git commit SHA:    $CommitSha"
    Write-Host "Git push:          $PushStatus"
    Write-Host "Market time:       $($Snapshot.market_time)"
    Write-Host "Generated at:      $($Snapshot.generated_at)"
    Write-Host "SSE Composite:     $($Shanghai.last)"
    Write-Host "ChiNext:           $($ChiNext.last)"
    Write-Host "Advancers:         $($Breadth.up)"
    Write-Host "Decliners:         $($Breadth.down)"
    Write-Host ("Market turnover:   {0:N2} CNY 100m" -f ([double]$Breadth.turnover_cny / 100000000))
    Write-Host "Sources:           $($Snapshot.sources -join ' / ')"
    $ExpectedOutputs = @(
        "latest.json", "latest_intraday.json", "latest_rotation.json", "latest_rotation.md",
        "research_context\market_technical.json", "research_context\rotation_context.json",
        "research_context\market_breadth_context.json", "research_context\watchlist_context.json"
    )
    foreach ($name in $ExpectedOutputs) {
        $status = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "data\$name")) { "OK" } else { "MISSING" }
        Write-Host ("{0,-50} {1}" -f $name, $status)
    }
    if ($PushStatus -like "FAILED*") {
        Write-Host ""
        Write-Host "Market data was updated successfully on this computer." -ForegroundColor Yellow
        Write-Host "The local commit/data is preserved. GitHub upload will be retried next time." -ForegroundColor Yellow
        if ($PublishDetail) { Write-Host $PublishDetail }
        if ($PushStatus -like "*CONFLICT*") {
            Write-Host "Remote and local Git history conflict. Resolve synchronization manually." -ForegroundColor Red
            Write-Host "No merge, reset, force push, or remote overwrite was attempted." -ForegroundColor Red
        }
    }
    Write-Host "========================================"
    Write-Host "Local market data is ready for analysis." -ForegroundColor Green
    Write-Host "========================================"
} catch {
    Write-Host "Result display failed, but market data remains preserved: $($_.Exception.Message)" -ForegroundColor Red
}

if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
# Publishing failures are reported above but do not turn a successful market
# collection into a failed process. Pre-market conflicts and collection errors
# exit earlier with a non-zero code.
exit $FinalExitCode
