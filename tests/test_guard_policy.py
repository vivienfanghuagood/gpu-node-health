"""Guard policy tests.

Every scenario here is one the cluster can actually produce. The ones that
matter most are the refusals: on wx-ms-w7900d the whole serving capacity is two
nodes, so the correct behaviour in almost every real situation is to want to
cordon and then decline, loudly.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "guard"))

from gpu_node_guard import policy  # noqa: E402
from gpu_node_guard.config import _PolicyConfig  # noqa: E402
from gpu_node_guard.k8s import parse_time, rfc3339  # noqa: E402

NOW = 1_758_000_000


def cfg(**over):
    base = dict(
        mode="enforce",
        condition_min_age_seconds=120,
        min_healthy_nodes=1,
        max_cordons_per_window=1,
        rate_limit_window_seconds=3600,
    )
    base.update(over)
    return _PolicyConfig(**base)


def node(name, conditions=None, unschedulable=False, ready=True, age=600):
    conds = [{
        "type": "Ready",
        "status": "True" if ready else "False",
        "lastTransitionTime": rfc3339(NOW - 100000),
    }]
    for ctype, status in (conditions or {}).items():
        conds.append({
            "type": ctype,
            "status": status,
            "reason": "Test",
            "message": f"{ctype} from test",
            "lastTransitionTime": rfc3339(NOW - age),
        })
    return {
        "metadata": {"name": name},
        "spec": {"unschedulable": unschedulable},
        "status": {"conditions": conds},
    }


def only(decisions, name):
    return next(d for d in decisions if d.node == name)


def test_healthy_fleet_produces_no_action():
    nodes = [node("a"), node("b")]
    decisions = policy.decide(nodes, NOW, cfg(), parse_time)
    assert {d.action for d in decisions} == {policy.NOOP}
    assert {d.reason for d in decisions} == {policy.R_HEALTHY}


def test_unrecoverable_node_is_cordoned_when_capacity_allows():
    nodes = [
        node("sick", {policy.GPU_UNRECOVERABLE: "True"}),
        node("b"), node("c"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.action == policy.CORDON
    assert d.level == "L3"


def test_two_node_cluster_refuses_to_cordon_its_last_spare():
    """The real wx-ms-w7900d shape: 0029 and 0043, one of them goes bad.

    Cordoning leaves one healthy node. With the shipped floor of 2 that is a
    refusal, and the refusal is the correct answer - halving the cluster's
    serving capacity unattended is not a trade an automated system gets to
    make.
    """
    nodes = [
        node("0029", {policy.GPU_WORKQUEUE_STALLED: "True"}),
        node("0043"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(min_healthy_nodes=2), parse_time), "0029")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_CAPACITY_FLOOR
    assert "below the floor of 2" in d.message


def test_observe_mode_never_acts_but_says_what_it_would_have_done():
    nodes = [node("sick", {policy.GPU_UNRECOVERABLE: "True"}), node("b"), node("c")]
    d = only(policy.decide(nodes, NOW, cfg(mode="observe"), parse_time), "sick")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_OBSERVE_MODE
    assert d.message.startswith("WOULD CORDON")


def test_an_unknown_mode_string_fails_closed():
    nodes = [node("sick", {policy.GPU_UNRECOVERABLE: "True"}), node("b"), node("c")]
    d = only(policy.decide(nodes, NOW, cfg(mode="ENFORCE "), parse_time), "sick")
    assert d.action == policy.NOOP


def test_condition_must_hold_before_it_counts():
    """Debounce comes from lastTransitionTime, not from a counter in memory."""
    nodes = [node("sick", {policy.GPU_UNRECOVERABLE: "True"}, age=30),
             node("b"), node("c")]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.reason == policy.R_TOO_YOUNG

    nodes[0] = node("sick", {policy.GPU_UNRECOVERABLE: "True"}, age=300)
    assert only(policy.decide(nodes, NOW, cfg(), parse_time), "sick").action == policy.CORDON


def test_a_condition_with_no_usable_timestamp_fails_closed():
    n = node("sick", {policy.GPU_UNRECOVERABLE: "True"})
    n["status"]["conditions"][1]["lastTransitionTime"] = "not a timestamp"
    d = only(policy.decide([n, node("b"), node("c")], NOW, cfg(), parse_time), "sick")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_TOO_YOUNG


def test_stale_health_data_blocks_action_on_that_node():
    """The agent admitting its inputs are broken outranks what it reported."""
    nodes = [
        node("sick", {
            policy.GPU_UNRECOVERABLE: "True",
            policy.GPU_HEALTH_DATA_STALE: "True",
        }),
        node("b"), node("c"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_STALE_DATA


def test_stale_data_refusal_is_visible_in_observe_mode_too():
    nodes = [
        node("sick", {
            policy.GPU_UNRECOVERABLE: "True",
            policy.GPU_HEALTH_DATA_STALE: "True",
        }),
        node("b"), node("c"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(mode="observe"), parse_time), "sick")
    assert d.reason == policy.R_STALE_DATA


def test_probe_failure_alone_never_cordons_a_node():
    """One bad card out of eight is not a reason to remove the other seven."""
    nodes = [node("sick", {policy.GPU_PROBE_FAILED: "True"}), node("b"), node("c")]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_REPORT_ONLY
    assert d.level == "L1"


def test_engine_timeout_is_l2_and_does_cordon():
    nodes = [node("sick", {policy.GPU_ENGINE_TIMEOUT: "True"}), node("b"), node("c")]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.action == policy.CORDON
    assert d.level == "L2"


def test_the_most_severe_condition_wins():
    nodes = [
        node("sick", {
            policy.GPU_PROBE_FAILED: "True",
            policy.GPU_UNRECOVERABLE: "True",
        }),
        node("b"), node("c"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.level == "L3"
    assert d.condition == policy.GPU_UNRECOVERABLE


def test_already_cordoned_node_is_left_alone():
    nodes = [
        node("sick", {policy.GPU_UNRECOVERABLE: "True"}, unschedulable=True),
        node("b"), node("c"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(), parse_time), "sick")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_ALREADY_CORDONED


def test_rate_limit_stops_the_second_cordon_in_the_same_window():
    nodes = [
        node("s1", {policy.GPU_UNRECOVERABLE: "True"}),
        node("s2", {policy.GPU_UNRECOVERABLE: "True"}),
        node("b"), node("c"), node("d"),
    ]
    decisions = policy.decide(nodes, NOW, cfg(), parse_time)
    actions = [d.action for d in decisions if d.node in ("s1", "s2")]
    assert actions.count(policy.CORDON) == 1
    refused = only(decisions, "s2")
    assert refused.reason == policy.R_RATE_LIMIT


def test_rate_limit_counts_cordons_this_guard_already_made():
    nodes = [node("sick", {policy.GPU_UNRECOVERABLE: "True"}), node("b"), node("c")]
    d = only(
        policy.decide(nodes, NOW, cfg(), parse_time, recent_cordons=[NOW - 60]),
        "sick",
    )
    assert d.reason == policy.R_RATE_LIMIT

    # ...and forgets them once the window passes.
    d = only(
        policy.decide(nodes, NOW, cfg(), parse_time, recent_cordons=[NOW - 7200]),
        "sick",
    )
    assert d.action == policy.CORDON


def test_capacity_does_not_count_sick_or_cordoned_or_notready_nodes():
    nodes = [
        node("ok"),
        node("cordoned", unschedulable=True),
        node("notready", ready=False),
        node("sick", {policy.GPU_UNRECOVERABLE: "True"}),
    ]
    assert policy.healthy_capacity(nodes, NOW, 120, parse_time) == 1


def test_capacity_floor_is_measured_after_the_cordon_not_before():
    """Three nodes, two already sick: what happens to the last healthy one.

    This is the scenario that would let a naive guard finish the job the
    incident started, so the floor has to be evaluated against what would
    REMAIN, not against what exists now. Here "now" is 2 (s2 and last are both
    schedulable and Ready) but "after" is 1, and only the second number is the
    one worth checking.

    The floor is inclusive: leaving exactly N healthy nodes is allowed. That
    boundary is pinned in both directions here because getting it off by one
    is the difference between protecting the cluster and halving it.
    """
    nodes = [
        node("s1", {policy.GPU_UNRECOVERABLE: "True"}, unschedulable=True),
        node("s2", {policy.GPU_UNRECOVERABLE: "True"}),
        node("last"),
    ]
    d = only(policy.decide(nodes, NOW, cfg(min_healthy_nodes=2), parse_time), "s2")
    assert d.action == policy.NOOP
    assert d.reason == policy.R_CAPACITY_FLOOR
    assert "would leave 1 healthy" in d.message

    d = only(policy.decide(nodes, NOW, cfg(min_healthy_nodes=1), parse_time), "s2")
    assert d.action == policy.CORDON


def test_policy_never_emits_anything_but_cordon_or_noop():
    """A structural guarantee: there is no code path to drain or uncordon."""
    nodes = [
        node("a", {t: "True" for t in (
            policy.GPU_UNRECOVERABLE, policy.GPU_WORKQUEUE_STALLED,
            policy.GPU_ENGINE_TIMEOUT, policy.GPU_PROBE_FAILED,
        )}),
        node("b", unschedulable=True),
        node("c", ready=False),
    ]
    for mode in ("observe", "enforce"):
        for floor in (0, 1, 2, 5):
            decisions = policy.decide(
                nodes, NOW, cfg(mode=mode, min_healthy_nodes=floor), parse_time
            )
            assert {d.action for d in decisions} <= {policy.CORDON, policy.NOOP}


@pytest.mark.parametrize("value,expected", [
    ("2026-09-17T10:00:00Z", 1789639200),
    ("", None),
    (None, None),
    ("2026-09-17 10:00:00", None),
    ("2026-09-17T10:00:00+00:00", None),
])
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_parse_time_accepts_microtime():
    """Lease renewTime is metav1.MicroTime, Node conditions are metav1.Time.

    Both have to round-trip through the same parser, and the fraction is
    dropped rather than rejected - a guard that could not read back its own
    Lease would stand down forever, which is how the first live run behaved.
    """
    from gpu_node_guard.k8s import rfc3339_micro
    assert parse_time("2026-09-17T10:00:00.123456Z") == 1789639200
    assert parse_time(rfc3339_micro(1789639200)) == 1789639200
    assert rfc3339_micro(1789639200).endswith(".000000Z")


# -- Event namespace ---------------------------------------------------------
#
# These pin a rule the API server enforces and that nothing else in the code
# would reveal: an Event's namespace must agree with its involvedObject's.
# Getting it wrong returns 422 per event and silently reduces the guard's
# written record to pod logs, which is the one failure mode that would break
# "every removal alerts" while leaving the process looking healthy.

from gpu_node_guard.k8s import K8sClient  # noqa: E402


def test_node_events_go_to_default_because_nodes_are_cluster_scoped():
    involved = {"kind": "Node", "name": "wx-ms-w7900d-0024"}
    assert K8sClient.event_namespace(involved, "gpu-node-health") == "default"


def test_an_empty_involved_namespace_is_treated_the_same_as_a_missing_one():
    involved = {"kind": "Node", "name": "n", "namespace": ""}
    assert K8sClient.event_namespace(involved, "gpu-node-health") == "default"


def test_namespaced_objects_keep_their_own_namespace():
    involved = {"kind": "Pod", "name": "exporter-x", "namespace": "kube-amd-gpu"}
    assert K8sClient.event_namespace(involved, "gpu-node-health") == "kube-amd-gpu"
