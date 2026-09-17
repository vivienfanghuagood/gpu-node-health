"""External liveness for the AMD metrics-exporter DaemonSet.

The operator's exporter has no readiness probe and no liveness probe, its PID 1
is a `bash` wrapper that stays `sleeping` after its `gpuagent` child becomes a
zombie, and the DaemonSet is owner-referenced by the `DeviceConfig` CRD - so a
patch adding a probe is reconciled away, and the CRD has no field for one. On
this cluster three of five exporters died that way, weeks apart, and every one
of them stayed `1/1 Running` and stayed a Ready endpoint of the Service.

So the probe has to be bolted on from outside. That is what this is.

The judgement it encodes: **"the pod is Running" is not evidence, and "I
deleted the pod" is not proof of recovery.** On 0004 the pod cannot be deleted
at all - a task wedged in the amdgpu driver holds the container open and
containerd returns DeadlineExceeded forever. A supervisor that assumed its
delete worked would sit there congratulating itself. So a delete is recorded as
an attempt, the pod is put on cooldown, and the unreachable clock keeps running
until a scrape actually succeeds.
"""

import collections

DELETE = "delete"
NOOP = "noop"

R_REACHABLE = "reachable"
R_NOT_RUNNING = "not-running"
R_NO_POD_IP = "no-pod-ip"
R_WITHIN_GRACE = "within-grace"
R_COOLDOWN = "cooldown"
R_DISABLED = "supervision-disabled"
R_WOULD_DELETE = "would-delete"
R_DELETE = "delete"

PodDecision = collections.namedtuple(
    "PodDecision", "pod node action reason unreachable_seconds message"
)


class ExporterSupervisor:
    def __init__(self, cfg, scrape):
        """`scrape(url) -> bool` returns True if the exporter answered."""
        self._cfg = cfg
        self._scrape = scrape
        self._unreachable_since = {}   # pod name -> first failure timestamp
        self._last_delete = {}         # node name -> timestamp

    def _url(self, ip):
        return f"http://{ip}:{self._cfg.EXPORTER_PORT}/metrics"

    def evaluate(self, pods, now):
        decisions = []
        live = set()

        for pod in pods:
            meta = pod.get("metadata") or {}
            status = pod.get("status") or {}
            spec = pod.get("spec") or {}
            name = meta.get("name", "")
            node = spec.get("nodeName", "")
            live.add(name)

            def d(action, reason, message, since=None):
                elapsed = 0 if since is None else round(now - since, 1)
                return PodDecision(name, node, action, reason, elapsed, message)

            if status.get("phase") != "Running":
                # Not our problem: the kubelet is already reporting this one
                # honestly, and something else will restart it.
                self._unreachable_since.pop(name, None)
                decisions.append(d(NOOP, R_NOT_RUNNING,
                                   f"phase={status.get('phase')}"))
                continue

            ip = status.get("podIP")
            if not ip:
                self._unreachable_since.pop(name, None)
                decisions.append(d(NOOP, R_NO_POD_IP, "Running with no podIP yet"))
                continue

            if self._scrape(self._url(ip)):
                # The only thing that clears the clock. Not a restart, not a
                # delete, not a Running phase - an actual answer on the wire.
                self._unreachable_since.pop(name, None)
                decisions.append(d(NOOP, R_REACHABLE, f"{self._url(ip)} answered"))
                continue

            since = self._unreachable_since.setdefault(name, now)
            elapsed = now - since

            if elapsed < self._cfg.EXPORTER_UNREACHABLE_SECONDS:
                decisions.append(d(
                    NOOP, R_WITHIN_GRACE,
                    f"unreachable for {elapsed:.0f}s, grace is "
                    f"{self._cfg.EXPORTER_UNREACHABLE_SECONDS}s",
                    since,
                ))
                continue

            last = self._last_delete.get(node)
            if last is not None and (now - last) < self._cfg.EXPORTER_DELETE_COOLDOWN_SECONDS:
                decisions.append(d(
                    NOOP, R_COOLDOWN,
                    f"unreachable for {elapsed:.0f}s but an exporter pod on "
                    f"{node} was already deleted {now - last:.0f}s ago. "
                    f"Deleting again would be a loop - on a node wedged in the "
                    f"driver the delete cannot complete at all.",
                    since,
                ))
                continue

            if not self._cfg.EXPORTER_SUPERVISION_ENABLED:
                decisions.append(d(
                    NOOP, R_DISABLED,
                    f"unreachable for {elapsed:.0f}s. Supervision is off, so "
                    f"nothing was deleted - this pod belongs to the AMD "
                    f"operator, not to us.",
                    since,
                ))
                continue

            if self._cfg.MODE != "enforce":
                decisions.append(d(
                    NOOP, R_WOULD_DELETE,
                    f"WOULD DELETE {name}: unreachable for {elapsed:.0f}s. "
                    f"GUARD_MODE is observe.",
                    since,
                ))
                continue

            self._last_delete[node] = now
            decisions.append(d(
                DELETE, R_DELETE,
                f"deleting {name} on {node}: /metrics unreachable for "
                f"{elapsed:.0f}s. The delete is an attempt, not a cure - the "
                f"unreachable clock keeps running until a scrape succeeds.",
                since,
            ))

        # Forget pods that no longer exist, so a churning DaemonSet cannot grow
        # this dict without bound.
        for gone in set(self._unreachable_since) - live:
            self._unreachable_since.pop(gone, None)

        return decisions
