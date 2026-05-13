from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


_LOCK = threading.RLock()
_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()


def register_process(process: subprocess.Popen[str]) -> None:
    with _LOCK:
        _ACTIVE_PROCESSES.add(process)


def unregister_process(process: subprocess.Popen[str]) -> None:
    with _LOCK:
        _ACTIVE_PROCESSES.discard(process)


def kill_process_tree(process: subprocess.Popen[str], sig: int = signal.SIGKILL) -> None:
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:  # pragma: no cover - non-POSIX fallback
            process.send_signal(sig)
    except ProcessLookupError:
        return


def terminate_process_tree(process: subprocess.Popen[str], grace_seconds: float = 1.0) -> None:
    kill_process_tree(process, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        kill_process_tree(process, signal.SIGKILL)


def terminate_all_processes(grace_seconds: float = 1.0) -> None:
    with _LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        terminate_process_tree(process, grace_seconds=grace_seconds)
