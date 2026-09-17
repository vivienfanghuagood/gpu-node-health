"""Agent configuration, entirely from environment variables.

Every knob has a default that is safe on a production GPU node: the agent only
reads host state and serves metrics. Nothing here can make the agent write to
the cluster except NODE_CONDITIONS_ENABLED, which is off until an operator
turns it on.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


class Config:
    # --- identity -----------------------------------------------------------
    # Injected by the DaemonSet via spec.nodeName. Everything the agent reports
    # is keyed on this; without it the agent refuses to start rather than
    # publishing metrics that cannot be attributed to a node.
    NODE_NAME = os.getenv("NODE_NAME", "")

    # --- serving ------------------------------------------------------------
    LISTEN_ADDR = os.getenv("LISTEN_ADDR", "0.0.0.0")
    LISTEN_PORT = _int("LISTEN_PORT", 9101)

    # How often every signal is re-collected. The kernel-log tailer runs
    # continuously in its own thread; this is the cadence for the /proc scan,
    # the local exporter scrape, and the patch-counter read.
    COLLECT_INTERVAL_SECONDS = _int("COLLECT_INTERVAL_SECONDS", 15)

    # --- S1 kernel log ------------------------------------------------------
    # Persistent log files ONLY. dmesg is a ring buffer and had already wrapped
    # on every long-uptime node in this fleet when the August incidents were
    # investigated, destroying the evidence. Never read dmesg here.
    KERNLOG_PATHS = _list("KERNLOG_PATHS", "/var/log/kern.log,/var/log/syslog")
    KERNLOG_ENABLED = _bool("KERNLOG_ENABLED", True)

    # --- S2 D-state census --------------------------------------------------
    DSTATE_ENABLED = _bool("DSTATE_ENABLED", True)
    PROC_PATH = os.getenv("PROC_PATH", "/proc")
    # A task in uninterruptible sleep this long is reported as stuck. The
    # kernel's own hung-task watchdog uses 120s; we report earlier so an alert
    # can fire before the node is unrecoverable.
    DSTATE_STUCK_SECONDS = _int("DSTATE_STUCK_SECONDS", 60)
    # Where a task is blocked, read from /proc/<tid>/wchan. This is the primary
    # classifier: on 0004 the two tasks wedged in the amdgpu driver were named
    # `grpcpp_sync_ser` and `llama-server`, so no list of process names would
    # have caught them - but both were sleeping in dma_fence_wait_any_timeout.
    DSTATE_GPU_WCHAN_SUBSTRINGS = _list(
        "DSTATE_GPU_WCHAN_SUBSTRINGS", "dma_fence,amdgpu,ttm_,drm_,kfd_"
    )
    # Fallback for when wchan is empty or unreadable. Kernel threads whose
    # names match these prefixes are the TTM/GPU workqueue workers; on the
    # wedged nodes these were kworker/u266:* and kworker/u267:* in state D,
    # which was the ONLY early signal available.
    DSTATE_GPU_COMM_PREFIXES = _list("DSTATE_GPU_COMM_PREFIXES", "kworker/u26")

    # --- S4 AMD device-metrics-exporter -------------------------------------
    # The operator already runs this DaemonSet on every labelled GPU node and it
    # serves 691 metric lines including gpu_health and a GPU->pod correlation.
    # Nothing in the cluster scrapes it (serviceMonitor.enable=false and
    # VictoriaMetrics has no scrape config), which is why the "health data
    # chain was never established". We scrape it locally and re-publish.
    EXPORTER_ENABLED = _bool("EXPORTER_ENABLED", True)
    EXPORTER_TIMEOUT_SECONDS = _int("EXPORTER_TIMEOUT_SECONDS", 10)
    # Leave EXPORTER_URL empty in-cluster. The exporter's Service is a plain
    # ClusterIP in front of a DaemonSet of non-hostNetwork pods, so a fixed
    # service URL would round-robin to another node's exporter and misattribute
    # its GPU health to this node. Empty means "discover the endpoint whose
    # EndpointSlice nodeName equals ours". Set it only for local testing or a
    # hostNetwork exporter.
    EXPORTER_URL = os.getenv("EXPORTER_URL", "")
    EXPORTER_SERVICE_NAMESPACE = os.getenv("EXPORTER_SERVICE_NAMESPACE", "kube-amd-gpu")
    EXPORTER_SERVICE_NAME = os.getenv("EXPORTER_SERVICE_NAME", "default-metrics-exporter")
    EXPORTER_PORT = _int("EXPORTER_PORT", 5000)
    EXPORTER_METRICS_PATH = os.getenv("EXPORTER_METRICS_PATH", "/metrics")

    # --- S5 kernel patch counters ------------------------------------------
    # Only present on nodes carrying the D1-D7 amdgpu patch set. Absent
    # elsewhere, which is not an error.
    PATCH_COUNTERS_ENABLED = _bool("PATCH_COUNTERS_ENABLED", True)
    PATCH_COUNTERS_PATH = os.getenv(
        "PATCH_COUNTERS_PATH", "/sys/module/amd_sched/parameters"
    )

    # --- Node conditions ----------------------------------------------------
    # Writing Node conditions needs patch on nodes/status. Default OFF so the
    # agent is pure-observer on first rollout; flip it on per-node once the
    # metrics have been trusted for a while.
    NODE_CONDITIONS_ENABLED = _bool("NODE_CONDITIONS_ENABLED", False)
    NODE_CONDITION_INTERVAL_SECONDS = _int("NODE_CONDITION_INTERVAL_SECONDS", 30)

    @classmethod
    def validate(cls) -> None:
        if not cls.NODE_NAME:
            raise SystemExit(
                "NODE_NAME is empty - set it from spec.nodeName via the "
                "downward API. Refusing to start: metrics without a node "
                "identity cannot be attributed and would be worse than none."
            )
