# Two cluster faults, their root causes, and the fixes

Both were found while deploying the telemetry stack. Neither is caused by this
project and neither is specific to GPU health — but both are exactly the kind
of thing this project exists to stop leaving in place: a failure that is
invisible from every dashboard, sitting quietly until it matters.

Each section ends with the fix. None of them are applied yet; all three touch
nodes carrying other people's services.

---

## 1. Dead metrics exporters, reported Ready

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
fleet sits at the kernel default:

```
fs.inotify.max_user_instances = 128     # 0004, 0005, 0006, 0024, 0029, 0043
```

and on 0004, uid 0 was already holding 138 inotify instances. Everything on
these nodes runs as root, so every container's Kubernetes watchers, every log
follower, every file watcher draws from that same pool of 128. The exporter
is not special — it is whichever process happened to start after the pool ran
dry. That is why the three deaths are weeks apart and look random: they are
whichever node last restarted an exporter into an exhausted pool.

0029 and 0043 are healthy only because their exporters started earlier. They
are one pod-churn away from the same failure.

### Why nobody noticed

Two independent things had to go wrong, and both did.

**The exporter container's PID 1 is a `bash` wrapper.** It starts `gpuagent`
and the exporter as children. On 0004 the `gpuagent` child is a zombie
(`State: Z`) and the `bash` wrapper is still sleeping. A container whose PID 1
is alive is a Running container, no matter what died underneath it.

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

**(a) The root cause — raise the inotify ceiling fleet-wide.** Nothing is
restarted; raising a bound that nothing is near cannot disturb a running
workload.

```sh
kubectl apply -k deploy/node-tuning
```

See [`deploy/node-tuning/inotify-limits.yaml`](../deploy/node-tuning/inotify-limits.yaml)
for the by-hand equivalent, which is the right form instead if node config is
owned by a configuration management system that would revert a live change.

**(b) Restart what already died.** The limit is what killed them; nothing
retries on its own.

```sh
kubectl -n kube-amd-gpu delete pod \
  default-metrics-exporter-wj6pd \
  default-metrics-exporter-nxt4l \
  default-metrics-exporter-swrmm
```

Verify with `up{job="amd-gpu-exporter"}` — it should go to 5/5, and
`AMDMetricsExporterDown` should clear.

**(c) The supervision gap stays open, so supervise it from outside.** Since
neither a probe nor a working PID 1 can be had through the operator,
`gpu-node-guard` takes it on: an exporter whose `/metrics` has been
unreachable for longer than a threshold gets its pod deleted, rate-limited and
with a Kubernetes Event, same guardrails as everything else the guard does.
This is the one piece not yet built.

**(d) Report upstream.** `rocm/device-metrics-exporter:v1.4.1` — PID 1 should
exit when `gpuagent` dies, and the DaemonSet wants a readiness probe. Worth
filing regardless of what we do locally.

---

## 2. Cluster DNS is broken for every pod

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
