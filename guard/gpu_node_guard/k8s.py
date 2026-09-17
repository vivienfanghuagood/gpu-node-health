"""In-cluster Kubernetes client for the guard (stdlib only).

Same reasoning as the agent's client: the set of calls is small and fixed, and
a component whose job is to work during an incident should not depend on a
package tree it cannot audit. It is a separate copy rather than a shared module
because the guard has a different, strictly larger permission surface - it can
write `.spec.unschedulable` - and keeping the two clients apart means the agent
physically cannot grow a cordon call by accident.
"""

import calendar
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class K8sError(Exception):
    pass


def parse_time(value):
    """RFC3339 -> epoch seconds, or None.

    Kubernetes emits UTC with a trailing Z for these fields, in two flavours:
    `metav1.Time` has second precision (Node conditions) and `metav1.MicroTime`
    carries six fractional digits (Lease renewTime). Both are accepted; the
    fraction is dropped, since nothing here cares about sub-second ages.

    Anything else returns None, and callers treat None as "no usable
    timestamp" - which makes a condition ineligible to trigger. Failing closed,
    not open.
    """
    if not value or not isinstance(value, str):
        return None
    head = value.split(".", 1)[0]
    if not head.endswith("Z"):
        head += "Z"
    try:
        return calendar.timegm(time.strptime(head, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def rfc3339(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def rfc3339_micro(ts):
    """The `metav1.MicroTime` wire format, for Lease acquireTime/renewTime.

    Those two fields are MicroTime, not Time, and the API server parses them
    with a layout that REQUIRES the six fractional digits - a plain
    second-precision stamp is rejected with a 400, not coerced. Found the hard
    way on the first live run.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000000Z"


class K8sClient:
    def __init__(self, sa_dir=SA_DIR):
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise K8sError("KUBERNETES_SERVICE_HOST unset - not running in-cluster")
        self._base = f"https://{host}:{port}"
        self._sa_dir = sa_dir

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
        if data is not None:
            # Any request with a body needs this. The API server rejects a
            # bodied request with no Content-Type as 415 UnsupportedMediaType,
            # which is how the first live run failed: the PATCH calls passed
            # one explicitly and the POSTs did not, so leader election could
            # never create its Lease. Defaulting here means a new call site
            # cannot repeat that.
            req.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise K8sError(f"{method} {path} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise K8sError(f"{method} {path} failed: {exc}") from exc

    # -- nodes ---------------------------------------------------------------

    def list_nodes(self, label_selector=""):
        query = ""
        if label_selector:
            query = "?labelSelector=" + urllib.parse.quote(label_selector, safe="")
        return (self._request("GET", "/api/v1/nodes" + query) or {}).get("items", [])

    def cordon(self, name):
        """Set .spec.unschedulable=true.

        This is the guard's entire mutating surface on Nodes. There is
        deliberately no uncordon() method: recovery goes through a human and a
        smoke test, and a method that does not exist cannot be called by a
        future bug.
        """
        return self._request(
            "PATCH",
            f"/api/v1/nodes/{name}",
            body={"spec": {"unschedulable": True}},
            content_type="application/strategic-merge-patch+json",
        )

    # -- events --------------------------------------------------------------

    def emit_event(self, namespace, involved, reason, message, etype="Warning",
                   now=None):
        """Record a core/v1 Event against `involved`.

        Every action AND every refusal gets one. The refusals matter more: a
        guard that declined to cordon because of the capacity floor has found
        something a human needs to act on, and if that only ever appeared in a
        pod log it would be invisible exactly when it counted.

        Events are best-effort. A failure to record one must never stop the
        loop, so callers swallow K8sError here - but the failure itself is
        surfaced as a metric so "the guard cannot write Events" is visible.
        """
        now = time.time() if now is None else now
        stamp = rfc3339(now)
        # Name must be unique; the API server would reject a repeat.
        suffix = f"{int(now * 1e6) % 10**10:010d}"
        body = {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {
                "name": f"{involved['name']}.{suffix}",
                "namespace": namespace,
            },
            "involvedObject": involved,
            "reason": reason,
            "message": message[:1024],
            "type": etype,
            "source": {"component": "gpu-node-guard"},
            "firstTimestamp": stamp,
            "lastTimestamp": stamp,
            "count": 1,
        }
        return self._request(
            "POST", f"/api/v1/namespaces/{namespace}/events", body=body
        )

    # -- leader election -----------------------------------------------------

    def get_lease(self, namespace, name):
        try:
            return self._request(
                "GET",
                f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{name}",
            )
        except K8sError as exc:
            if "-> 404" in str(exc):
                return None
            raise

    def create_lease(self, namespace, name, holder, duration, now):
        body = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": duration,
                "acquireTime": rfc3339_micro(now),
                "renewTime": rfc3339_micro(now),
            },
        }
        return self._request(
            "POST",
            f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases",
            body=body,
        )

    def update_lease(self, namespace, name, lease):
        return self._request(
            "PUT",
            f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{name}",
            body=lease,
            content_type="application/json",
        )

    # -- pods (exporter supervision) ----------------------------------------

    def list_pods(self, namespace, label_selector=""):
        query = ""
        if label_selector:
            query = "?labelSelector=" + urllib.parse.quote(label_selector, safe="")
        return (
            self._request("GET", f"/api/v1/namespaces/{namespace}/pods" + query) or {}
        ).get("items", [])

    def delete_pod(self, namespace, name):
        return self._request("DELETE", f"/api/v1/namespaces/{namespace}/pods/{name}")
