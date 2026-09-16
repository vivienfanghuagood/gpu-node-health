"""S4: probe the node-local AMD device-metrics-exporter.

The AMD GPU operator already runs `default-metrics-exporter` on every node
carrying feature.node.kubernetes.io/amd-gpu=true. It works: it serves ~691
metric lines on :5000 including gpu_health and, via the kubelet pod-resources
API, a GPU -> namespace/pod/container correlation.

What was missing is that nothing ever scraped it -
DeviceConfig.metricsExporter.prometheus.serviceMonitor.enable is false, the
cluster has no prometheus-operator CRDs, and VictoriaMetrics runs with no
scrape configuration at all. That is the whole of the "health data chain was
never established" finding.

The bulk scrape is vmagent's job (deploy/vmagent.yaml). This module only
answers two questions the agent needs for its own conditions:

  1. Is the exporter actually answering on this node right now?
  2. Which GPUs does it consider unhealthy, and who is sitting on them?

gpu_health is NOT treated as sufficient on its own. Its judgement is driven by
ECC/RAS and topology state, which is unlikely to move during a fence/MES
stall - every GPU on this fleet reports gpu_health=1 today. It is a useful
corroborating signal and the source of tenant attribution, not a hang detector.
"""

import re
import time
import urllib.error
import urllib.request

# Matches a Prometheus sample line, splitting the label set from the value.
_SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>.*)\}\s+(?P<value>\S+)\s*$")
_LABEL = re.compile(r'(?P<k>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<v>(?:[^"\\]|\\.)*)"')


def _parse_labels(raw):
    return {m.group("k"): m.group("v").replace('\\"', '"') for m in _LABEL.finditer(raw)}


class NodeLocalEndpointResolver:
    """Resolve the exporter pod running on this specific node.

    `default-metrics-exporter` is a ClusterIP Service in front of a DaemonSet,
    and the exporter pods are NOT hostNetwork. Scraping the Service address
    would land on an arbitrary node's exporter, so the agent would cheerfully
    report a healthy neighbour's GPUs as its own - exactly the class of
    silent misattribution this project exists to remove.

    The resolved pod IP is cached and re-resolved whenever a scrape fails, so
    an exporter pod restart (new IP) self-heals within one collection cycle
    without hammering the API server on the happy path.
    """

    def __init__(self, client_factory, namespace, service, node_name, port, path):
        self._client_factory = client_factory
        self._ns = namespace
        self._svc = service
        self._node = node_name
        self._port = port
        self._path = path
        self._cached = None

    def invalidate(self):
        self._cached = None

    def url(self):
        if self._cached:
            return self._cached
        slices = self._client_factory().list_endpointslices(self._ns, self._svc)
        for sl in slices.get("items", []):
            for ep in sl.get("endpoints", []):
                if ep.get("nodeName") != self._node:
                    continue
                if not (ep.get("conditions") or {}).get("ready", True):
                    continue
                for addr in ep.get("addresses", []):
                    self._cached = f"http://{addr}:{self._port}{self._path}"
                    return self._cached
        raise LookupError(
            f"no ready {self._ns}/{self._svc} endpoint on node {self._node}"
        )


class ExporterProbe:
    def __init__(self, url, timeout, resolver=None):
        """`url` pins a fixed endpoint; `resolver` discovers the node-local one.

        A fixed URL wins when set, which keeps local testing and hostNetwork
        deployments trivial. Otherwise the resolver is consulted.
        """
        self._url = url
        self._timeout = timeout
        self._resolver = resolver

    def collect(self):
        started = time.monotonic()
        url = ""
        try:
            # Surfaced in /debug as "url": an operator checking whether a node
            # is scraping its OWN exporter should not have to exec into the
            # pod and re-run the resolver by hand.
            url = self._url or self._resolver.url()
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad: urllib errors, OSError, and K8sError/LookupError
            # from the endpoint resolver all mean the same thing here. Any
            # failure to reach the exporter is reported as unreachable rather
            # than raised: a probe that cannot answer must say so, never stay
            # silent.
            if self._resolver is not None:
                self._resolver.invalidate()
            return {
                "reachable": False,
                "error": str(exc)[:200],
                "url": url,
                "scrape_seconds": round(time.monotonic() - started, 3),
                "gpu_total": 0,
                "healthy": 0,
                "unhealthy": [],
            }

        healthy = 0
        unhealthy = []
        gpu_total = 0

        for line in body.splitlines():
            if line.startswith("#") or not line:
                continue
            m = _SAMPLE.match(line)
            if not m:
                continue
            name = m.group("name")

            if name == "gpu_nodes_total":
                try:
                    gpu_total = int(float(m.group("value")))
                except ValueError:
                    pass
                continue

            if name != "gpu_health":
                continue

            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            labels = _parse_labels(m.group("labels"))
            if value >= 1.0:
                healthy += 1
            else:
                # Carry the tenant attribution through: when a GPU goes bad we
                # want to name the affected pod in the alert, not just the card.
                unhealthy.append({
                    "gpu_id": labels.get("gpu_id", "?"),
                    "namespace": labels.get("namespace", ""),
                    "pod": labels.get("pod", ""),
                    "container": labels.get("container", ""),
                })

        return {
            "reachable": True,
            "error": "",
            "url": url,
            "scrape_seconds": round(elapsed, 3),
            "gpu_total": gpu_total,
            "healthy": healthy,
            "unhealthy": unhealthy,
        }
