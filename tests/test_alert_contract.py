"""The alert rules and the code that feeds them are one interface. Check it.

These tests exist because of a bug that shipped: `GPUGuardWouldHaveCordoned`
matched `reason="would-cordon"`, a constant that was defined in policy.py and
emitted by nothing. The rule loaded without error, evaluated without error, and
was silently incapable of ever firing - and it happened to be the single most
important rule while the guard runs in observe mode.

Nothing caught it because every layer was individually valid. The YAML parsed,
the constant existed, the metric existed. Only the *join* between them was
wrong, and no test looked at the join.

So these read the shipped alert rules, pull out the label values the rules
actually match on, and check each one against the strings the code actually
produces. A rule that can never fire is a failing test here.
"""

import os
import re
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "guard"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from gpu_node_guard import metrics, policy  # noqa: E402
from gpu_node_guard.supervisor import ExporterSupervisor  # noqa: E402

ALERTS = os.path.join(
    os.path.dirname(__file__), "..", "deploy", "telemetry", "alerts.yaml"
)


def _rules():
    with open(ALERTS) as fh:
        doc = yaml.safe_load(fh)
    out = []
    for group in doc["groups"]:
        for rule in group.get("rules", []):
            if "alert" in rule:
                out.append((group["name"], rule))
    return out


def _guard_reasons_in_code():
    """Every reason string policy.py can put in a Decision."""
    return {
        getattr(policy, name)
        for name in dir(policy)
        if name.startswith("R_") and isinstance(getattr(policy, name), str)
    }


def _exporter_reasons_in_code():
    """Every reason string the supervisor can put in a PodDecision.

    Read off the source rather than a constant list, because the supervisor
    writes them as literals - which is exactly why they need checking.
    """
    src = sys.modules[ExporterSupervisor.__module__].__file__
    with open(src) as fh:
        body = fh.read()
    return set(re.findall(r'reason=["\']([a-z-]+)["\']', body)) | set(
        re.findall(r'["\']([a-z-]+)["\'],\s*# reason', body)
    )


def test_alerts_file_parses_and_has_rules():
    rules = _rules()
    assert len(rules) >= 28, f"expected the full rule set, found {len(rules)}"


@pytest.mark.parametrize("group,rule", _rules(), ids=lambda x: getattr(x, "get", lambda *_: x)("alert", x) if isinstance(x, dict) else str(x))
def test_every_rule_has_a_summary(group, rule):
    """An alert with no summary is a pager that says nothing."""
    assert rule.get("annotations", {}).get("summary"), rule["alert"]


def test_every_reason_matched_by_a_rule_is_one_the_code_emits():
    """The bug that motivated this file.

    A rule matching reason="would-cordon" is not a typo that fails loudly - it
    is a rule that evaluates cleanly forever and never fires.
    """
    known = _guard_reasons_in_code() | _exporter_reasons_in_code()
    bad = []
    for group, rule in _rules():
        for reason in re.findall(r'reason=["\']([^"\']+)["\']', rule["expr"]):
            # Skip regex matchers (reason=~"a|b"); those are checked below.
            if reason not in known:
                bad.append((rule["alert"], reason))
    assert not bad, (
        "these rules match reason label values that no code path emits, so "
        f"they can never fire: {bad}. Known reasons: {sorted(known)}"
    )


def test_regex_reason_matchers_have_at_least_one_live_alternative():
    known = _guard_reasons_in_code() | _exporter_reasons_in_code()
    for group, rule in _rules():
        for alts in re.findall(r'reason=~["\']([^"\']+)["\']', rule["expr"]):
            options = set(alts.split("|"))
            assert options & known, (
                f"{rule['alert']}: none of {sorted(options)} is emitted by any "
                "code path"
            )


def test_every_gpuguard_metric_a_rule_uses_is_one_the_guard_renders():
    """Catches a renamed or never-implemented series.

    render() is driven by the state dict, so build one with every key populated
    and read the metric names straight out of the exposition text.
    """
    dec = policy.Decision("n", policy.NOOP, policy.R_HEALTHY, None, None, "m")
    from gpu_node_guard.supervisor import PodDecision

    state = {
        "mode": "observe", "min_healthy_nodes": 2, "is_leader": True,
        "last_loop": 1.0, "loop_errors": 0, "event_errors": 0,
        "nodes_watched": 1, "healthy_capacity": 1, "cordons_total": 0,
        "decisions": [dec],
        "exporter_decisions": [
            PodDecision("p", "n", "noop", "reachable", 0, "m")
        ],
    }
    rendered = set(
        re.findall(r"^(gpuguard_[a-z_]+)", metrics.render(state), re.M)
    )
    assert rendered, "render() produced no gpuguard_* series"

    used = set()
    for group, rule in _rules():
        used |= set(re.findall(r"\b(gpuguard_[a-z_]+)", rule["expr"]))

    missing = used - rendered
    assert not missing, (
        f"alert rules reference gpuguard series the guard never renders: "
        f"{sorted(missing)}"
    )


def test_exporter_unreachable_rule_does_not_use_the_colliding_pod_label():
    """`pod` on a gpuguard series is the guard's own pod.

    The scrape config relabels `pod` onto every target, so a rule grouping by
    `pod` to identify a metrics-exporter would silently group by the guard
    instead - and with one guard replica that collapses all five exporters into
    one series.
    """
    for group, rule in _rules():
        if "gpuguard_exporter" not in rule["expr"]:
            continue
        by = re.findall(r"by\s*\(([^)]*)\)", rule["expr"])
        for clause in by:
            labels = {x.strip() for x in clause.split(",")}
            assert "pod" not in labels, (
                f"{rule['alert']} groups an exporter series by `pod`, which is "
                "the guard's own pod. Use exporter_pod."
            )
