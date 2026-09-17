# Three cluster faults, their root causes, and the fixes

All three were found while deploying the telemetry stack. None is caused by
this project and none is specific to GPU health — but all three are exactly the
kind of thing this project exists to stop leaving in place: a failure that is
invisible from every dashboard, sitting quietly until it matters.

Fault 1 is not history. It is happening now.

---

## 1. Node 0004 is wedged in the amdgpu driver, and has been for 19 days

### What it looks like

Nothing. That is the entire problem. The node is `Ready`, the device plugin
advertises 8 healthy GPUs, `gpu_health` reads 1, and no alert has ever fired.

### What is actually true

As of 2026-09-17, on `wx-ms-w7900d-0004`:

```
272 tasks in state D, every one of them blocked in the DMA-fence path
  1 × python3               wchan dma_fence_wait_any_timeout
271 × kworker/u270:*+ttm    wchan dma_fence_default_wait
```

Sampled five times over 100 seconds: the count never moved and no task ever
left D. These are not transient waits under load. They are permanent.

The oldest of them has been there **19 days, 15 hours** — the gRPC worker
thread of the metrics exporter's `gpuagent`:

```
tgid 2423359 (gpuagent)          state Z
 tid  510392 (grpcpp_sync_ser)   state D   19d15h   dma_fence_wait_any_timeout
```

with this kernel stack:

```
dma_fence_wait_any_timeout
drm_suballoc_new            [amdkcl]
amdgpu_sa_bo_new            [amdgpu]
amdgpu_ib_get               [amdgpu]
amdgpu_job_alloc_with_ib    [amdgpu]
amdgpu_vm_sdma_alloc_job    [amdgpu]
amdgpu_vm_sdma_prepare      [amdgpu]
amdgpu_vm_pt_clear          [amdgpu]
amdgpu_vm_init              [amdgpu]
amdgpu_driver_open_kms      [amdgpu]
drm_file_alloc / drm_open / drm_stub_open / chrdev_open / do_filp_open
```

Read that stack bottom-up: the task called `open("/dev/dri/card*")`. Opening
the device makes the driver initialise a GPU VM, which needs to clear page
tables, which needs an SDMA indirect buffer, which needs space in the
suballocator, which is full of buffers pinned by fences that will never
signal. So the *open* blocks. Forever, uninterruptibly.

**Any new process that touches a GPU on this node hangs at `open()`.** Not the
GPU work — the open. That is why `kubectl delete pod` cannot finish and why a
host-side `kill -9` does nothing: you cannot signal a task in `D`.

### Root cause

The kernel log names it, at 2026-09-02T17:36:28:

```
[drm:amddrm_sched_entity_push_job [amd_sched]] *ERROR* Trying to push to a killed entity
```

A job was submitted to a GPU scheduler entity that had already been torn down.
That is the orphan-fence link of the August incident chain, verbatim: the
platform mass-kills pods while work is in flight, the entity goes away, the
fences it owned are never signalled, and everything that later needs the
resources those fences pin waits forever.

The same minute, the kernel's hung-task watchdog began reporting — and what it
reported was not only ours:

```
INFO: task grpcpp_sync_ser:3954076 blocked for more than 122 seconds.
INFO: task grpcpp_sync_ser:510392  blocked for more than 122 seconds.
INFO: task llama-server:2908826    blocked for more than 122 seconds.
```

`llama-server` is a tenant. Days later the driver started reporting resource
exhaustion as leaked contexts piled up:

```
2026-09-07  amdgpu 0000:43:00.0: amdgpu: No more SDMA queue to allocate (16 total queues)
```

Nobody was watching any of these lines.

### Why our own agent would also have missed it

Two gaps, both now fixed, both worth stating because they are the same mistake
in two places — **describing the fault by what it is called instead of by what
it is doing.**

**The D-state census listed `/proc`, which yields thread-group leaders only.**
The wedged task here is a *thread*; its leader is a zombie. A process-level
census reads `Z`, skips it, and reports zero D-state processes on a node with
272 of them. `dstate.py` now walks `/proc/<pid>/task/<tid>`.

**The GPU classifier matched process names against `kworker/u26`.** The tasks
actually wedged are `kworker/u270:*` (not `u26`), `grpcpp_sync_ser` and
`llama-server`. No list of names would have caught them. What they have in
common is not what they are called, it is where they are blocked — so the
classifier now reads `/proc/<tid>/wchan`, which is world-readable and names
the kernel function, and matches on `dma_fence` / `amdgpu` / `ttm_` / `drm_` /
`kfd_`. Names remain only as a fallback when `wchan` is unavailable.

With both fixes, a full scan of 0004 costs 0.12s and reports
`gpu_stuck=272, max_seconds=<forever>` — which crosses `GPUWorkqueueStalled`
immediately.

`sched_killed_entity` and `sdma_queue_exhausted` are now kernel-log rules,
the first classified **fatal** (it feeds `GPUUnrecoverable`). It is the
earliest moment at which this hang is still distinguishable from healthy
operation.

### Fix

**There is no software fix.** A task in uninterruptible sleep cannot be
killed, and the fences pinning the suballocator cannot be signalled from
userspace. The node needs a GPU reset, and in practice a reboot.

0004/0005/0006 are cordoned control-plane nodes, so nothing is being scheduled
onto them — but they are also the nodes running the metrics exporters, and
they carry other people's services. **Rebooting them is not this project's
call to make.** What this document can do is say plainly that the node is in
the failure state the August incidents ended in, that it got there on
2026-09-02, and that nothing in the cluster noticed for 19 days.

### What is not yet confirmed

0005 and 0006 show the *identical* containerd symptom — `KillContainer` and
`KillPodSandbox` both returning `DeadlineExceeded` indefinitely, which is what
a task stuck in `D` inside the container does. Neither node is reachable over
SSH from here, so their driver state is inferred, not measured.

The cheap way to settle it is to label them for the agent:

```sh
kubectl label node wx-ms-w7900d-0005 gpu-health.amd.io/agent=true
kubectl label node wx-ms-w7900d-0006 gpu-health.amd.io/agent=true
```

The agent is unprivileged (uid 10001, read-only rootfs, all capabilities
dropped) and only reads `/proc` and `/var/log`. It would answer the question
within one collection interval. Removing the label is the rollback.

**Deferred.** 0004/0005/0006 are not to be touched for now — too much else
depends on them. That includes the labelling above and any reboot.

But the question got answered anyway, for free, by an experiment already in
flight: the three dead exporter pods were deleted at `2026-09-17T03:40:36Z`
with `deletionGracePeriodSeconds: 1`. Two and a half hours later all three were
still `Terminating`:

```
default-metrics-exporter-wj6pd  1/1  Terminating  wx-ms-w7900d-0004
default-metrics-exporter-nxt4l  1/1  Terminating  wx-ms-w7900d-0005
default-metrics-exporter-swrmm  1/1  Terminating  wx-ms-w7900d-0006
```

A one-second grace period that has not expired in 8,700 seconds is not a slow
shutdown. The kubelet cannot tear the sandbox down because a task inside it is
in `D`, and a task in `D` cannot be signalled. 0004 is measured; 0005 and 0006
now show the *same terminal behaviour under the same stimulus*, which is as
close to measurement as it gets without logging in. Their driver state should
be read as confirmed-by-behaviour rather than inferred.

Note what this also means: **the DaemonSet will not replace them.** A pod that
never finishes terminating never frees its slot, so those three nodes have no
exporter and will have none until they reboot. `up{job="amd-gpu-exporter"}`
stays at 2/5.

### The same chain, on two other nodes

0004 is not a one-off. Sweeping the retained kernel logs (`kern.log*`
including the rotated `.gz`, never `dmesg`) on the three worker nodes turns up
the same signatures with different endings, and the differences are what
justify the severity split in [`kernlog.py`](../agent/gpu_health_agent/signals/kernlog.py).

**0024, 2026-08-29 — the identical chain, survived.**

```
11:15:26  [drm:amddrm_sched_entity_push_job] *ERROR* Trying to push to a killed entity
11:19:18  INFO: task llama-server:112094 blocked for more than 122 seconds.
11:19:18  INFO: task kworker/u266:1:112173 blocked for more than 122 seconds.
          ... 9 more kworker/u266:* in the same second
```

Same orphan-fence error, same tenant name (`llama-server`) as the first
casualty, same kworker pile-up behind it. **The gap between the error and the
first hung task is 3 minutes 52 seconds** — on 0004 the same gap was about a
minute. That window is the entire value of the `sched_killed_entity` rule: it
is the last moment the node is still distinguishable from a healthy one, and
it arrives minutes before the hung-task watchdog says anything. 0024's D-state
count today is 0, so this one cleared.

**0029, 2026-09-07 — the precursor alone, no wedge.**

```
23:17:28 .. 23:19:00   amdgpu 0000:83:00.0: No more SDMA queue to allocate (16 total queues)   × 24
```

A 90-second burst on one device, then nothing. No `killed entity` before or
after it, and 0029's D-state count today is 0. (Its August `MES might be in
unrecoverable state` / `GPU reset begin` pair is on a *different* device,
`43:00.0`, and is the known 2026-08-19/20 incident.)

This is the case that matters for tuning: 0004 emitted the same SDMA line on
the same date and *is* wedged; 0029 emitted it 24 times and recovered. **SDMA
queue exhaustion on its own is survivable** — it says contexts are leaking,
not that the node is gone. So it is a `warning`, and only `killed entity` is
`fatal`. That split was chosen from 0004's evidence alone; 0024 and 0029
confirm it independently.

**0043** is clean across its entire retained history — no fatal signature of
any kind.

Two consequences. First, the fatal rule would have fired on a *worker* node,
not just on the control-plane nodes we cannot touch. Second, a node that
recovers still leaves the signature behind, so the conditions must key on
activity inside a 15-minute window rather than on cumulative counts — which is
what [`conditions.py`](../agent/gpu_health_agent/conditions.py) already does,
and this is the evidence for why.

---

## 2. Dead metrics exporters, reported Ready

This is a **second, independent fault** that happened to land on the same
pods. Fault 1 is why `gpuagent` stopped answering; this is why the exporter
process itself died and never came back.

### What it looks like

```
$ kubectl -n kube-amd-gpu get pods -l app.kubernetes.io/name=metrics-exporter
default-metrics-exporter-hnfmp   1/1   Running   1           98d    wx-ms-w7900d-0043
default-metrics-exporter-nxt4l   1/1   Running   0           6d18h  wx-ms-w7900d-0005
default-metrics-exporter-swrmm   1/1   Running   2           259d   wx-ms-w7900d-0006
default-metrics-exporter-vg8qn   1/1   Running   2 (25d ago) 245d   wx-ms-w7900d-0029
default-metrics-exporter-wj6pd   1/1   Running   0           87d    wx-ms-w7900d-0004
```

Five Running pods, five Ready endpoints on the `default-metrics-exporter`
Service. Three of them have served nothing for weeks. Anything scraping that
Service gets connection-refused three times in five, silently, forever.

### Root cause

All three dead exporters stopped on the same line, on three different dates:

| Node | Died | Last log line |
|---|---|---|
| 0006 | 2026-08-19 06:07 | `exporter slurm.go:78: too many open files` |
| 0004 | 2026-09-02 17:37 | `exporter slurm.go:78: too many open files` |
| 0005 | 2026-09-10 08:49 | `exporter slurm.go:78: too many open files` |

"Too many open files" reads as a file-descriptor limit, and it is not. On 0004
the container's process had **12 fds open against a soft limit of 1024**.

`inotify_init1()` returns `EMFILE` — which Go renders with that same string —
when the calling **uid** has exhausted its inotify instances. Every node in the
fleet sat at the kernel default:

```
fs.inotify.max_user_instances = 128     # 0004, 0005, 0006, 0024, 0029, 0043
```

and on 0004, uid 0 was already holding 138 inotify instances. Everything on
these nodes runs as root, so every container's Kubernetes watchers, every log
follower, every file watcher draws from that same pool of 128. The exporter
is not special — it is whichever process happened to start after the pool ran
dry. That is why the three deaths are weeks apart and look random.

0029 and 0043 were healthy only because their exporters started earlier. They
were one pod-churn away from the same failure.

On 0004 the order was: the `gpuagent` worker wedged in the driver (fault 1),
the exporter's calls to it started returning `DeadlineExceeded`, and at 17:37
— one minute after the kernel's first hung-task report — the exporter died on
inotify and stayed dead.

### Why nobody noticed

Two independent things had to go wrong, and both did.

**The exporter container's PID 1 is a `bash` wrapper.** It starts `gpuagent`
and the exporter as children. On 0004 the `gpuagent` child is a zombie
(`State: Z`) and the `bash` wrapper is still sleeping in `do_wait`. A container
whose PID 1 is alive is a Running container, no matter what died underneath it.

**The DaemonSet has no readinessProbe and no livenessProbe.** Either one would
have caught this the same minute it happened.

```
$ kubectl -n kube-amd-gpu get ds default-metrics-exporter -o json | jq '...'
probes: NONE
```

### Why it can't be fixed where it belongs

The DaemonSet is owned by the GPU Operator:

```
ownerReferences: [{kind: DeviceConfig, name: default, controller: true}]
```

so a probe patched onto it is reverted at the next reconcile. And the
`DeviceConfig` CRD has no field for one — `metricsExporter` accepts
`config, enable, image, imagePullPolicy, imageRegistrySecret, nodePort,
podAnnotations, podResourceAPISocketPath, port, prometheus, rbacConfig,
resource, selector, serviceAnnotations, serviceType, tolerations,
upgradePolicy`, and nothing for probes, resources or env.

### Fix

**(a) The root cause — raise the inotify ceiling fleet-wide.** ✅ **applied
2026-09-17.** All seven nodes went `128 → 8192` (`max_user_watches` →
1048576), confirmed from the init-container logs and from the host `sysctl` on
0004. Nothing was restarted; raising a bound that nothing is near cannot
disturb a running workload.

```sh
kubectl apply -k deploy/node-tuning
```

See [`deploy/node-tuning/inotify-limits.yaml`](../deploy/node-tuning/inotify-limits.yaml)
for the by-hand equivalent, which is the right form instead if node config is
owned by a configuration management system that would revert a live change.

**(b) Restart what already died.** ⚠️ **issued, cannot complete.** The delete
was accepted and all three pods entered `Terminating`, where they remain:

```
FailedKillPod  error killing pod: failed to "KillContainer" ... DeadlineExceeded
                                  failed to "KillPodSandbox" ... DeadlineExceeded
```

This is fault 1 blocking it. The container cannot be torn down while one of its
tasks is wedged in `D` inside the driver, and the DaemonSet controller will not
create a replacement until the old pod is gone. So
`up{job="amd-gpu-exporter"}` stays at 2/5 and `AMDMetricsExporterDown`
stays firing until those nodes are rebooted. The inotify fix is still what
stops it happening again on 0024/0029/0043.

**(c) The supervision gap stays open, so supervise it from outside.** Since
neither a probe nor a working PID 1 can be had through the operator,
`gpu-node-guard` takes it on: an exporter whose `/metrics` has been
unreachable for longer than a threshold gets its pod deleted, rate-limited and
with a Kubernetes Event, same guardrails as everything else the guard does.
Fault 1 is also the reason the guard must treat "deleted the pod" as *not*
proof of recovery — it has to verify the replacement actually serves.

**(d) Report upstream.** `rocm/device-metrics-exporter:v1.4.1` — PID 1 should
exit when `gpuagent` dies, and the DaemonSet wants a readiness probe. Worth
filing regardless of what we do locally.

---

## 3. Cluster DNS is broken for every pod

### What it looks like

`kubernetes.default` does not resolve from any pod in the cluster. Neither
does any Service name.

### Root cause

```
# /var/lib/kubelet/config.yaml
clusterDNS:
  - 10.232.0.10
clusterDomain: amd.gpu.dc
```

```
$ kubectl -n kube-system get svc kube-dns
kube-dns   ClusterIP   10.233.0.10   53/UDP,53/TCP,9153/TCP
```

The kubelets hand every pod a `/etc/resolv.conf` pointing at **10.232.0.10**.
The resolver is at **10.233.0.10**. Confirmed by raw query from inside a pod:
10.232.0.10 times out, 10.233.0.10 answers.

`10.232.0.0/16` is the **pod** CIDR (pod IPs on this cluster are 10.232.x.x);
the Service CIDR is `--service-cluster-ip-range=10.233.0.0/16`. So it is a
digit typo in one octet, cluster-wide, since install.

### Why the obvious shortcut doesn't work

Giving kube-dns a second Service with `clusterIP: 10.232.0.10` would fix it
with no node changes — but 10.232.0.10 is outside the Service CIDR, so the API
server refuses to allocate it. There is no way around the kubelet config.

### Current workaround

Every Deployment in `deploy/telemetry/` carries a pod-level override:

```yaml
dnsPolicy: None
dnsConfig:
  nameservers: ["10.233.0.10"]
  searches:
    - amd-telemetry.svc.amd.gpu.dc
    - svc.amd.gpu.dc
    - amd.gpu.dc
```

It is marked as a workaround in each file. It works, and it is wrong: it makes
each of our pods immune while leaving every other pod in the cluster broken,
and it hard-codes a resolver address into application manifests.

### Fix

**Not applied — the staged kubelet restart has not been authorised.** The
workaround above stays in place until it is.

On every node:

```sh
sudo sed -i 's/^- 10\.232\.0\.10$/- 10.233.0.10/' /var/lib/kubelet/config.yaml
grep -A1 clusterDNS /var/lib/kubelet/config.yaml     # confirm before restarting
sudo systemctl restart kubelet
```

Three things about this that are easy to get wrong:

- **Restarting kubelet does not restart running containers.** The pods on the
  node keep running and kubelet re-syncs to them. The exposure is the window
  where the node reports NotReady.
- **Existing pods keep their broken resolv.conf.** It is written at pod
  creation. So this fix is gradual: it repairs new pods immediately and old
  pods only as they are recreated. Nothing needs to be force-restarted.
- **Check first whether `/var/lib/kubelet/config.yaml` is generated** by
  kubespray or similar. If it is, fix the inventory instead, or the next run
  puts the typo back.

Order the nodes by how much they can afford to be NotReady for a few seconds:

```
0024 (stress box)  →  0043  →  0029  →  0005  →  0004  →  0006 last
```

0006 carries important services and goes last, after the pattern is proven on
five other nodes.

Once a node is done, verify from a freshly created pod on it:

```sh
kubectl run dnscheck --image=... --restart=Never --overrides='{"spec":{"nodeName":"<node>"}}' \
  -- getent hosts kubernetes.default
```

**After all nodes are fixed**, delete the `dnsPolicy: None` / `dnsConfig`
blocks from the three Deployments in `deploy/telemetry/` and re-apply. Leaving
them in place would hide a regression: if the kubelet config ever reverts, our
pods would keep working and we would find out from somebody else's outage.
