"""Minimal in-cluster Kubernetes client (stdlib only).

The agent needs exactly two calls - read a Node and patch its status
conditions - so pulling in the full `kubernetes` package and its transitive
dependency tree is not worth it on a DaemonSet that must stay boring.
"""

import json
import os
import ssl
import urllib.error
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class K8sError(Exception):
    pass


class K8sClient:
    def __init__(self, sa_dir=SA_DIR):
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise K8sError("KUBERNETES_SERVICE_HOST unset - not running in-cluster")
        self._base = f"https://{host}:{port}"

        try:
            with open(os.path.join(sa_dir, "token")) as fh:
                self._token = fh.read().strip()
        except OSError as exc:
            raise K8sError(f"cannot read service account token: {exc}") from exc

        ca = os.path.join(sa_dir, "ca.crt")
        self._ctx = ssl.create_default_context(cafile=ca if os.path.exists(ca) else None)

    def _request(self, method, path, body=None, content_type=None, timeout=10):
        url = self._base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise K8sError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise K8sError(f"{method} {path} failed: {exc}") from exc

    # -- surface used by the agent ----------------------------------------

    def get_node(self, name):
        return self._request("GET", f"/api/v1/nodes/{name}")

    def list_endpointslices(self, namespace, service):
        """EndpointSlices backing `service`.

        Used to find the AMD metrics exporter pod running on THIS node.
        Its Service is a plain ClusterIP, so scraping the service address
        would round-robin across every node's exporter and attribute another
        node's GPU health to this one. EndpointSlice endpoints carry
        `nodeName`, which lets us pick the local pod exactly.
        """
        return self._request(
            "GET",
            f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices"
            f"?labelSelector=kubernetes.io%2Fservice-name%3D{service}",
        )

    def patch_node_status_conditions(self, name, conditions):
        """Merge `conditions` into .status.conditions.

        Node conditions carry patchMergeKey=type, so a strategic merge patch
        updates matching entries in place and appends new ones without
        disturbing the conditions kubelet owns (Ready, MemoryPressure, ...).
        Never send a plain JSON merge patch here: that would replace the whole
        list and wipe the kubelet's conditions.
        """
        return self._request(
            "PATCH",
            f"/api/v1/nodes/{name}/status",
            body={"status": {"conditions": conditions}},
            content_type="application/strategic-merge-patch+json",
        )
