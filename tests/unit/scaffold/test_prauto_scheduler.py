"""Regression coverage for the detached PRauto scheduler wrapper.

The fixture is a clean, repo-shaped directory with a harmless heartbeat script.
It verifies wrapper dispatch mechanics without invoking an agent, GitHub, or network.

spec: approved PRauto scheduler-fix plan acceptance criterion: a clean checkout
dispatches successfully and creates its heartbeat log directory.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCHEDULER = ROOT / ".prauto/scheduler/prauto-heartbeat.sh"


def _wait_for_file(path: Path, *, timeout_seconds: float = 2) -> None:
    """Wait briefly for the detached fixture to prove it started."""
    deadline = time.monotonic() + timeout_seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), "detached heartbeat did not create its startup marker"


def _wait_for_text(path: Path, expected: str, *, timeout_seconds: float = 2) -> None:
    """Wait for the detached process's redirected output to be flushed."""
    deadline = time.monotonic() + timeout_seconds
    while (not path.exists() or expected not in path.read_text()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.is_file(), "scheduler did not create the detached heartbeat log"
    assert expected in path.read_text(), "detached heartbeat did not write to its log"


def _terminate_if_running(pid: int) -> None:
    """Prevent a failing assertion from leaving a fixture heartbeat behind."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGTERM)


def test_scheduler_dispatches_from_clean_repo_and_creates_heartbeat_log(tmp_path: Path) -> None:
    """A first scheduler tick creates runtime state before detached log redirection.

    spec: approved PRauto scheduler-fix plan acceptance criterion: a clean checkout
    dispatches successfully and creates its heartbeat log directory.
    """
    repo = tmp_path / "clean-repo"
    scheduler_dir = repo / ".prauto/scheduler"
    scheduler_dir.mkdir(parents=True)
    scheduler = scheduler_dir / "prauto-heartbeat.sh"
    shutil.copy2(SCHEDULER, scheduler)

    marker = tmp_path / "heartbeat-started"
    heartbeat = repo / ".prauto/heartbeat.sh"
    heartbeat.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "fixture heartbeat started\\n" > "$PRAUTO_TEST_MARKER"\n'
        'printf "fixture heartbeat started\\n"\n'
    )
    heartbeat.chmod(0o755)

    env = os.environ.copy()
    env["PRAUTO_TEST_MARKER"] = str(marker)
    result = subprocess.run(
        ["bash", str(scheduler)],
        capture_output=True,
        check=False,
        cwd=repo,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    pid_match = re.search(r"pid (\d+)", result.stdout)
    assert pid_match is not None, result.stdout
    heartbeat_pid = int(pid_match.group(1))

    try:
        _wait_for_file(marker)
        assert marker.read_text() == "fixture heartbeat started\n"
        log = repo / ".prauto/state/heartbeat_cron.log"
        _wait_for_text(log, "fixture heartbeat started")
    finally:
        _terminate_if_running(heartbeat_pid)
