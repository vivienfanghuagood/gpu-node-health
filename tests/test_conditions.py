"""Tests for the condition policy - the code that decides whether a node is sick.

Every scenario here is drawn from the real August 2026 incidents on
wx-ms-w7900d-0006 and -0029, or from the stress campaign on -0024.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from gpu_health_agent.conditions import (  # noqa: E402
    GPU_ENGINE_TIMEOUT,
    GPU_HEALTH_DATA_STALE,
    GPU_PROBE_FAILED,
    GPU_UNRECOVERABLE,
    GPU_WORKQUEUE_STALLED,
    ConditionEvaluator,
)


def kern(counts=None, last_hit=None, files_ok=None):
    return {
        "counts": counts or {},
        "last_hit": last_hit or {},
        "files_ok": files_ok if files_ok is not None else {"/var/log/kern.log": True},
    }


def dstate(**kw):
    base = {
        "total": 0, "stuck": 0, "gpu_total": 0, "gpu_stuck": 0,
        "max_seconds": 0.0, "gpu_max_seconds": 0.0, "readable": True,
        "offenders": [],
    }
    base.update(kw)
    return base


def exporter(**kw):
    base = {
        "reachable": True, "error": "", "scrape_seconds": 0.01,
        "gpu_total": 8, "healthy": 8, "unhealthy": [],
    }
    base.update(kw)
    return base


def test_healthy_node_asserts_nothing():
    ev = ConditionEvaluator()
    out = ev.evaluate(1000.0, kern(), dstate(), exporter())
    assert not any(c["active"] for c in out.values()), out


def test_mes_unrecoverable_trips_gpu_unrecoverable():
    ev = ConditionEvaluator()
    ev.evaluate(1000.0, kern({"mes_unrecoverable": 0}), dstate(), exporter())
    out = ev.evaluate(
        1030.0,
        kern(
            {"mes_unrecoverable": 1},
            {"mes_unrecoverable": (1029.0, "amdgpu: MES might be in unrecoverable state")},
        ),
        dstate(), exporter(),
    )
    assert out[GPU_UNRECOVERABLE]["active"]
    assert out[GPU_UNRECOVERABLE]["reason"] == "KernelFatalGPUEvent"
    # The operator must be able to read the triggering line straight off the
    # condition without going to the node.
    assert "unrecoverable state" in out[GPU_UNRECOVERABLE]["message"]


def test_old_event_outside_window_does_not_pin_condition():
    """A reset three weeks ago must not make the node permanently unrecoverable."""
    ev = ConditionEvaluator(window_seconds=900)
    ev.evaluate(1000.0, kern({"gpu_reset_begin": 5}), dstate(), exporter())
    # Same cumulative count, well past the window: no NEW events.
    out = ev.evaluate(9000.0, kern({"gpu_reset_begin": 5}), dstate(), exporter())
    assert not out[GPU_UNRECOVERABLE]["active"]


def test_stuck_ttm_worker_trips_workqueue_stalled_without_any_kernel_log():
    """The signal that would have caught node 0006 before it died.

    No kernel hung-task report is required: /proc alone is enough, and it is
    available ~60s earlier than the kernel's own 120s complaint.
    """
    ev = ConditionEvaluator()
    out = ev.evaluate(
        1000.0, kern(),
        dstate(gpu_total=3, gpu_stuck=3, gpu_max_seconds=180.0, total=3, stuck=3),
        exporter(),
    )
    assert out[GPU_WORKQUEUE_STALLED]["active"]
    assert out[GPU_WORKQUEUE_STALLED]["reason"] == "StuckGPUWorkerThreads"


def test_single_ring_timeout_is_not_enough():
    """One ring timeout under load is routine; recurrence is the signature."""
    ev = ConditionEvaluator(engine_timeout_recurrence=2)
    ev.evaluate(1000.0, kern({"ring_timeout": 0}), dstate(), exporter())
    out = ev.evaluate(1060.0, kern({"ring_timeout": 1}), dstate(), exporter())
    assert not out[GPU_ENGINE_TIMEOUT]["active"]

    out = ev.evaluate(1120.0, kern({"ring_timeout": 2}), dstate(), exporter())
    assert out[GPU_ENGINE_TIMEOUT]["active"]
    assert out[GPU_ENGINE_TIMEOUT]["reason"] == "RecurringEngineTimeout"


def test_unreachable_exporter_is_a_failure_not_a_pass():
    ev = ConditionEvaluator()
    out = ev.evaluate(
        1000.0, kern(), dstate(),
        exporter(reachable=False, error="connection refused"),
    )
    assert out[GPU_PROBE_FAILED]["active"]
    assert out[GPU_PROBE_FAILED]["reason"] == "ExporterUnreachable"


def test_unhealthy_gpu_names_the_affected_tenant():
    ev = ConditionEvaluator()
    out = ev.evaluate(
        1000.0, kern(), dstate(),
        exporter(healthy=7, unhealthy=[{
            "gpu_id": "3", "namespace": "shared-model-serving",
            "pod": "mineru-6bdb4ccd4c-fxxwk", "container": "vllm",
        }]),
    )
    assert out[GPU_PROBE_FAILED]["active"]
    msg = out[GPU_PROBE_FAILED]["message"]
    assert "gpu3" in msg and "shared-model-serving/mineru-6bdb4ccd4c-fxxwk" in msg


def test_blind_agent_reports_stale_rather_than_healthy():
    """The core lesson of the incident: silence must never read as health."""
    ev = ConditionEvaluator()
    out = ev.evaluate(
        1000.0,
        kern(files_ok={"/var/log/kern.log": False}),
        dstate(readable=False),
        exporter(),
    )
    assert out[GPU_HEALTH_DATA_STALE]["active"]
    assert "BLIND" in out[GPU_HEALTH_DATA_STALE]["message"]
    assert "/proc unreadable" in out[GPU_HEALTH_DATA_STALE]["message"]


def test_counter_reset_after_agent_restart_does_not_go_negative():
    """Counters restart at 0 when the agent restarts; that is not -N events."""
    ev = ConditionEvaluator()
    ev.evaluate(1000.0, kern({"gpu_reset_begin": 9}), dstate(), exporter())
    out = ev.evaluate(1030.0, kern({"gpu_reset_begin": 0}), dstate(), exporter())
    assert not out[GPU_UNRECOVERABLE]["active"]
