from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "git_native.ps1"
FAKE_GIT = ROOT / "tests" / "fake_git.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def run_ps(scenario: str, expression: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["A_SHARE_GIT_EXECUTABLE"] = str(POWERSHELL)
    env["A_SHARE_GIT_PREFIX_ARGUMENTS"] = f"-NoLogo|-NoProfile|-ExecutionPolicy|Bypass|-File|{FAKE_GIT}"
    env["A_SHARE_GIT_TEST_SCENARIO"] = scenario
    command = f". '{HELPER}'; $result = & {{ {expression} }}; $result | ConvertTo-Json -Compress"
    return subprocess.run(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_git_stdout_exit_zero() -> None:
    data = payload(run_ps("stdout_ok", "Invoke-NativeGit @('status')"))
    assert data["ExitCode"] == 0
    assert data["StdOut"] == "git stdout ok"


def test_git_stderr_warning_exit_zero() -> None:
    data = payload(run_ps("warning_ok", "Invoke-NativeGit @('add','data/')"))
    assert data["ExitCode"] == 0
    assert "harmless" in data["StdErr"]


def test_lf_crlf_warning_does_not_throw() -> None:
    data = payload(run_ps("crlf_warning_ok", "Invoke-Git @('add','data/history/README.md')"))
    assert data["ExitCode"] == 0
    assert "LF will be replaced by CRLF" in data["StdErr"]


def test_curl_28_is_network_failure() -> None:
    data = payload(run_ps("network", "$r = Invoke-NativeGit @('pull','--ff-only'); [pscustomobject]@{ Kind = Get-GitFailureKind $r.StdErr; ExitCode = $r.ExitCode }"))
    assert data == {"Kind": "NETWORK", "ExitCode": 128}


def test_non_fast_forward_is_conflict() -> None:
    data = payload(run_ps("conflict", "$r = Invoke-NativeGit @('pull','--ff-only'); [pscustomobject]@{ Kind = Get-GitFailureKind $r.StdErr; ExitCode = $r.ExitCode }"))
    assert data == {"Kind": "CONFLICT", "ExitCode": 128}


def test_commit_success() -> None:
    data = payload(run_ps("commit_ok", "Invoke-Git @('commit','-m','market snapshot')"))
    assert data["ExitCode"] == 0
    assert "market snapshot" in data["StdOut"]


def test_push_network_failure_preserves_pending_status() -> None:
    data = payload(run_ps("pending_network", "Invoke-PendingGitPush 'main' $false"))
    assert data["Status"] == "FAILED"
    assert data["FailureKind"] == "NETWORK"
    assert data["ExitCode"] == 128


def test_next_run_uploads_old_commit_after_network_recovers() -> None:
    data = payload(run_ps("pending_recovery", "Invoke-PendingGitPush 'main' $false"))
    assert data["Status"] == "SUCCESS"
    assert data["ExitCode"] == 0


def test_no_pending_commit_skips_push() -> None:
    data = payload(run_ps("no_pending", "Invoke-PendingGitPush 'main' $false"))
    assert data["Status"] == "SKIPPED"
    assert data["ExitCode"] == 0
