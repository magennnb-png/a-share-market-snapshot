param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("0905", "1105", "1405")]
    [string]$Slot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Generator = Join-Path $ProjectRoot "report_generator.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found: $Python"
}
if (-not (Test-Path -LiteralPath $Generator)) {
    throw "Report generator not found: $Generator"
}

Set-Location -LiteralPath $ProjectRoot

# 1. Refresh public market data without publishing an intermediate commit.
& $Python -m a_share_bridge.main --scheduled --no-publish
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# 2. Build the requested report, then 3. publish data and report together.
& $Python $Generator --slot $Slot --scheduled --publish
exit $LASTEXITCODE
