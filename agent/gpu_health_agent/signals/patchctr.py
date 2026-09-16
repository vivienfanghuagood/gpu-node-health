"""S5: read amdgpu patch-set counters exposed through sysfs.

Only nodes carrying the D1-D7 patch set have these. Their absence is normal
and is reported as "present: False", never as an error.

Why this exists: the D4 watchdog and D1 remediation paths are, as of the
validation campaign so far, never-executed code. Two full stress phases
(saturated VRAM at production thresholds, and rolling SIGKILL at 1/15 of
production thresholds) produced D4_watchdog_hits=0 and D1_remediate_hits=0.
Until those counters move under a reproduced fault, the patch's efficacy is
unevidenced. Counting them as first-class metrics - rather than grepping logs
after an incident - is what makes that judgement possible.

The kernel side does not export these yet. Until it does, `present` stays
False and the log-derived counters in signals/kernlog.py ("patch" severity)
are the fallback, at lower fidelity.
"""

import os

# Counter files we look for, mapped to the metric suffix we publish.
KNOWN_COUNTERS = {
    "d4_watchdog_fired_total": "d4_watchdog_fired_total",
    "d1_remediate_fences_total": "d1_remediate_fences_total",
    "ttm_delete_giveup_total": "ttm_delete_giveup_total",
    "ttm_delete_leaked_bytes": "ttm_delete_leaked_bytes",
    "d3_failover_total": "d3_failover_total",
}


class PatchCounters:
    def __init__(self, path):
        self._path = path

    def collect(self):
        if not os.path.isdir(self._path):
            return {"present": False, "counters": {}}

        counters = {}
        for fname, metric in KNOWN_COUNTERS.items():
            full = os.path.join(self._path, fname)
            try:
                with open(full, "r") as fh:
                    counters[metric] = int(fh.read().strip())
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, ValueError):
                # Present but unreadable/garbled is worth distinguishing from
                # absent: it means the interface changed under us.
                counters[metric] = -1

        return {"present": bool(counters), "counters": counters}
