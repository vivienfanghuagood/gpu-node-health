"""Tests for the signal collectors."""

import builtins
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from gpu_health_agent.signals.dstate import DStateCensus, _read_stat  # noqa: E402
from gpu_health_agent.signals.kernlog import KernelLogWatcher  # noqa: E402


# --- kernel log matching -------------------------------------------------

def _drain(watcher, path, lines):
    """Feed lines through a watcher's real file-following path.

    The watcher must be started BEFORE the lines are written: on first open it
    deliberately seeks to EOF so that rotated history is not replayed as live
    faults (see test_backlog_is_skipped_on_first_open).
    """
    watcher.start()
    time.sleep(0.3)
    with open(path, "a") as fh:
        for line in lines:
            fh.write(line + "\n")
    deadline = time.time() + 5
    while time.time() < deadline:
        if sum(watcher.snapshot()["counts"].values()) >= len(lines):
            break
        time.sleep(0.05)
    watcher.stop()
    return watcher.snapshot()


# Verbatim signatures from the wx-ms-w7900d kernel logs.
REAL_LINES = [
    "amdgpu 0000:03:00.0: amdgpu: MES might be in unrecoverable state, issue a GPU reset",
    "amdgpu 0000:03:00.0: amdgpu: GPU reset begin!",
    "[drm] VRAM is lost due to GPU reset!",
    "INFO: task kworker/u266:10:1234 blocked for more than 122 seconds.",
    "amdgpu 0000:03:00.0: amdgpu: ring sdma0 timeout, signaled seq=12, emitted seq=14",
]


def test_real_incident_lines_are_matched():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kern.log")
        open(path, "w").close()
        w = KernelLogWatcher([path])
        snap = _drain(w, path, REAL_LINES)

    counts = snap["counts"]
    assert counts["mes_unrecoverable"] == 1
    assert counts["gpu_reset_begin"] == 1
    assert counts["vram_lost"] == 1
    assert counts["ring_timeout"] == 1
    # The GPU-specific blocked-worker rule must win over the generic one,
    # otherwise a wedged TTM worker is filed as a routine slow task.
    assert counts["gpu_workqueue_blocked"] == 1
    assert counts["other_task_blocked"] == 0


def test_non_gpu_blocked_task_is_filed_separately():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kern.log")
        open(path, "w").close()
        w = KernelLogWatcher([path])
        snap = _drain(w, path, ["INFO: task nfsd:912 blocked for more than 122 seconds."])

    assert snap["counts"]["other_task_blocked"] == 1
    assert snap["counts"]["gpu_workqueue_blocked"] == 0


def test_backlog_is_skipped_on_first_open():
    """Months of rotated history must not replay as live faults."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kern.log")
        with open(path, "w") as fh:
            fh.write("amdgpu: GPU reset begin!\n" * 50)
        w = KernelLogWatcher([path])
        w.start()
        time.sleep(0.5)
        w.stop()
        assert w.snapshot()["counts"]["gpu_reset_begin"] == 0


def test_rotation_is_followed():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kern.log")
        open(path, "w").close()
        w = KernelLogWatcher([path])
        w.start()
        time.sleep(0.3)

        with open(path, "a") as fh:
            fh.write("[drm] VRAM is lost due to GPU reset!\n")
        time.sleep(1.5)

        # logrotate: move the file aside and create a fresh one.
        os.rename(path, path + ".1")
        with open(path, "w") as fh:
            fh.write("amdgpu: GPU reset begin!\n")
        time.sleep(2.0)
        w.stop()

        counts = w.snapshot()["counts"]
        assert counts["vram_lost"] == 1, counts
        assert counts["gpu_reset_begin"] == 1, counts


def test_orphan_fence_signature_is_fatal():
    """Verbatim from 0004's kern.log, 2026-09-02T17:36 - one minute before the
    first task wedged in the driver and stayed there for 19 days.

    This is the earliest point at which the hang is still distinguishable from
    normal operation, and until now no rule matched it.
    """
    lines = [
        "[drm:amddrm_sched_entity_push_job [amd_sched]] *ERROR* Trying to "
        "push to a killed entity",
        "amdgpu 0000:43:00.0: amdgpu: No more SDMA queue to allocate "
        "(16 total queues)",
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kern.log")
        open(path, "w").close()
        w = KernelLogWatcher([path])
        snap = _drain(w, path, lines)

    assert snap["counts"]["sched_killed_entity"] == 1
    assert snap["counts"]["sdma_queue_exhausted"] == 1
    sev = {name: s for name, _, s in __import__(
        "gpu_health_agent.signals.kernlog", fromlist=["RULES"]).RULES}
    assert sev["sched_killed_entity"] == "fatal"
    assert sev["sdma_queue_exhausted"] == "warning"


def test_unreadable_file_is_reported_not_swallowed():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "does-not-exist.log")
        w = KernelLogWatcher([path])
        w.start()
        time.sleep(0.5)
        w.stop()
        assert w.snapshot()["files_ok"][path] is False


# --- /proc parsing -------------------------------------------------------

def _write_stat(root, pid, comm, state, starttime=100, tid=None, wchan=None):
    """Build a fake /proc/<pid>/task/<tid> entry.

    The census reads per-thread stat, so a fixture that only writes
    /proc/<pid>/stat describes a tree the agent never looks at.
    """
    tid = pid if tid is None else tid
    d = os.path.join(root, str(pid), "task", str(tid))
    os.makedirs(d, exist_ok=True)
    fields = ["0"] * 50
    fields[0] = state
    fields[19] = str(starttime)
    with open(os.path.join(d, "stat"), "w") as fh:
        fh.write(f"{tid} ({comm}) " + " ".join(fields) + "\n")
    if wchan is not None:
        with open(os.path.join(d, "wchan"), "w") as fh:
            fh.write(wchan)


def test_comm_containing_parens_and_spaces_is_parsed():
    """Userspace controls comm; splitting on whitespace would misparse state."""
    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 42, "we (are) evil", "D")
        comm, state, starttime = _read_stat(
            os.path.join(d, "42", "task", "42", "stat")
        )
        assert comm == "we (are) evil"
        assert state == "D"
        assert starttime == 100


def test_wedged_thread_under_a_zombie_leader_is_seen():
    """The 0004 case: leader is Z, one thread is wedged in the amdgpu driver.

    A census that lists /proc and reads the leader's state reports this node
    as having zero D-state processes, which is how it stayed invisible for 19
    days. Keyed off the real tids and wchan observed on the node.
    """
    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 2423359, "gpuagent", "Z")
        _write_stat(d, 2423359, "grpcpp_sync_ser", "D", tid=510392,
                    wchan="dma_fence_wait_any_timeout")

        census = DStateCensus(
            d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"],
            gpu_wchan_substrings=["dma_fence", "amdgpu"],
        )
        out = census.collect()

    assert out["total"] == 1
    # Classified GPU-related by wchan; its name matches no prefix we have.
    assert out["gpu_total"] == 1
    assert out["gpu_stuck"] == 1
    assert out["offenders"][0]["tid"] == 510392
    assert out["offenders"][0]["comm"] == "grpcpp_sync_ser"
    assert out["offenders"][0]["wchan"] == "dma_fence_wait_any_timeout"


def test_wchan_beats_comm_and_comm_is_only_a_fallback():
    with tempfile.TemporaryDirectory() as d:
        # A tenant binary blocked in the driver: no name-based rule catches it.
        _write_stat(d, 1, "llama-server", "D", wchan="amdgpu_vm_init")
        # A kworker blocked on something unrelated to the GPU.
        _write_stat(d, 2, "kworker/u266:1+ttm", "D", wchan="nfs_wait_bit_killable")
        # wchan unreadable: fall back to the name.
        _write_stat(d, 3, "kworker/u266:2+ttm", "D")
        _write_stat(d, 4, "nfsd", "D")

        census = DStateCensus(
            d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"],
            gpu_wchan_substrings=["dma_fence", "amdgpu"],
        )
        out = census.collect()

    assert out["total"] == 4
    assert out["gpu_total"] == 2   # llama-server (wchan) + pid 3 (fallback)
    by_comm = {o["comm"]: o["gpu_worker"] for o in out["offenders"]}
    assert by_comm["llama-server"] is True
    assert by_comm["kworker/u266:1+ttm"] is False   # wchan overrides the name
    assert by_comm["kworker/u266:2+ttm"] is True    # no wchan, name wins
    assert by_comm["nfsd"] is False


def test_gpu_worker_in_d_is_counted_and_timed():
    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 1, "kworker/u266:10+ttm", "D")
        _write_stat(d, 2, "kworker/u267:3+ttm", "D")
        _write_stat(d, 3, "bash", "S")
        _write_stat(d, 4, "nfsd", "D")

        census = DStateCensus(d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"])
        out = census.collect()

    assert out["total"] == 3          # two kworkers + nfsd
    assert out["gpu_total"] == 2      # only the kworkers
    assert out["gpu_stuck"] == 2      # threshold is 0, so both count
    assert out["readable"] is True
    assert out["offenders"][0]["gpu_worker"] in (True, False)


def test_process_leaving_d_resets_its_timer():
    with tempfile.TemporaryDirectory() as d:
        census = DStateCensus(d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"])

        _write_stat(d, 1, "kworker/u266:10+ttm", "D")
        census.collect()
        assert (("1", 100) in census._since)

        _write_stat(d, 1, "kworker/u266:10+ttm", "S")
        census.collect()
        assert (("1", 100) not in census._since)


def test_pid_reuse_does_not_inherit_a_stale_stuck_time():
    with tempfile.TemporaryDirectory() as d:
        census = DStateCensus(d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"])

        _write_stat(d, 7, "kworker/u266:1+ttm", "D", starttime=100)
        census.collect()

        # Same pid, different process (new starttime).
        _write_stat(d, 7, "kworker/u266:1+ttm", "D", starttime=999)
        census.collect()

        assert ("7", 100) not in census._since
        assert ("7", 999) in census._since


def test_unreadable_proc_reports_blind():
    census = DStateCensus("/nonexistent-proc", 60, ["kworker/u26"])
    out = census.collect()
    assert out["readable"] is False


def test_a_denied_wchan_is_counted_not_silently_downgraded_to_a_name_guess():
    """A refused wchan read is missing data, not a negative.

    Reading /proc/<tid>/wchan goes through ptrace_may_access(), and unlike stat
    it fails the whole read rather than zeroing a few fields, so a confined or
    unprivileged reader gets PermissionError. Not yet seen on this cluster -
    the AppArmor ptrace denials in kern.log are the stat read and are harmless
    (see the dstate module docstring). The old code caught PermissionError in
    the same handler as "the task exited" and returned "", which sent the
    caller to the comm-prefix fallback. F11 is the record of why that fallback
    is worthless here: the tasks actually wedged in the driver were called
    grpcpp_sync_ser and llama-server.

    So the census must be able to say "I could not tell" - a confident zero
    built out of refused reads is the failure this whole project is about.
    """
    import errno

    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 1, "llama-server", "D", wchan="amdgpu_vm_init")
        _write_stat(d, 2, "some-tenant-binary", "D", wchan="dma_fence_wait")
        _write_stat(d, 3, "nfsd", "D")  # genuinely absent wchan, not denied

        denied = os.path.join(d, "2", "task", "2", "wchan")
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if str(path) == denied:
                raise PermissionError(errno.EACCES, "denied by AppArmor")
            return real_open(path, *a, **kw)

        census = DStateCensus(
            d, stuck_seconds=0, gpu_comm_prefixes=["kworker/u26"],
            gpu_wchan_substrings=["dma_fence", "amdgpu"],
        )
        builtins.open = fake_open
        try:
            out = census.collect()
        finally:
            builtins.open = real_open

    assert out["wchan_denied"] == 1, "the refusal has to be visible somewhere"
    # The absent-wchan task is NOT a denial - those are different facts.
    assert out["total"] == 3

    by_comm = {o["comm"]: o for o in out["offenders"]}
    # Still classified (by name, the only thing left) but flagged as a guess.
    assert by_comm["some-tenant-binary"]["wchan_denied"] is True
    assert by_comm["llama-server"]["wchan_denied"] is False
    assert by_comm["nfsd"]["wchan_denied"] is False


def test_the_denied_sentinel_never_leaks_into_a_reported_wchan():
    """Whatever the internal marker is, an operator must never see it."""
    import errno

    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 1, "victim", "D", wchan="amdgpu_vm_init")
        target = os.path.join(d, "1", "task", "1", "wchan")
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if str(path) == target:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, *a, **kw)

        census = DStateCensus(
            d, stuck_seconds=0, gpu_comm_prefixes=[],
            gpu_wchan_substrings=["amdgpu"],
        )
        builtins.open = fake_open
        try:
            out = census.collect()
        finally:
            builtins.open = real_open

    assert out["offenders"][0]["wchan"] == ""
    assert "denied" not in out["offenders"][0]["wchan"]


def test_an_unreadable_proc_reports_zero_denials_rather_than_omitting_the_key():
    """The blind-census path must still carry the field its alert reads."""
    census = DStateCensus("/nonexistent-proc", 60, ["kworker/u26"])
    out = census.collect()
    assert out["readable"] is False
    assert out["wchan_denied"] == 0
