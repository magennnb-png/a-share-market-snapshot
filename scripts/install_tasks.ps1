param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_snapshot.ps1"
$Times = @("08:58", "10:58", "13:58")

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Runner not found: $Runner"
}

foreach ($Time in $Times) {
    $Suffix = $Time.Replace(":", "")
    $TaskName = "A-Share-Market-Snapshot-$Suffix"
    $TaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
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

Write-Host "Created tasks at 08:58, 10:58 and 13:58."
Write-Host "The Python runner performs the mainland China holiday check before fetching data."
