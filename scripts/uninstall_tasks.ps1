$ErrorActionPreference = "Stop"
foreach ($Suffix in @("0858", "1058", "1358")) {
    $TaskName = "A-Share-Market-Snapshot-$Suffix"
    schtasks.exe /Delete /TN $TaskName /F 2>$null
}
foreach ($Slot in @("0905", "1105", "1405")) {
    $TaskName = "A-Share-Market-Report-$Slot"
    schtasks.exe /Delete /TN $TaskName /F 2>$null
}
