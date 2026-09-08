#!/usr/bin/env python3
"""Detach a command as a daemon, immune to process-group teardown.

Usage: daemonize.py LOGFILE -- CMD [ARG ...]

Double-forks and calls setsid(2) so the command runs in its own session and
process group (re-parented to launchd), surviving the caller's process-group
SIGTERM — the Hermes terminal tool / cron scheduler tears down with killpg,
which misses setsid children. Prints the daemon PID to stdout and returns
immediately, so a supervisor can report the PID without tracking the child.

This is the Hermes scheduler binding's detach primitive; see
spec/AI_PRAUTO.md §Executor and Scheduler. Other bindings supply their own.
"""

from __future__ import annotations

import os
import sys


def _usage() -> int:
    sys.stderr.write("usage: daemonize.py LOGFILE -- CMD [ARG ...]\n")
    return 2


def main(argv: list[str]) -> int:
    if "--" not in argv:
        return _usage()
    sep = argv.index("--")
    logfile = argv[0]
    cmd = argv[sep + 1:]
    if not cmd:
        return _usage()

    # Open the log before forking so both forks share one append fd.
    log_fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)

    pid1 = os.fork()
    if pid1 > 0:
        # Original process: reap the intermediate, then return immediately.
        os.waitpid(pid1, 0)
        os.close(log_fd)
        os.close(devnull)
        return 0

    # Intermediate (child1): become a session leader, then fork the daemon.
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        # Report the daemon PID to the caller, then exit (reaped by original).
        sys.stdout.write(str(pid2) + "\n")
        sys.stdout.flush()
        os._exit(0)

    # Daemon (grandchild): own session, stdio redirected to the log.
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if log_fd > 2:
        os.close(log_fd)
    if devnull > 2:
        os.close(devnull)

    os.execvp(cmd[0], cmd)
    # execvp only returns on failure.
    os._exit(127)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
