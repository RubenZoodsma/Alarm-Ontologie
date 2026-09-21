"""
shadow_cat2b_aggregate.py — SHADOW, EXPERIMENTAL. See shadow_common.py's
and shadow_cat2a_aggregate.py's docstrings for the general approach.

Rewrite of cat2b_metric_sensor.dlog's self-join ("does an active alarm
sharing the same metricType, from a DIFFERENT sensor, exist") as a
standing COUNT(DISTINCT sensor) aggregate per (patient, metricType). No
two-phase insert needed (unlike cat2a): the arriving alarm's own sensor
IS counted once fully inserted, but that's fine — COUNT decomposes
cleanly where MAX doesn't. If the post-insert distinct-sensor count is
>= 2, at least one of those sensors isn't mine (mine is only ever one of
them), so `FILTER(?c >= 2)` after a completely normal single-phase insert
answers the question correctly. See representation/rules/shadow/
cat2b_sensor_count_aggregate.dlog's own header for the full reasoning.

Not yet proven a bottleneck in isolation the way cat2a was (cat2a+cat2b
were only isolated together this session, not separately) — same
structural shape as cat2a though (self-join on a shared concept-level
pivot), so the same latent concurrency-scaling risk plausibly applies;
this harness checks whether it does.
"""
from pathlib import Path

import shadow_common as SC

M = SC.M
R = SC.R
ROOT = SC.ROOT
SHADOW_CAT2B_RULE = SC.SHADOW_RULES_DIR / "cat2b_sensor_count_aggregate.dlog"


def build_check_query(alarm: str, tgraph: str, pgraph: str, patient_iri: str) -> str:
    return (
        f"select ?active where {{ "
        f"GRAPH ?g1 {{ "
        f"<{alarm}> <{M.MDA}hasCategory> <https://w3id.org/mda/vocab/alarm-category/Physiological> . "
        f"<{alarm}> <{M.MDA}hasMessage> ?msg . "
        f"}} "
        f"GRAPH ?g1 {{ ?msg <{M.MDA}concernsPatient> ?patient }} "
        f"GRAPH ?g2 {{ ?msg <{M.MDA}triggeredBy> ?device }} "
        f"GRAPH ?g3 {{ ?device <{M.MDA}hasSensor> ?sensorIn }} "
        f"GRAPH ?g4 {{ ?sensorIn <{M.MDA}sensorProducesSignal> ?signalIn }} "
        f"GRAPH ?g5 {{ ?signalIn <{M.MDA}analyzedBy> ?analysisIn }} "
        f"GRAPH ?g6 {{ "
        f"?analysisIn <{M.MDA}producesMetric> ?metricIn . "
        f"?metricIn <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?metricType . "
        f"}} "
        f'BIND(IRI(CONCAT(STR(<{patient_iri}>), "|", STR(?metricType))) AS ?mtKey) '
        f"?mtKey <{M.MDA}shadowSensorCount> ?c . "
        f"FILTER(?c >= 2) "
        f"BIND(<{alarm}> AS ?active) "
        f"}} limit 1"
    )


if __name__ == "__main__":
    kb = M.load_kb()
    scratch_root = ROOT / "MDA-POC-RDFox" / "_scratch" / "shadow_cat2b"

    print("=== correctness: cat2b_pos/cat2b_neg ===")
    ok = SC.run_correctness_check(
        kb, scratch_root,
        patients=("cat2b_pos", "cat2b_neg"),
        expected={"cat2b_pos": True, "cat2b_neg": False},
        check_kind="silencedBy",
        shadow_rule_path=SHADOW_CAT2B_RULE,
        check_query_builder=build_check_query,
    )
    print()

    if ok:
        print("=== performance: patient 2826, alarms [3600:3700] ===")
        SC.run_performance_comparison(
            kb, scratch_root, "2826", 3600, 3700,
            shadow_rule_path=SHADOW_CAT2B_RULE,
            check_query_builder=build_check_query,
            check_kind="silencedBy",
            production_enabled_rule="cat2b",
        )
    else:
        print("Correctness check failed -- not running the performance comparison.")
