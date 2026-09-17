"""S2: census of tasks in uninterruptible sleep (state D).

This is the cheapest early warning available for the failure mode that took
down nodes 0006 and 0029. When the TTM delayed-delete workqueue blocks on a
fence that will never signal, its kernel worker threads (kworker/u266:*,
kworker/u267:*, typically with a "+ttm" suffix) sit in D forever. The node
keeps reporting Ready, the device plugin keeps advertising 8 healthy GPUs, and
the scheduler keeps placing new tenants onto it.

Nothing about this requires talking to the GPU, so it works even when every
GPU interface is already wedged - which is exactly when it is needed.

THREADS, NOT PROCESSES
----------------------
The census walks /proc/<pid>/task/<tid>, not /proc/<pid>. Listing /proc yields
only thread-group leaders, and the leader's state is not the thread's: a
multithreaded process whose leader has already exited reads as Z while one of
its threads is wedged in the driver forever.

That is not hypothetical. On 2026-09-17 node 0004 held this, live, for 19 days:

    tgid 2423359 (gpuagent)          state Z   <- all /proc listing can see
     tid  510392 (grpcpp_sync_ser)   state D   wchan dma_fence_wait_any_timeout

with the kernel stack

    dma_fence_wait_any_timeout <- drm_suballoc_new <- amdgpu_ib_get
      <- amdgpu_vm_sdma_prepare <- amdgpu_vm_pt_clear <- amdgpu_vm_init
      <- amdgpu_driver_open_kms <- drm_open   (i.e. open("/dev/dri/card*"))

A process-only census reports zero D-state processes on that node. Silence
reading as health is the exact thing this project exists to remove, so the
census counts tasks.

Cost: one procfs stat read per thread per collection cycle. Measured at a few
tens of milliseconds on these nodes at a 30s interval - the same order as the
process-only scan it replaces, because most processes are single-threaded.

CLASSIFICATION BY wchan, NOT JUST BY NAME
-----------------------------------------
The original classifier matched comm against "kworker/u26". That catches the
TTM workqueue case and nothing else: the two tasks actually wedged on 0004
were named `grpcpp_sync_ser` and `llama-server`. What they had in common was
not their name, it was where they were blocked.

/proc/<tid>/wchan names the kernel function the task is sleeping in, so a task
blocked anywhere in the DRM/amdgpu/TTM/fence stack is classified as GPU-related
regardless of what the binary is called. comm prefixes remain as a fallback for
the case where wchan is empty.

It is not, however, world-readable in the way that sentence originally claimed:
the read goes through ptrace_may_access(), so a confined or unprivileged reader
can be refused. A refusal is reported as `wchan_denied` rather than silently
taking the name-based fallback's answer - the fallback is known not to reach
the tasks that matter, so a denial is missing information, not a negative
result. See _read_wchan.

A NOTE ON THE APPARMOR DENIALS IN kern.log
------------------------------------------
On these nodes this agent generates a steady stream of

    apparmor="DENIED" operation="ptrace" class="ptrace"
    profile="cri-containerd.apparmor.d" comm="python" requested_mask="read"

roughly one line per collect cycle. Measured on 0024 2026-09-17: it is the
/proc/<tid>/stat read, not the wchan read, and it is *not* a failure.
do_task_stat() consults ptrace_may_access() only to decide whether to fill in
the wchan/kstkeip/kstkesp fields; when refused it zeroes those three and
returns the rest, so the `state` character this census depends on is correct
and open() does not raise. Reading pid 1 is enough to produce a line. The
apparent one-per-cycle rate is the kernel's audit deduplication, not the number
of refused reads - a full 1928-pid sweep logs two lines.

Recorded because it is exactly the kind of thing that costs an afternoon: the
lines name our agent, say DENIED, and mean nothing is wrong.
"""

import os
import time


def _read_stat(stat_path):
    """Return (comm, state, starttime) for a task, or None if it is gone.

    /proc/<pid>/stat cannot be split on whitespace: comm is wrapped in
    parentheses and may itself contain spaces and parentheses (kernel worker
    names like "kworker/u266:10+ttm" are tame, but userspace can be hostile).
    The reliable parse is to cut at the LAST ')'.
    """
    try:
        with open(stat_path, "rb") as fh:
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


# Returned instead of a symbol when the kernel refused to tell us where a task
# is blocked. Distinct from "" on purpose - see _read_wchan.
WCHAN_DENIED = "\x00denied"


def _read_wchan(wchan_path):
    """Return the kernel symbol a task is sleeping in, "", or WCHAN_DENIED.

    "" means there is genuinely nothing to report: the file is absent on a task
    that just exited, and the kernel writes a literal "0" for a task that is
    running.

    A permission failure is a different thing and gets its own value. Unlike
    /proc/<tid>/stat, which degrades to zeroed fields when ptrace_may_access()
    refuses, wchan fails the whole read with EPERM - so a confined or
    unprivileged reader gets PermissionError rather than an answer.

    This has not been observed on this cluster: every wchan read attempted on
    0024 on 2026-09-17 succeeded, including the kernel threads whose stat reads
    do trip AppArmor (see the module docstring - those denials are the stat
    read, and they are harmless). The handling is here because the failure mode
    it prevents is silent. Folding a denial into "" would make the caller fall
    back to matching the task's *name*, and F11 is the record of why that
    fallback catches nothing: the three tasks actually wedged in the driver on
    0004 were called grpcpp_sync_ser, llama-server and kworker/u270:* - no name
    list reaches them, which is the entire reason wchan is the primary signal.

    So a denied read must not be reported as "this task is not GPU-related". It
    is "we could not tell", and the difference between those two is the whole
    subject of this project.
    """
    try:
        with open(wchan_path, "rb") as fh:
            value = fh.read().decode("utf-8", errors="replace").strip()
    except PermissionError:
        return WCHAN_DENIED
    except (FileNotFoundError, ProcessLookupError, OSError):
        return ""
    return "" if value == "0" else value


class DStateCensus:
    """Tracks how long each task has been continuously in state D.

    Tasks are keyed by (tid, starttime) rather than tid alone so that pid reuse
    cannot be misread as a task that has been stuck for hours.
    """

    def __init__(self, proc_path, stuck_seconds, gpu_comm_prefixes,
                 gpu_wchan_substrings=()):
        self._proc_path = proc_path
        self._stuck_seconds = stuck_seconds
        self._gpu_prefixes = tuple(gpu_comm_prefixes)
        self._gpu_wchan = tuple(gpu_wchan_substrings)
        self._since = {}  # (tid, starttime) -> unix ts first seen in D

    def _is_gpu_task(self, comm, wchan):
        # wchan is the stronger signal: it says where the task is blocked, not
        # what it is called. Only fall back to the name when wchan told us
        # nothing.
        #
        # A DENIED read also falls back to the name, because a guess is all
        # that is left - but the caller counts those separately rather than
        # letting the guess pass for an observation.
        if wchan and wchan != WCHAN_DENIED:
            return any(s in wchan for s in self._gpu_wchan)
        return any(comm.startswith(p) for p in self._gpu_prefixes)

    def _iter_tasks(self, pid):
        """Yield (tid, task_dir) for every thread of pid.

        A process that exits mid-scan loses its task directory; that is normal
        and yields nothing rather than raising.
        """
        task_root = os.path.join(self._proc_path, pid, "task")
        try:
            tids = os.listdir(task_root)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            return
        for tid in tids:
            if tid.isdigit():
                yield tid, os.path.join(task_root, tid)

    def collect(self):
        now = time.time()
        try:
            pids = [e for e in os.listdir(self._proc_path) if e.isdigit()]
        except OSError:
            return {
                "total": 0, "stuck": 0, "gpu_total": 0, "gpu_stuck": 0,
                "max_seconds": 0.0, "gpu_max_seconds": 0.0,
                "readable": False, "offenders": [], "wchan_denied": 0,
            }

        seen = set()
        total = stuck = gpu_total = gpu_stuck = wchan_denied = 0
        max_secs = gpu_max_secs = 0.0
        offenders = []

        for pid in pids:
            for tid, task_dir in self._iter_tasks(pid):
                info = _read_stat(os.path.join(task_dir, "stat"))
                if info is None:
                    continue
                comm, state, starttime = info
                if state != "D":
                    continue

                key = (tid, starttime)
                seen.add(key)
                first = self._since.setdefault(key, now)
                secs = now - first

                # Only read wchan for tasks that are actually in D. Doing it
                # for every thread on the node would dominate the scan cost
                # for information nothing uses.
                wchan = _read_wchan(os.path.join(task_dir, "wchan"))
                denied = wchan == WCHAN_DENIED
                if denied:
                    # A task in D whose blocking location we were refused. Its
                    # GPU classification below is a guess off the task name,
                    # and the name-based fallback is known not to reach the
                    # tasks that matter. Counted so the blindness is visible
                    # instead of being absorbed into a confident zero.
                    wchan_denied += 1

                total += 1
                max_secs = max(max_secs, secs)
                is_gpu = self._is_gpu_task(comm, wchan)
                if is_gpu:
                    gpu_total += 1
                    gpu_max_secs = max(gpu_max_secs, secs)

                if secs >= self._stuck_seconds:
                    stuck += 1
                    if is_gpu:
                        gpu_stuck += 1
                    offenders.append(
                        {"tid": int(tid), "pid": int(pid), "comm": comm,
                         "wchan": "" if denied else wchan,
                         "wchan_denied": denied,
                         "seconds": round(secs, 1),
                         "gpu_worker": is_gpu}
                    )

        # Forget tasks that left D or exited, so a later reappearance is timed
        # from scratch rather than inheriting a stale start time.
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
            # Tasks in D whose wchan the kernel refused to disclose, so their
            # GPU/non-GPU split is a name-based guess. Non-zero means the
            # census is partially blind in exactly the direction that hides a
            # hang - alert on it, do not read it as health.
            "wchan_denied": wchan_denied,
            "offenders": offenders[:20],
        }
