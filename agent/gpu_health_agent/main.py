"""gpu-health-agent entrypoint.

Runs on every GPU node as a DaemonSet. Collects the signals that were missing
during the August GPU hang incidents, publishes them as Prometheus metrics,
and - only when explicitly enabled - reflects them into Node conditions for
gpu-node-guard to act on.

The agent never evicts, cordons, or kills anything. Remediation is the guard's
job, and the guard is a separate process with a separate ServiceAccount so
that a bug in signal collection cannot take a node out of service.
"""

import json
import logging
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import metrics
from .conditions import ALL_CONDITIONS, ConditionEvaluator
from .config import Config
from .k8s import K8sClient, K8sError
from .signals.dstate import DStateCensus
from .signals.exporter import ExporterProbe, NodeLocalEndpointResolver
from .signals.kernlog import KernelLogWatcher
from .signals.patchctr import PatchCounters

LOG = logging.getLogger("gpu-health-agent")

CONDITION_PREFIX = "gpu-health.amd.io/"


class LazyClient:
    """One in-cluster API client, built on first use and shared.

    Construction reads the ServiceAccount token, which does not exist outside
    a cluster. Deferring it keeps the agent runnable on a laptop (with
    EXPORTER_URL pinned and conditions disabled) instead of dying at startup.
    """

    def __init__(self):
        self._client = None

    def __call__(self):
        if self._client is None:
            self._client = K8sClient()
        return self._client


class Collector:
    """Owns all signal sources and the most recent collected state."""

    def __init__(self, cfg, client_factory):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._state = {"collected_at": 0, "collect_errors": 0}

        self._kernlog = (
            KernelLogWatcher(cfg.KERNLOG_PATHS) if cfg.KERNLOG_ENABLED else None
        )
        self._dstate = (
            DStateCensus(
                cfg.PROC_PATH, cfg.DSTATE_STUCK_SECONDS, cfg.DSTATE_GPU_COMM_PREFIXES
            )
            if cfg.DSTATE_ENABLED else None
        )
        resolver = None
        if cfg.EXPORTER_ENABLED and not cfg.EXPORTER_URL:
            resolver = NodeLocalEndpointResolver(
                client_factory,
                cfg.EXPORTER_SERVICE_NAMESPACE,
                cfg.EXPORTER_SERVICE_NAME,
                cfg.NODE_NAME,
                cfg.EXPORTER_PORT,
                cfg.EXPORTER_METRICS_PATH,
            )
        self._exporter = (
            ExporterProbe(cfg.EXPORTER_URL, cfg.EXPORTER_TIMEOUT_SECONDS, resolver)
            if cfg.EXPORTER_ENABLED else None
        )
        self._patch = (
            PatchCounters(cfg.PATCH_COUNTERS_PATH)
            if cfg.PATCH_COUNTERS_ENABLED else None
        )
        self._evaluator = ConditionEvaluator()

    def start(self):
        if self._kernlog:
            self._kernlog.start()

    def state(self):
        with self._lock:
            return dict(self._state)

    def collect_once(self):
        started = time.monotonic()
        now = time.time()
        new = {"collected_at": now}

        try:
            new["kernlog"] = self._kernlog.snapshot() if self._kernlog else {}
            new["dstate"] = self._dstate.collect() if self._dstate else {}
            new["exporter"] = self._exporter.collect() if self._exporter else {}
            new["patch"] = self._patch.collect() if self._patch else {}

            evaluated = self._evaluator.evaluate(
                now, new["kernlog"], new["dstate"], new["exporter"]
            )
            new["condition_detail"] = evaluated
            new["conditions"] = {k: v["active"] for k, v in evaluated.items()}
            errors = 0
        except Exception:
            # A crash in collection must not take the agent down: a dead agent
            # is indistinguishable from a healthy quiet one to anything
            # downstream, which is the exact failure mode this project exists
            # to eliminate. Stay up, count the error, and let
            # gpuhealth_collect_errors_total drive an alert.
            LOG.exception("collection pass failed")
            errors = 1

        with self._lock:
            prior_errors = self._state.get("collect_errors", 0)
            if errors:
                self._state["collect_errors"] = prior_errors + 1
                self._state["collect_seconds"] = round(time.monotonic() - started, 3)
            else:
                new["collect_errors"] = prior_errors
                new["collect_seconds"] = round(time.monotonic() - started, 3)
                self._state = new


class ConditionPublisher:
    """Mirrors evaluated conditions onto the Node object."""

    def __init__(self, cfg, collector, client_factory):
        self._cfg = cfg
        self._collector = collector
        self._client_factory = client_factory
        self._last_status = {}       # condition name -> "True"/"False"
        self._transitions = {}       # condition name -> RFC3339 of last flip

    def publish(self):
        detail = self._collector.state().get("condition_detail") or {}
        if not detail:
            return

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conditions = []
        for name in ALL_CONDITIONS:
            info = detail.get(name)
            if info is None:
                continue
            status = "True" if info["active"] else "False"
            # Only advance lastTransitionTime when the status actually flipped,
            # so "how long has this node been broken" stays truthful. The guard
            # reads this field to decide whether a condition has persisted long
            # enough to act on, so a heartbeat must not reset it.
            if self._last_status.get(name) != status:
                self._transitions[name] = now
            self._last_status[name] = status

            conditions.append({
                "type": CONDITION_PREFIX + name,
                "status": status,
                "reason": info["reason"],
                "message": info["message"][:1024],
                "lastHeartbeatTime": now,
                "lastTransitionTime": self._transitions[name],
            })

        try:
            self._client_factory().patch_node_status_conditions(
                self._cfg.NODE_NAME, conditions
            )
        except K8sError as exc:
            LOG.error("failed to publish node conditions: %s", exc)


class Handler(BaseHTTPRequestHandler):
    collector = None  # injected below

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            state = self.collector.state()
            self._send(200, metrics.render(Config.NODE_NAME, state))
        elif path in ("/healthz", "/readyz"):
            state = self.collector.state()
            fresh = (
                state.get("collected_at", 0) > 0
                and time.time() - state["collected_at"]
                < max(60, Config.COLLECT_INTERVAL_SECONDS * 4)
            )
            self._send(200 if fresh else 503, "ok\n" if fresh else "stale\n")
        elif path == "/debug":
            self._send(
                200,
                json.dumps(self.collector.state(), indent=2, default=str),
                "application/json",
            )
        else:
            self._send(404, "not found\n")

    def log_message(self, fmt, *args):
        # Default BaseHTTPRequestHandler logging writes every scrape to
        # stderr, which at a 15s scrape interval is pure noise.
        return


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Config.validate()

    client_factory = LazyClient()
    collector = Collector(Config, client_factory)
    collector.start()
    collector.collect_once()

    Handler.collector = collector
    server = ThreadingHTTPServer((Config.LISTEN_ADDR, Config.LISTEN_PORT), Handler)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    LOG.info(
        "serving on %s:%s for node %s",
        Config.LISTEN_ADDR, Config.LISTEN_PORT, Config.NODE_NAME,
    )

    publisher = (
        ConditionPublisher(Config, collector, client_factory)
        if Config.NODE_CONDITIONS_ENABLED else None
    )
    if publisher:
        LOG.info("node condition publishing ENABLED")
    else:
        LOG.info(
            "node condition publishing disabled (observer mode); "
            "set NODE_CONDITIONS_ENABLED=true to turn it on"
        )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    last_publish = 0.0
    while not stop.is_set():
        collector.collect_once()
        if publisher and time.time() - last_publish >= Config.NODE_CONDITION_INTERVAL_SECONDS:
            publisher.publish()
            last_publish = time.time()
        stop.wait(Config.COLLECT_INTERVAL_SECONDS)

    LOG.info("shutting down")
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
