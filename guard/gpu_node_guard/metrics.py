"""Prometheus text exposition for the guard.

Same hand-rolled approach as the agent, for the same reason: no third-party
dependency on a component that has to work while the cluster is unhealthy.

The series that matter most are the refusals. `gpuguard_decision` carries the
reason as a label, so `gpuguard_decision{reason="capacity-floor"}` firing is an
alertable statement - "something is sick enough to cordon and I am not allowed
to" - which is a page-a-human event, not a quiet log line.
"""

PREFIX = "gpuguard"


def _esc(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


class Registry:
    def __init__(self):
        self._lines = []
        self._declared = set()

    def metric(self, name, value, labels=None, mtype="gauge", help_text=""):
        full = f"{PREFIX}_{name}"
        if full not in self._declared:
            if help_text:
                self._lines.append(f"# HELP {full} {help_text}")
            self._lines.append(f"# TYPE {full} {mtype}")
            self._declared.add(full)
        rendered = ",".join(f'{k}="{_esc(v)}"' for k, v in (labels or {}).items())
        suffix = f"{{{rendered}}}" if rendered else ""
        self._lines.append(f"{full}{suffix} {value}")

    def render(self):
        return "\n".join(self._lines) + "\n"


def render(state):
    """`state` is the dict main.py keeps; see _snapshot there."""
    r = Registry()

    r.metric("up", 1, help_text="gpu-node-guard process is serving.")
    r.metric(
        "is_leader", 1 if state.get("is_leader") else 0,
        help_text="1 on the replica currently holding the lease. Only the "
                  "leader evaluates or acts.",
    )
    r.metric(
        "enforce_mode", 1 if state.get("mode") == "enforce" else 0,
        help_text="1 when GUARD_MODE=enforce. 0 means every decision below is "
                  "hypothetical.",
    )
    r.metric(
        "last_loop_timestamp_seconds", int(state.get("last_loop", 0)),
        help_text="Unix time of the last completed evaluation. Alert on this "
                  "going stale: a guard that stopped evaluating looks exactly "
                  "like a fleet with no problems.",
    )
    r.metric(
        "loop_errors_total", state.get("loop_errors", 0), mtype="counter",
        help_text="Evaluation loops that raised.",
    )
    r.metric(
        "event_errors_total", state.get("event_errors", 0), mtype="counter",
        help_text="Kubernetes Events the guard failed to record. Non-zero "
                  "means actions and refusals are happening unwitnessed.",
    )
    r.metric(
        "nodes_watched", state.get("nodes_watched", 0),
        help_text="Nodes matching the guard's label selector.",
    )
    r.metric(
        "healthy_capacity", state.get("healthy_capacity", 0),
        help_text="Nodes that are Ready, schedulable and not triggering a "
                  "cordon. This is what the capacity floor is measured "
                  "against.",
    )
    r.metric(
        "min_healthy_nodes", state.get("min_healthy_nodes", 0),
        help_text="Configured capacity floor.",
    )
    r.metric(
        "cordons_total", state.get("cordons_total", 0), mtype="counter",
        help_text="Cordons actually performed by this guard since start.",
    )

    # Standing facts about each watched node, published separately from the
    # decisions. A cordon is permanent (this guard never uncordons) while a
    # decision follows the conditions, which expire - so once the condition
    # clears, the node reads `healthy` while still being out of service. These
    # two series are the only thing that keeps saying so.
    for st in state.get("node_states", ()):
        r.metric(
            "node_unschedulable", st.unschedulable, labels={"node": st.node},
            help_text="1 while the node is cordoned, whoever cordoned it. This "
                      "guard never uncordons, so a 1 here persists until a "
                      "human clears it - which is exactly why it is a gauge "
                      "and not an increase() over the cordon counter.",
        )
        r.metric(
            "node_ready", st.ready, labels={"node": st.node},
            help_text="kubelet's own Ready condition. Stays 1 through a GPU "
                      "hang - recorded here so a dashboard can show that next "
                      "to the GPU conditions rather than beside them.",
        )

    for dec in state.get("decisions", ()):
        r.metric(
            "decision", 1,
            labels={
                "node": dec.node,
                "action": dec.action,
                "reason": dec.reason,
                "level": dec.level,
                "condition": dec.condition,
            },
            help_text="Current decision per node, one series each. The "
                      "reason label is the interesting one - reason="
                      "\"capacity-floor\" or \"rate-limit\" means the guard "
                      "wanted to act and was not allowed to.",
        )

    for dec in state.get("exporter_decisions", ()):
        r.metric(
            "exporter_decision", 1,
            labels={
                # NOT "pod". The scrape config sets `pod` to the target being
                # scraped - which is the GUARD's own pod - so a `pod` label in
                # the payload collides and Prometheus silently renames ours to
                # `exported_pod`. Seen live on the first scrape. Same trap F9
                # recorded for `node` on the agent job; the fix is to never let
                # a payload label share a name with a relabelled one.
                "exporter_pod": dec.pod,
                "node": dec.node,
                "action": dec.action,
                "reason": dec.reason,
            },
            help_text="Current decision per metrics-exporter pod.",
        )
        r.metric(
            "exporter_unreachable_seconds", dec.unreachable_seconds,
            labels={"exporter_pod": dec.pod, "node": dec.node},
            help_text="How long this exporter's /metrics has been "
                      "unreachable. Reset only by a successful scrape - never "
                      "by a restart or a delete.",
        )

    return r.render()
