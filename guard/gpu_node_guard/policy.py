"""What the guard is allowed to do, and why it is usually allowed to do nothing.

This module is pure: it takes a parsed snapshot of the fleet and a clock, and
returns a list of decisions. No HTTP, no clients, no clock of its own. That is
deliberate - this is the code that can take a production node out of service on
a cluster whose entire serving capacity is two nodes, so it has to be the code
that is easiest to test against a hand-written scenario.

The shape of the thing:

    conditions on a Node  ->  a trigger level  ->  guardrails  ->  a decision

Every decision is returned, including the refusals. A guard that silently
declines to act is the same failure mode this whole project exists to remove -
silence reading as health - one level up. So "I would have cordoned 0029 but
the capacity floor stopped me" is a first-class output with its own metric and
its own Kubernetes Event, not a log line nobody reads.

Two things this module will never emit:

* a drain. Cordoning stops the bleeding by keeping new tenants off a sick node;
  draining tries to rescue the ones already on it, and on a wedged node the
  eviction will hang in D state anyway. Evicting 45 pods off 0029 on the
  strength of an automated signal is not a trade worth making unattended.
* an uncordon. A node that recovered still has to pass a human and a smoke
  test before it takes traffic again. Automatic recovery is how a flapping
  signal turns into a flapping fleet.
"""

import collections

# Condition types published by gpu-health-agent (agent/gpu_health_agent/conditions.py).
GPU_UNRECOVERABLE = "gpu-health.amd.io/GPUUnrecoverable"
GPU_WORKQUEUE_STALLED = "gpu-health.amd.io/GPUWorkqueueStalled"
GPU_ENGINE_TIMEOUT = "gpu-health.amd.io/GPUEngineTimeout"
GPU_PROBE_FAILED = "gpu-health.amd.io/GPUProbeFailed"
GPU_HEALTH_DATA_STALE = "gpu-health.amd.io/GPUHealthDataStale"

# Level -> the conditions that trigger it, in descending severity. The mapping
# comes from the plan's L1-L4 table, minus the parts that require draining.
#
# L3 is the "stop the bleeding now" level: the node is in, or is entering, the
# state the August incidents ended in. L2 is "this is recurring, stop feeding
# it". L1 is a single card and is reported only - taking a whole 8-GPU node out
# of service because one card failed a probe costs more than it saves, and the
# device plugin is the right place to fix that.
LEVEL_RULES = (
    ("L3", (GPU_UNRECOVERABLE, GPU_WORKQUEUE_STALLED)),
    ("L2", (GPU_ENGINE_TIMEOUT,)),
    ("L1", (GPU_PROBE_FAILED,)),
)

CORDON_LEVELS = frozenset({"L2", "L3"})

# Decision actions.
CORDON = "cordon"
NOOP = "noop"

# Why a decision came out the way it did. These strings are metric label values
# and Event reasons, so they are part of the interface - renaming one breaks
# dashboards and alert rules.
R_HEALTHY = "healthy"
R_ALREADY_CORDONED = "already-cordoned"
R_REPORT_ONLY = "report-only-level"
R_TOO_YOUNG = "condition-too-young"
R_STALE_DATA = "health-data-stale"
R_OBSERVE_MODE = "observe-mode"
R_CAPACITY_FLOOR = "capacity-floor"
R_RATE_LIMIT = "rate-limit"
R_CORDON = "cordon"
# There is deliberately no R_WOULD_CORDON. One existed, was never emitted by
# any code path, and an alert rule matched it anyway - so the most important
# rule in observe mode ("this node WOULD have been cordoned") could never fire.
# Every other reason here answers "why did it come out this way", and the
# observe-mode answer to that is observe-mode. Keep them all one kind of thing.

Decision = collections.namedtuple(
    "Decision", "node action reason level condition message"
)


def _condition_index(node):
    return {
        c.get("type"): c
        for c in ((node.get("status") or {}).get("conditions") or [])
        if c.get("type")
    }


def _is_true(cond):
    return (cond or {}).get("status") == "True"


def _schedulable(node):
    return not ((node.get("spec") or {}).get("unschedulable"))


def _ready(node):
    return _is_true(_condition_index(node).get("Ready"))


def trigger_level(node, now, min_age_seconds, parse_time):
    """Highest level this node currently triggers, with the condition behind it.

    Returns (level, condition_dict) or (None, None).

    A condition only counts once it has held for `min_age_seconds`. There is no
    separate streak counter for this: the agent advances `lastTransitionTime`
    only on an actual flip, so the age of that timestamp already IS the dwell
    time, and reusing it keeps the debounce honest across guard restarts. A
    guard that reset its own streak counter every time its pod was rescheduled
    would debounce nothing.
    """
    index = _condition_index(node)
    for level, types in LEVEL_RULES:
        for ctype in types:
            cond = index.get(ctype)
            if not _is_true(cond):
                continue
            since = parse_time(cond.get("lastTransitionTime"))
            if since is None or (now - since) < min_age_seconds:
                return level, dict(cond, _too_young=True)
            return level, cond
    return None, None


def healthy_capacity(nodes, now, min_age_seconds, parse_time, exclude=()):
    """Nodes that are Ready, schedulable, and not currently triggering a cordon.

    This is the number the capacity floor is measured against. It counts what
    would still be able to take work, not what merely exists: a node that is
    Ready and schedulable but already screaming GPUUnrecoverable is not spare
    capacity, and counting it as such is how a guard talks itself into
    cordoning the last good node.
    """
    count = 0
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        if name in exclude:
            continue
        if not _ready(node) or not _schedulable(node):
            continue
        level, _ = trigger_level(node, now, min_age_seconds, parse_time)
        if level in CORDON_LEVELS:
            continue
        count += 1
    return count


def decide(nodes, now, cfg, parse_time, recent_cordons=()):
    """Return a Decision for every node in `nodes`.

    `recent_cordons` is a sequence of timestamps of cordons this guard has
    already performed; it drives the rate limit. It is held by the caller
    rather than here so this function stays pure.
    """
    budget = cfg.max_cordons_per_window - sum(
        1 for ts in recent_cordons if now - ts < cfg.rate_limit_window_seconds
    )
    decisions = []

    for node in nodes:
        name = (node.get("metadata") or {}).get("name", "")
        index = _condition_index(node)
        level, cond = trigger_level(node, now, cfg.condition_min_age_seconds, parse_time)
        ctype = (cond or {}).get("type", "")
        cmsg = (cond or {}).get("message", "")

        def d(action, reason, message):
            return Decision(name, action, reason, level or "", ctype, message)

        if level is None:
            decisions.append(d(NOOP, R_HEALTHY, "no GPU health condition active"))
            continue

        if cond.get("_too_young"):
            decisions.append(d(
                NOOP, R_TOO_YOUNG,
                f"{ctype} is active but has held for less than "
                f"{cfg.condition_min_age_seconds}s",
            ))
            continue

        if level not in CORDON_LEVELS:
            decisions.append(d(
                NOOP, R_REPORT_ONLY,
                f"{ctype} is a {level} signal: reported, never cordoned. A "
                f"single failed card does not justify removing 8. ({cmsg})",
            ))
            continue

        # Refuse to act on a node whose health data is admittedly broken. This
        # is the one guardrail that is not about capacity: GPUHealthDataStale
        # means the agent is telling us its own inputs are unreliable, and
        # acting on unreliable inputs is worse than not acting. Note the
        # ordering - this is checked BEFORE the mode check, so it shows up in
        # observe mode too, where it is free to discover.
        if _is_true(index.get(GPU_HEALTH_DATA_STALE)):
            decisions.append(d(
                NOOP, R_STALE_DATA,
                f"{ctype} is active but so is GPUHealthDataStale - the agent "
                f"says its own data is unreliable, so this is not actioned. "
                f"This needs a human, not a cordon.",
            ))
            continue

        if not _schedulable(node):
            decisions.append(d(
                NOOP, R_ALREADY_CORDONED,
                f"{ctype} is active and the node is already unschedulable",
            ))
            continue

        remaining = healthy_capacity(
            nodes, now, cfg.condition_min_age_seconds, parse_time, exclude=(name,)
        )
        if remaining < cfg.min_healthy_nodes:
            decisions.append(d(
                NOOP, R_CAPACITY_FLOOR,
                f"{ctype} is active on {name}, but cordoning it would leave "
                f"{remaining} healthy schedulable node(s), below the floor of "
                f"{cfg.min_healthy_nodes}. Refusing, and alerting instead. "
                f"({cmsg})",
            ))
            continue

        if budget <= 0:
            decisions.append(d(
                NOOP, R_RATE_LIMIT,
                f"{ctype} is active on {name}, but "
                f"{cfg.max_cordons_per_window} cordon(s) already happened "
                f"within {cfg.rate_limit_window_seconds}s. More than that at "
                f"once is a fleet-wide event, which is a human's call. ({cmsg})",
            ))
            continue

        if cfg.mode != "enforce":
            decisions.append(d(
                NOOP, R_OBSERVE_MODE,
                f"WOULD CORDON {name}: {ctype} active and every guardrail "
                f"passed. GUARD_MODE is observe, so nothing was done. ({cmsg})",
            ))
            continue

        budget -= 1
        decisions.append(d(
            CORDON, R_CORDON,
            f"cordoned {name}: {ctype} active. New pods will not be scheduled "
            f"here. Existing pods are left alone - this guard never drains, "
            f"and never uncordons. ({cmsg})",
        ))

    return decisions
