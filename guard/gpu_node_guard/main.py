"""gpu-node-guard: watch GPU health conditions, cordon when allowed to.

Structure mirrors the agent - a serving thread and an evaluation loop - with
two additions the agent does not need: a Lease so only one replica ever acts,
and a hard separation between deciding (policy.py, pure) and doing (here).

What this process will and will not do is worth restating at the top of the
file that does it:

  will    cordon a node whose GPU health conditions have held long enough,
          once every guardrail passes and GUARD_MODE=enforce
  will    record a Kubernetes Event for every action AND every refusal
  will    delete an unreachable metrics-exporter pod, if explicitly enabled
  will not drain, evict, taint, or uncordon anything, under any configuration
"""

import http.server
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

from . import metrics, policy
from .config import Config
from .k8s import K8sClient, K8sError, parse_time, rfc3339
from .supervisor import DELETE, ExporterSupervisor

log = logging.getLogger("gpu-node-guard")


class Leader:
    """Lease-based leader election, reduced to what one Deployment needs.

    The guard runs a single replica, so this is not about scaling - it is about
    the window during a rolling update when two pods briefly overlap. Two
    guards evaluating at once would each see the other's cordon budget as
    unspent and could cordon two nodes on a two-node cluster.
    """

    def __init__(self, client, cfg):
        self._client = client
        self._cfg = cfg
        self._identity = f"{cfg.POD_NAME}.{socket.gethostname()}"
        self.is_leader = False

    def acquire_or_renew(self, now):
        ns, name = self._cfg.NAMESPACE, self._cfg.LEASE_NAME
        try:
            lease = self._client.get_lease(ns, name)
            if lease is None:
                self._client.create_lease(
                    ns, name, self._identity, self._cfg.LEASE_DURATION_SECONDS, now
                )
                self.is_leader = True
                return True

            spec = lease.setdefault("spec", {})
            holder = spec.get("holderIdentity")
            renewed = parse_time(spec.get("renewTime"))
            expired = (
                renewed is None
                or (now - renewed) > self._cfg.LEASE_DURATION_SECONDS
            )

            if holder != self._identity and not expired:
                self.is_leader = False
                return False

            if holder != self._identity:
                spec["holderIdentity"] = self._identity
                spec["acquireTime"] = rfc3339(now)
            spec["leaseDurationSeconds"] = self._cfg.LEASE_DURATION_SECONDS
            spec["renewTime"] = rfc3339(now)
            self._client.update_lease(ns, name, lease)
            self.is_leader = True
            return True
        except K8sError as exc:
            # Losing the lease means standing down, not carrying on. A guard
            # that cannot talk to the API server also cannot cordon, so the
            # safe reading of an error here is "I am not the leader".
            log.warning("lease: %s", exc)
            self.is_leader = False
            return False


def scrape_ok(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            # A 200 with an empty body is the exporter's own failure mode after
            # gpuagent dies, so require that it actually said something.
            return bool(resp.read(64).strip())
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Guard:
    def __init__(self, client, cfg):
        self._client = client
        self._cfg = cfg
        self._leader = Leader(client, cfg)
        self._supervisor = ExporterSupervisor(
            cfg, lambda url: scrape_ok(url, cfg.EXPORTER_TIMEOUT_SECONDS)
        )
        self._recent_cordons = []
        self._lock = threading.Lock()
        self._state = {
            "mode": cfg.MODE,
            "min_healthy_nodes": cfg.MIN_HEALTHY_NODES,
            "is_leader": False,
            "last_loop": 0,
            "loop_errors": 0,
            "event_errors": 0,
            "nodes_watched": 0,
            "healthy_capacity": 0,
            "cordons_total": 0,
            "decisions": [],
            "exporter_decisions": [],
        }

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _event(self, kind, name, node, reason, message, etype):
        involved = {"kind": kind, "name": name, "namespace": self._cfg.NAMESPACE}
        if kind == "Node":
            involved = {"kind": "Node", "name": name}
        elif kind == "Pod":
            involved = {
                "kind": "Pod", "name": name,
                "namespace": self._cfg.EXPORTER_NAMESPACE,
            }
        try:
            self._client.emit_event(
                self._cfg.NAMESPACE, involved, reason, message, etype
            )
        except K8sError as exc:
            with self._lock:
                self._state["event_errors"] += 1
            log.warning("event %s/%s: %s", reason, name, exc)

    def _run_nodes(self, now):
        nodes = self._client.list_nodes(self._cfg.NODE_LABEL_SELECTOR)
        decisions = policy.decide(
            nodes, now, self._cfg.policy(), parse_time, self._recent_cordons
        )
        capacity = policy.healthy_capacity(
            nodes, now, self._cfg.CONDITION_MIN_AGE_SECONDS, parse_time
        )

        cordoned = 0
        for dec in decisions:
            if dec.action == policy.CORDON:
                try:
                    self._client.cordon(dec.node)
                except K8sError as exc:
                    log.error("cordon %s failed: %s", dec.node, exc)
                    self._event("Node", dec.node, dec.node, "GPUCordonFailed",
                                f"wanted to cordon {dec.node} but the patch "
                                f"failed: {exc}", "Warning")
                    continue
                self._recent_cordons.append(now)
                cordoned += 1
                log.warning("%s", dec.message)
                self._event("Node", dec.node, dec.node, "GPUNodeCordoned",
                            dec.message, "Warning")
            elif dec.reason in _NOTEWORTHY:
                # Refusals and would-haves are the whole point of observe mode.
                log.warning("%s: %s", dec.node, dec.message)
                self._event("Node", dec.node, dec.node,
                            _EVENT_REASON[dec.reason], dec.message, "Warning")

        # Drop rate-limit history that has aged out.
        cutoff = now - self._cfg.RATE_LIMIT_WINDOW_SECONDS
        self._recent_cordons = [t for t in self._recent_cordons if t >= cutoff]

        return nodes, decisions, capacity, cordoned

    def _run_exporters(self, now):
        pods = self._client.list_pods(
            self._cfg.EXPORTER_NAMESPACE, self._cfg.EXPORTER_LABEL_SELECTOR
        )
        decisions = self._supervisor.evaluate(pods, now)
        for dec in decisions:
            if dec.action == DELETE:
                try:
                    self._client.delete_pod(self._cfg.EXPORTER_NAMESPACE, dec.pod)
                except K8sError as exc:
                    log.error("delete %s failed: %s", dec.pod, exc)
                    continue
                log.warning("%s", dec.message)
                self._event("Pod", dec.pod, dec.node, "ExporterPodDeleted",
                            dec.message, "Warning")
            elif dec.reason in _NOTEWORTHY_EXPORTER:
                log.warning("%s", dec.message)
        return decisions

    def loop_once(self, now):
        if not self._leader.acquire_or_renew(now):
            with self._lock:
                self._state["is_leader"] = False
                self._state["last_loop"] = now
            return

        nodes, decisions, capacity, cordoned = self._run_nodes(now)
        exporter_decisions = self._run_exporters(now)

        with self._lock:
            self._state.update({
                "is_leader": True,
                "last_loop": now,
                "nodes_watched": len(nodes),
                "healthy_capacity": capacity,
                "decisions": decisions,
                "exporter_decisions": exporter_decisions,
            })
            self._state["cordons_total"] += cordoned

    def run_forever(self):
        while True:
            start = time.time()
            try:
                self.loop_once(start)
            except Exception:  # noqa: BLE001 - the loop must outlive any bug
                with self._lock:
                    self._state["loop_errors"] += 1
                log.exception("evaluation loop failed")
            time.sleep(max(1.0, self._cfg.LOOP_INTERVAL_SECONDS - (time.time() - start)))


# Reasons worth an Event. `healthy` and `already-cordoned` are not: they are the
# steady state and would bury the ones that matter.
_NOTEWORTHY = {
    policy.R_CAPACITY_FLOOR,
    policy.R_RATE_LIMIT,
    policy.R_STALE_DATA,
    policy.R_OBSERVE_MODE,
}
_EVENT_REASON = {
    policy.R_CAPACITY_FLOOR: "GPUCordonRefusedCapacityFloor",
    policy.R_RATE_LIMIT: "GPUCordonRefusedRateLimit",
    policy.R_STALE_DATA: "GPUCordonRefusedStaleData",
    policy.R_OBSERVE_MODE: "GPUCordonWouldHave",
}
_NOTEWORTHY_EXPORTER = {"would-delete", "supervision-disabled", "cooldown"}


def make_handler(guard):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            payload = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            state = guard.snapshot()
            if self.path.startswith("/metrics"):
                self._send(200, metrics.render(state))
            elif self.path.startswith("/healthz"):
                self._send(200, "ok\n")
            elif self.path.startswith("/readyz"):
                fresh = time.time() - state["last_loop"] < 300
                self._send(200 if fresh else 503,
                           "ok\n" if fresh else "no recent evaluation\n")
            elif self.path.startswith("/debug"):
                dump = dict(state)
                dump["decisions"] = [d._asdict() for d in state["decisions"]]
                dump["exporter_decisions"] = [
                    d._asdict() for d in state["exporter_decisions"]
                ]
                self._send(200, json.dumps(dump, indent=2, default=str),
                           "application/json")
            else:
                self._send(404, "not found\n")

        def log_message(self, *_args):
            pass

    return Handler


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = Config
    if cfg.MODE not in {"observe", "enforce"}:
        log.warning("GUARD_MODE=%r is not observe or enforce; treating as "
                    "observe", cfg.MODE)
    log.info(
        "gpu-node-guard starting: mode=%s selector=%s floor=%d "
        "rate=%d/%ds min_age=%ds exporter_supervision=%s",
        cfg.MODE, cfg.NODE_LABEL_SELECTOR, cfg.MIN_HEALTHY_NODES,
        cfg.MAX_CORDONS_PER_WINDOW, cfg.RATE_LIMIT_WINDOW_SECONDS,
        cfg.CONDITION_MIN_AGE_SECONDS, cfg.EXPORTER_SUPERVISION_ENABLED,
    )

    guard = Guard(K8sClient(), cfg)
    server = http.server.ThreadingHTTPServer(
        (cfg.LISTEN_ADDR, cfg.LISTEN_PORT), make_handler(guard)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    guard.run_forever()


if __name__ == "__main__":
    main()
