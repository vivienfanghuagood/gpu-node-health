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
| `gpu-health-agent` | DaemonSet, one per GPU node | running on 0024/0029/0043 in observer mode |
| telemetry stack | vmagent / vmalert / Alertmanager / KSM / node-exporter | deployed, see [deploy/telemetry](deploy/telemetry/) |
| dashboards | Grafana, folder "GPU Health" | in the repo; mount patch not applied yet |
| node tuning | DaemonSet, fleet-wide | written, not applied — see [docs/cluster-faults.md](docs/cluster-faults.md) |
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
kubectl apply -k deploy/base
kubectl label node <node> gpu-health.amd.io/agent=true
```

Rollout is per-node opt-in via that label, because the cluster's entire serving
capacity is two nodes (0029 and 0043). Removing the label is the rollback.

The image is pinned **by digest**, not by tag — a node quietly running a
different build than the one that was validated is the same class of silent
drift this project exists to remove.

### Building and publishing the image

The image lives in the cluster's Harbor, which is reachable at
`36.150.116.202:1808` from inside the Radeon cloud network and at
`10.5.10.12:1808` from the nodes. Every node's containerd already trusts both
over plain HTTP via `/etc/containerd/certs.d`, so **no node-level change is
needed** to pull.

Harbor is *not* reachable from outside that network: from a dev box the TCP
connect succeeds and the first byte of payload is met with a reset. So the
build and push run on a node. `docker.io` is unreachable from there too, hence
the `BASE_IMAGE` build arg:

```sh
# on wx-ms-w7900d-0004, with the repo contents in ~/gha-build
sudo docker build \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  -t 10.5.10.12:1808/radeon-cloud-global/gpu-health-agent:<ver> .

# Push via ctr, not docker. Harbor speaks plain HTTP, and teaching dockerd
# about an insecure registry means editing daemon.json - the build hosts run
# other people's long-lived containers, so a daemon reload is not ours to do.
# ctr takes --plain-http per invocation and touches no shared config.
sudo docker save 10.5.10.12:1808/radeon-cloud-global/gpu-health-agent:<ver> \
  | sudo ctr -n k8s.io images import --all-platforms -
sudo ctr -n k8s.io images push --plain-http -u <user>:<pass> \
  10.5.10.12:1808/radeon-cloud-global/gpu-health-agent:<ver>
```

Push to `radeon-cloud-global` or `library`. `radeon-cloud-user` is a
**proxy-cache project** and rejects pushes (`can not push artifact to a proxy
project`).

Then put the digest ctr printed into `deploy/base/agent-daemonset.yaml`.

### deploy/dev

`deploy/dev` runs the agent from source mounted out of a ConfigMap on top of
`python:3.12-slim`. It predates Harbor access and is kept only for iterating
without a registry round-trip; it has no immutable digest and no way to roll
back to a known build, so **production uses `deploy/base`**. Regenerate the
source ConfigMaps after editing the agent:

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

2026-09-16, observer mode, on 0024 (stress node), 0029 and 0043 (the two
serving nodes). 0004/0005/0006 are deliberately untouched — they carry
important services.

On 0024, fault detection:

- kernel log and `/proc` both readable as uid 10001 + gid 4 (no root)
- synthetic `MES might be in unrecoverable state` and
  `kworker/u266:9 blocked for more than 122 seconds` injected via `/dev/kmsg`
  were detected within 30s and raised `GPUUnrecoverable` and
  `GPUWorkqueueStalled`, each carrying the triggering log line
- 0024 has no exporter, and the agent reported *no exporter on this node*
  rather than scraping one of the five on other nodes

On 0029 and 0043, the exporter path:

- both reachable, 8/8 GPUs healthy, ~55ms scrape
- each resolved its **own** node's exporter pod — 0029 → `10.232.11.237`,
  0043 → `10.232.30.197`, matching the EndpointSlice `nodeName` mapping. This
  is the misattribution case the resolver exists for, confirmed live.
- no conditions active on either node

Later the same day all three nodes were moved off the ConfigMap overlay onto
the digest-pinned Harbor image, and re-checked: same exporter URLs, same 8/8,
no init container left in the pod spec.

Not yet validated: S5 (no node carries the D1–D7 patch set yet) and the
`gpu_health=0` branch of S4 (no GPU has gone unhealthy since rollout).
