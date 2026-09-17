# Telemetry: collection and alerting

The August 2026 GPU hangs were expensive because nothing noticed. This
directory is the half of that problem that is not about detection at all — it
is about the detection actually reaching a human.

## What was here before

`amd-telemetry` already had VictoriaMetrics (90-day retention) and Grafana.
What it did not have was anything that *collected*: VictoriaMetrics ran with
`-storageDataPath`, `-retentionPeriod` and `-httpListenAddr` and nothing else,
making it a pure push target that had never scraped a thing. The AMD metrics
exporter had been serving ~691 metric lines per node since the day it was
deployed with no one on the other end of the wire.

The store held **2 distinct metric names**. It now holds **769**.

So this was never "build a monitoring stack from scratch" — storage and
display were fine. The collection and alerting layers were simply absent.

## Components

| Component | Form | Role |
|---|---|---|
| `vmagent` | Deployment | scrapes everything, remote-writes to VictoriaMetrics |
| `vmalert` | Deployment | evaluates `alerts.yaml`, notifies Alertmanager |
| `alertmanager` | Deployment | groups, deduplicates, inhibits |
| `kube-state-metrics` | Deployment | Node conditions and enrolment labels from the API server |
| `node-exporter` | DaemonSet | host-level metrics on enrolled nodes |

## Scrape targets

| Job | What |
|---|---|
| `gpu-health-agent` | the agent's `:9101`, every 15s |
| `amd-gpu-exporter` | each exporter pod's `:5000`, **per pod, never via the Service** |
| `kube-state-metrics` | Node objects |
| `node-exporter` | enrolled nodes' `:9100` |
| `vmagent` | itself |

Three deliberate restraints in `scrape.yaml`, each of which would otherwise
corrupt the data quietly:

- **Never scrape the `default-metrics-exporter` Service.** It is a plain
  ClusterIP over five non-hostNetwork pods on five nodes. Scraping it
  round-robins, and one node's GPUs get attributed to another.
- **Never relabel `pod` / `namespace` / `container` on the exporter job.** The
  exporter emits those itself to name the *tenant* sitting on each GPU. That
  is what lets an alert say who is affected.
- **Never stamp `node` on the agent job.** The agent already emits it. Doing
  both collides into `exported_node` and splits every query in half.

## Alerting

`alerts.yaml`, 18 rules in 5 groups. Every rule comes from an observed failure
mode on this cluster rather than a generic GPU dashboard.

The group that matters most is `gpu-node-health.chain` — the chain watching
itself. `GPUHealthDataStale`, `GPUHealthAgentDown`, `GPUHealthAgentMissing`,
`GPUHealthAgentMissingOnNode` and `GPUHealthCollectStale` all exist because
the most expensive failure was not a GPU dying, it was health reporting being
dead while everything looked green.

`GPUHealthAgentMissingOnNode` is why kube-state-metrics is in this stack: a
node that carries the enrolment label but has no agent series is a node that
silently stopped being monitored, which reads from every dashboard as a node
with no problems. Detecting that needs the *expected* set, which only the API
server knows.

Several signals are deliberately covered twice — once off the agent's own
metrics and once off the Node object via kube-state-metrics — so that a broken
or unscrapeable agent cannot produce a clean board.

### Notifications go nowhere yet

Alertmanager's only receiver is `noop`. It groups, deduplicates, inhibits and
shows everything in its UI, and sends nothing outward. There is no mail relay,
chat webhook or pager integration configured for this cluster, and choosing
one is not a decision to make silently inside a commit — it needs an endpoint
and credentials from whoever owns the rotation.

This is stated rather than papered over: a stack that looks like it is
alerting and is not would be this project's own failure mode, one level up.

## Dashboards

Three, provisioned from `dashboards/` into a **GPU Health** folder in the
cluster's existing Grafana at `http://36.150.116.200:30091/`. They are linked
to each other through the dropdown in the top right.

| Dashboard | Answers |
|---|---|
| **GPU Node Health** | is the detection chain alive, and what does it see |
| **GPU Fleet** | what the AMD exporter sees, per card and per tenant |
| **GPU Guard** | what the actuator decided, and — far more often — why it decided to do nothing |

The first dashboard is ordered so that *is the chain alive* comes before
*what is the chain reporting*: the top row puts "agents reporting" next to
"nodes enrolled", because the gap between those two numbers is the failure
this project was built for, and it is the one that reads as health everywhere
else.

**GPU Guard** repeats that ordering for the same reason — "is the guard up and
elected" comes before "what did it decide", because a guard that is down looks
exactly like a guard with nothing to report. While the guard runs in observe
mode the panel that carries the weight is **Would have cordoned (24h)**: it is
the dry-run record, and it is the evidence that decides whether `enforce` is
safe to turn on.

Two details on that board are not cosmetic:

- Every singleton gauge is wrapped in `max()`. Without it a rolling restart
  leaves two `pod` series in the lookback window and a stat panel picks one
  arbitrarily, so a live guard can read **DOWN**. Observed on 2026-09-17.
- The exporter panels group by `exporter_pod`, never `pod`. The scrape config
  relabels `pod` onto every target, so on a `gpuguard_*` series `pod` is the
  *guard's* pod — grouping by it would collapse all five exporters into one.

Provisioning is deliberately awkward, and the awkwardness is documented in
[`grafana-dashboard-mount.patch.yaml`](grafana-dashboard-mount.patch.yaml).
Grafana here predates this project and is not ours; its dashboard directory is
a ConfigMap volume belonging to somebody else, and a ConfigMap volume is
read-only, so a second ConfigMap cannot be mounted inside it. The patch turns
the provider volume into a *projected* volume merging both owners' ConfigMaps
and gives our dashboards their own mount. It has to be applied by hand, once,
because kustomize can only patch resources it also owns:

```sh
kubectl -n amd-telemetry patch deployment grafana \
  --patch-file deploy/telemetry/grafana-dashboard-mount.patch.yaml
```

`allowUiUpdates` is **false** for this folder. These dashboards come from git;
letting the UI save over them would recreate exactly the drift between "what
was reviewed" and "what is running" that this project exists to remove.

## Three cluster faults found while deploying this

Root causes for all three are in [`docs/cluster-faults.md`](../../docs/cluster-faults.md),
with the exact fix for each.

**1. Node 0004 is wedged in the amdgpu driver, right now.** 272 tasks in
uninterruptible sleep, every one of them in the DMA-fence path; the oldest has
been there 19 days. It got that way on 2026-09-02 with
`[drm:amddrm_sched_entity_push_job] *ERROR* Trying to push to a killed entity`
— the orphan-fence link of the August incident chain, verbatim — and the
kernel's hung-task watchdog named a tenant (`llama-server`) among the
casualties. The node reports Ready with 8 healthy GPUs and no alert has ever
fired. Any new process that opens `/dev/dri/*` on it hangs in `open()`, which
is why the dead exporter pods below cannot be deleted. There is no software
fix: a task in `D` cannot be killed. 0005 and 0006 show the identical
containerd symptom but are not reachable over SSH, so their driver state is
inferred rather than measured.

Finding this exposed two blind spots in our own agent, both now closed: the
D-state census listed `/proc` (thread-group leaders only, so a wedged thread
under a zombie leader was invisible), and it classified GPU tasks by process
name (`kworker/u26`, which matches none of the three names actually wedged).
It now walks `/proc/<pid>/task/<tid>` and classifies by `wchan` — where the
task is blocked, not what it is called. `Trying to push to a killed entity` is
now a **fatal** kernel-log rule feeding `GPUUnrecoverable`.

**2. Cluster DNS is broken for every pod.** The kubelets are configured with
`clusterDNS: 10.232.0.10` while the kube-dns Service lives at `10.233.0.10` —
one digit, cluster-wide, since install. `kubernetes.default` does not resolve
from any pod. Every Deployment here carries a `dnsConfig` pointing at the real
resolver — a workaround, marked as one in each file. It cannot be fixed with a
second Service, because 10.232.0.10 is in the pod CIDR, outside the Service
CIDR the API server allocates from. The fix is kubelet config plus a staged
kubelet restart.

**3. Three of five AMD metrics exporters are dead and reported Ready.** All
three died on the identical line — `exporter slurm.go:78: too many open
files` — weeks apart. It is not an fd limit: the process had 12 fds against a
soft limit of 1024. It is `fs.inotify.max_user_instances`, at the kernel
default of **128** on every node in the fleet, with uid 0 already holding 138
instances on 0004. Everything on these nodes runs as root and draws from that
one pool, so the exporter is simply whichever process started after it ran
dry — which is why the three deaths look random.

Nothing noticed for two reasons, both of which had to be true: the container's
PID 1 is a `bash` wrapper that keeps sleeping after its `gpuagent` child
becomes a zombie, and the operator's DaemonSet has no readiness or liveness
probe. The `DeviceConfig` CRD has no field for one and the DaemonSet is
owner-referenced by the operator, so it cannot be fixed where it belongs.

The ceiling fix — `kubectl apply -k deploy/node-tuning` — is **applied**: all
seven nodes went 128 → 8192 on 2026-09-17, so 0024/0029/0043 are no longer one
pod-churn from the same death. Deleting the three dead pods was authorised and
issued, and **cannot complete**: fault 1 holds their containers open, so those
three nodes have no exporter until they reboot. `up{job="amd-gpu-exporter"}` is
**3/6** — three dead on 0004/0005/0006, three live on 0024/0029/0043.
The supervision gap stays open and is picked up by `gpu-node-guard`.

`AMDMetricsExporterDown` is currently the only thing that distinguishes any of
this from healthy.

## Validated

2026-09-16, on the live cluster.

| Check | Result |
|---|---|
| all five jobs scraping | 13 targets, 2 down — both genuinely dead exporters |
| tenant attribution survives relabelling | `gpu_health` keeps `namespace`/`pod`; no `exported_*` series exist |
| per-node attribution | `sum(gpu_health) by (node)` → 0029: 8, 0043: 8 |
| 18 alert rules load | no evaluation errors |
| **end-to-end injection** | `MES might be in unrecoverable state` written to `/dev/kmsg` on 0024 → `GPUUnrecoverable` **and** `GPUFatalKernelEvent` firing in Alertmanager within ~100s, via two independent rule paths |
| missing-agent join | returns empty live; returns 0024 when 0024's agent series is excluded |

2026-09-17: `NODE_CONDITIONS_ENABLED` was turned on, so the five
`gpu-health.amd.io/*` conditions now exist on 0024/0029/0043 and
`GPUNodeConditionFromAPIServer` has real data through kube-state-metrics for
the first time. The kubelet's own conditions survived the patch.

Not yet validated: notification delivery — there is still no receiver, and
that remains the blocker for `gpu-node-guard` ever leaving observe mode, since
"摘除必告警" cannot hold when alerts go nowhere.
