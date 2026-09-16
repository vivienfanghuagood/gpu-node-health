"""S2: census of processes in uninterruptible sleep (state D).

This is the cheapest early warning available for the failure mode that took
down nodes 0006 and 0029. When the TTM delayed-delete workqueue blocks on a
fence that will never signal, its kernel worker threads (kworker/u266:*,
kworker/u267:*, typically with a "+ttm" suffix) sit in D forever. The node
keeps reporting Ready, the device plugin keeps advertising 8 healthy GPUs, and
the scheduler keeps placing new tenants onto it.

Nothing about this requires talking to the GPU, so it works even when every
GPU interface is already wedged - which is exactly when it is needed.
"""

import os
import time


def _read_stat(proc_path, pid):
    """Return (comm, state, starttime) for a pid, or None if it is gone.

    /proc/<pid>/stat cannot be split on whitespace: comm is wrapped in
    parentheses and may itself contain spaces and parentheses (kernel worker
    names like "kworker/u266:10+ttm" are tame, but userspace can be hostile).
    The reliable parse is to cut at the LAST ')'.
    """
    try:
        with open(os.path.join(proc_path, str(pid), "stat"), "rb") as fh:
            raw = fh.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None

    close = raw.rfind(")")
    open_paren = raw.find("(")
    if close == -1 or open_paren == -1 or close < open_paren:
        return None

    comm = raw[open_paren + 1:close]
    rest = raw[close + 2:].split()
    if len(rest) < 20:
        return None

    state = rest[0]
    try:
        # Field 22 overall; `rest` begins at field 3, so index 19.
        starttime = int(rest[19])
    except ValueError:
        return None
    return comm, state, starttime


class DStateCensus:
    """Tracks how long each process has been continuously in state D.

    Processes are keyed by (pid, starttime) rather than pid alone so that pid
    reuse cannot be misread as a process that has been stuck for hours.
    """

    def __init__(self, proc_path, stuck_seconds, gpu_comm_prefixes):
        self._proc_path = proc_path
        self._stuck_seconds = stuck_seconds
        self._gpu_prefixes = tuple(gpu_comm_prefixes)
        self._since = {}  # (pid, starttime) -> unix ts first seen in D

    def _is_gpu_worker(self, comm):
        return any(comm.startswith(p) for p in self._gpu_prefixes)

    def collect(self):
        now = time.time()
        try:
            pids = [e for e in os.listdir(self._proc_path) if e.isdigit()]
        except OSError:
            return {
                "total": 0, "stuck": 0, "gpu_total": 0, "gpu_stuck": 0,
                "max_seconds": 0.0, "gpu_max_seconds": 0.0,
                "readable": False, "offenders": [],
            }

        seen = set()
        total = stuck = gpu_total = gpu_stuck = 0
        max_secs = gpu_max_secs = 0.0
        offenders = []

        for pid in pids:
            info = _read_stat(self._proc_path, pid)
            if info is None:
                continue
            comm, state, starttime = info
            if state != "D":
                continue

            key = (pid, starttime)
            seen.add(key)
            first = self._since.setdefault(key, now)
            secs = now - first

            total += 1
            max_secs = max(max_secs, secs)
            is_gpu = self._is_gpu_worker(comm)
            if is_gpu:
                gpu_total += 1
                gpu_max_secs = max(gpu_max_secs, secs)

            if secs >= self._stuck_seconds:
                stuck += 1
                if is_gpu:
                    gpu_stuck += 1
                offenders.append(
                    {"pid": int(pid), "comm": comm,
                     "seconds": round(secs, 1), "gpu_worker": is_gpu}
                )

        # Forget processes that left D or exited, so a later reappearance is
        # timed from scratch rather than inheriting a stale start time.
        for key in list(self._since):
            if key not in seen:
                del self._since[key]

        # Longest-stuck first; the head of this list is what an operator needs.
        offenders.sort(key=lambda o: o["seconds"], reverse=True)

        return {
            "total": total,
            "stuck": stuck,
            "gpu_total": gpu_total,
            "gpu_stuck": gpu_stuck,
            "max_seconds": round(max_secs, 1),
            "gpu_max_seconds": round(gpu_max_secs, 1),
            "readable": True,
            "offenders": offenders[:20],
        }
