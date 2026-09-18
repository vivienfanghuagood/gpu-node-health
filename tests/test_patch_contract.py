"""The D1-D7 amdgpu patches and the rules that watch them are one interface.

Same failure mode as test_alert_contract.py, one layer down. There, a guard
alert matched a reason string nothing emitted. Here, a kernlog rule can match a
printk format string no kernel ever prints - and the symptom is identical and
worse: the counter reads 0, and 0 is exactly what a well-behaved patch looks
like. "The patch never fired" and "we cannot see the patch fire" are the same
number on the dashboard.

That is not hypothetical on this fleet. `d3_failover` has been shipping as a
rule since the patch counters were added, and D3 contains no printk at all, so
it has always been structurally incapable of firing. It went unnoticed for the
same reason the guard bug did: every layer was individually valid.

So these tests pin each patch rule against the actual printk format strings in
the diffs at 0024:/root/incident_export/, plus lines captured verbatim from
0024's kern.log. A rule that cannot fire must be listed in KNOWN_DEAD with a
reason, which makes it a deliberate, reviewed statement instead of a silent
zero.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

# _COMPILED is what KernelLogWatcher actually matches against; test that
# rather than the raw RULES strings so the test exercises the shipped form.
from gpu_health_agent.signals.kernlog import _COMPILED as RULES  # noqa: E402

# Lines observed verbatim on wx-ms-w7900d-0024, or rendered by filling in the
# format specifiers of a format string. The format strings are not copied from
# the diffs - they are extracted from the modules that are actually loaded:
#
#   for m in amd-sched amdttm amdgpu; do zstdcat $(modinfo -n $m) | strings; done
#
# That distinction is the whole point. A diff describes what someone intended
# to build; the .rodata of the running module describes what will actually be
# printed. On this fleet those two disagreed - see KNOWN_DEAD below - and only
# the second one can be trusted. Note the [TTM] prefixes: they come from
# pr_fmt() and are absent from the diff, so a sample transcribed from the diff
# would silently be a different string than the kernel emits.
SAMPLES = {
    # amd-sched.ko - observed verbatim in kern.log 2026-09-15T04:38:14 on 0024.
    "d4_watchdog": (
        "amdgpu 0000:63:00.0: drm_sched sdma0: D4 watchdog: fence context "
        "1265 stalled: emitted=13742 signaled=13570 stale=0s"
    ),
    # amd-sched.ko, the D3.1 eviction warning in d4_ctx_lookup(). Not yet
    # observed; this is the watchdog reporting that it just stopped watching a
    # stalled context, so a nonzero count here invalidates any zero read of
    # d4_watchdog over the same window.
    "d4_map_evicted": (
        "drm_sched sdma0: D4 watchdog: map evicted pending-stalled context "
        "1265 (emit=13742 done=13570)"
    ),
    # amd-sched.ko, d4_remediate_stalled_ctx(). Never yet observed on any node.
    "d1_remediate": (
        "amdgpu 0000:63:00.0: drm_sched sdma0: D1 remediate: force-signaled "
        "3 stalled fence(s) on context 1265 with -ECANCELED"
    ),
    # amdttm.ko, the pr_err after TTM_DEL_MAX_TRIES attempts.
    "ttm_giving_up": (
        "[TTM] ttm: BO 00000000deadbeef delete GIVING UP after 4 attempts "
        "(~120s); fences never signaled. Leaking BO instead of hanging TTM "
        "workqueue."
    ),
    # amdttm.ko, the pr_warn_ratelimited inside the retry loop.
    "ttm_delete_blocked": (
        "[TTM] ttm: BO 00000000deadbeef delete blocked on unsignaled fences "
        "(attempt 2/4)"
    ),
}

# Rules that are known to be incapable of firing, with the reason. Anything in
# here is a gap in the patch set, not a gap in the rule - the fix is kernel
# side. Listing it is what keeps a structural zero from reading as a healthy
# zero.
KNOWN_DEAD = {
    "d3_failover": (
        "d3_pick_move_entity() selects an alternate SDMA entity with no printk "
        "on any path. Confirmed against the loaded module, not just the diff: "
        "`zstdcat $(modinfo -n amdgpu) | strings` on 0024 yields no D3/D4/D1 "
        "strings at all, while amd-sched.ko and amdttm.ko each yield their "
        "full set. Needs a kernel-side dev_warn before this rule can fire. "
        "Until then a D3 failover - the precondition for the C0 cross-context "
        "fence loss - is unobservable."
    ),
}

PATCH_RULES = [(name, pat) for name, pat, sev in RULES if sev == "patch"]


def test_there_are_patch_rules():
    assert PATCH_RULES, "patch instrumentation rules disappeared"


@pytest.mark.parametrize("name,pattern", PATCH_RULES)
def test_patch_rule_matches_real_printk(name, pattern):
    """Every patch rule matches a line the kernel actually prints."""
    if name in KNOWN_DEAD:
        pytest.skip(f"known dead: {KNOWN_DEAD[name]}")
    assert name in SAMPLES, (
        f"patch rule {name!r} has no sample line. Add the printk format "
        f"string from its diff to SAMPLES, or list it in KNOWN_DEAD with a "
        f"reason. An unpinned patch rule is how d3_failover survived."
    )
    assert pattern.search(SAMPLES[name]), (
        f"patch rule {name!r} does not match the line the kernel emits:\n"
        f"  pattern: {pattern.pattern}\n"
        f"  line:    {SAMPLES[name]}\n"
        f"This rule would read 0 forever, which is indistinguishable from a "
        f"patch that is behaving."
    )


def test_known_dead_rules_are_actually_dead():
    """KNOWN_DEAD must shrink only when the kernel side is fixed.

    If someone adds the missing printk and a sample for it, this fails and
    forces the entry out of KNOWN_DEAD - so the list cannot quietly outlive
    the defect it documents.
    """
    for name in KNOWN_DEAD:
        assert name not in SAMPLES, (
            f"{name!r} now has a sample line, so it is no longer dead. "
            f"Remove it from KNOWN_DEAD."
        )
        assert any(n == name for n, _ in PATCH_RULES), (
            f"{name!r} is listed in KNOWN_DEAD but is not a patch rule"
        )


def test_patch_rules_do_not_drive_conditions():
    """Patch counters are evidence, not a health verdict.

    D1 force-signaling a tenant fence with -ECANCELED is the patch doing its
    job; it must not be able to cordon the node by itself. Severity is the
    only thing separating the two, so assert it directly.
    """
    for name, _, sev in RULES:
        if name in {n for n, _ in PATCH_RULES}:
            assert sev == "patch", (
                f"{name!r} is patch instrumentation but has severity {sev!r}; "
                f"it would feed node conditions and could cordon a node for "
                f"the patch working correctly."
            )


def test_each_sample_is_claimed_by_its_own_rule_first():
    """First-match-wins must attribute each line to the rule that owns it.

    Two patch rules now share the "D4 watchdog: " prefix - d4_watchdog and
    d4_map_evicted - and they mean opposite things. The first says D4 caught a
    stalled context; the second says D4 threw one away and stopped watching it.
    If a loosened pattern let the earlier rule swallow the eviction line, the
    eviction counter would read 0 and the watchdog counter would read healthy
    activity, which is the most misleading pair of numbers this file can
    produce. Assert the attribution, not just that something matched.
    """
    for name, line in SAMPLES.items():
        winner = next(
            (n for n, rx, _ in RULES if rx.search(line)), None
        )
        assert winner == name, (
            f"sample for {name!r} is claimed first by rule {winner!r}; "
            f"_match() returns on the first hit, so {name!r} would never be "
            f"counted and {winner!r} would be counted for the wrong event"
        )


def test_samples_are_not_matched_by_fatal_or_warning_rules():
    """A patch line must not also trip a fatal rule.

    RULES is first-match-wins, so a patch line that happens to contain e.g.
    'blocked for more than' would be counted as a hung task and escalate. The
    D2 retry line is the realistic candidate - it says 'blocked'.
    """
    for name, line in SAMPLES.items():
        for other, pattern, sev in RULES:
            if sev == "patch":
                continue
            assert not pattern.search(line), (
                f"patch sample {name!r} also matches {sev} rule {other!r}; "
                f"first-match-wins would score the patch working as a fault"
            )
