param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_report.ps1"
$Schedules = @(
    @{ Time = "09:05"; Slot = "0905" },
    @{ Time = "11:05"; Slot = "1105" },
    @{ Time = "14:05"; Slot = "1405" }
)

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Runner not found: $Runner"
}

# Remove legacy snapshot-only tasks so each scheduled run produces one atomic
# market-data + report commit instead of an intermediate snapshot commit.
foreach ($LegacySuffix in @("0858", "1058", "1358")) {
    schtasks.exe /Delete /TN "A-Share-Market-Snapshot-$LegacySuffix" /F 2>$null
}

foreach ($Schedule in $Schedules) {
    $Time = $Schedule.Time
    $Slot = $Schedule.Slot
    $TaskName = "A-Share-Market-Report-$Slot"
    $TaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Slot $Slot"
    $Arguments = @(
        "/Create", "/TN", $TaskName,
        "/TR", $TaskCommand,
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/ST", $Time,
        "/F"
    )
    & schtasks.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create scheduled task: $TaskName"
    }
}

Write-Host "Created report tasks at 09:05, 11:05 and 14:05."
Write-Host "Each task checks the trading calendar, fetches quotes, builds one report, then commits and pushes all outputs together."
