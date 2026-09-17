"""Prometheus text exposition, hand-rolled.

No prometheus_client dependency on purpose. This agent is meant to run on
every GPU node in the fleet, including nodes that are already sick; the fewer
moving parts between "read /proc" and "serve text", the fewer ways it has to
become another thing that needs debugging during an incident. The exposition
format is a handful of lines of string formatting.
"""

from .signals.kernlog import RULES

PREFIX = "gpuhealth"

_SEVERITY_OF = {name: sev for name, _, sev in RULES}


def _esc(value):
    """Escape a label value per the Prometheus exposition spec."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


class _Buf:
    def __init__(self, node):
        self._node = node
        self._lines = []
        self._declared = set()

    def metric(self, name, value, labels=None, mtype="gauge", help_text=""):
        full = f"{PREFIX}_{name}"
        if full not in self._declared:
            if help_text:
                self._lines.append(f"# HELP {full} {help_text}")
            self._lines.append(f"# TYPE {full} {mtype}")
            self._declared.add(full)

        all_labels = {"node": self._node}
        all_labels.update(labels or {})
        rendered = ",".join(
            f'{k}="{_esc(v)}"' for k, v in all_labels.items()
        )
        if isinstance(value, bool):
            value = 1 if value else 0
        self._lines.append(f"{full}{{{rendered}}} {value}")

    def text(self):
        return "\n".join(self._lines) + "\n"


def render(node, state):
    """Turn a collected state dict into exposition text."""
    b = _Buf(node)

    b.metric("agent_up", 1, help_text="1 while the agent is serving.")
    b.metric(
        "collect_timestamp_seconds", round(state.get("collected_at", 0), 3),
        help_text="Unix time of the last completed collection pass.",
    )
    b.metric(
        "collect_duration_seconds", state.get("collect_seconds", 0),
        help_text="Wall-clock seconds the last collection pass took.",
    )
    b.metric(
        "collect_errors_total", state.get("collect_errors", 0),
        mtype="counter",
        help_text="Collection passes that raised. Non-zero means this agent's "
                  "own data is suspect.",
    )

    # --- S1 kernel log ---------------------------------------------------
    kern = state.get("kernlog") or {}
    for rule, count in sorted((kern.get("counts") or {}).items()):
        b.metric(
            "kernlog_events_total", count,
            labels={"rule": rule, "severity": _SEVERITY_OF.get(rule, "unknown")},
            mtype="counter",
            help_text="Kernel-log signature hits since agent start.",
        )
    for rule, (ts, _line) in sorted((kern.get("last_hit") or {}).items()):
        b.metric(
            "kernlog_last_event_timestamp_seconds", round(ts, 3),
            labels={"rule": rule},
            help_text="Unix time of the most recent hit for this signature.",
        )
    for path, ok in sorted((kern.get("files_ok") or {}).items()):
        b.metric(
            "kernlog_file_readable", ok, labels={"path": path},
            help_text="0 means the agent cannot read this log file, so every "
                      "kernel-log signal from this node is blind.",
        )

    # --- S2 D-state ------------------------------------------------------
    d = state.get("dstate") or {}
    b.metric(
        "proc_readable", d.get("readable", False),
        help_text="0 means /proc is unreadable and the D-state census is blind.",
    )
    for scope, total_key, stuck_key, max_key in (
        ("all", "total", "stuck", "max_seconds"),
        ("gpu_worker", "gpu_total", "gpu_stuck", "gpu_max_seconds"),
    ):
        b.metric(
            "dstate_processes", d.get(total_key, 0), labels={"scope": scope},
            help_text="Tasks (threads) currently in uninterruptible sleep. "
                      "Counted per thread, not per process: the wedged task "
                      "is often a thread of a process whose leader has "
                      "already exited.",
        )
        b.metric(
            "dstate_stuck_processes", d.get(stuck_key, 0), labels={"scope": scope},
            help_text="Tasks in D for longer than the stuck threshold.",
        )
        b.metric(
            "dstate_max_seconds", d.get(max_key, 0), labels={"scope": scope},
            help_text="Longest continuous time any task has spent in D.",
        )

    # --- S4 AMD exporter --------------------------------------------------
    e = state.get("exporter") or {}
    b.metric(
        "exporter_reachable", e.get("reachable", False),
        help_text="0 means the node-local AMD device-metrics-exporter did not "
                  "answer.",
    )
    b.metric("exporter_scrape_seconds", e.get("scrape_seconds", 0))
    b.metric("exporter_gpu_total", e.get("gpu_total", 0))
    b.metric(
        "exporter_gpu_unhealthy", len(e.get("unhealthy") or []),
        help_text="GPUs the AMD exporter reports as unhealthy.",
    )
    for u in e.get("unhealthy") or []:
        b.metric(
            "exporter_gpu_unhealthy_info", 1,
            labels={
                "gpu_id": u.get("gpu_id", "?"),
                "namespace": u.get("namespace", ""),
                "pod": u.get("pod", ""),
                "container": u.get("container", ""),
            },
            help_text="One series per unhealthy GPU, carrying the tenant "
                      "workload currently occupying it.",
        )

    # --- S5 patch counters ------------------------------------------------
    p = state.get("patch") or {}
    b.metric(
        "patch_counters_present", p.get("present", False),
        help_text="1 when this node exposes amdgpu D1-D7 patch counters.",
    )
    for name, value in sorted((p.get("counters") or {}).items()):
        b.metric(f"patch_{name}", value, mtype="counter")

    # --- derived conditions ----------------------------------------------
    for name, active in sorted((state.get("conditions") or {}).items()):
        b.metric(
            "condition", 1 if active else 0, labels={"condition": name},
            help_text="1 while this node health condition is asserted.",
        )

    return b.text()
