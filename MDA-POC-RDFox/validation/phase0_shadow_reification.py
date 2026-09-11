"""
phase0_shadow_reification.py — Phase 0 of the RDFox migration plan
(/Users/rzoodsm2/.claude/plans/we-re-going-for-the-magical-candy.md).

Builds the transient/persistent named-graph shape as an ADDITIONAL,
parallel representation alongside the existing CODE/evaluation_poc/core/
op_knowledge.py pipeline — touches nothing there — and checks, at every
instant in Timeline.ticks (op_knowledge.py's ticks_for), that filtering
the new representation by validFrom<=t<=validUntil reproduces EXACTLY
the triple set situation_at(t, mode="situational") already produces.

Per-event decomposition hypothesis being tested
-------------------------------------------------
situation_at(t) reasons over the MERGED conditions of every alarm active
at t in one batched call (background_at(t) + merged conditions), for
caching/performance reasons. This script instead computes each event's
own situational/persisting content INDEPENDENTLY (reasoning over just
that event's own background_for_key + condition_for_event), then unions
per-event results at query time by validity interval. These two are only
equivalent if no situational/persisting fact ever depends on merging two
DIFFERENT alarms' conditions into one reasoning pass — which matches this
project's own established design principle that cross-alarm reasoning is
always done via a separate downstream coincidence check (cat3a/cat3b),
never by merging two alarms' conditions before reasoning. This script
checks that hypothesis empirically rather than assuming it.

RESOLVED GAP (see the plan's "Findings from running Phase 0" section):
this script reasons over each alarm's own condition INDEPENDENTLY, then
unions per-alarm named graphs at query time. `op_knowledge.py`'s
merge_conditions (lines 803-817) instead gives `kb.last_wins`-tagged leaf
properties (e.g. hasRate) REPLACE semantics when two alarms are
simultaneously active on the same subject — "the latest alarm's value
replaces any earlier one... each describes a single current condition,
not an accumulating fact." Confirmed via `cat2a_pos`
(DATA/CAT_evaluation/events_data.csv): two overlapping alarms on the same
device/metric. Decision: retract-on-insert (the earlier alarm's `last_wins`
triple is suppressed while a later, conflicting one is valid; restored once
the later one ends and the earlier one is still active) — see the plan for
the full transactional design (an override stack per (subject, predicate),
generalizing beyond the pairwise case).

`_resolve_last_wins` below implements the QUERY-TIME-EQUIVALENT of that
transactional design (provably the same result: "value from whichever
currently-valid contributor has the latest start" IS the stack's top at
any instant, since push happens at start and pop at end) — simpler to
validate here than literally simulating a transaction sequence.

IMPORTANT LIMITATION this shortcut leaves for Phase 1's real rule design
(not fully closed by this script): `_resolve_last_wins` filters already-
reasoned triples, i.e. it patches the SYMPTOM (a stale raw hasRate value
plus whatever it entailed) rather than the CAUSE the real engine actually
avoids: op_knowledge.py resolves last_wins conflicts on the RAW condition
BEFORE reasoning, so a suppressed value never gets a chance to entail
anything downstream at all. This script's per-event reasoning has already
happened independently by the time conflicts are resolved, so it can only
suppress a losing event's raw triples after the fact — if a losing event's
hasRate value had entailed a DIFFERENT mda:impliesClinicalEvent tag on the
same reasoning pass (not exercised by today's fabricated cases — no
last_wins conflict here changes a clinical entailment), that entailed
triple would need explicit suppression too, which this script does not
attempt. Phase 1's actual RDFox rules must resolve last_wins conflicts on
the raw leaf value BEFORE the entailment rule fires, not patch the output
after — flagged here so this shortcut isn't silently assumed to be the
right shape for the real implementation.

Usage
-----
  cd MDA-POC-RDFox/validation
  python3 phase0_shadow_reification.py --dataset ../../DATA/CAT_evaluation/events_data.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "CODE" / "evaluation_poc" / "core"))
import op_knowledge as K


class ReifiedEvent:
    """One alarm's shadow representation: its two named-graph validity
    windows, and the triples that belong in each — computed once, reused
    at every tick's membership query, mirroring how Timeline._conditions/
    _background_by_key are already memoised per (label, device_id) key."""

    def __init__(self, kb: K.KB, event, tl: K.Timeline):
        key = (event.label, event.device_id)
        cond = tl._conditions[key]
        bg = tl._background_by_key[key]

        # Independent, single-event reasoning pass — NOT the merged
        # multi-alarm pass situation_at uses internally.
        #
        # TRIED AND REVERTED: splitting clinical_context into a
        # condition-dependent half (impliesClinicalEvent) and a
        # structural-only half (administers/targetsProcess/presentIn/
        # organPartOfSystem/approximates), on the theory that the
        # structural half should get the same 15-min persistence window
        # hasOperationState/hasFunctionalUnit/hasSensor already get. That
        # theory was plausible but WRONG, caught immediately by re-running
        # Phase 0: it introduced 14 EXTRA-side mismatches on the fabricated
        # suite that hadn't existed before (e.g. cat2a_neg still showing
        # VentilationTherapy/administers content 1.5 minutes after its own
        # alarm ended). Root cause: clinical_context has NO standalone
        # persistence mechanism at all in the real system — situation_at
        # only computes it when SOME alarm is active (`if active:`,
        # entailed_situation(kb, background_at(t)+merged)), and unlike
        # hasOperationState there is no separate persistence loop that
        # re-adds it later. It doesn't "persist for 15 minutes tied to its
        # own alarm" — it simply isn't recomputed at all once nothing is
        # active, UNLESS a different alarm happens to be active at the
        # same tick and its background_at(t) still includes this alarm's
        # revealing structure (which IS the leak this whole finding is
        # about). So the correct treatment is the ORIGINAL one below:
        # zero-persistence, tied to this alarm's own active window only —
        # not a bug that needed "fixing" with its own grace period.
        situational_content = K.clinical_context(kb, bg + cond)
        persisting_content = K.inferred_states(kb, bg + cond)

        # situation_at unconditionally folds in this event's own message
        # graph (hasStart/hasEnd/hasCategory/isOfType/hasMessage/
        # concernsPatient/triggeredByStructure/rdf:type) for every ACTIVE
        # alarm (op_knowledge.py:1139-1140, `g += self._messages[id(e)]`,
        # added before the situational/all split, so it's in BOTH modes) —
        # this belongs in the transient bucket, same boundary as "active".
        message_graph = tl._messages[id(event)]

        # isMonitoredBy is asserted for every REVEALING alarm, not just
        # active ones (op_knowledge.py:1141-1150) — same half-open window
        # as the persisting content below, so it belongs in the persistent
        # bucket, not transient.
        is_monitored_by = (K.patient_iri(tl.patient), K.MDA.isMonitoredBy, K.device_iri(event.device_id))

        self.event = event
        self.transient_from = event.start
        self.transient_until = event.end
        self.transient_triples = frozenset(cond) | frozenset(situational_content) | frozenset(message_graph)

        # revealing_at only extends a structural/persisting view past
        # `end` if there is something to persist at all (op_knowledge.py's
        # Timeline.revealing_at: the in-window clause is gated on
        # `len(self._persisted_for(key)) > 0`) — replicate that gate here,
        # not just the +window arithmetic. The window itself is HALF-OPEN
        # on the right (revealing_at's extension clause is strictly
        # `e.end < t < e.end + window`, never `<=` at either end) — the
        # active clause (`e.start <= t <= e.end`) is what makes the LEFT
        # side of the combined interval closed.
        has_persisting = len(persisting_content) > 0
        self.persistent_from = event.start
        self.persistent_until = event.end + tl.window if has_persisting else event.end
        self.persistent_upper_closed = not has_persisting  # end-only case: closed, like active_at
        self.persistent_triples = frozenset(bg) | frozenset(persisting_content) | {is_monitored_by}

    def transient_at(self, t) -> frozenset:
        return self.transient_triples if self.transient_from <= t <= self.transient_until else frozenset()

    def _persistent_valid(self, t) -> bool:
        if self.persistent_upper_closed:
            return self.persistent_from <= t <= self.persistent_until
        return self.persistent_from <= t < self.persistent_until

    def persistent_at(self, t) -> frozenset:
        return self.persistent_triples if self._persistent_valid(t) else frozenset()


def _resolve_last_wins(contributions: list, last_wins: set) -> set:
    """
    contributions: list of (event, triple_set) pairs, one per event
    currently valid (transient or persistent) at the tick being checked.
    For each (subject, predicate) pair where predicate is in `last_wins`,
    keep only the triple contributed by the event with the LATEST
    `event.start` among those currently contributing it — every other
    contributor's triple for that (subject, predicate) is dropped, even
    if their own value differed. Ties (identical start) resolved by
    device_id as a deterministic tiebreak; op_knowledge.py's own
    merge_conditions has no tie-break rule since arrival order is assumed
    unique in practice, so this is a shadow-script convenience with no
    real-engine analog to match.
    """
    winner_start = {}   # (s, p) -> latest start seen so far
    winner_key = {}      # (s, p) -> tiebreak key of the current winner
    kept = {}            # (s, p) -> winning triple

    passthrough = set()
    for event, triples in contributions:
        for s, p, o in triples:
            if p not in last_wins:
                passthrough.add((s, p, o))
                continue
            key = (s, p)
            tiebreak = (event.start, event.device_id)
            if key not in winner_start or tiebreak > winner_key[key]:
                winner_start[key] = event.start
                winner_key[key] = tiebreak
                kept[key] = (s, p, o)

    return passthrough | set(kept.values())


# Predicates whose type-derived, concept-to-concept facts (e.g.
# device:MechanicalVentilator administers therapeuticModality:
# VentilationTherapy) situation_at leaks across alarms via
# background_at(t)'s SHARED reasoning input: whenever ANY alarm is active,
# every currently-revealing device's type chain gets walked, not just the
# active alarm's own — so a device's chain can reappear on an unrelated
# alarm's tick for up to 15 minutes after its OWN alarm ended, contradicting
# clinicalEvents.ttl's own "gone within 1 second" claim for situational
# content generally (true for impliesClinicalEvent specifically, not for
# this chain). PROVISIONAL DECISION (see the plan's "Second finding"
# section — flagged as overridable, not a closed call): don't replicate
# this leak. Scope each alarm's own type-derived chain to its own
# active/revealing window only, exactly as this script's independent
# per-event model already does by construction. Confirmed via the real
# 27-alarm corpus: every one of 161 mismatches is on the MISSING side only
# (never extra) — one clean, isolated category, not several different bugs
# (subjects are a mix of bare vocabulary concepts, e.g. device:
# MechanicalVentilator, and grounded Metric/PhysiologicalProperty
# instances one layer deeper in the same chain — checked both). Classified
# separately below so the report distinguishes this deliberate difference
# from a genuine equivalence failure.
KNOWN_DEVIATION_PREDICATES = {
    K.MDA.administers, K.MDA.targetsProcess, K.MDA.presentIn, K.MDA.organPartOfSystem,
    K.MDA.approximates,  # the SAME leak one layer deeper: a revealing device's
    # own Metric->approximates->Property entailment rides on the same shared
    # background_at(t) reasoning input, not just the bare concept-to-concept
    # facts above — confirmed via a grounded Metric instance IRI in the real
    # corpus (sarahChen_06's ventilator), not only vocabulary concepts.
}


def run(dataset: Path, n_patients: int | None) -> None:
    kb = K.load_kb()
    events = K.load_events(dataset)
    events = [e for e in events if e.label in kb.type_index]
    groups = K.group_by_patient(events)
    if n_patients is not None:
        selected = sorted(groups)[:n_patients]
        groups = {p: groups[p] for p in selected}

    total_ticks = 0
    total_mismatches = 0
    total_deviations = 0
    reasoning_cache: dict = {}

    for patient in sorted(groups):
        evs = groups[patient]
        tl = K.build_timeline(kb, patient, evs, reasoning_cache=reasoning_cache)

        reified = [ReifiedEvent(kb, e, tl) for e in evs]
        # Pre-split persistent contributions into (structural, persisting)
        # so situational_view_at doesn't need the background_for_key call
        # done above as a placeholder.
        for re_, e in zip(reified, evs):
            key = (e.label, e.device_id)
            bg = tl._background_by_key[key]
            re_.persistent_structural = frozenset(bg)
            re_.persistent_persisting_only = re_.persistent_triples - frozenset(bg)

        for t in tl.ticks:
            expected = set(tl.situation_at(t, mode="situational"))

            contributions = []
            for re_ in reified:
                triples = set(re_.transient_at(t))
                if re_._persistent_valid(t):
                    triples |= re_.persistent_persisting_only
                if triples:
                    contributions.append((re_.event, triples))
            actual = _resolve_last_wins(contributions, kb.last_wins)

            total_ticks += 1
            if actual != expected:
                missing = expected - actual
                extra = actual - expected
                # A tick counts as a KNOWN DEVIATION only if every difference
                # is on the missing side and every missing triple's predicate
                # is in the accepted-leak set — any "extra" triple, or any
                # missing triple on a different predicate, is a genuine
                # mismatch regardless of what else is on the tick.
                is_known_deviation = (
                    not extra
                    and missing
                    and all(p in KNOWN_DEVIATION_PREDICATES for _, p, _ in missing)
                )
                if is_known_deviation:
                    total_deviations += 1
                else:
                    total_mismatches += 1
                    print(f"[MISMATCH] patient={patient} t={t}")
                    if missing:
                        print(f"  missing ({len(missing)}): " + "; ".join(str(x) for x in list(missing)[:5]))
                    if extra:
                        print(f"  extra   ({len(extra)}): " + "; ".join(str(x) for x in list(extra)[:5]))

    if total_ticks:
        print(f"\n[phase0] {total_ticks} ticks checked")
        print(f"         {total_mismatches} genuine mismatches ({total_mismatches / total_ticks:.1%})")
        print(f"         {total_deviations} known deviations ({total_deviations / total_ticks:.1%}) — "
              f"the accepted administers/targetsProcess/presentIn/organPartOfSystem/approximates "
              f"leak-fix, see KNOWN_DEVIATION_PREDICATES above")
    else:
        print("[phase0] no ticks")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=K.ROOT / "DATA" / "CAT_evaluation" / "events_data.csv")
    ap.add_argument("--patients", type=int, default=None)
    args = ap.parse_args()
    run(args.dataset, args.patients)


if __name__ == "__main__":
    main()
