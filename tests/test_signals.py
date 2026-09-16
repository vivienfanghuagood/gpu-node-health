"""Tests for the signal collectors."""

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


def test_unreadable_file_is_reported_not_swallowed():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "does-not-exist.log")
        w = KernelLogWatcher([path])
        w.start()
        time.sleep(0.5)
        w.stop()
        assert w.snapshot()["files_ok"][path] is False


# --- /proc parsing -------------------------------------------------------

def _write_stat(root, pid, comm, state, starttime=100):
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    fields = ["0"] * 50
    fields[0] = state
    fields[19] = str(starttime)
    with open(os.path.join(d, "stat"), "w") as fh:
        fh.write(f"{pid} ({comm}) " + " ".join(fields) + "\n")


def test_comm_containing_parens_and_spaces_is_parsed():
    """Userspace controls comm; splitting on whitespace would misparse state."""
    with tempfile.TemporaryDirectory() as d:
        _write_stat(d, 42, "we (are) evil", "D")
        comm, state, starttime = _read_stat(d, 42)
        assert comm == "we (are) evil"
        assert state == "D"
        assert starttime == 100


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
