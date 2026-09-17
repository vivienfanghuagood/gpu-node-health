"""S1: follow persistent kernel logs and count GPU fault signatures.

Deliberately reads /var/log/kern.log and /var/log/syslog rather than dmesg.
dmesg is a fixed-size ring buffer; on this fleet (uptimes up to 38 weeks) it
had already wrapped by the time the August GPU hangs were investigated, and
the only surviving evidence was in the rotated files on disk. An agent that
reads dmesg would silently observe nothing on exactly the nodes that matter
most.

Rotation is handled by tracking (st_dev, st_ino): when logrotate swaps the
file out we finish reading the old handle, then reopen from offset 0.
"""

import os
import re
import threading
import time

# Each rule maps a kernel-log regex to a stable metric label and a severity.
#
# "fatal"   - the node is in or entering an unrecoverable GPU state
# "warning" - a recoverable fault, meaningful when it recurs
# "patch"   - instrumentation emitted by the D1-D7 amdgpu patch set; counted so
#             the patch can be judged on evidence instead of on its author's
#             say-so. These never drive node conditions on their own.
RULES = [
    # --- fatal ----------------------------------------------------------
    ("mes_unrecoverable", r"MES might be in unrecoverable state", "fatal"),
    ("gpu_reset_begin", r"GPU reset begin", "fatal"),
    ("vram_lost", r"VRAM is lost due to GPU reset", "fatal"),
    # A hung-task report naming a GPU/TTM workqueue worker. This was the ONLY
    # early signal present on node 0006 before it became unrecoverable, and
    # nothing in the cluster was watching for it.
    (
        "gpu_workqueue_blocked",
        r"kworker/u\d+:\d+.*blocked for more than \d+ seconds",
        "fatal",
    ),
    # The orphan-fence signature. The GPU scheduler is being handed a job for
    # an entity that has already been torn down - which is precisely what the
    # incident chain produces when the platform mass-kills pods while work is
    # in flight. Observed on 0004 at 2026-09-02T17:36, one minute before the
    # first task wedged in the driver and never came back:
    #
    #   [drm:amddrm_sched_entity_push_job [amd_sched]] *ERROR* Trying to push
    #   to a killed entity
    #
    # Nothing was watching for it, and it is the earliest point at which this
    # class of hang is still distinguishable from normal operation.
    ("sched_killed_entity", r"Trying to push to a killed entity", "fatal"),
    # --- warning --------------------------------------------------------
    # Ring/queue resource exhaustion. Not fatal by itself, but on 0004 it
    # followed the killed-entity errors by days as leaked contexts accumulated:
    #   amdgpu 0000:43:00.0: amdgpu: No more SDMA queue to allocate (16 total)
    ("sdma_queue_exhausted", r"No more \S+ queue to allocate", "warning"),
    ("ring_timeout", r"ring \S+ timeout", "warning"),
    ("job_timedout", r"job timedout", "warning"),
    ("gpu_reset_succeeded", r"GPU reset\(\d+\) succeeded|GPU reset succeeded", "warning"),
    ("vm_fault", r"amdgpu:.*VM_L2_PROTECTION_FAULT|amdgpu.*page fault", "warning"),
    # A hung task that is NOT a GPU workqueue worker - tracked separately so a
    # busy NFS mount does not masquerade as a GPU fault.
    ("other_task_blocked", r"blocked for more than \d+ seconds", "warning"),
    # --- patch instrumentation ------------------------------------------
    ("d4_watchdog", r"D4 watchdog: fence context .* stalled", "patch"),
    ("d1_remediate", r"D1 remediate: force-signal", "patch"),
    ("ttm_giving_up", r"ttm: BO .* GIVING UP", "patch"),
    ("d3_failover", r"D3: .*failover", "patch"),
]

_COMPILED = [(name, re.compile(pat), sev) for name, pat, sev in RULES]

# Ordering matters for the two blocked-task rules: the GPU-specific one must
# win. _match returns on the first hit, and RULES lists it first.


class KernelLogWatcher:
    """Tails the configured log files in a background thread."""

    def __init__(self, paths, from_start=False):
        self._paths = list(paths)
        self._from_start = from_start
        self._lock = threading.Lock()
        self._counts = {name: 0 for name, _, _ in RULES}
        self._last_hit = {}          # rule name -> (unix_ts, raw line)
        self._files_ok = {}          # path -> bool, for self-diagnosis
        self._stop = threading.Event()
        self._thread = None

    # -- public surface ---------------------------------------------------

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="kernlog", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """Counts since agent start, plus the last raw line per rule."""
        with self._lock:
            return {
                "counts": dict(self._counts),
                "last_hit": dict(self._last_hit),
                "files_ok": dict(self._files_ok),
            }

    def severity_counts(self):
        """Total hits grouped by severity, for condition evaluation."""
        snap = self.snapshot()["counts"]
        out = {"fatal": 0, "warning": 0, "patch": 0}
        for name, _, sev in RULES:
            out[sev] += snap.get(name, 0)
        return out

    # -- internals --------------------------------------------------------

    def _run(self):
        handles = {}  # path -> (file object, st_dev, st_ino)
        while not self._stop.is_set():
            for path in self._paths:
                try:
                    self._pump(path, handles)
                    ok = True
                except FileNotFoundError:
                    ok = False
                except OSError:
                    ok = False
                with self._lock:
                    self._files_ok[path] = ok
            self._stop.wait(1.0)
        for fh, _, _ in handles.values():
            try:
                fh.close()
            except OSError:
                pass

    def _pump(self, path, handles):
        st = os.stat(path)
        entry = handles.get(path)

        if entry is not None:
            fh, dev, ino = entry
            rotated = (dev, ino) != (st.st_dev, st.st_ino)
            truncated = fh.tell() > st.st_size
            if rotated or truncated:
                # Drain whatever is left in the old handle before letting go of
                # it, so lines written between our last read and the rotation
                # are not lost.
                if rotated:
                    self._consume(fh)
                fh.close()
                entry = None

        if entry is None:
            fh = open(path, "r", errors="replace")
            if not self._from_start and path not in handles:
                # First sight of this file: skip the backlog. Historical
                # analysis belongs to the log-shipping pipeline, not to a
                # liveness agent that would otherwise replay months of old
                # faults as if they were happening now.
                fh.seek(0, os.SEEK_END)
            st = os.fstat(fh.fileno())
            handles[path] = (fh, st.st_dev, st.st_ino)
            entry = handles[path]

        self._consume(entry[0])

    def _consume(self, fh):
        while True:
            line = fh.readline()
            if not line:
                return
            self._match(line.rstrip("\n"))

    def _match(self, line):
        for name, rx, _sev in _COMPILED:
            if rx.search(line):
                with self._lock:
                    self._counts[name] += 1
                    self._last_hit[name] = (time.time(), line[-400:])
                return
