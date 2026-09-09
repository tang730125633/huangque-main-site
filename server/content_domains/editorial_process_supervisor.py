"""Linux-only, per-command subreaper. Never import into the threaded API as a reaper.

Run with the API's Python and stdlib only. Detached Chrome/FFmpeg descendants are
adopted here, not by init, even if Node exits before its cleanup hooks run.
"""
import ctypes
import os
import pathlib
import signal
import subprocess
import sys
import time

TIMEOUT_EXIT = 124
STARTUP_EXIT = 125
GRACE_SECONDS = 1.0


def _enable_subreaper():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("editorial supervisor requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    # Check procfs BEFORE spawning a command: no unsupported best-effort fallback.
    return pathlib.Path(f"/proc/self/task/{os.getpid()}/children")


def _drain_children(children_file):
    """Return only after ECHILD, including detached/double-forked descendants.

    Kill only direct children. Their PIDs cannot be reused until *we* reap them;
    there is deliberately no waitpid between reading this list and signaling it.
    Killing a parent adopts its remaining children, so repeat until empty. If the
    kernel cannot terminate a child, stay its reaper; the API's bounded wait trips
    a circuit breaker instead of killing us and abandoning it to init.
    """
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        try:
            for value in children_file.read_text(encoding="ascii").split():
                os.kill(int(value), signal.SIGKILL)
        except OSError:
            # Keep ownership on procfs/permission failure as well. The API will
            # report a bounded cleanup failure, not silently abandon children.
            pass
        time.sleep(.02)


def run(command, timeout):
    children_file = _enable_subreaper()
    children_file.read_text(encoding="ascii")
    interrupted = False

    def cancel(signum, frame):
        nonlocal interrupted
        interrupted = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    process = None
    timed_out = False
    try:
        process = subprocess.Popen(command)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if interrupted or time.monotonic() >= deadline:
                timed_out = True
                process.terminate()  # Allow Puppeteer's actual exit hooks first.
                grace = time.monotonic() + GRACE_SECONDS
                while process.poll() is None and time.monotonic() < grace:
                    time.sleep(.02)
                break
            time.sleep(.02)
        return TIMEOUT_EXIT if timed_out else (0 if process.returncode == 0 else 1)
    finally:
        # Also reclaim leftovers after a successful or failed Node exit.
        _drain_children(children_file)
        if process is not None and process.returncode is None:
            process.returncode = -signal.SIGKILL  # Already reaped by _drain_children.


def main():
    try:
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            raise ValueError("usage: supervisor TIMEOUT -- COMMAND [ARG ...]")
        timeout = float(sys.argv[1])
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be positive and finite")
        return run(sys.argv[3:], timeout)
    except Exception as error:
        print(f"editorial supervisor failed: {type(error).__name__}: {error}", file=sys.stderr)
        return STARTUP_EXIT


if __name__ == "__main__":
    sys.exit(main())
