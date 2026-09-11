"""
shadow_cat1b_aggregate.py — SHADOW, EXPERIMENTAL. See shadow_common.py's
own docstring for the general approach and shadow_cat2a_aggregate.py's
docstring for the fuller rationale (standing AGGREGATE instead of an
open-world existential search) — this applies the same idea to
cat1b_asystole_ibp.dlog's `FILTER NOT EXISTS { ... }` block.

Unlike cat2a, this rule needs NO two-phase insert: the arriving alarm's
own message is reached via `patient -> isMonitoredBy -> ibpDevice ->
hasFunctionalUnit`, structurally distinct from the arriving alarm's own
`triggeredBy` device chain (the IBP monitor is a different device from
whatever triggered a HeartRate/Asystole alarm), so it can never
self-match the very thing it's checking for — a normal, single-phase
insert followed by the check is safe. See representation/rules/shadow/
cat1b_trigger_count_aggregate.dlog's own header for the full reasoning,
including why the check is `FILTER NOT EXISTS { ?fu shadowTriggerCount
?anyCount }` and not `FILTER(?c = 0)` (an AGGREGATE rule never produces a
fact for an empty group).

Not currently a proven bottleneck (cat1b's on-demand query was isolated
this session and found fast even at real-corpus scale) — this harness is
about verifying the redesign generalizes cleanly and stays correct, not
about chasing a known slowdown the way cat2a's harness was.
"""
from pathlib import Path

import shadow_common as SC

M = SC.M
R = SC.R
ROOT = SC.ROOT
SHADOW_CAT1B_RULE = SC.SHADOW_RULES_DIR / "cat1b_trigger_count_aggregate.dlog"


def build_check_query(alarm: str, tgraph: str, pgraph: str, patient_iri: str) -> str:
    return (
        f"select ?evidence where {{ "
        f"GRAPH ?g1 {{ <{alarm}> <{M.MDA}hasMessage> ?msg }} "
        f"GRAPH ?g1 {{ ?msg <{M.MDA}concernsPatient> ?patient }} "
        f"GRAPH ?g2 {{ ?msg <{M.MDA}triggeredBy> ?device }} "
        f"GRAPH ?g3 {{ ?device <{M.MDA}hasSensor> ?sensor }} "
        f"GRAPH ?g4 {{ ?sensor <{M.MDA}sensorProducesSignal> ?signal }} "
        f"GRAPH ?g5 {{ ?signal <{M.MDA}analyzedBy> ?analysis }} "
        f"GRAPH ?g6 {{ ?analysis <{M.MDA}producesMetric> ?metric }} "
        f"GRAPH ?g7 {{ ?metric <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        f"<https://w3id.org/mda/vocab/metric/HeartRate> }} "
        f"GRAPH ?g8 {{ ?metric <{M.MDA}hasRate> <https://w3id.org/mda/vocab/metric-rate/Absent> }} "
        f"GRAPH ?g9 {{ ?patient <{M.MDA}isMonitoredBy> ?ibpDevice }} "
        f"GRAPH ?g10 {{ ?ibpDevice <{M.MDA}hasFunctionalUnit> ?ibpFunctionalUnit }} "
        f"GRAPH ?g11 {{ ?ibpFunctionalUnit <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        f"<https://w3id.org/mda/vocab/functional-unit/FU_InvasiveBloodPressure> }} "
        f"FILTER NOT EXISTS {{ ?ibpFunctionalUnit <{M.MDA}shadowTriggerCount> ?anyCount }} "
        f"BIND(?ibpFunctionalUnit AS ?evidence) "
        f"}} limit 1"
    )


if __name__ == "__main__":
    kb = M.load_kb()
    scratch_root = ROOT / "MDA-POC-RDFox" / "_scratch" / "shadow_cat1b"

    print("=== correctness: cat1b_pos/cat1b_neg ===")
    ok = SC.run_correctness_check(
        kb, scratch_root,
        patients=("cat1b_pos", "cat1b_neg"),
        expected={"cat1b_pos": True, "cat1b_neg": False},
        check_kind="flaggedLikelyFalsePositive",
        shadow_rule_path=SHADOW_CAT1B_RULE,
        check_query_builder=build_check_query,
    )
    print()

    if ok:
        print("=== performance: patient 2826, alarms [3600:3700] ===")
        SC.run_performance_comparison(
            kb, scratch_root, "2826", 3600, 3700,
            shadow_rule_path=SHADOW_CAT1B_RULE,
            check_query_builder=build_check_query,
            check_kind="flaggedLikelyFalsePositive",
            production_enabled_rule="cat1b",
        )
    else:
        print("Correctness check failed -- not running the performance comparison.")
