"""Shared fixtures.

orca is a client, so there is nothing here that stands in for a backend beyond the doubles each
test writes for itself. The one shared helper is Git, because workspace discovery reads a real
repository rather than a description of one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()
