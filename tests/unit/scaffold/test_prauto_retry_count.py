"""Regression coverage for PRauto retry state lifecycle scoping.

The shell snippets use a temporary PRauto state directory and make no network
or GitHub calls.

spec: spec/AI_PRAUTO.md §Retry tracking and §Issue restart protocol
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
STATE_LIBRARY = ROOT / ".prauto/lib/state.sh"
HELPERS_LIBRARY = ROOT / ".prauto/lib/helpers.sh"
ISSUE_NUMBER = "177"
FIRST_READY_TIMESTAMP = "2026-09-01T00:00:00Z"
SECOND_READY_TIMESTAMP = "2026-09-02T00:00:00Z"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a state-library snippet without sharing process or filesystem state."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ | (env or {}),
    )


def _source_state_library(state_root: Path, ready_timestamp: str) -> str:
    """Return shell setup for an isolated retry-state lifecycle."""
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_root))}",
            f"READY_LABEL_TIMESTAMP={shlex.quote(ready_timestamp)}",
            f"source {shlex.quote(str(HELPERS_LIBRARY))}",
            f"source {shlex.quote(str(STATE_LIBRARY))}",
            "ensure_state_dirs",
        ]
    )


def test_retry_count_increments_only_within_the_same_ready_lifecycle(tmp_path: Path) -> None:
    """A fresh shell process preserves the count only for its ready-label lifecycle.

    spec: spec/AI_PRAUTO.md §Retry tracking — genuine attempt starts consume the
    configured retry budget; §Issue restart protocol — a re-queued issue is
    a fresh lifecycle.
    """
    state_root = tmp_path / "prauto"
    first_attempt = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert first_attempt.returncode == 0, first_attempt.stderr

    same_lifecycle = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert same_lifecycle.returncode == 0, same_lifecycle.stderr

    persisted_same_lifecycle = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert persisted_same_lifecycle.returncode == 0, persisted_same_lifecycle.stderr


def test_retry_count_resets_when_ready_label_starts_a_new_lifecycle(tmp_path: Path) -> None:
    """A re-queued issue starts from retry zero rather than old local state.

    spec: spec/AI_PRAUTO.md §Issue restart protocol — restoring
    ``prauto:ready`` starts a new job lifecycle with a fresh retry budget.
    """
    state_root = tmp_path / "prauto"
    seeded = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert seeded.returncode == 0, seeded.stderr

    requeued = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, SECOND_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert requeued.returncode == 0, requeued.stderr

    persisted_requeue = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, SECOND_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert persisted_requeue.returncode == 0, persisted_requeue.stderr


def test_retry_count_reaches_four_before_the_next_dispatch_checks_the_limit(tmp_path: Path) -> None:
    """The fourth genuine attempt is persisted for the next dispatch boundary.

    spec: spec/AI_PRAUTO.md §Retry tracking — count three advances to four and
    the following dispatch observes four before it can start another worker.
    """
    state_root = tmp_path / "prauto"
    counter_file = state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.parent.mkdir(parents=True)
    counter_file.write_text(
        json.dumps(
            {
                "issue_number": int(ISSUE_NUMBER),
                "count": 3,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    fourth_attempt = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 3',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 4',
            ]
        )
    )
    assert fourth_attempt.returncode == 0, fourth_attempt.stderr
    next_dispatch = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 4',
            ]
        )
    )
    assert next_dispatch.returncode == 0, next_dispatch.stderr


def test_retry_count_recovers_from_missing_or_invalid_local_records(tmp_path: Path) -> None:
    """Invalid retry state never blocks or inflates the next valid attempt.

    spec: spec/AI_PRAUTO.md §Retry tracking — local recovery state is
    fail-safe and must not cause a job to be abandoned from corrupt state.
    """
    state_root = tmp_path / "prauto"
    missing = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
            ]
        )
    )
    assert missing.returncode == 0, missing.stderr

    counter_file = state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.write_text("{not-json\n")
    recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
                f"jq -e '.count == 1' {shlex.quote(str(counter_file))}",
            ]
        )
    )
    assert recovered.returncode == 0, recovered.stderr

    counter_file.write_text(
        '{"issue_number": 177, "count": 3, "last_updated": "2026-08-31T00:00:00Z"}\n'
    )
    legacy_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"COUNTER_FILE={shlex.quote(str(counter_file))}",
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert legacy_recovered.returncode == 0, legacy_recovered.stderr

    legacy_persisted = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert legacy_persisted.returncode == 0, legacy_persisted.stderr

    counter_file.write_text(
        '{"issue_number": 177, "count": 1.5, '
        f'"ready_label_timestamp": "{FIRST_READY_TIMESTAMP}", '
        '"last_updated": "2026-09-01T00:00:00Z"}\n'
    )
    fractional_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert fractional_recovered.returncode == 0, fractional_recovered.stderr

    fractional_persisted = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert fractional_persisted.returncode == 0, fractional_persisted.stderr

    counter_file.write_text(
        json.dumps(
            {
                "issue_number": 999,
                "count": 3,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    foreign_issue_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert foreign_issue_recovered.returncode == 0, foreign_issue_recovered.stderr


def test_quota_resume_and_repause_do_not_advance_an_existing_retry_count(tmp_path: Path) -> None:
    """Heartbeat's quota-resume branch bypasses normal retry dispatch.

    spec: spec/AI_PRAUTO.md §Retry tracking — quota-pause cycles bypass the
    normal dispatch path and do not consume a retry slot; §Quota-pause and
    resume — a resumed session may pause again when quota is exhausted.
    """
    prauto_copy = tmp_path / "prauto"
    shutil.copytree(PRAUTO, prauto_copy)
    (prauto_copy / "config.local.env").write_text(
        "\n".join(
            [
                "PRAUTO_WORKER_ID=worker",
                "PRAUTO_GIT_AUTHOR_NAME=worker",
                "PRAUTO_GIT_AUTHOR_EMAIL=worker@example.invalid",
                "PRAUTO_AGENT=claude",
                "PRAUTO_OPEN_ISSUE_LIMIT=1",
                "PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY=false",
                "PRAUTO_CLUSTER_PROVISION_ENABLED=false",
                "",
            ]
        )
    )
    seeded = _run_bash(
        "\n".join(
            [
                f"PRAUTO_DIR={shlex.quote(str(prauto_copy))}",
                f"READY_LABEL_TIMESTAMP={shlex.quote(FIRST_READY_TIMESTAMP)}",
                f"source {shlex.quote(str(prauto_copy / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(prauto_copy / 'lib/state.sh'))}",
                "ensure_state_dirs",
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert seeded.returncode == 0, seeded.stderr

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(
        "[\n"
        '  {"createdAt":"2026-09-01T00:01:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Plan\\n## Implementation Plan\\n- resume"},\n'
        '  {"createdAt":"2026-09-01T00:02:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Paused — claude quota exhausted.\\n'
        'prauto:quota-paused\\nprauto:agent=claude\\nprauto:session=prior-session"},\n'
        '  {"createdAt":"2026-09-01T00:02:30Z","author":{"login":"reviewer"},'
        '"body":"go ahead"}\n'
        "]\n"
    )
    before_comments = json.loads(comments_file.read_text())
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$1" == api && "$2" == user ]]; then printf 'worker\\n'; exit 0; fi
            if [[ "$1" == api ]]; then printf '%s\\n' "$READY_TIMESTAMP"; exit 0; fi
            if [[ "$1" == issue && "$2" == list ]]; then
              printf '%s\\n' '[{"number":177,"title":"retry","labels":[{"name":"prauto:wip"}]}]'
              exit 0
            fi
            if [[ "$1" == pr && "$2" == list ]]; then exit 0; fi
            if [[ "$1" == issue && "$2" == view ]]; then
              if [[ " $* " == *' --json labels '* ]]; then
                printf '{"labels":[{"name":"prauto:wip"}]}\\n'
              else
                cat "$GH_COMMENTS"
              fi
              exit 0
            fi
            if [[ "$1" == issue && "$2" == comment ]]; then
              while [[ "$1" != --body ]]; do shift; done; body="$2"
              jq --arg body "$body" '
                . + [{createdAt:"2026-09-01T00:03:00Z", author:{login:"worker"}, body:$body}]
              ' "$GH_COMMENTS" > "$GH_COMMENTS.tmp"
              mv "$GH_COMMENTS.tmp" "$GH_COMMENTS"
              exit 0
            fi
            exit 0
            """
        )
    )
    gh_stub.chmod(0o755)
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *' worktree add '* ]]; then mkdir -p \"${@: -2:1}\"; fi\n"
    )
    git_stub.chmod(0o755)
    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *' --resume '* ]]; then\n"
        "  printf '%s\\n' \"$@\" > \"$CLAUDE_RESUME_ARGS\"\n"
        "  printf '{\"is_error\":true,\"result\":\"quota exhausted\"}\\n'\n"
        "else\n"
        "  printf '{\"is_error\":false}\\n'\n"
        "fi\n"
    )
    claude_stub.chmod(0o755)

    result = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_RESUME_ARGS": str(tmp_path / "claude-resume-args.txt"),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert result.returncode == 0, result.stderr
    persisted_count = _run_bash(
        "\n".join(
            [
                f"PRAUTO_DIR={shlex.quote(str(prauto_copy))}",
                f"READY_LABEL_TIMESTAMP={shlex.quote(FIRST_READY_TIMESTAMP)}",
                f"source {shlex.quote(str(prauto_copy / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(prauto_copy / 'lib/state.sh'))}",
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert persisted_count.returncode == 0, persisted_count.stderr
    resume_args = (tmp_path / "claude-resume-args.txt").read_text().splitlines()
    assert "--resume" in resume_args
    assert resume_args[resume_args.index("--resume") + 1] == "prior-session"
    after_comments = json.loads(comments_file.read_text())
    new_comments = after_comments[len(before_comments) :]
    assert len(new_comments) == 2, result.stdout
    assert "Resumed" in new_comments[0]["body"]
    assert "prauto:quota-paused" in new_comments[1]["body"]
    assert "prauto:session=prior-session" in new_comments[1]["body"]


def test_normal_dispatch_waits_when_retry_state_cannot_be_persisted(tmp_path: Path) -> None:
    """A failed retry-state write prevents both heartbeat and agent dispatch.

    spec: spec/AI_PRAUTO.md §Retry tracking — normal dispatch increments before
    its heartbeat and worker invocation, and waits when that persistence fails.
    """
    prauto_copy = tmp_path / "prauto"
    shutil.copytree(PRAUTO, prauto_copy)
    (prauto_copy / "config.local.env").write_text(
        "\n".join(
            [
                "PRAUTO_WORKER_ID=worker",
                "PRAUTO_GIT_AUTHOR_NAME=worker",
                "PRAUTO_GIT_AUTHOR_EMAIL=worker@example.invalid",
                "PRAUTO_AGENT=claude",
                "PRAUTO_OPEN_ISSUE_LIMIT=1",
                "PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY=false",
                "PRAUTO_CLUSTER_PROVISION_ENABLED=false",
                "",
            ]
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(
        "[\n"
        '  {"createdAt":"2026-09-01T00:01:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Plan\\n## Implementation Plan\\n- work"},\n'
        '  {"createdAt":"2026-09-01T00:02:00Z","author":{"login":"reviewer"},'
        '"body":"go ahead"}\n'
        "]\n"
    )
    (bin_dir / "gh").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "$1" == api && "$2" == user ]]; then printf 'worker\\n'; exit 0; fi
            if [[ "$1" == api ]]; then printf '%s\\n' "$READY_TIMESTAMP"; exit 0; fi
            if [[ "$1" == issue && "$2" == list ]]; then
              printf '%s\\n' '[{"number":177,"title":"retry","labels":[{"name":"prauto:wip"}]}]'
              exit 0
            fi
            if [[ "$1" == pr && "$2" == list ]]; then exit 0; fi
            if [[ "$1" == issue && "$2" == view ]]; then
              if [[ " $* " == *' --json labels '* ]]; then
                printf '{"labels":[{"name":"prauto:wip"}]}\\n'
              else
                cat "$GH_COMMENTS"
              fi
              exit 0
            fi
            if [[ "$1" == issue && "$2" == comment ]]; then exit 97; fi
            exit 0
            """
        )
    )
    (bin_dir / "mktemp").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_dir / "git").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "claude").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> \"$CLAUDE_ARGS\"\n"
        "printf '{\"is_error\":false}\\n'\n"
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    claude_args = tmp_path / "claude-args.txt"
    result = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_ARGS": str(claude_args),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "could not persist retry state" in result.stdout
    assert "--session-id" not in claude_args.read_text()
    assert len(json.loads(comments_file.read_text())) == 2

    counter_file = prauto_copy / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.write_text(
        json.dumps(
            {
                "issue_number": int(ISSUE_NUMBER),
                "count": 4,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    claude_args.unlink()
    abandoned = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_ARGS": str(claude_args),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert abandoned.returncode == 0, abandoned.stderr
    assert "exceeded max retries (4/4)" in abandoned.stdout
    assert "--session-id" not in claude_args.read_text()
    assert len(json.loads(comments_file.read_text())) == 2
