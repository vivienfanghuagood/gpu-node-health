# gpu-node-guard

Watches the GPU health conditions the agent publishes on Node objects, and
cordons a node when every guardrail allows it.

**It ships in observe mode and it is not deployed.** See *Before deploying*
below — the permission it needs is one a human has to approve.

## What it does and does not do

| | |
|---|---|
| will | cordon a node whose GPU health condition has held long enough, once every guardrail passes **and** `GUARD_MODE=enforce` |
| will | record a Kubernetes Event for every action **and every refusal** |
| will | delete an unreachable `metrics-exporter` pod, if explicitly enabled |
| **will not** | drain, evict, taint, or uncordon anything, under any configuration |

Those last two are not configuration, they are absence of code. There is no
`uncordon()` method on the client and no `create` on `pods/eviction` in the
ClusterRole, so both the code and the RBAC would have to change.

**Why no drain.** Cordoning stops the bleeding — it keeps new tenants off a
sick node, which is the amplifier link of the August incident chain. Draining
tries to rescue the tenants already there, and on a node wedged in the amdgpu
driver the eviction hangs in `D` state exactly like everything else. Evicting
45 pods off 0029 on the strength of an automated signal is not a trade this
gets to make unattended.

**Why no uncordon.** A node that stopped reporting a condition has not been
proven healthy; it has been proven quiet, which is the thing this whole project
exists to distinguish. Recovery goes through a human and a smoke test.

## Levels

| Level | Condition | Action |
|---|---|---|
| L3 | `GPUUnrecoverable`, `GPUWorkqueueStalled` | cordon |
| L2 | `GPUEngineTimeout` | cordon |
| L1 | `GPUProbeFailed` | **report only** |

L1 is report-only because one card failing a probe does not justify removing
the other seven. That belongs in the device plugin, not here.

## Guardrails

Every one of these is mandatory, and every refusal is a first-class output with
its own metric label and its own Event. A guard that silently declines to act
is the same failure mode this project exists to remove, one level up.

| Guardrail | Default | What it stops |
|---|---|---|
| `GUARD_MODE` | `observe` | everything. Anything that is not exactly `enforce` is observe, so a typo fails closed |
| `GUARD_CONDITION_MIN_AGE_SECONDS` | 120 | acting on a flap |
| `GUARD_MIN_HEALTHY_NODES` | 2 | cordoning into an outage |
| `GUARD_MAX_CORDONS_PER_WINDOW` | 1 / hour | a fleet-wide event becoming a fleet-wide removal |
| `GPUHealthDataStale` | — | acting on data the agent itself says is unreliable |

Two of these deserve explanation.

**The capacity floor of 2 means "never, automatically" on this cluster.** The
entire serving capacity of `wx-ms-w7900d` is 0029 and 0043, so cordoning either
leaves one healthy node, below the floor. That is the correct value today, not
a placeholder: the refusal it produces — `GPUGuardCordonRefusedCapacityFloor`,
severity critical — is *more* useful than a cordon would be, because it says "a
serving node is sick, it is still taking new tenants, and a human has to decide
what to trade." The floor is evaluated against what would **remain** after the
cordon, and it does not count nodes that are themselves already sick.

**The debounce reads `lastTransitionTime`, not an in-memory counter.** The
agent advances that timestamp only on an actual flip, so the age of the
timestamp already *is* the dwell time — and it survives a guard restart. A
streak counter that reset every time the pod was rescheduled would debounce
nothing.

## Exporter supervision

Off by default (`GUARD_EXPORTER_SUPERVISION=false`), because it deletes pods
belonging to the AMD operator.

It exists because the operator's `metrics-exporter` DaemonSet has neither a
readiness nor a liveness probe, its PID 1 is a `bash` wrapper that keeps
running after its `gpuagent` child becomes a zombie, and the DaemonSet is
owner-referenced by the `DeviceConfig` CRD — so a patch adding a probe gets
reconciled away, and the CRD has no field for one. Three of five exporters on
this cluster died that way, weeks apart, and every one stayed `1/1 Running` and
stayed a Ready endpoint of the Service. The probe can only be bolted on from
outside.

The judgement it encodes: **"the pod is Running" is not evidence, and "I
deleted the pod" is not proof of recovery.** On 0004 the pod cannot be deleted
at all — a task wedged in the driver holds the container open and containerd
returns `DeadlineExceeded` forever. So a delete is recorded as an *attempt*,
the node goes on cooldown, and the unreachable clock keeps running until a
scrape actually succeeds. Only a successful scrape clears it — not a restart,
not a delete, not a `Running` phase.

## Deployed 2026-09-17, in `observe` mode

Running on 0024 from `deploy/guard-dev` (ConfigMap-mounted source on
`python:3.12-slim` — same validation mechanism as the agent's dev overlay, and
the same caveat: no provenance, no digest, not the production path). The
production path is `Dockerfile.guard` built and pushed to Harbor, which has to
happen on 0004 and so is waiting on 0004.

What the first live runs found, all of it fixed in place:

| Symptom | Cause |
|---|---|
| `415 UnsupportedMediaType` creating the Lease | `Content-Type` was only set when a caller passed one, and the POST call sites did not |
| `400`, `parsing time "…Z" as ".000000"` | Lease timestamps are `metav1.MicroTime`; six fractional digits are **required**, not optional |
| `exported_pod` appearing in VictoriaMetrics | the payload's `pod` label collided with the one the scrape config sets — renamed to `exporter_pod` |

And what it got right unprompted: 0024 carries `GPUProbeFailed`, and the guard
classified it `L1 report-only` and did not cordon — declining to take an 8-GPU
node out of service over one card is the entire reason that level exists.
Exporter supervision independently reported 0004/0005/0006 unreachable and
0029/0043 fine, which is the same 2-of-5 picture `up{job="amd-gpu-exporter"}`
shows, reached without being told.

One property worth knowing: the unreachable clock lives in memory, so
restarting the guard restarts every exporter's grace period. That errs toward
not deleting, which is the direction it should err in.

## Before enforcing

`kubectl apply -k deploy/guard` creates a ServiceAccount with **`patch` on
`nodes`**. That is the permission the whole manifest exists to request, and it
is not ours to grant:

- The OneClick platform's `ValidatingAdmissionPolicy` states outright that
  "Node spec and taints are immutable through this writer". That policy binds a
  different SA on a different cluster, so it does not technically apply here —
  but *"node spec is not automatically mutable"* is a deliberate design
  position of this platform, and routing around it quietly would be the wrong
  way to disagree with it. It needs a decision from whoever owns the platform.
- Alertmanager's only receiver is `noop`. The 摘除必告警 guardrail — every
  removal must alert — is unsatisfiable until there is a real receiver, so
  `enforce` must not be turned on before then.

Deploying in `observe` mode is materially safer than that sounds, since nothing
in observe mode writes to a Node at all — but the SA still *holds* the
permission, which is the part that needs sign-off.

## Endpoints

`:9102/metrics`, `/healthz`, `/readyz`, and `/debug` — the last dumps the full
decision list as JSON, so checking why a node was or was not cordoned does not
require reading logs or exec'ing into the pod.

## Tests

```sh
python3 -m pytest tests/test_guard_policy.py -q
```

`policy.py` is pure — it takes a parsed fleet snapshot and a clock and returns
decisions, with no client and no clock of its own. This is the code that can
take a production node out of service on a two-node cluster, so it is the code
that has to be testable against a hand-written scenario. Every test is a
situation this cluster can actually produce, and the most important ones are
the refusals.
