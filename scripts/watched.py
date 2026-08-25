#!/usr/bin/env python3
"""watched.py: stall watchdog wrapper for long-running pipeline commands.

Runs a command, streams its combined stdout/stderr through unchanged, and
tracks output *freshness*. If the child produces no output for --stale-secs,
the wrapper prints a one-line diagnosis, kills the child's process group, and
exits 99, so a stall becomes a loud, bounded failure instead of a silent hang.

Why this exists: network scripts can sleep inside retry machinery (server
Retry-After honored between retries, outside every request timeout). CappedRetry
bounds each sleep, but a watchdog is the backstop for any stall class we have
not met yet, and the freshness signal (not the content) is what detects it.

Usage:
  python3 scripts/watched.py [--stale-secs 300] -- <command> [args...]

  python3 scripts/watched.py --stale-secs 300 -- \
      python3 scripts/verify_citations.py research/slug/sections/ \
      --output research/slug/round4/citation-verification.md --check-urls

Exit codes: the child's own exit code on normal completion; 99 on a stall kill;
2 on usage error.

Sizing --stale-secs: stay ABOVE the wrapped command's legitimate worst-case
quiet period. With CappedRetry (30s cap x 4 retries) a throttled entry can be
legitimately silent for ~120s, so the 300s default will not false-alarm; a
false kill wastes a whole rerun, so prefer too high over too low.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time


def main():
    ap = argparse.ArgumentParser(
        description="Run a command; kill it if its output goes stale.")
    ap.add_argument("--stale-secs", type=int, default=300,
                    help="kill after this many seconds without output (default 300)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="-- followed by the command to run")
    args = ap.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        sys.stderr.write("watched.py: no command given (use -- <command>)\n")
        return 2

    last_output = [time.monotonic()]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group so the kill reaches children
    )

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            last_output[0] = time.monotonic()
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    while True:
        try:
            proc.wait(timeout=5)
            t.join(timeout=5)
            return proc.returncode
        except subprocess.TimeoutExpired:
            pass
        stale = time.monotonic() - last_output[0]
        if stale > args.stale_secs:
            sys.stderr.write(
                f"\nwatched.py: STALL: no output for {int(stale)}s "
                f"(limit {args.stale_secs}s); killing pid {proc.pid} "
                f"(likely a sleep inside retry/backoff machinery; see "
                f"CappedRetry in the network scripts).\n")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(3)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            return 99


if __name__ == "__main__":
    sys.exit(main())
