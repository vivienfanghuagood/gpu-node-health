"""Guard configuration, entirely from environment variables.

Every default here is the cautious one. The guard ships in observe mode, with a
capacity floor that is higher than this cluster's spare capacity, so a fresh
deployment with no configuration at all can do exactly nothing except talk.
Turning it into something that acts requires two separate deliberate changes -
the mode AND the floor - which is the point.
"""

import os


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- mode ---------------------------------------------------------------
    # observe: evaluate everything, emit metrics and Events, change nothing.
    # enforce: additionally cordon.
    #
    # Anything that is not exactly "enforce" is treated as observe, so a typo
    # fails closed. The plan requires a full week in observe before this is
    # flipped, and requires Alertmanager to have a real receiver first - a
    # guard that cordons a node while alerts go to /dev/null is exactly the
    # silent-removal failure the "摘除必告警" guardrail exists to prevent.
    MODE = os.getenv("GUARD_MODE", "observe").strip().lower()

    # --- what it watches ----------------------------------------------------
    # Same label the agent rolls out on, so the guard's view and the agent's
    # deployment can never drift apart: a node without an agent has no GPU
    # health conditions, and a guard that watched a wider set would read that
    # absence as health.
    NODE_LABEL_SELECTOR = os.getenv(
        "GUARD_NODE_LABEL_SELECTOR", "gpu-health.amd.io/agent=true"
    )
    LOOP_INTERVAL_SECONDS = _int("GUARD_LOOP_INTERVAL_SECONDS", 30)

    # --- guardrails ---------------------------------------------------------
    # A condition must have held this long before it can trigger anything.
    # Measured from the condition's own lastTransitionTime, which the agent
    # advances only on a real flip, so this survives a guard restart.
    CONDITION_MIN_AGE_SECONDS = _int("GUARD_CONDITION_MIN_AGE_SECONDS", 120)

    # Refuse to cordon if doing so would leave fewer than this many healthy,
    # schedulable, non-triggering nodes. On wx-ms-w7900d the entire serving
    # capacity is 0029 and 0043, so the default of 2 means: never automatically
    # cordon anything, ever, until the fleet is bigger. That is not a
    # placeholder - it is the correct value for this cluster today, and the
    # refusal it produces is a useful alert in its own right.
    MIN_HEALTHY_NODES = _int("GUARD_MIN_HEALTHY_NODES", 2)

    MAX_CORDONS_PER_WINDOW = _int("GUARD_MAX_CORDONS_PER_WINDOW", 1)
    RATE_LIMIT_WINDOW_SECONDS = _int("GUARD_RATE_LIMIT_WINDOW_SECONDS", 3600)

    # --- exporter supervision ----------------------------------------------
    # The AMD operator's metrics-exporter DaemonSet has neither a readiness nor
    # a liveness probe, its PID 1 is a bash wrapper that keeps running after
    # gpuagent dies, and the DaemonSet is owner-referenced by the DeviceConfig
    # CRD so a patch adding a probe gets reconciled away. The probe therefore
    # has to live outside, and this is outside.
    #
    # Off by default: this deletes somebody else's pods.
    EXPORTER_SUPERVISION_ENABLED = _bool("GUARD_EXPORTER_SUPERVISION", False)
    EXPORTER_NAMESPACE = os.getenv("GUARD_EXPORTER_NAMESPACE", "kube-amd-gpu")
    EXPORTER_LABEL_SELECTOR = os.getenv(
        "GUARD_EXPORTER_LABEL_SELECTOR",
        "app.kubernetes.io/name=metrics-exporter",
    )
    EXPORTER_PORT = _int("GUARD_EXPORTER_PORT", 5000)
    EXPORTER_TIMEOUT_SECONDS = _int("GUARD_EXPORTER_TIMEOUT_SECONDS", 10)
    # How long an exporter must be unreachable before its pod is deleted.
    # Generous on purpose: a restarting exporter is unreachable too, and
    # deleting it again would be a loop.
    EXPORTER_UNREACHABLE_SECONDS = _int("GUARD_EXPORTER_UNREACHABLE_SECONDS", 900)
    # Minimum gap between two deletions of the SAME pod's successor on a node.
    EXPORTER_DELETE_COOLDOWN_SECONDS = _int(
        "GUARD_EXPORTER_DELETE_COOLDOWN_SECONDS", 3600
    )

    # --- identity & serving -------------------------------------------------
    NAMESPACE = os.getenv("GUARD_NAMESPACE", "gpu-node-health")
    POD_NAME = os.getenv("POD_NAME", "gpu-node-guard")
    LEASE_NAME = os.getenv("GUARD_LEASE_NAME", "gpu-node-guard")
    LEASE_DURATION_SECONDS = _int("GUARD_LEASE_DURATION_SECONDS", 30)
    LISTEN_ADDR = os.getenv("LISTEN_ADDR", "0.0.0.0")
    LISTEN_PORT = _int("LISTEN_PORT", 9102)

    # --- adapter for policy.decide -----------------------------------------
    # policy.py takes a small flat object rather than this class, so the policy
    # tests can build a scenario without touching the environment.
    @classmethod
    def policy(cls):
        return _PolicyConfig(
            mode=cls.MODE,
            condition_min_age_seconds=cls.CONDITION_MIN_AGE_SECONDS,
            min_healthy_nodes=cls.MIN_HEALTHY_NODES,
            max_cordons_per_window=cls.MAX_CORDONS_PER_WINDOW,
            rate_limit_window_seconds=cls.RATE_LIMIT_WINDOW_SECONDS,
        )


class _PolicyConfig:
    __slots__ = (
        "mode",
        "condition_min_age_seconds",
        "min_healthy_nodes",
        "max_cordons_per_window",
        "rate_limit_window_seconds",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])
