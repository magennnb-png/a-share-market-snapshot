from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def _run(args: list[str], root: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, encoding="utf-8", errors="replace", capture_output=True, check=check)


def publish_outputs(
    root: Path,
    output_paths: list[Path],
    generated_at: datetime,
    commit_message: str | None = None,
) -> dict[str, Any]:
    relative = [str(path.relative_to(root)).replace("\\", "/") for path in output_paths]
    result: dict[str, Any] = {"staged": False, "committed": False, "pushed": False, "errors": []}
    try:
        _run(["git", "add", "--", *relative], root)
        result["staged"] = True
        diff = _run(["git", "diff", "--cached", "--quiet", "--", *relative], root, check=False)
        if diff.returncode == 0:
            result["message"] = "生成文件无变化，无需提交"
            return result
        else:
            message = commit_message or f"data: update A-share snapshot {generated_at:%Y-%m-%d %H:%M}"
            commit = _run(["git", "commit", "-m", message, "--", *relative], root)
            result["committed"] = True
            result["commit_output"] = commit.stdout.strip()
        branch = _run(["git", "branch", "--show-current"], root).stdout.strip()
        if not branch:
            raise subprocess.CalledProcessError(1, ["git", "branch", "--show-current"], stderr="detached HEAD，不能自动 push")
        push = _run(["git", "push", "origin", branch], root)
        result["pushed"] = True
        result["push_output"] = (push.stdout + push.stderr).strip()
    except subprocess.CalledProcessError as exc:
        result["errors"].append((exc.stdout + exc.stderr).strip() or str(exc))
    return result
