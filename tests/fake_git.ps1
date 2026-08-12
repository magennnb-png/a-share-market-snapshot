param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GitArguments
)

$scenario = $env:A_SHARE_GIT_TEST_SCENARIO
$command = $GitArguments -join " "

switch ($scenario) {
    "stdout_ok" {
        [Console]::Out.WriteLine("git stdout ok")
        exit 0
    }
    "warning_ok" {
        [Console]::Error.WriteLine("warning: harmless git warning")
        exit 0
    }
    "crlf_warning_ok" {
        [Console]::Error.WriteLine("warning: in the working copy of 'data/history/README.md', LF will be replaced by CRLF the next time Git touches it")
        exit 0
    }
    "network" {
        [Console]::Error.WriteLine("fatal: unable to access GitHub: curl 28 Failed to connect to github.com port 443")
        exit 128
    }
    "conflict" {
        [Console]::Error.WriteLine("fatal: Not possible to fast-forward, aborting. divergent branches")
        exit 128
    }
    "commit_ok" {
        if ($GitArguments[0] -eq "commit") {
            [Console]::Out.WriteLine("[main abc1234] market snapshot")
            exit 0
        }
        exit 2
    }
    "pending_network" {
        if ($GitArguments[0] -eq "rev-parse") { [Console]::Out.WriteLine("abc1234"); exit 0 }
        if ($GitArguments[0] -eq "rev-list") { [Console]::Out.WriteLine("1"); exit 0 }
        if ($GitArguments[0] -eq "push") {
            [Console]::Error.WriteLine("curl 28 Failed to connect to github.com port 443")
            exit 128
        }
        exit 2
    }
    "pending_recovery" {
        if ($GitArguments[0] -eq "rev-parse") { [Console]::Out.WriteLine("abc1234"); exit 0 }
        if ($GitArguments[0] -eq "rev-list") { [Console]::Out.WriteLine("1"); exit 0 }
        if ($GitArguments[0] -eq "push") { [Console]::Out.WriteLine("old commit uploaded"); exit 0 }
        exit 2
    }
    "no_pending" {
        if ($GitArguments[0] -eq "rev-parse") { [Console]::Out.WriteLine("abc1234"); exit 0 }
        if ($GitArguments[0] -eq "rev-list") { [Console]::Out.WriteLine("0"); exit 0 }
        if ($GitArguments[0] -eq "push") { [Console]::Error.WriteLine("push should not run"); exit 9 }
        exit 2
    }
    default {
        [Console]::Error.WriteLine("unknown fake scenario: $scenario ($command)")
        exit 2
    }
}
