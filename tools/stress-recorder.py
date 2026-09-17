#!/usr/bin/env python3
"""Second-resolution recorder for the 0024 fault-injection campaign.

Why this exists rather than "watch the dashboards": every number this project
cares about is an *interval*. Trigger -> first `Trying to push to a killed
entity` -> first hung task -> our Node Condition -> the alert firing. F12 put
that first interval at 3m52s on 0024 and ~1m on 0004 from two historical log
samples, which is enough to justify the rule and nowhere near enough to
characterise it. VictoriaMetrics is scraped at 30s, so it structurally cannot
measure a 60-second claim; the dashboards round the exact thing under test.

So this polls the sources directly - the agent's own /debug on the node, the
guard's /metrics, and the apiserver - at a fixed cadence, and writes one TSV
row per sample. TSV, not JSON-per-line, because the first thing anyone does
with this is `awk` a column and diff two runs.

Everything is recorded, including failures to record. A sample where the agent
was unreachable writes a row with agent_ok=0 rather than no row at all: a gap
in the timeline is exactly what a GPU hang looks like, and a recorder that
silently skips those samples would erase its own most important evidence -
which is this project's founding failure mode, one level up.

Usage (on a host with kubectl against the wx-ms-w7900d cluster):

    stress-recorder.py --node wx-ms-w7900d-0024 --out /tmp/stress/p1.tsv

Mark a phase boundary from another shell while it runs:

    echo "P1 vmfault dev0 injected" > /tmp/stress/marker

The marker text lands in the `marker` column of the next sample and then
clears, so injections are timestamped by the same clock as the observations
rather than by a human reading two terminals.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

AGENT_PORT = 9101
GUARD_PORT = 9102

# The five conditions the agent publishes, shortened for column headers. Full
# names are reconstructed with this prefix; kubelet's own conditions are
# deliberately not recorded - they stay True through a GPU hang, which is the
# whole reason this project exists.
COND_PREFIX = "gpu-health.amd.io/"
CONDITIONS = (
    ("unrec", "GPUUnrecoverable"),
    ("wq", "GPUWorkqueueStalled"),
    ("eng", "GPUEngineTimeout"),
    ("probe", "GPUProbeFailed"),
    ("stale", "GPUHealthDataStale"),
)


def sh(args, timeout=15):
    try:
        out = subprocess.run(
            args, capture_output=True, timeout=timeout, check=True
        ).stdout
        return out.decode("utf-8", "replace")
    except Exception:
        return None


def get_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def get_text(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def resolve_pods(namespace, node):
    """(agent_url_base, guard_url_base) - by pod IP, never by Service.

    Service DNS is broken cluster-wide (kubelet clusterDNS 10.232.0.10 vs the
    real 10.233.0.10, see docs/cluster-faults.md) and the agent Service would
    round-robin across nodes anyway, which is the exact mis-attribution this
    project was built to eliminate. Pod IP, matched on spec.nodeName.
    """
    raw = sh(["kubectl", "-n", namespace, "get", "pod", "-o", "json"])
    if not raw:
        return None, None
    agent = guard = None
    for p in json.loads(raw).get("items", []):
        name = p["metadata"]["name"]
        ip = (p.get("status") or {}).get("podIP")
        if not ip:
            continue
        if name.startswith("gpu-health-agent-") and p["spec"].get("nodeName") == node:
            agent = f"http://{ip}:{AGENT_PORT}"
        elif name.startswith("gpu-node-guard-"):
            guard = f"http://{ip}:{GUARD_PORT}"
    return agent, guard


def guard_view(text, node):
    """Pull the guard's view of one node out of its metrics exposition.

    Parsed with string operations rather than a Prometheus client because the
    recorder has to run on a cluster node with nothing installed on it, and
    this is a dozen lines.
    """
    view = {
        "reason": "?",
        "level": "?",
        "cordons": "?",
        "capacity": "?",
        "loop_errors": "?",
        "event_errors": "?",
    }
    if not text:
        return view
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        key, _, val = line.rpartition(" ")
        if key.startswith("gpuguard_decision{") and f'node="{node}"' in key:
            # Only the active decision carries 1; the others are held at 0 so
            # the series keep existing (a disappearing series reads as health).
            if val.strip() in ("1", "1.0"):
                for field, label in (("reason", "reason="), ("level", "level=")):
                    if label in key:
                        view[field] = key.split(label, 1)[1].split('"')[1]
        elif key == "gpuguard_cordons_total":
            view["cordons"] = val.strip()
        elif key == "gpuguard_healthy_capacity":
            view["capacity"] = val.strip()
        elif key == "gpuguard_loop_errors_total":
            view["loop_errors"] = val.strip()
        elif key == "gpuguard_event_errors_total":
            view["event_errors"] = val.strip()
    return view


def node_view(node):
    raw = sh(["kubectl", "get", "node", node, "-o", "json"])
    if not raw:
        return {"api_ok": 0}
    obj = json.loads(raw)
    index = {
        c.get("type"): c for c in (obj.get("status") or {}).get("conditions") or []
    }
    view = {
        "api_ok": 1,
        "unschedulable": 1 if (obj.get("spec") or {}).get("unschedulable") else 0,
        "ready": (index.get("Ready") or {}).get("status", "?"),
    }
    for short, full in CONDITIONS:
        cond = index.get(COND_PREFIX + full) or {}
        view[f"api_{short}"] = {"True": "T", "False": "F"}.get(cond.get("status"), "?")
        # The transition time is the debounce clock the guard actually reads
        # (policy.trigger_level), so record it rather than inferring dwell from
        # when this recorder first saw the flip.
        view[f"api_{short}_since"] = cond.get("lastTransitionTime", "")
    return view


def take_marker(path):
    try:
        with open(path) as f:
            text = f.read().strip()
        os.unlink(path)
        return text.replace("\t", " ")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="wx-ms-w7900d-0024")
    ap.add_argument("--namespace", default="gpu-node-health")
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--marker-file", default="/tmp/stress/marker")
    args = ap.parse_args()

    agent_url, guard_url = resolve_pods(args.namespace, args.node)
    if not agent_url:
        print(f"no gpu-health-agent pod on {args.node}", file=sys.stderr)
        return 2

    # Freeze the kernlog column set from the first reading. It is derived from
    # the agent's rule table, so pinning it here means a rule added mid-campaign
    # shows up as a schema mismatch in the log rather than as silently shifted
    # columns in a file somebody is about to awk.
    first = get_json(agent_url + "/debug") or {}
    kern_keys = sorted(((first.get("kernlog") or {}).get("counts") or {}).keys())

    cols = (
        ["t", "iso", "marker", "api_ok", "ready", "unschedulable"]
        + [f"api_{s}" for s, _ in CONDITIONS]
        + [f"api_{s}_since" for s, _ in CONDITIONS]
        + ["agent_ok"]
        + [f"ag_{s}" for s, _ in CONDITIONS]
        + [
            "dstate_gpu_stuck",
            "dstate_gpu_max_s",
            "dstate_stuck",
            "dstate_total",
            "dstate_readable",
            "exp_reachable",
            "exp_scrape_s",
            "exp_healthy",
            "exp_total",
            "collect_errors",
            "collect_s",
        ]
        + [f"kern_{k}" for k in kern_keys]
        # The agent keeps, per rule, the epoch of the last line that matched it.
        # That is the kernel's own timestamp for the event, not the time this
        # recorder noticed it, so differencing two of these columns measures the
        # in-kernel interval (killed-entity -> first hung task) rather than the
        # interval plus one polling period of this script. Every number this
        # campaign is trying to produce is one of those differences.
        + [f"kernts_{k}" for k in kern_keys]
        + [
            "kern_files_ok",
            "guard_ok",
            "guard_reason",
            "guard_level",
            "guard_cordons",
            "guard_capacity",
            "guard_loop_errors",
            "guard_event_errors",
        ]
    )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    started = time.time()
    with open(args.out, "w", buffering=1) as fh:
        fh.write("\t".join(cols) + "\n")
        n = 0
        while time.time() - started < args.duration:
            tick = time.time()
            row = {c: "" for c in cols}
            row["t"] = f"{tick:.3f}"
            row["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(tick))
            row["marker"] = take_marker(args.marker_file)

            row.update({k: str(v) for k, v in node_view(args.node).items()})

            dbg = get_json(agent_url + "/debug")
            if dbg is None:
                # Re-resolve: a restarted agent pod comes back on a new IP, and
                # a recorder that kept polling the old one would report a dead
                # node for the rest of the run.
                new_agent, new_guard = resolve_pods(args.namespace, args.node)
                if new_agent:
                    agent_url = new_agent
                if new_guard:
                    guard_url = new_guard
                row["agent_ok"] = "0"
            else:
                row["agent_ok"] = "1"
                conds = dbg.get("conditions") or {}
                for short, full in CONDITIONS:
                    row[f"ag_{short}"] = "T" if conds.get(full) else "F"
                ds = dbg.get("dstate") or {}
                row["dstate_gpu_stuck"] = str(ds.get("gpu_stuck", ""))
                row["dstate_gpu_max_s"] = str(ds.get("gpu_max_seconds", ""))
                row["dstate_stuck"] = str(ds.get("stuck", ""))
                row["dstate_total"] = str(ds.get("total", ""))
                row["dstate_readable"] = "1" if ds.get("readable") else "0"
                ex = dbg.get("exporter") or {}
                row["exp_reachable"] = "1" if ex.get("reachable") else "0"
                row["exp_scrape_s"] = str(ex.get("scrape_seconds", ""))
                row["exp_healthy"] = str(ex.get("healthy", ""))
                row["exp_total"] = str(ex.get("gpu_total", ""))
                row["collect_errors"] = str(dbg.get("collect_errors", ""))
                row["collect_s"] = str(dbg.get("collect_seconds", ""))
                kl = dbg.get("kernlog") or {}
                counts = kl.get("counts") or {}
                hits = kl.get("last_hit") or {}
                for k in kern_keys:
                    row[f"kern_{k}"] = str(counts.get(k, ""))
                    hit = hits.get(k)
                    row[f"kernts_{k}"] = f"{hit[0]:.3f}" if hit else ""
                extra = set(counts) - set(kern_keys)
                if extra:
                    print(f"new kernlog rules not in header: {sorted(extra)}",
                          file=sys.stderr)
                files = kl.get("files_ok") or {}
                row["kern_files_ok"] = "1" if files and all(files.values()) else "0"

            gtext = get_text(guard_url + "/metrics") if guard_url else None
            if gtext is None:
                # Re-resolve on the guard's own failure, not just the agent's.
                # The guard is a Deployment, so any rollout moves it to a new
                # pod IP - and the first version of this script only
                # re-resolved when the *agent* failed, so a guard restart
                # blanked every guard column for the rest of the run. Caught on
                # its own first real recording, 2026-09-17 08:06. Each endpoint
                # has to recover from its own failure; sharing one trigger
                # means the quiet one stays broken.
                _, new_guard = resolve_pods(args.namespace, args.node)
                if new_guard and new_guard != guard_url:
                    guard_url = new_guard
                    gtext = get_text(guard_url + "/metrics")
            row["guard_ok"] = "1" if gtext else "0"
            gv = guard_view(gtext, args.node)
            row["guard_reason"] = gv["reason"]
            row["guard_level"] = gv["level"]
            row["guard_cordons"] = gv["cordons"]
            row["guard_capacity"] = gv["capacity"]
            row["guard_loop_errors"] = gv["loop_errors"]
            row["guard_event_errors"] = gv["event_errors"]

            fh.write("\t".join(row[c] for c in cols) + "\n")
            n += 1
            if n % 30 == 0:
                print(f"{row['iso']} {n} samples", file=sys.stderr)

            # Sleep to the next grid point rather than for a fixed interval, so
            # a slow apiserver call does not make the whole timeline drift.
            time.sleep(max(0.0, args.interval - (time.time() - tick)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
