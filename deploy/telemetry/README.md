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

## Two cluster faults found while deploying this

Both are pre-existing and neither is fixed here, because fixing either means
touching nodes that carry other people's services.

**1. Cluster DNS is broken for every pod.** The kubelets are configured with
`clusterDNS: 10.232.0.10` while the kube-dns Service actually lives at
`10.233.0.10`. `kubernetes.default` does not resolve from any pod. Every
Deployment here carries a `dnsConfig` pointing at the real resolver — a
workaround, marked as one. The fix is kubelet config plus a kubelet restart on
every node.

**2. Three of five AMD metrics exporters are dead and reported Ready.** The
operator's exporter DaemonSet has **no readiness and no liveness probe**. On
0004 the exporter hit `gpuagent get metrics failed: DeadlineExceeded` followed
by `too many open files` on 2026-09-02 and has served nothing since. It is
still `1/1 Running` and still a Ready endpoint of `default-metrics-exporter`,
so anything scraping that Service gets connection-refused three times in five.
0005 and 0006 are in the same state.

`AMDMetricsExporterDown` is the only thing that currently distinguishes this
from healthy.

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

Not yet validated: notification delivery (there is no receiver), and
`GPUNodeConditionFromAPIServer` (the agent still runs with
`NODE_CONDITIONS_ENABLED=false`, so no GPU conditions exist on Node objects).
