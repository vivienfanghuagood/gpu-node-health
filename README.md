# gpu-node-health

Health detection for AMD GPU nodes on the `wx-ms-w7900d` cluster.

This exists because of the August 2026 GPU hang incidents. The expensive part
of those incidents was not that a GPU died — it was that **nothing noticed**,
so the scheduler kept placing tenants onto a dead node. The device plugin
reported 8 healthy GPUs because the driver had loaded; the metrics exporter
was running but nothing scraped it; `dmesg` had already wrapped by the time
anyone looked.

The guiding rule for everything here: **silence must never read as health.**

## Components

| Component | Form | Status |
|---|---|---|
| `gpu-health-agent` | DaemonSet, one per GPU node | built, running on 0024 in observer mode |
| `gpu-node-guard` | Deployment, leader-elected | not started |

The agent only observes and reports. Cordoning lives in the guard, a separate
process with a separate ServiceAccount, so a bug in signal collection cannot
take a node out of service.

## Signals

| Signal | Source | Catches |
|---|---|---|
| S1 kernel log | `/var/log/kern.log`, `/var/log/syslog` | MES unrecoverable, GPU reset, VRAM loss, hung tasks, ring timeouts |
| S2 D-state census | `/proc/*/stat` via hostPID | `kworker/u26*+ttm` wedged in uninterruptible sleep |
| S4 AMD exporter | node-local `default-metrics-exporter` | ECC/RAS, `gpu_health`, and GPU → tenant pod attribution |
| S5 patch counters | sysfs | D1–D7 amdgpu patch activity (absent today; not an error) |

**Never read `dmesg`.** It is a ring buffer and had already wrapped on every
long-uptime node in this fleet, destroying the evidence for the incidents this
project was built to detect. Persistent log files only.

S3 (an end-to-end GPU probe that actually submits work to each card) is
deliberately not implemented yet: it would force the agent onto a ~10GB ROCm
base image. S1 and S2 alone would have caught both August incidents. Evaluate
the operator's built-in `testRunner` (`rocm/test-runner:v1.4.1`, currently
disabled) before writing one.

## Conditions

`GPUUnrecoverable`, `GPUWorkqueueStalled`, `GPUEngineTimeout`,
`GPUProbeFailed`, `GPUHealthDataStale`.

Two design points worth stating outright:

- Conditions are driven by **activity within a 15-minute window**, not by
  cumulative counters. A node that reset a GPU three weeks ago is not
  currently unrecoverable.
- `GPUHealthDataStale` is a first-class condition. An agent that cannot read
  `/proc` or the kernel log says so loudly instead of reporting all-clear.

## Design constraints

**Zero third-party dependencies.** The agent runs on nodes that are already
sick; it must not become another thing to debug during an incident. Stdlib
only: `http.server`, `urllib.request`, `ssl`, `os`, `re`.

**Node-local exporter discovery.** `default-metrics-exporter` is a ClusterIP
Service in front of a DaemonSet of non-hostNetwork pods — five endpoints on
five different nodes. Scraping the Service address would round-robin and let a
node report a neighbour's GPUs as its own. The agent resolves the EndpointSlice
entry whose `nodeName` matches its own, and reports *unreachable* when there
isn't one rather than falling back.

**No root.** `/proc/<pid>/stat` is world-readable and `/var/log/kern.log` is
`root:adm 0640`, so `supplementalGroups: [4]` is enough. An agent needing root
on every GPU node would be a worse trade than the incident it prevents.

## Deploying

```sh
kubectl apply -k deploy/dev                       # validation overlay
kubectl label node <node> gpu-health.amd.io/agent=true
```

Rollout is per-node opt-in via that label, because the cluster's entire serving
capacity is two nodes (0029 and 0043). Removing the label is the rollback.

`deploy/dev` runs the agent from source mounted out of a ConfigMap on top of
`python:3.12-slim`, which is already present in containerd on every node. This
is a **validation mechanism only** — the cluster is reachable through a slow
tunnel, a 49MB image tar would not transfer, and there is no internal registry.
Before the agent goes anywhere near 0029/0043, build the image from
`Dockerfile` and push it somewhere the nodes can pull from: ConfigMap-mounted
source has no immutable digest and no way to roll back to a known build.

Regenerate the source ConfigMaps after editing the agent:

```sh
./deploy/render-dev-configmap.sh
```

`NODE_CONDITIONS_ENABLED` defaults to `false`. The agent writes nothing to the
cluster until it is flipped. Rollout order is: trust the metrics, then let the
metrics drive conditions, then let conditions drive the guard.

## Tests

```sh
python3 -m pytest tests/ -q
```

Every scenario in `tests/test_conditions.py` is drawn from a real incident, and
`tests/test_signals.py` matches against verbatim log lines from the August
hangs.

## Validated on the cluster

On 0024, 2026-09-16, in observer mode:

- kernel log and `/proc` both readable as uid 10001 + gid 4 (no root)
- synthetic `MES might be in unrecoverable state` and
  `kworker/u266:9 blocked for more than 122 seconds` injected via `/dev/kmsg`
  were detected within 30s and raised `GPUUnrecoverable` and
  `GPUWorkqueueStalled`, each carrying the triggering log line
- exporter discovery correctly reported *no exporter on this node* rather than
  scraping one of the five on other nodes

S4's success path is still unvalidated on a live node: 0024 has no exporter.
The safe target is a cordoned, tenant-free control-plane node (0004/0005/0006),
which has an exporter but carries no tenants.
