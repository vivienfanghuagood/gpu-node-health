"""Tests for the AMD exporter probe and node-local endpoint discovery.

The discovery path exists because `default-metrics-exporter` is a ClusterIP
Service in front of a DaemonSet of non-hostNetwork pods. Scraping the service
address would round-robin across nodes, so a healthy node could report a sick
neighbour's GPUs as its own - or, worse, a sick node could report a healthy
neighbour's and be left in service. These tests pin that down.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from gpu_health_agent.signals.exporter import (  # noqa: E402
    ExporterProbe,
    NodeLocalEndpointResolver,
)


class FakeClient:
    def __init__(self, slices):
        self.slices = slices
        self.calls = 0

    def list_endpointslices(self, namespace, service):
        self.calls += 1
        return self.slices


def _slice(*endpoints):
    return {"items": [{"endpoints": list(endpoints)}]}


def _ep(ip, node, ready=True):
    return {"addresses": [ip], "nodeName": node, "conditions": {"ready": ready}}


def _resolver(client, node="wx-ms-w7900d-0029"):
    return NodeLocalEndpointResolver(
        lambda: client, "kube-amd-gpu", "default-metrics-exporter",
        node, 5000, "/metrics",
    )


def test_resolves_the_endpoint_on_this_node_only():
    client = FakeClient(_slice(
        _ep("10.244.1.7", "wx-ms-w7900d-0043"),
        _ep("10.244.2.9", "wx-ms-w7900d-0029"),
        _ep("10.244.3.4", "wx-ms-w7900d-0044"),
    ))
    assert _resolver(client).url() == "http://10.244.2.9:5000/metrics"


def test_never_falls_back_to_another_nodes_exporter():
    """No local endpoint must raise, not silently pick a neighbour."""
    client = FakeClient(_slice(_ep("10.244.1.7", "wx-ms-w7900d-0043")))
    try:
        _resolver(client).url()
    except LookupError as exc:
        assert "wx-ms-w7900d-0029" in str(exc)
    else:
        raise AssertionError("resolved a foreign node's exporter")


def test_not_ready_local_endpoint_is_not_used():
    client = FakeClient(_slice(_ep("10.244.2.9", "wx-ms-w7900d-0029", ready=False)))
    try:
        _resolver(client).url()
    except LookupError:
        pass
    else:
        raise AssertionError("scraped a not-ready exporter")


def test_resolution_is_cached_then_invalidated_on_failure():
    client = FakeClient(_slice(_ep("10.244.2.9", "wx-ms-w7900d-0029")))
    r = _resolver(client)
    r.url()
    r.url()
    assert client.calls == 1, "resolver hit the API server on the happy path"

    # An exporter pod restart gives it a new IP; the probe's failure must
    # force a re-resolve rather than pinning the dead address forever.
    r.invalidate()
    client.slices = _slice(_ep("10.244.2.55", "wx-ms-w7900d-0029"))
    assert r.url() == "http://10.244.2.55:5000/metrics"
    assert client.calls == 2


def test_probe_reports_unreachable_when_discovery_fails():
    """A probe that cannot find its exporter must say so, never pass silently."""
    client = FakeClient(_slice(_ep("10.244.1.7", "wx-ms-w7900d-0043")))
    out = ExporterProbe("", 1, _resolver(client)).collect()
    assert out["reachable"] is False
    assert out["healthy"] == 0
    assert "wx-ms-w7900d-0029" in out["error"]


def test_explicit_url_wins_over_discovery():
    """Local testing and hostNetwork deployments pin the URL directly."""
    client = FakeClient(_slice(_ep("10.244.2.9", "wx-ms-w7900d-0029")))
    probe = ExporterProbe("http://127.0.0.1:1/metrics", 1, _resolver(client))
    out = probe.collect()
    assert out["reachable"] is False
    assert client.calls == 0, "discovery ran despite an explicit URL"


# --- parsing -------------------------------------------------------------

def test_tenant_attribution_is_carried_off_the_exporter():
    """Real label set from default-metrics-exporter on 0029."""
    body = (
        "# HELP gpu_health GPU health\n"
        'gpu_nodes_total{hostname="wx-ms-w7900d-0029"} 8\n'
        'gpu_health{gpu_id="0",hostname="wx-ms-w7900d-0029"} 1\n'
        'gpu_health{gpu_id="3",hostname="wx-ms-w7900d-0029",'
        'container="vllm",namespace="shared-model-serving",'
        'pod="minicpm5-2b-a-6bdb4ccd4c-fxxwk"} 0\n'
    )

    class _Resp:
        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import gpu_health_agent.signals.exporter as mod
    original = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = lambda *a, **kw: _Resp()
    try:
        out = ExporterProbe("http://x/metrics", 1).collect()
    finally:
        mod.urllib.request.urlopen = original

    assert out["reachable"] is True
    assert out["gpu_total"] == 8
    assert out["healthy"] == 1
    assert out["unhealthy"] == [{
        "gpu_id": "3",
        "namespace": "shared-model-serving",
        "pod": "minicpm5-2b-a-6bdb4ccd4c-fxxwk",
        "container": "vllm",
    }]
