"""Turn raw signals into node health conditions.

Kept free of I/O so the policy can be unit-tested directly against synthetic
signal dicts - this is the part that decides whether a production node gets
cordoned, so it needs to be the most testable code in the repo.

Design notes that matter:

* Conditions are driven by activity WITHIN A WINDOW, not by cumulative
  counters. A node that reset a GPU once three weeks ago is not currently
  unrecoverable, and a monotonically-increasing counter would pin the
  condition on forever.

* GPUHealthDataStale is a first-class condition, not an afterthought. During
  the August incidents the monitoring chain was silent throughout, and the
  silence was read as health. An agent that cannot read /proc or the kernel
  log must say so loudly rather than reporting all-clear.

* Nothing here trusts a single signal to mean "the GPU is fine". The absence
  of evidence is not asserted as evidence of absence.
"""

import collections

# Kernel-log rules that each condition watches.
# sched_killed_entity belongs here rather than with the engine timeouts: it is
# not a job that ran too long, it is a job submitted against a scheduler entity
# that no longer exists. On 0004 that line appeared once and the node has had a
# task permanently wedged in amdgpu ever since - there is no recovery path from
# it short of a GPU reset.
_FATAL_HANG_RULES = (
    "mes_unrecoverable", "gpu_reset_begin", "vram_lost", "sched_killed_entity",
)
_WORKQUEUE_RULES = ("gpu_workqueue_blocked",)
_ENGINE_RULES = ("ring_timeout", "job_timedout")

# Condition names, published both as Node conditions and as metric labels.
GPU_UNRECOVERABLE = "GPUUnrecoverable"
GPU_WORKQUEUE_STALLED = "GPUWorkqueueStalled"
GPU_ENGINE_TIMEOUT = "GPUEngineTimeout"
GPU_PROBE_FAILED = "GPUProbeFailed"
GPU_HEALTH_DATA_STALE = "GPUHealthDataStale"

ALL_CONDITIONS = (
    GPU_UNRECOVERABLE,
    GPU_WORKQUEUE_STALLED,
    GPU_ENGINE_TIMEOUT,
    GPU_PROBE_FAILED,
    GPU_HEALTH_DATA_STALE,
)


class ConditionEvaluator:
    def __init__(self, window_seconds=900, engine_timeout_recurrence=2):
        self._window = window_seconds
        self._engine_recurrence = engine_timeout_recurrence
        self._history = collections.deque()  # (ts, counts dict)

    def evaluate(self, now, kernlog, dstate, exporter):
        """Return {condition_name: {active, reason, message}}."""
        counts = (kernlog or {}).get("counts") or {}
        last_hit = (kernlog or {}).get("last_hit") or {}
        files_ok = (kernlog or {}).get("files_ok") or {}
        dstate = dstate or {}
        exporter = exporter or {}

        # Exactly one history sample per evaluation. The baseline is then the
        # oldest sample still inside the window, and every condition below
        # measures its own rules against that same baseline.
        self._history.append((now, dict(counts)))
        while len(self._history) > 1 and now - self._history[0][0] > self._window:
            self._history.popleft()
        baseline = self._history[0][1]

        def hits(rules):
            return sum(
                max(0, counts.get(r, 0) - baseline.get(r, 0)) for r in rules
            )

        def latest_line(rules):
            best_ts, best_line = 0, ""
            for r in rules:
                entry = last_hit.get(r)
                if entry and entry[0] > best_ts:
                    best_ts, best_line = entry
            return best_line

        out = {}

        # --- GPUUnrecoverable ------------------------------------------
        n = hits(_FATAL_HANG_RULES)
        out[GPU_UNRECOVERABLE] = {
            "active": n > 0,
            "reason": "KernelFatalGPUEvent" if n else "NoFatalGPUEvent",
            "message": (
                f"{n} unrecoverable GPU event(s) in the last {self._window}s: "
                f"{latest_line(_FATAL_HANG_RULES)}"
                if n else
                f"No MES/reset/VRAM-loss events in the last {self._window}s."
            ),
        }

        # --- GPUWorkqueueStalled ---------------------------------------
        # Two independent routes into this condition: the kernel's own
        # hung-task report, and our /proc census. The census usually wins by a
        # wide margin - the kernel only complains after 120s, and only if
        # hung_task_panic style reporting is enabled.
        wq_log = hits(_WORKQUEUE_RULES)
        gpu_stuck = dstate.get("gpu_stuck", 0)
        gpu_max = dstate.get("gpu_max_seconds", 0)
        active = wq_log > 0 or gpu_stuck > 0
        if wq_log > 0 and gpu_stuck > 0:
            reason = "WorkqueueBlockedAndStuckWorkers"
        elif wq_log > 0:
            reason = "KernelReportedBlockedWorker"
        elif gpu_stuck > 0:
            reason = "StuckGPUWorkerThreads"
        else:
            reason = "WorkqueuesResponsive"
        out[GPU_WORKQUEUE_STALLED] = {
            "active": active,
            "reason": reason,
            "message": (
                f"{gpu_stuck} GPU workqueue thread(s) stuck in D "
                f"(longest {gpu_max}s); {wq_log} kernel hung-task report(s) "
                f"in the last {self._window}s. "
                f"{latest_line(_WORKQUEUE_RULES)}"
                if active else
                "GPU workqueue threads are not blocked."
            ),
        }

        # --- GPUEngineTimeout ------------------------------------------
        # A single ring timeout is recoverable and routine under load; the
        # incident signature was recurrence at roughly two-minute intervals
        # after an apparently successful reset. Requiring recurrence is what
        # separates the two.
        eng = hits(_ENGINE_RULES)
        recurring = eng >= self._engine_recurrence
        out[GPU_ENGINE_TIMEOUT] = {
            "active": recurring,
            "reason": "RecurringEngineTimeout" if recurring else "NoRecurringTimeout",
            "message": (
                f"{eng} ring/job timeout(s) in the last {self._window}s "
                f"(threshold {self._engine_recurrence}): "
                f"{latest_line(_ENGINE_RULES)}"
                if recurring else
                f"{eng} ring/job timeout(s) in the last {self._window}s; "
                f"below the recurrence threshold."
            ),
        }

        # --- GPUProbeFailed --------------------------------------------
        unhealthy = exporter.get("unhealthy") or []
        unreachable = exporter.get("reachable") is False
        active = unreachable or bool(unhealthy)
        if unreachable:
            reason = "ExporterUnreachable"
            msg = (
                "Node-local AMD metrics exporter did not answer: "
                f"{exporter.get('error', '')}"
            )
        elif unhealthy:
            reason = "ExporterReportsUnhealthyGPU"
            who = ", ".join(
                f"gpu{u['gpu_id']}"
                + (f" ({u['namespace']}/{u['pod']})" if u.get("pod") else "")
                for u in unhealthy
            )
            msg = f"AMD exporter reports {len(unhealthy)} unhealthy GPU(s): {who}"
        else:
            reason = "ExporterHealthy"
            msg = (
                f"AMD exporter reachable; {exporter.get('healthy', 0)} of "
                f"{exporter.get('gpu_total', 0)} GPU(s) report healthy. "
                "Note: gpu_health is ECC/topology-driven and is not expected "
                "to detect a fence or MES stall on its own."
            )
        out[GPU_PROBE_FAILED] = {
            "active": active, "reason": reason, "message": msg,
        }

        # --- GPUHealthDataStale ----------------------------------------
        blind = []
        if dstate.get("readable") is False:
            blind.append("/proc unreadable")
        for path, ok in sorted(files_ok.items()):
            if not ok:
                blind.append(f"{path} unreadable")
        out[GPU_HEALTH_DATA_STALE] = {
            "active": bool(blind),
            "reason": "SignalSourcesUnreadable" if blind else "SignalsFresh",
            "message": (
                "This node's health signals are BLIND, so a green status here "
                "means nothing: " + "; ".join(blind)
                if blind else
                "All configured signal sources are readable."
            ),
        }

        return out
