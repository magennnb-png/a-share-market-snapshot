# Native Git helpers for Windows PowerShell 5.1.
# Git success is determined only by the process exit code. Text written to
# stderr is retained as a warning and never becomes a terminating ErrorRecord.

function ConvertTo-NativeArgument([string]$Value) {
    if ($null -eq $Value -or $Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-GitExecutable {
    if ($env:A_SHARE_GIT_EXECUTABLE) { return $env:A_SHARE_GIT_EXECUTABLE }
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command git -ErrorAction SilentlyContinue }
    if (-not $command) { return $null }
    return $command.Source
}

function Invoke-NativeGit([string[]]$Arguments) {
    $executable = Get-GitExecutable
    if (-not $executable) {
        return [pscustomobject]@{
            ExitCode = 9009
            StdOut = ""
            StdErr = "Git is not installed or is not in PATH."
            Arguments = $Arguments
        }
    }

    $allArguments = @()
    if ($env:A_SHARE_GIT_PREFIX_ARGUMENTS) {
        $allArguments += ($env:A_SHARE_GIT_PREFIX_ARGUMENTS -split '\|')
    }
    $allArguments += $Arguments

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $executable
    $startInfo.Arguments = (($allArguments | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "Could not start Git." }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            StdOut = $stdout.TrimEnd("`r", "`n")
            StdErr = $stderr.TrimEnd("`r", "`n")
            Arguments = $Arguments
        }
    } catch {
        return [pscustomobject]@{
            ExitCode = 9009
            StdOut = ""
            StdErr = $_.Exception.Message
            Arguments = $Arguments
        }
    } finally {
        $process.Dispose()
    }
}

function Write-GitResult($Result) {
    if ($Result.StdOut) { Write-Host $Result.StdOut }
    if ($Result.StdErr) {
        if ($Result.ExitCode -eq 0) {
            Write-Host $Result.StdErr -ForegroundColor Yellow
        } else {
            Write-Host $Result.StdErr -ForegroundColor Red
        }
    }
}

function Get-GitFailureKind([string]$Text) {
    $value = ([string]$Text).ToLowerInvariant()
    if ($value -match 'curl\s+28|failed to connect|could not connect|couldn''t connect|could not resolve host|couldn''t resolve host|connection reset|connection timed out|timed out|network is unreachable|temporary failure in name resolution') {
        return "NETWORK"
    }
    if ($value -match 'non-fast-forward|not possible to fast-forward|cannot fast-forward|divergent branch|divergent branches|merge conflict|would be overwritten by merge|\[rejected\].*fetch first') {
        return "CONFLICT"
    }
    if ($value -match 'authentication failed|permission denied|could not read username|invalid username or password|repository not found') {
        return "AUTH"
    }
    return "OTHER"
}

function Invoke-Git([string[]]$Arguments) {
    $result = Invoke-NativeGit $Arguments
    Write-GitResult $result
    if ($result.ExitCode -ne 0) {
        $text = (($result.StdOut, $result.StdErr) -join "`n").Trim()
        $exception = New-Object System.Exception($text)
        $exception.Data["GitExitCode"] = $result.ExitCode
        $exception.Data["GitFailureKind"] = Get-GitFailureKind $text
        throw $exception
    }
    return $result
}

function Get-GitAheadCount([string]$Branch) {
    $verify = Invoke-NativeGit @("rev-parse", "--verify", "refs/remotes/origin/$Branch")
    if ($verify.ExitCode -ne 0) { return -1 }
    $count = Invoke-NativeGit @("rev-list", "--count", "origin/$Branch..HEAD")
    if ($count.ExitCode -ne 0) { return -1 }
    $parsed = 0
    if ([int]::TryParse($count.StdOut.Trim(), [ref]$parsed)) { return $parsed }
    return -1
}

function Invoke-PendingGitPush([string]$Branch, [bool]$ForceAttempt = $false) {
    $ahead = Get-GitAheadCount $Branch
    if ($ahead -eq 0 -and -not $ForceAttempt) {
        return [pscustomobject]@{ Status = "SKIPPED"; FailureKind = ""; Detail = "No unpushed commit."; ExitCode = 0 }
    }
    $result = Invoke-NativeGit @("push", "origin", $Branch)
    Write-GitResult $result
    if ($result.ExitCode -eq 0) {
        return [pscustomobject]@{ Status = "SUCCESS"; FailureKind = ""; Detail = $result.StdOut; ExitCode = 0 }
    }
    $detail = (($result.StdOut, $result.StdErr) -join "`n").Trim()
    return [pscustomobject]@{
        Status = "FAILED"
        FailureKind = Get-GitFailureKind $detail
        Detail = $detail
        ExitCode = $result.ExitCode
    }
}
