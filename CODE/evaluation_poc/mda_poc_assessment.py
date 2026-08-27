"""
mda_poc_assessment.py — CAT1/CAT2/CAT3 alarm management rule engine.

The entry point for the MDA-POC results pipeline, kept directly under
evaluation_poc/ rather than buried in a subfolder — this is the file you run.
It is the "engine" half of an engine/reasoner split (CODE/evaluation_poc/
reasoner/assess.py is the reasoner half; CODE/evaluation_poc/core/
op_knowledge.py is the shared extraction/minting/reasoning library both sit
on top of). Owns the loop only: for each patient, walks events in ARRIVAL
order (each alarm's own start time — distinct from cq_temporal/op_inference.py's
state-change-instant ticks, which serves CQ9-11/visualization, a separate
concern left untouched by this file). For each arriving alarm:

  1. Extraction/minting is reused, not reimplemented, but NOT called once per
     patient the way CQ9-11/visualization call it: K.build_timeline and
     R.resolve_identity are both re-invoked at EVERY arriving alarm, scoped to
     events_so_far = only this patient's events with start <= this alarm's
     own start. This is deliberate, not a performance oversight — see
     "Why re-resolve identity per alarm" below.
  2. The reasoner (assess.py) is given the situational graph AT THE ARRIVING
     ALARM'S OWN START INSTANT — which, via Timeline.situation_at/
     background_at (themselves already scoped to events_so_far, doubly so
     now), already combines (a) whatever is still active/persisting from
     earlier alarms and (b) this alarm's own newly minted knowledge (merged in
     exactly the way op_knowledge.py's existing merge_conditions/
     entailed_situation always has been) — i.e. "the reasoner assesses rules
     combining the new alarm's extracted knowledge with the current
     situational graph," per the manuscript's own description of the
     MDA-POC's methodology.
  3. Fired outcomes are recorded to a CSV. Nothing here writes an outcome
     back into the knowledge graph — every alarm's knowledge merges into the
     patient's timeline exactly as it already did before this file existed;
     rule outcome is bookkeeping for the manuscript's performance count
     (flagged-false-positive for CAT1, silenced for CAT2, triggered
     sequence for CAT3), not a KG mutation.

Why re-resolve identity per alarm
----------------------------------
K.build_timeline's own docstring requires "one device's (or one patient's)
FULL event history, never a subset of a single tick" for
resolve_particular_identities — correct for CQ9-11/visualization, which
reason over an already-complete corpus and have no arrival-order claim to
keep. This engine does have one (Section 4.4's RDF-stream framing: a rule may
never be satisfied by knowledge not yet observed), and calling build_timeline
once per patient with the patient's COMPLETE event list — future alarms
included — would silently violate it: an identity-refinement value
(mda:hasAnatomicalPosition / mda:hasLaterality — the only two properties
resolve_particular_identities' conflict/union logic ever touches, see
refining_properties()) revealed only by a LATER alarm would already be baked
into the graph built for an EARLIER one.

Rebuilding the Timeline from events_so_far at every arrival closes this
without touching build_timeline/resolve_particular_identities themselves
(both stay exactly as they are, correctly, for CQ9-11/visualization). The
tradeoff is real but bounded: this reruns identity resolution and background
construction up to O(n) times per patient instead of once, but the dominant
cost — OWL-RL closure — is unaffected, since reasoning_cache already memoises
it by (label, device_id) key across every one of these calls, for this
patient and every other. Re-run against DATA/CAT_evaluation and
DATA/POC_EVENTS after any change here and diff cat_rules_outcomes.csv — see
EVALUATION/MDA-POC/cat_rules_report.md for the existing validation
methodology this must not regress. Revisit before the full temporal-corpus
run if profiling shows the per-tick rebuild is too slow at that scale.

Usage
-----
  python3 mda_poc_assessment.py --dataset ../DATA/CAT_evaluation/events_data.csv
  python3 mda_poc_assessment.py                      # defaults to DATA/POC_EVENTS
"""

from __future__ import annotations

import argparse
import itertools
import time
import csv
import sys
from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import RDF

sys.path.append(str(Path(__file__).resolve().parent / "core"))
sys.path.append(str(Path(__file__).resolve().parent / "reasoner"))

import op_knowledge as K
import assess as R

DEFAULT_DATASET = K.ROOT / "DATA" / "POC_EVENTS" / "events_data.csv"
OUT = K.ROOT / "EVALUATION" / "MDA-POC" / "cat_rules_outcomes.csv"
EVENTS_OUT = K.ROOT / "EVALUATION" / "MDA-POC" / "clinical_events_detected.ttl"

CLINICAL_EVENT = Namespace("https://w3id.org/mda/vocab/clinical-event/")

CAT1_RULES = {"cat1a_signal_quality", "cat1b_asystole_ibp"}
CAT2_RULES = {"cat2a_process_priority", "cat2b_metric_sensor"}
CAT3_RULES = {"cat3a_cardiorespiratory_arrest", "cat3b_ventilation_failure"}


def mint_cardiorespiratory_arrest(patient: str, cardiac_node, respiratory_node, start, events_graph: Graph) -> None:
    """
    Construct a new, named mda:ClinicalEvent individual for one
    cardiac/respiratory arrest coincidence — the one thing cat3a_
    cardiorespiratory_arrest.rq's plain SELECT cannot do itself (OWL-RL,
    and a stateless SELECT, can each tell you a coincidence holds, but
    neither can conditionally mint a new individual). Written into a
    SEPARATE side graph (events_graph), not merged into the live Timeline
    — deliberately: this is a historical record of what was detected at
    `start`, not a standing claim the Timeline's own temporal model should
    reason about further, and merging into it would risk the carefully
    validated background/condition/persistence machinery documented in
    this module's own docstring.

    Deduplication (one node per genuine episode, not one per firing) is
    the CALLER's responsibility (see run()'s open_pairs rising-edge
    tracking) — this function is called only on a FALSE->TRUE transition
    for a given (cardiac_node, respiratory_node) pair, never on every
    arrival where the rule still matches, since the rule fires on every
    arrival for as long as the coincidence holds.
    """
    event = K.INST[f"ClinicalEvent_CardioRespiratoryArrest_{K._clean(patient)}_{start.strftime('%Y%m%dT%H%M%S')}"]
    events_graph.add((event, RDF.type, K.MDA.ClinicalEvent))
    events_graph.add((event, RDF.type, CLINICAL_EVENT.CardioRespiratoryArrest))
    events_graph.add((event, K.MDA.evidencedBy, cardiac_node))
    events_graph.add((event, K.MDA.evidencedBy, respiratory_node))
    events_graph.add((event, K.MDA.concernsPatient, K.patient_iri(patient)))


def mint_ventilation_failure(patient: str, device_node, process_node, start, events_graph: Graph) -> None:
    """
    Same shape as mint_cardiorespiratory_arrest, for cat3b_
    ventilation_failure.rq's coincidence instead. evidencedBy's range was
    widened to a union of mda:PhysiologicalProcess and mda:Device
    specifically for this (see ontology.ttl's own comment on that property)
    — device_node is a raw Device-level operation-state fact, not a
    Process-anchored impliesClinicalEvent tag the way cardiac/respiratory arrest
    are, so it would not fit the original PhysiologicalProcess-only range.

    Deduplication is the CALLER's responsibility, same rising-edge shape as
    cat3a's own open_pairs (see run()) — keyed on (device_node, process_node)
    instead of (cardiac_node, respiratory_node).
    """
    event = K.INST[f"ClinicalEvent_VentilationFailure_{K._clean(patient)}_{start.strftime('%Y%m%dT%H%M%S')}"]
    events_graph.add((event, RDF.type, K.MDA.ClinicalEvent))
    events_graph.add((event, RDF.type, CLINICAL_EVENT.VentilationFailure))
    events_graph.add((event, K.MDA.evidencedBy, device_node))
    events_graph.add((event, K.MDA.evidencedBy, process_node))
    events_graph.add((event, K.MDA.concernsPatient, K.patient_iri(patient)))


def run(dataset: Path, n_patients: int = None) -> tuple:
    kb = K.load_kb()
    events = K.load_events(dataset)

    # Static, catalogue-only filter — applied once, before the stream starts,
    # against kb.type_index (already fully built at this point from
    # kg_generated.ttl). This is NOT a forward-looking read: it never
    # consults which alarms occur, in what order, or how many times: only
    # whether a label is a member of the fixed, already-published archetype
    # catalogue. An event with no archetype is already a guaranteed no-op
    # downstream (condition_for_event/background_graph/
    # resolve_particular_identities all return empty/skip on
    # kb.type_index.get(label) is None — see their own docstrings) — dropping
    # it here changes no rule's firing outcome, only how many events the
    # per-tick engine wastes a rebuild on. See
    # CODE/evaluation_poc/data/check_label_coverage.py for the coverage
    # analysis behind this (which labels are absent and why) before assuming
    # a large drop here means a labelling bug rather than equipment this
    # dataset simply doesn't have.
    before = len(events)
    events = [e for e in events if e.label in kb.type_index]
    dropped = before - len(events)
    if dropped:
        print(f"[catalogue-filter] dropped {dropped}/{before} events "
              f"({dropped / before:.1%}) with no matching archetype")

    groups = K.group_by_patient(events)

    if n_patients is not None:
        # Deterministic (sorted patient id), not random, so the same
        # --patients N always selects the same patient(s) for a repeatable
        # feasibility timing run — independent of how many patients the
        # dataset file itself was already restricted to at export time.
        total = len(groups)
        selected = sorted(groups)[:n_patients]
        groups = {p: groups[p] for p in selected}
        print(f"[patients] restricted to {len(groups)}/{total}: {', '.join(selected)}")

    rules = R.load_rules()
    priority_rank = R.load_priority_rank()

    # Shared across EVERY patient in this run, not just within one — see
    # K.build_timeline's reasoning_cache docstring. A (label, device_id) pair
    # reasoned about for one patient is reused for the next patient that
    # references the same device_id, real whenever the device pool is
    # reused across patients over time (events_seed.py's own model).
    reasoning_cache = {}

    # events_graph collects minted mda:ClinicalEvent individuals (currently
    # cat3a_cardiorespiratory_arrest and cat3b_ventilation_failure).
    #
    # open_pairs[patient] tracks which (cardiacNode, respiratoryNode) pairs
    # are CURRENTLY coinciding, as of the last arrival checked for that
    # patient — not "ever seen this run". mda:impliesClinicalEvent is
    # mda:situational (zero persistence, verified empirically — see
    # clinicalEvents.ttl's header): it genuinely goes away the instant
    # either alarm ends and genuinely comes back if the same pair of
    # conditions later recoincides. Deduping on "ever seen" would
    # incorrectly treat two separate, non-overlapping episodes of the same
    # pair as one — clinically wrong (a patient who arrests, recovers, and
    # arrests again hours later had TWO events, not one already recorded).
    # So this is a rising-edge detector instead: at every arrival, compute
    # which pairs are true RIGHT NOW; mint only for pairs that are newly
    # true (not already open); pairs that drop out get removed from
    # open_pairs, so a later recurrence of the same pair mints again.
    #
    # open_vent_pairs[patient] is the same rising-edge tracker for cat3b,
    # keyed on (deviceNode, processNode) instead — the device fault side is
    # NOT mda:situational the same way (mda:hasDeviceOperationState is
    # mda:persistsPostAlarm, see ontology.ttl), but ReducedPulmonaryFunction
    # (the process side) is, so the coincidence as a whole still only holds
    # while both are true — a separate dict, not a shared one, since a
    # (cardiacNode, respiratoryNode) pair and a (deviceNode, processNode)
    # pair are never comparable keys.
    events_graph = Graph()
    open_pairs = {}
    open_vent_pairs = {}

    # Ascending by event count, not insertion order: for a multi-patient
    # feasibility/timing run this reports the cheap patients first, so
    # per-patient cost data arrives incrementally rather than only after the
    # single most expensive patient (which may dominate total wall time —
    # see EVALUATION/MDA-POC/cat_rules_report.md's superlinear-scaling
    # finding) finally finishes.
    outcomes = []
    for patient in sorted(groups, key=lambda p: len(groups[p])):
        evs = groups[patient]
        patient_start = time.perf_counter()
        ordered = sorted(evs, key=lambda ev: ev.start)

        # One Timeline per patient, extended incrementally — not rebuilt from
        # events_so_far at every arrival. See Timeline.empty()/.observe()
        # (op_knowledge.py) for why this produces the IDENTICAL result to the
        # previous per-tick full rebuild, verified by diff, not just a faster
        # approximation of it.
        tl = K.Timeline.empty(kb, patient, reasoning_cache=reasoning_cache)

        # Grouped by exact tick, not one event at a time: two alarms sharing
        # the same .start must each see the OTHER as already observed when
        # either is evaluated (matching the old events_so_far's `ev.start <=
        # e.start`, true for both ties on both sides) — observing one tie
        # member before evaluating the other would make evaluation order
        # within a tie (arbitrary — Python's stable sort on equal keys)
        # silently affect the result. Real ties occur in this data (e.g. two
        # alarms at the same device_id-distinct timestamp), not a
        # hypothetical.
        for _start, group in itertools.groupby(ordered, key=lambda ev: ev.start):
            group = list(group)
            for e in group:
                tl.observe(e)
            for e in group:
                alarm = K.alarm_iri(e.device_id, e.start)

                # tl.background_at(e.start), not the removed background_upto:
                # nothing in the MDA-POC's model persists unbounded. Structural
                # facts (device/sensor/functional-unit presence) and condition
                # facts tagged mda:persistsPostAlarm now follow the exact same
                # rule — visible while an alarm is active, or for
                # postAlarmValidityDuration (15 min, ontology.ttl) beyond it,
                # never longer. CAT1b's IBP-presence check is bound by this
                # too: it can only still find an ended IBP alarm's sensor
                # within that same 15-minute window, not indefinitely — see
                # DATA/CAT_evaluation's cat1b_pos/cat1b_neg gap, sized to this
                # window, not to an arbitrary "long enough to prove the point"
                # duration.
                query_graph = Graph()
                query_graph += tl.background_at(e.start)
                query_graph += tl.situation_at(e.start, mode="situational")
                query_graph += priority_rank
                R.add_triggered_by(query_graph, tl.revealing_at(e.start), kb, tl._identity)

                fired = R.assess(rules, query_graph, alarm)
                for rule_id, rows in fired.items():
                    for row in rows:
                        outcomes.append({
                            "patient": patient,
                            "alarm_label": e.label,
                            "device_id": e.device_id,
                            "start": e.start.isoformat(),
                            "rule": rule_id,
                            **row,
                        })

                # Rising-edge check, run every arrival regardless of whether
                # cat3a_cardiorespiratory_arrest fired this time — a rule
                # that DIDN'T fire still tells us something: the pair(s)
                # previously open for this patient are no longer true, and
                # should stop being tracked as open (see open_pairs' own
                # comment above for why "ever seen" is the wrong dedup key).
                currently_true = {(row["cardiacNode"], row["respiratoryNode"])
                                   for row in fired.get("cat3a_cardiorespiratory_arrest", [])}
                previously_open = open_pairs.get(patient, set())
                for pair in currently_true - previously_open:
                    mint_cardiorespiratory_arrest(patient, pair[0], pair[1], e.start, events_graph)
                open_pairs[patient] = currently_true

                # Same rising-edge check for cat3b_ventilation_failure, see
                # open_vent_pairs' own comment above.
                vent_currently_true = {(row["deviceNode"], row["processNode"])
                                        for row in fired.get("cat3b_ventilation_failure", [])}
                vent_previously_open = open_vent_pairs.get(patient, set())
                for pair in vent_currently_true - vent_previously_open:
                    mint_ventilation_failure(patient, pair[0], pair[1], e.start, events_graph)
                open_vent_pairs[patient] = vent_currently_true

                print(f"  {patient}  {e.start:%H:%M:%S}  {e.label:<45} "
                      f"{'-> ' + ', '.join(sorted(fired)) if fired else ''}")
        elapsed = time.perf_counter() - patient_start
        print(f"[patient done] {patient}  {len(ordered)} events  {elapsed:.1f}s  "
              f"({elapsed / len(ordered):.3f}s/event)")
    return outcomes, events_graph


def write_csv(outcomes: list, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in outcomes for k in row})
    fields = ["patient", "alarm_label", "device_id", "start", "rule"] + \
              [f for f in fields if f not in ("patient", "alarm_label", "device_id", "start", "rule")]
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in outcomes:
            writer.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--events-out", type=Path, default=EVENTS_OUT,
                     help="Where to write minted mda:ClinicalEvent individuals "
                          "(cat3a_cardiorespiratory_arrest and "
                          "cat3b_ventilation_failure produce these).")
    ap.add_argument("--patients", type=int, default=None,
                     help="Restrict to the first N patients (sorted patient id) "
                          "in the dataset — for feasibility/timing runs against "
                          "a large corpus without re-exporting a smaller CSV.")
    args = ap.parse_args()

    print(f"[dataset] {args.dataset}")
    outcomes, events_graph = run(args.dataset, n_patients=args.patients)
    write_csv(outcomes, args.out)
    if len(events_graph):
        args.events_out.parent.mkdir(parents=True, exist_ok=True)
        events_graph.serialize(destination=args.events_out, format="turtle")
        n_events = len(set(events_graph.subjects()))
        print(f"[output] {n_events} clinical event(s) -> {args.events_out.name}")

    cat1 = sum(1 for o in outcomes if o["rule"] in CAT1_RULES)
    cat2 = sum(1 for o in outcomes if o["rule"] in CAT2_RULES)
    cat3 = sum(1 for o in outcomes if o["rule"] in CAT3_RULES)
    print(f"\n[output] {len(outcomes)} outcome(s) -> {args.out.name}")
    print(f"         CAT1 (flagged likely false positive): {cat1}")
    print(f"         CAT2 (silenced): {cat2}")
    print(f"         CAT3 (triggered sequence): {cat3}")
    for rule_id in sorted(CAT1_RULES | CAT2_RULES | CAT3_RULES):
        n = sum(1 for o in outcomes if o["rule"] == rule_id)
        print(f"           {rule_id:<28} {n}")


if __name__ == "__main__":
    main()
