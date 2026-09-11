"""
replay_driver.py — Phase 2 window operator, replay/validation mode.

Self-contained: imports only engine/mint.py (this folder's own forked copy
of the real MDA-framework grounding pipeline — see mint.py's own docstring
for what it is and why it exists instead of hand-authored data) and the
Python standard library — except scan_patient_ids/load_events_for_patients,
which lazily import pandas (only paid for callers that actually use the
large-corpus loading path; load_events/group_by_patient below stay
stdlib-only for the regression harness).

Reads DATA/CAT_evaluation/events_data.csv (a DATA file — referenced by
path, not duplicated, since it's the input corpus, not framework
knowledge) in arrival order per patient, and replays each alarm's
arrive/end/end+window lifecycle as real RDFox transactions:

  - Alarm arrives: mint its knowledge via mint.py (the real
    extraction+minting pipeline, not reinvented), split into:
      * transient graph: the message (alarm_message + add_triggered_by)
        and condition (condition_for_event) content — valid [start, end].
      * persistent graph: the structural/background content
        (background_for_key) plus mda:isMonitoredBy — valid
        [start, end + PT15M]. Per the plan's own §1 design, EVERY alarm's
        structural content gets the full post-alarm window unconditionally
        (not gated on whether it happens to carry persisting operation
        state — that gate belongs to the legacy rdflib engine's
        revealing_at, which this redesign does not carry over; see the
        plan's Phase 0 "second finding" and correction #1: no tier of
        knowledge here is ever kept around indefinitely, so simply always
        scheduling the drop is what keeps this compliant, not a
        conditional skip).
  - Alarm ends: DROP the transient graph.
  - Alarm end + PT15M: DROP the persistent graph (always scheduled, never
    conditionally skipped — see above).
  - kb.last_wins conflict resolution (every leaf/condition property the
    ontology tags as such, read generically off mint.py's KB, not a
    hand-picked predicate list): retract-on-insert, restore-on-end, via a
    small per-(subject,predicate) stack — see the plan's §2 for the full
    design.

Identity resolution (mint.py's resolve_identity) is re-run per arriving
alarm over that patient's events STRICTLY so far (start <= this alarm's
own start), never the full future history — the same discipline
assess.py's own resolve_identity docstring documents and requires.

Time is LOGICAL, not wall-clock — this replays a fixed historical corpus,
not a live stream. A real production scheduler is out of scope for this
phase — see the plan's Phase 2 entry.

RDFox mechanics used, all confirmed by direct testing:
  - Named-graph insert: TriG `GRAPH <iri> { ... }` via `import <file>`.
  - Named-graph drop: `DELETE WHERE { GRAPH <iri> { ?s ?p ?o } }`.
  - `import`/`DELETE`/`INSERT DATA` all run as plain shell-script lines;
    a script needs a trailing `quit` and must be run with stdin closed.

Usage
-----
  cd MDA-POC-RDFox/engine
  python3 replay_driver.py
"""

from __future__ import annotations

import csv
import itertools
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import mint as M

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET = ROOT / "DATA" / "CAT_evaluation" / "events_data.csv"
LICENSE = ROOT / "RDFox.lic"
RDFOX_BIN = Path.home() / "Downloads" / "RDFox-macOS-arm64-7.6b" / "RDFox"
REPRESENTATION_DIR = Path(__file__).resolve().parent.parent / "representation"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

WINDOW = timedelta(minutes=15)  # ontology.ttl's postAlarmValidityDuration

# CAT1/CAT2 fabricated patients: expected flaggedLikelyFalsePositive/
# silencedBy outcome. The CAT3 patients below are included here too
# (all False) as a sanity check that neither CAT3 fixture accidentally
# also trips a CAT1/CAT2 rule — see EXPECTED_CAT3A/EXPECTED_CAT3B for
# their own, actual expected outcome.
EXPECTED = {
    "cat1a_pos": True, "cat1a_neg": False,
    "cat1b_pos": True, "cat1b_neg": False,
    "cat2a_pos": True, "cat2a_neg": False,
    "cat2b_pos": True, "cat2b_neg": False,
    "cat3a_pos": False, "cat3a_neg": False, "cat3c": False,
    "cat3b_pos": False, "cat3b_neg": False, "cat3d": False, "cat3e": False,
}

# CAT3a (cardiorespiratory arrest): Asystolie + Apneu.
#   cat3a_pos: 08:00:00-08:01:00 / 08:00:30-08:01:30 — genuine 30s overlap.
#   cat3a_neg: 08:00:00-08:00:20 / 08:00:40-08:01:00 — 20s gap, no overlap.
#   cat3c: same overlap as cat3a_pos, arrival order swapped (Apneu first)
#     — confirms order doesn't matter, no ?alarm anchor in the check.
EXPECTED_CAT3A = {"cat3a_pos": True, "cat3a_neg": False, "cat3c": True}

# CAT3b (ventilation failure): MEDIBUS Ventilator storing (device fault) +
# MEDIBUS MV ondergrens (reduced pulmonary function).
#   cat3b_pos: 08:00:00-08:01:00 / 08:00:30-08:01:30 — direct overlap.
#   cat3b_neg: fault ends 08:00:20, second alarm arrives 08:20:00 — past
#     the 15-minute post-alarm window, does not fire.
#   cat3d: same overlap as cat3b_pos, arrival order swapped.
#   cat3e: fault ends 08:00:20, second alarm arrives 08:10:00 — inside the
#     15-minute window, fires. Specifically exercises mint.py's
#     background_for_key routing hasDeviceOperationState into the
#     persistent graph (see that function's own comment) — this is the
#     exact case that was silently broken before that fix.
EXPECTED_CAT3B = {"cat3b_pos": True, "cat3b_neg": False, "cat3d": True, "cat3e": True}


@dataclass
class Event:
    patient: str
    label: str
    device_id: str
    start: datetime
    end: datetime


def load_events(path: Path) -> list:
    events = []
    with path.open(encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader)  # header
        for row in reader:
            patient, label, device_id, start, end = row
            events.append(Event(patient, label, device_id,
                                 datetime.fromisoformat(start), datetime.fromisoformat(end)))
    return events


def group_by_patient(events: list) -> dict:
    groups: dict = {}
    for e in events:
        groups.setdefault(e.patient, []).append(e)
    return groups


def scan_patient_ids(path: Path) -> list:
    """Every distinct patientID in a large events CSV, without building a
    single Event. Uses pandas (a lazy import — the only place in this
    module that needs a dependency beyond the standard library) reading
    just the patientID column: pandas' C parser only tokenizes the one
    column asked for, whereas a plain csv.reader loop still pays full
    per-row tokenization cost for every column even when only row[0] is
    read — confirmed directly the naive version of this function (a
    csv.reader loop reading only row[0]) still took ~34s against pandas'
    ~7s on the real 13.8M-row corpus, because tokenizing all 5 columns
    per row, not date-parsing, is what actually dominates at that scale."""
    import pandas as pd
    ids = pd.read_csv(path, sep=";", usecols=["patientID"], dtype=str)
    return sorted(ids["patientID"].unique())


def load_events_for_patients(path: Path, patient_ids: set) -> list:
    """Build Event objects ONLY for rows whose patientID is in
    `patient_ids`. Also pandas-based, for the same reason as
    scan_patient_ids: reads the whole file (pandas has no way to skip
    rows before parsing them, so this cost doesn't shrink with a smaller
    `patient_ids`), but its C parser does that full read far faster than
    a Python-level csv.reader loop does even when the loop itself skips
    most rows — confirmed directly (~26s pandas vs ~28s csv.reader
    despite the csv.reader version constructing far fewer Event objects)
    on the real 13.8M-row corpus. Pair with scan_patient_ids to choose
    `patient_ids` first."""
    import pandas as pd
    df = pd.read_csv(path, sep=";", dtype=str)
    df = df[df["patientID"].isin(patient_ids)]
    return [
        Event(row.patientID, row.label, row.device_id,
              datetime.fromisoformat(row.start), datetime.fromisoformat(row.end))
        for row in df.itertuples(index=False)
    ]


def graph_iri(alarm: str, suffix: str) -> str:
    return f"<{alarm}#{suffix}>"


VALID_FROM = f"<{M.MDA}validFrom>"
VALID_UNTIL = f"<{M.MDA}validUntil>"
XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"


def validity_triples(graph: str, valid_from: datetime, valid_until: datetime) -> list:
    """The plan's §2 metadata triples for one graph — deliberately BARE
    (no GRAPH{} wrapper): these land in the default graph so the graph's
    own per-alarm DELETE WHERE (scoped to `graph` specifically) never
    touches them. Kept alive only by the batched GC pass (plan's §4)."""
    return [
        f'{graph} {VALID_FROM} "{valid_from.isoformat()}"^^{XSD_DATETIME} .',
        f'{graph} {VALID_UNTIL} "{valid_until.isoformat()}"^^{XSD_DATETIME} .',
    ]


def graph_triples(g) -> list:
    """rdflib Graph -> list of 'subject predicate object .' N-Triples lines
    — reuses rdflib's own serializer instead of hand-formatting triples
    (as the now-deleted data/archetypes.py did), so literal
    escaping/datatypes are never re-implemented by hand."""
    text = g.serialize(format="nt")
    return [line.strip() for line in text.splitlines() if line.strip()]


def subject_predicate(triple: str) -> tuple:
    parts = triple.rstrip(" .").split(" ", 2)
    return parts[0], parts[1]


class Driver:
    """Builds the RDFox shell script for one patient's full event stream.

    `file_counter` MUST be shared across every patient processed in the
    same run (build_script creates one itertools.count() and passes it to
    each patient's Driver) — one Driver instance per patient restarting
    its own counter at 1 caused every patient after the first to silently
    overwrite an earlier patient's tx_0001.trig/tx_0002.trig in the shared
    scratch directory, since all patients' scripts are generated before
    RDFox ever reads any of them back."""

    def __init__(self, kb, scratch_dir: Path, file_counter):
        self.kb = kb
        self.scratch = scratch_dir
        self.commands: list[str] = []
        self.pending: list[tuple] = []  # (when, command_text)
        self.last_wins_stack: dict = {}  # (subj,pred) -> [(start, graph, triple), ...]
        self._file_counter = file_counter
        self.identity_tracker = M.new_identity_tracker(kb)

    def _write_trig(self, blocks: dict, bare: list | None = None) -> str:
        path = self.scratch / f"tx_{next(self._file_counter):04d}.trig"
        with path.open("w") as f:
            for graph, triples in blocks.items():
                if not triples:
                    continue
                f.write(f"GRAPH {graph} {{\n")
                for t in triples:
                    f.write(f"  {t}\n")
                f.write("}\n")
            # Bare (no GRAPH{} wrapper) triples land in the default graph —
            # see validity_triples's own docstring for why (plan's §2).
            for t in bare or []:
                f.write(f"{t}\n")
        return str(path)

    def schedule(self, when: datetime, command: str):
        self.pending.append((when, command))

    def flush_due(self, up_to: datetime):
        due = sorted((p for p in self.pending if p[0] <= up_to), key=lambda p: p[0])
        for _, cmd in due:
            self.commands.append(cmd)
        self.pending = [p for p in self.pending if p[0] > up_to]

    def flush_all(self):
        for _, cmd in sorted(self.pending, key=lambda p: p[0]):
            self.commands.append(cmd)
        self.pending = []

    def insert_alarm(self, event: Event, identity: dict) -> dict:
        """Phase 1 of a two-phase insert: everything about this alarm
        EXCEPT its own `hasPriority` triple, which is held back and must
        be completed via complete_alarm() after any per-alarm checks have
        run. This is what lets cat2a's on-demand check (see
        representation/rules/shadow/cat2a_priority_aggregate.dlog and
        this module's CAT2A_AGGREGATE_RULE) query the standing
        `shadowMaxActiveRank` aggregate BEFORE this alarm's own rank
        could pollute it — a MAX aggregate can't be decomposed after the
        fact to "exclude me" the way a COUNT can, so the check has to run
        against the pre-arrival state, not the post-insert one. Every
        other check (cat1a/cat1b/cat2b/impliesClinicalEvent) is
        unaffected by hasPriority's absence and runs fine against
        phase-1-only data.

        Returns the pending state complete_alarm() needs; always
        two-phase now regardless of which rules are enabled — holding
        back one triple and inserting it separately is cheap enough not
        to be worth conditioning on enabled_rules.
        """
        kb = self.kb
        alarm_uri = M.alarm_iri(event.patient, event.device_id, event.start)
        alarm = str(alarm_uri)
        tgraph = graph_iri(alarm, "transient")
        pgraph = graph_iri(alarm, "persistent")

        msg_graph = M.alarm_message(kb, event, identity)
        M.add_triggered_by(msg_graph, [event], kb, identity)
        cond_graph = M.condition_for_event(kb, event.patient, event.label, event.device_id, identity)
        bg_graph = M.background_for_key(kb, event.patient, event.label, event.device_id, identity)
        # mda:approximates used to be derived here via a per-alarm OWL-RL
        # pass (mint.clinical_context, now deleted — see mint.py's module
        # docstring) — replaced by representation/rules/
        # approximates_bridge.dlog, loaded unconditionally alongside
        # FRAMEWORK_FILES, which derives it natively in RDFox instead.

        all_msg_triples = graph_triples(msg_graph)
        incoming_prio = msg_graph.value(alarm_uri, M.MDA.hasPriority)
        priority_pred = f"<{M.MDA}hasPriority>"
        priority_triple = None
        phase1_msg_triples = []
        for t in all_msg_triples:
            _, pred = subject_predicate(t)
            if pred == priority_pred and priority_triple is None:
                priority_triple = t
            else:
                phase1_msg_triples.append(t)

        transient_triples_phase1 = phase1_msg_triples + graph_triples(cond_graph)
        full_transient_triples = all_msg_triples + graph_triples(cond_graph)
        patient = M.patient_iri(event.patient)
        dev = M.device_iri(event.patient, event.device_id)
        persistent_triples = graph_triples(bg_graph) + [f"<{patient}> <{M.MDA}isMonitoredBy> <{dev}> ."]

        # last_wins conflict handling uses the FULL triple set (hasPriority
        # is never last_wins-tagged, so holding it back doesn't affect this).
        for triple in list(full_transient_triples):
            subj, pred = subject_predicate(triple)
            if pred not in self.kb.last_wins_str:
                continue
            key = (subj, pred)
            stack = self.last_wins_stack.setdefault(key, [])
            if stack:
                _, old_graph, old_triple = stack[-1]
                self.commands.append(f"DELETE WHERE {{ GRAPH {old_graph} {{ {old_triple} }} }}")
            stack.append((event.start, tgraph, triple))

        metadata = (
            validity_triples(tgraph, event.start, event.end)
            + validity_triples(pgraph, event.start, event.end + WINDOW)
        )
        path = self._write_trig({tgraph: transient_triples_phase1, pgraph: persistent_triples}, bare=metadata)
        self.commands.append(f"# arrive (phase1): {event.label} @ {event.device_id} {event.start.isoformat()}")
        self.commands.append(f"import {path}")

        self.schedule(event.end, self._drop_transient_cmd(event, tgraph, full_transient_triples))
        self.schedule(event.end + WINDOW, self._drop_persistent_cmd(event, pgraph))

        return {
            "alarm": alarm, "tgraph": tgraph, "pgraph": pgraph, "patient": str(patient),
            "priority_triple": priority_triple, "incoming_prio": incoming_prio,
        }

    def complete_alarm(self, pending: dict) -> None:
        """Phase 2: insert the hasPriority triple held back by
        insert_alarm(), completing this alarm's membership in the
        cat2a shadowMaxActiveRank aggregate for FUTURE checks. No-op if
        this archetype never minted a hasPriority triple at all."""
        if pending["priority_triple"] is None:
            return
        self.commands.append(f"# arrive (phase2): complete {pending['alarm']}")
        self.commands.append(
            f"INSERT DATA {{ GRAPH {pending['tgraph']} {{ {pending['priority_triple']} }} }}"
        )

    def _drop_transient_cmd(self, event: Event, graph: str, transient_triples: list) -> str:
        restores = []
        for triple in transient_triples:
            subj, pred = subject_predicate(triple)
            if pred not in self.kb.last_wins_str:
                continue
            key = (subj, pred)
            stack = self.last_wins_stack.get(key, [])
            if not stack or stack[-1][2] != triple:
                continue
            stack.pop()
            if stack:
                _, restore_graph, restore_triple = stack[-1]
                restores.append(f"INSERT DATA {{ GRAPH {restore_graph} {{ {restore_triple} }} }}")
        cmd = f"# end: {event.label} @ {event.device_id} {event.end.isoformat()}\n"
        cmd += f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}"
        for r in restores:
            cmd += "\n" + r
        return cmd

    def _drop_persistent_cmd(self, event: Event, graph: str) -> str:
        return (f"# window-expiry: {event.label} @ {event.device_id}\n"
                f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}")


RULES_DIR = REPRESENTATION_DIR / "rules"

FRAMEWORK_FILES = [
    DATA_DIR / "ontology.ttl",
    DATA_DIR / "vocab_generated.ttl",
    DATA_DIR / "clinicalEvent_vocab.ttl",
    DATA_DIR / "inference.ttl",
    DATA_DIR / "priority_rank.ttl",
    # mda:validFrom/validUntil — the reified-time redesign's own vocabulary
    # (see the plan's §1). Additive only; nothing consumes these terms
    # until the rollout's later steps land.
    DATA_DIR / "graph_metadata.ttl",
    # A .dlog rules file, not framework .ttl data — deliberately mixed in
    # here rather than RULE_FILES below: it's prerequisite infrastructure
    # (mda:approximates, consumed by 4 of the domain rules), not an
    # optional domain rule a caller would ever want to disable. Replaces
    # the per-alarm owlrl.DeductiveClosure call mint.py used to make (see
    # that file's module docstring) — RDFox derives these natively now.
    RULES_DIR / "approximates_bridge.dlog",
]

# One file per rule (representation/rules/) so a caller (poc_entry.py) can
# enable/disable each independently — split out of the original combined
# clinical_rules.dlog/cat_rules.dlog for exactly that reason.
RULE_FILES = {
    "cardiac_arrest": RULES_DIR / "cardiac_arrest.dlog",
    "respiratory_arrest": RULES_DIR / "respiratory_arrest.dlog",
    "reduced_pulmonary_function": RULES_DIR / "reduced_pulmonary_function.dlog",
    "cat1a": RULES_DIR / "cat1a_signal_quality.dlog",
    "cat1b": RULES_DIR / "cat1b_asystole_ibp.dlog",
    "cat2a": RULES_DIR / "cat2a_process_priority.dlog",
    "cat2b": RULES_DIR / "cat2b_metric_sensor.dlog",
    # EXPERIMENTAL, not a real file (never imported — see
    # ONDEMAND_RULE_NAMES/_cat2a_ondemand_aggregate_branch): a THIRD cat2a
    # implementation, alongside the standing shadowMaxActiveRank aggregate
    # rule ("cat2a") and the already-tried-and-worse on-demand self-join,
    # for isolated A/B timing against real patient 2826. Points at the
    # same file as "cat2a" purely for documentation/traceability — this
    # variant checks the identical domain condition, just evaluated as a
    # fresh MAX aggregate per alarm instead of a continuously-maintained
    # one. Enable EITHER "cat2a" OR "cat2a_ondemand_agg" at a time to
    # compare, not both (both together just runs and times the same check
    # twice under two different implementations).
    "cat2a_ondemand_agg": RULES_DIR / "cat2a_process_priority.dlog",
    # CAT3a/CAT3b: coincidence checks over the CLINICAL_RULE_NAMES tags
    # below, ported from the older rdflib POC's CODE/evaluation_poc/
    # reasoner/rules/cat3a_cardiorespiratory_arrest.rq and
    # cat3b_ventilation_failure.rq. Not real files (never imported — same
    # documentation-only pattern as cat2a_ondemand_agg above): there is no
    # standing Datalog rule for either, because minting a NEW ClinicalEvent
    # individual on a rising-edge coincidence isn't something Datalog
    # materialization can do (see clinicalEvents.ttl's own header) — the
    # per-alarm on-demand query + Python-side episode dedup at the
    # check-emission site (build_script) and count_cat3_episodes() IS the
    # implementation. Point at their building-block dependency's own file
    # purely for traceability.
    "cat3a": RULES_DIR / "cardiac_arrest.dlog",
    "cat3b": RULES_DIR / "reduced_pulmonary_function.dlog",
}

CLINICAL_RULE_NAMES = {"cardiac_arrest", "respiratory_arrest", "reduced_pulmonary_function"}

# CAT3a/CAT3b's own dependency on specific CLINICAL_RULE_NAMES members
# (not all three — cat3a needs the cardiac+respiratory pair, cat3b needs
# only reduced_pulmonary_function, whose OTHER half is a raw device fact,
# not a clinical-rule tag at all — see cat3b's own query comment below).
# Checked in build_script, once, at rule_names-validation time: enabling
# cat3a/cat3b without their inputs would silently just never fire, which
# is a worse failure mode than a clear error.
CAT3_DEPENDENCIES = {
    "cat3a": {"cardiac_arrest", "respiratory_arrest"},
    "cat3b": {"reduced_pulmonary_function"},
}

# cat2a/cat2b are NOT imported as standing Datalog rules (unlike every other
# name in RULE_FILES) — build_script instead runs ONDEMAND_QUERY_BODIES below
# as a one-shot `select ... limit 1` at each alarm's own check point. Why:
# both rules' only new predicate (mda:silencedBy) has exactly one consumer —
# that same per-alarm check — so there's no reason to pay for RDFox
# continuously, incrementally re-maintaining their 14-atom self-join against
# every relevant transaction in the WHOLE store for the rule's entire
# lifetime, when the answer is only ever read once, at one specific instant.
# Root cause confirmed by direct RDFox instrumentation (not inferred): the
# expense is RDFox's incremental "delete, then re-derive" maintenance for a
# STANDING rule, triggered by every relevant import/DELETE WHERE anywhere in
# the store — proportional to how many standing rules could be affected by
# what changed, NOT to how much actually matches right now (a fresh,
# stateless query shaped identically to a standing rule's own join,
# re-issued over the exact same accumulated state, answered in 0.000s; the
# structural fan-out at that exact point was trivial — every hop count 1).
# cat1a/cat1b/cardiac_arrest/respiratory_arrest/reduced_pulmonary_function
# join within a SINGLE alarm's own chain (lower combinatorial risk than
# cat2a/cat2b's cross-alarm self-join), but all reference at least one
# `kb.last_wins_str`-tagged predicate (hasQualityState, hasRate) — the same
# argument applies: each predicate they read has exactly one consumer (this
# same per-alarm/per-check point), so there's no reason to pay standing
# incremental maintenance for a value nothing else ever reads back.
#
# IMPORTANT: this does NOT touch Driver's physical graph-drop mechanism
# (_drop_transient_cmd/_drop_persistent_cmd) or its last_wins retract/
# restore stack — those stay exactly as the project's own design doc
# (~/.claude/plans/we-re-going-for-the-magical-candy.md) specifies: a
# landmark/sliding-window RDF-stream-processing model where physically
# dropping a graph the instant it's invalid IS the discard mechanism, and
# "currently in the store" and "currently valid" are the same condition by
# construction — the plan's own Phase 1 finding is exactly why NONE of
# these on-demand query bodies below need any validFrom/validUntil interval
# filtering: physical dropping already guarantees it. An earlier version of
# this fix considered replacing physical dropping with an append-only store
# filtered by explicit validity intervals at query time — reconsidered
# because that would make the store grow unboundedly for the life of a
# batch, directly working against the landmark-window discard model that's
# this project's actual RDF-stream-processing design, and turned out to be
# unnecessary once the real root cause (above) was understood: it's
# standing-rule maintenance cost, not physical deletion itself, that's
# expensive.
# cardiac_arrest/respiratory_arrest/reduced_pulmonary_function were tried
# on-demand too and REVERTED — real-corpus timing (patient 2826) got WORSE,
# not better: multiple new stalls (several seconds to 20s each) plus a
# fresh dead stop near the end, timing out again. Root cause understood,
# not just observed: unlike cat1a/cat1b/cat2a/cat2b, these three checks are
# NOT alarm-scoped (impliesClinicalEvent's subject is a
# PhysiologicalProcess, not an alarm — see the check-emission site's own
# comment) and run UNCONDITIONALLY on every single alarm regardless of
# relevance, with no VALUES-bound candidate set to narrow the search. Under
# a standing rule, that same check is a cheap read against an answer
# RDFox maintains incrementally; on demand, it's a full, unscoped query
# recomputed from scratch on every alarm — the wrong trade for a check
# that's both unconditional and unbounded, even though it was the right
# trade for cat2a/cat2b (checked once each, alarm-scoped, but with a
# 14-atom cross-alarm self-join RDFox had to re-justify on every unrelated
# mutation in the store). On-demand conversion isn't a universal win — it
# only pays off when what's being converted was itself the standing-rule
# maintenance cost, not merely "any rule with a last-wins predicate."
ONDEMAND_RULE_NAMES = {"cat1a", "cat1b", "cat2a", "cat2b", "cat2a_ondemand_agg", "cat3a", "cat3b"}

# Hand-translated equivalents of cat2a_process_priority.dlog's/
# cat2b_metric_sensor.dlog's rule BODIES (not the head — only the join
# itself is needed here). NOT auto-generated from those files: confirmed
# directly against the real RDFox 7.6b binary that its bracket quad syntax
# (`[?s,?p,?o] ?g`), used throughout every .dlog file in this project, is
# RULE-file-only — it errors ("Line 1, column 24: Resource expected.")
# inside a plain `select` command, which only accepts standard SPARQL
# (`GRAPH ?g { ?s ?p ?o }`). Translating one syntax into the other is a real
# structural rewrite (bracket atoms -> GRAPH blocks, Datalog's comma
# conjunction -> SPARQL's `.`), not a text substitution, so it's done here
# by hand instead of by a fragile auto-translator.
#
# MUST BE KEPT IN SYNC BY HAND with the corresponding .dlog file if its join
# logic ever changes — there is no automated link between the two. Verified
# equivalent (not just assumed) by running engine/replay_driver.py's own
# run() regression — the same cat2a_pos/cat2a_neg/cat2b_pos/cat2b_neg
# fabricated fixtures that validate the .dlog files themselves — against
# this on-demand version and confirming identical PASS/FAIL.
#
# `?alarm` is bound via a `VALUES` clause at the call site (build_script),
# not string-substituted into this template — avoids any risk of a
# substring match inside a longer variable name (e.g. `?alarmStart`).
_ALARMCAT = "https://w3id.org/mda/vocab/alarm-category/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_METRIC = "https://w3id.org/mda/vocab/metric/"
_METRIC_RATE = "https://w3id.org/mda/vocab/metric-rate/"
_FUNCTIONAL_UNIT = "https://w3id.org/mda/vocab/functional-unit/"
_OPERATION_STATE = "https://w3id.org/mda/vocab/operation-state/"
_QUALITY_STATE = "https://w3id.org/mda/vocab/quality-state/"
_CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"
_DEVICE = "https://w3id.org/mda/vocab/device/"
_ONDEMAND_BODY_TEMPLATES = {
    # Alarm-scoped (bound via VALUES ?alarm at the call site), projects
    # ?signal — hand-translated from cat1a_signal_quality.dlog's body.
    # NOT given the same one-graph-var-per-group fix as cat2a/cat2b/cat1b's
    # arriving-side prefix — tried it, and it broke cat1a_pos (a real
    # fixture with two alarms on one device whose sensor/signal identity
    # resolution genuinely diverges via add_triggered_by's identity-
    # dependent FunctionalUnit lookup, not just redundant re-minting of
    # one stable chain). That interaction needs its own dedicated
    # verification before touching this rule again — reverted rather than
    # guessed at further. Left in its original, correctness-verified shape.
    "cat1a": """
        GRAPH ?g1 { ?alarm <@MDA@hasMessage> ?msg }
        GRAPH ?g2 { ?msg <@MDA@triggeredBy> ?device }
        GRAPH ?g3 { ?device <@MDA@hasSensor> ?sensor }
        GRAPH ?g4 { ?sensor <@MDA@sensorProducesSignal> ?signal }
        GRAPH ?g5 { ?signal <@MDA@analyzedBy> ?analysis }
        GRAPH ?g6 { ?analysis <@MDA@producesMetric> ?metric }
        GRAPH ?g7 { ?signal <@MDA@hasQualityState> ?quality }
        FILTER(?quality != <@QUALITYSTATE@Good>)
        BIND(?signal AS ?evidence)
    """,
    # Alarm-scoped, projects ?ibpFunctionalUnit — hand-translated from
    # cat1b_asystole_ibp.dlog's body. REDESIGNED this session from an
    # open-world "no other alarm on this pathway" NOT EXISTS (confirmed
    # the dominant real cost this session: 12+s cumulative over ~800
    # alarms in one real-corpus window, dwarfing every other rule) into a
    # closed-world "pathway's own current state looks healthy" check —
    # three criteria, each independently OPTIONAL (unbound passes), no
    # cross-alarm search. See cat1b_asystole_ibp.dlog's own header for the
    # full reasoning, INCLUDING why each criterion is OPTIONAL rather than
    # a mandatory prerequisite (a mandatory metric-chain requirement was
    # confirmed empirically unsatisfiable: mda:producesMetric only ever
    # exists in an alarm's transient graph, always paired with one of the
    # 5 bad hasRate values in the same grounding — the "no information
    # should not resolve to healthy" gate is instead the EXISTING,
    # unchanged isMonitoredBy hop below, which only resolves if the IBP
    # device has had its own recent alarm activity at all).
    # g1/g3(struct)/g6(cond) below use the SAME ONE-graph-var-per-group fix
    # as cat1a (see its own comment) for the arriving alarm's OWN chain —
    # unambiguous, since there's exactly one arriving alarm. g9 onward
    # (the IBP pathway's own state, reached via ?patient isMonitoredBy
    # ?ibpDevice) is deliberately LEFT AS INDEPENDENT graph variables:
    # unlike the arriving alarm's own chain, multiple currently-active
    # alarms on the IBP pathway is a real, intentional "is ANY of them
    # reporting a bad state" OR condition (matching this rule's own
    # closed-world redesign, see this dict's own header comment) — forcing
    # them onto one shared graph variable would silently collapse that OR
    # into "the SAME one alarm must supply every criterion," an unverified
    # and likely wrong semantic change. Left alone pending its own
    # dedicated check, not fixed by assumption.
    "cat1b": """
        GRAPH ?g1 { ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?msg <@MDA@triggeredBy> ?device . }
        GRAPH ?g3 { ?device <@MDA@hasSensor> ?sensor . ?sensor <@MDA@sensorProducesSignal> ?signal .
                    ?signal <@MDA@analyzedBy> ?analysis . }
        GRAPH ?g6 { ?analysis <@MDA@producesMetric> ?metric . ?metric <@RDFTYPE@> <@METRIC@HeartRate> .
                    ?metric <@MDA@hasRate> <@METRICRATE@Absent> . }
        GRAPH ?g9 { ?patient <@MDA@isMonitoredBy> ?ibpDevice }
        GRAPH ?g10 { ?ibpDevice <@MDA@hasFunctionalUnit> ?ibpFunctionalUnit }
        GRAPH ?g11 { ?ibpFunctionalUnit <@RDFTYPE@> <@FUNCTIONALUNIT@FU_InvasiveBloodPressure> }

        OPTIONAL {
          GRAPH ?g12a { ?ibpFunctionalUnit <@MDA@hasSensor> ?ibpSensor1 }
          GRAPH ?g12b { ?ibpSensor1 <@MDA@hasSensorOperationState> ?ibpState }
        }
        FILTER( !BOUND(?ibpState) ||
                (?ibpState != <@OPSTATE@Disabled> && ?ibpState != <@OPSTATE@Disconnected> &&
                 ?ibpState != <@OPSTATE@Malfunction> && ?ibpState != <@OPSTATE@Warning>) )

        OPTIONAL {
          GRAPH ?g13a { ?ibpFunctionalUnit <@MDA@hasSensor> ?ibpSensor2 }
          GRAPH ?g13b { ?ibpSensor2 <@MDA@sensorProducesSignal> ?ibpSignal }
          GRAPH ?g13c { ?ibpSignal <@MDA@hasQualityState> ?ibpQuality }
        }
        FILTER( !BOUND(?ibpQuality) ||
                (?ibpQuality != <@QUALITYSTATE@Impaired> && ?ibpQuality != <@QUALITYSTATE@ScalingError>) )

        OPTIONAL {
          GRAPH ?g14a { ?ibpFunctionalUnit <@MDA@hasSensor> ?ibpSensor3 }
          GRAPH ?g14b { ?ibpSensor3 <@MDA@sensorProducesSignal> ?ibpSignal2 }
          GRAPH ?g14c { ?ibpSignal2 <@MDA@analyzedBy> ?ibpAnalysis }
          GRAPH ?g14d { ?ibpAnalysis <@MDA@producesMetric> ?ibpMetric }
          GRAPH ?g14e { ?ibpMetric <@MDA@hasRate> ?ibpRate }
        }
        FILTER( !BOUND(?ibpRate) ||
                (?ibpRate != <@METRICRATE@Absent> && ?ibpRate != <@METRICRATE@Decreased> &&
                 ?ibpRate != <@METRICRATE@Increased> && ?ibpRate != <@METRICRATE@SeverelyDecreased> &&
                 ?ibpRate != <@METRICRATE@SeverelyIncreased>) )

        BIND(?ibpFunctionalUnit AS ?evidence)
    """,
    "cat2a": """
        GRAPH ?g1 {
          ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
          ?alarm <@MDA@hasMessage> ?msg .
          ?msg <@MDA@concernsPatient> ?patient .
          ?alarm <@MDA@hasPriority> ?incomingPrio .
          ?alarm <@MDA@hasStart> ?alarmStart .
        }
        GRAPH ?g2 { ?msg <@MDA@triggeredBy> ?device }
        GRAPH ?g3 { ?device <@MDA@hasSensor> ?sensor }
        GRAPH ?g4 { ?sensor <@MDA@sensorProducesSignal> ?signal }
        GRAPH ?g5 { ?signal <@MDA@analyzedBy> ?analysis }
        GRAPH ?g6 { ?analysis <@MDA@producesMetric> ?metric }
        GRAPH ?g7 { ?metric <@MDA@approximates> ?property }
        ?property <@MDA@isPropertyOf> ?process .
        ?incomingPrio <@MDA@priorityRank> ?incomingRank .

        GRAPH ?g8 {
          ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
          ?active <@MDA@hasMessage> ?activeMsg .
          ?activeMsg <@MDA@concernsPatient> ?patient .
          ?active <@MDA@hasPriority> ?activePrio .
          ?active <@MDA@hasStart> ?activeStart .
        }
        GRAPH ?g9 { ?activeMsg <@MDA@triggeredBy> ?activeDevice }
        GRAPH ?g10 { ?activeDevice <@MDA@hasSensor> ?activeSensor }
        GRAPH ?g11 { ?activeSensor <@MDA@sensorProducesSignal> ?activeSignal }
        GRAPH ?g12 { ?activeSignal <@MDA@analyzedBy> ?activeAnalysis }
        GRAPH ?g13 { ?activeAnalysis <@MDA@producesMetric> ?activeMetric }
        GRAPH ?g14 { ?activeMetric <@MDA@approximates> ?activeProperty }
        ?activeProperty <@MDA@isPropertyOf> ?process .
        ?activePrio <@MDA@priorityRank> ?activeRank .

        FILTER(?active != ?alarm)
        FILTER(?activeRank >= ?incomingRank)
        FILTER(?activeStart < ?alarmStart)
    """,
    "cat2b": """
        GRAPH ?g1 {
          ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
          ?alarm <@MDA@hasMessage> ?msg .
          ?msg <@MDA@concernsPatient> ?patient .
          ?alarm <@MDA@hasStart> ?alarmStart .
        }
        GRAPH ?g2 { ?msg <@MDA@triggeredBy> ?device }
        GRAPH ?g3 { ?device <@MDA@hasSensor> ?sensorIn }
        GRAPH ?g4 { ?sensorIn <@MDA@sensorProducesSignal> ?signalIn }
        GRAPH ?g5 { ?signalIn <@MDA@analyzedBy> ?analysisIn }
        GRAPH ?g6 {
          ?analysisIn <@MDA@producesMetric> ?metricIn .
          ?metricIn <@RDFTYPE@> ?metricType .
        }

        GRAPH ?g7 {
          ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
          ?active <@MDA@hasMessage> ?activeMsg .
          ?activeMsg <@MDA@concernsPatient> ?patient .
          ?active <@MDA@hasStart> ?activeStart .
        }
        GRAPH ?g8 { ?activeMsg <@MDA@triggeredBy> ?activeDevice }
        GRAPH ?g9 { ?activeDevice <@MDA@hasSensor> ?sensorActive }
        GRAPH ?g10 { ?sensorActive <@MDA@sensorProducesSignal> ?signalActive }
        GRAPH ?g11 { ?signalActive <@MDA@analyzedBy> ?analysisActive }
        GRAPH ?g12 {
          ?analysisActive <@MDA@producesMetric> ?activeMetric .
          ?activeMetric <@RDFTYPE@> ?metricType .
        }

        FILTER(?active != ?alarm)
        FILTER(?sensorActive != ?sensorIn)
        FILTER(?activeStart < ?alarmStart)
    """,
    # cardiac_arrest/respiratory_arrest/reduced_pulmonary_function were
    # tried here too and REVERTED to standing rules — see their own .dlog
    # headers and ONDEMAND_RULE_NAMES's comment for why (unscoped,
    # unconditional-per-alarm checks; on-demand made real-corpus timing
    # WORSE, not better). No template for them here on purpose — don't
    # re-add without re-measuring against the same real-corpus case.
}
# Plan's §3: replaces "still physically present" with an explicit
# validity check, so a batched/delayed physical eviction (plan's §4) can't
# silently make a stale-but-not-yet-dropped graph look active. Only graph
# variables used OUTSIDE any FILTER NOT EXISTS or OPTIONAL span get a
# filter appended here — a variable scoped entirely inside a negation
# isn't bound in the outer WHERE at all (cat1b's ?g12 is handled by hand,
# inside its own negation, in the template above), and a variable scoped
# inside an OPTIONAL must stay genuinely optional: cat1b's rewritten
# criteria (see its own .dlog header) rely on "unbound passes" — auto-
# injecting a MANDATORY validUntil check for an OPTIONAL graph var would
# force it to always resolve, silently turning "optional" back into
# "required" and reintroducing the exact unsatisfiability bug that
# rewrite exists to fix.
_NOT_EXISTS_RE = re.compile(r"FILTER NOT EXISTS\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_OPTIONAL_RE = re.compile(r"OPTIONAL\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_GRAPH_VAR_RE = re.compile(r"GRAPH\s+(\?g\w*)\s*\{")


def _append_validity_filters(body: str) -> str:
    outer = _OPTIONAL_RE.sub(" ", _NOT_EXISTS_RE.sub(" ", body))
    graph_vars = sorted(set(_GRAPH_VAR_RE.findall(outer)))
    tail = " ".join(
        f"{v} <{M.MDA}validUntil> {v}_until . FILTER(?now <= {v}_until)"
        for v in graph_vars
    )
    return f"{body} {tail}" if tail else body


ONDEMAND_QUERY_BODIES = {
    name: _append_validity_filters(" ".join(
        tmpl.replace("@MDA@", str(M.MDA)).replace("@ALARMCAT@", _ALARMCAT)
            .replace("@RDFTYPE@", _RDF_TYPE).replace("@METRIC@", _METRIC)
            .replace("@METRICRATE@", _METRIC_RATE).replace("@FUNCTIONALUNIT@", _FUNCTIONAL_UNIT)
            .replace("@QUALITYSTATE@", _QUALITY_STATE).replace("@OPSTATE@", _OPERATION_STATE)
            .split()
    ))
    for name, tmpl in _ONDEMAND_BODY_TEMPLATES.items()
}

# cat2a/cat2b's PROMOTED aggregate rewrite (validation/shadow_cat2a_
# aggregate.py, validation/shadow_cat2b_aggregate.py — validated there
# first: correctness against the fabricated fixtures, and, against real
# patient 2826, cat2a >=8x faster and cat2b >=30x faster than the
# self-join above, which is what actually made that patient's replay
# stall). Standing AGGREGATE rules instead of a per-alarm cross-alarm
# self-join — see representation/rules/shadow/*.dlog's own headers for
# the full reasoning. cat1a/cat1b are NOT promoted: cat1a needs no
# redesign (no cross-alarm join at all), and cat1b's own shadow harness
# showed a small performance REGRESSION (0.44s vs 0.31s on the same real
# window) with no bottleneck to justify it — the aggregate pattern isn't
# a universal win, only where a real self-join cost exists to eliminate.
SHADOW_RULES_DIR = RULES_DIR / "shadow"
CAT2A_AGGREGATE_RULE = SHADOW_RULES_DIR / "cat2a_priority_aggregate.dlog"
CAT2B_AGGREGATE_RULE = SHADOW_RULES_DIR / "cat2b_sensor_count_aggregate.dlog"
AGGREGATE_PROMOTED_RULES = {"cat2a", "cat2b"}


def _cat2a_aggregate_branch(alarm: str, patient_iri: str, incoming_prio) -> str | None:
    """None if this archetype never minted hasPriority at all — cat2a can
    never fire for it (mirrors APPROXIMATES_COVERED_METRIC_TYPES's own
    dead-end short-circuit, just for a different reason).

    ONE graph variable per co-asserted group, not one per hop — confirmed
    directly (real patient 2826, this session) that independent per-hop
    graph variables let RDFox mix-and-match across every currently-open
    alarm's own redundant copy of the same structural facts, exploding a
    single alarm's own chain lookup from 1 real answer into 24,435 (a real
    CSV dump of the raw query results, not a guess) — because
    hasSensor/sensorProducesSignal/analyzedBy are re-minted into EVERY
    alarm's own persistent graph (kb.node_kind: Device/Sensor/Signal are
    "individuated", SignalAnalysis untagged — both route to
    ground_chain's `background`, i.e. the persistent graph), and every
    alarm sharing this device currently has its own open copy. `?gStruct`
    below pins all three hops to come from ONE such copy instead of
    letting RDFox pick a different copy per hop. `?gCond` similarly pins
    hasMessage/triggeredBy/producesMetric/approximates together — these
    ARE always co-asserted in one alarm's own transient graph (confirmed:
    ground_chain routes Metric, kb.node_kind "stateful", to `condition`;
    insert_alarm bundles msg_graph + cond_graph into the same tgraph
    block) — rather than each hop's own independent variable."""
    if incoming_prio is None:
        return None
    return (
        f"GRAPH ?gCond {{ <{alarm}> <{M.MDA}hasMessage> ?msg . ?msg <{M.MDA}triggeredBy> ?device . }} "
        f"GRAPH ?gStruct {{ ?device <{M.MDA}hasSensor> ?sensor . "
        f"?sensor <{M.MDA}sensorProducesSignal> ?signal . ?signal <{M.MDA}analyzedBy> ?analysis . }} "
        f"GRAPH ?gCond {{ ?analysis <{M.MDA}producesMetric> ?metric . ?metric <{M.MDA}approximates> ?property . }} "
        f"?property <{M.MDA}isPropertyOf> ?process . "
        f'BIND(IRI(CONCAT(STR(<{patient_iri}>), "|", STR(?process))) AS ?ppKey) '
        f"?ppKey <{M.MDA}shadowMaxActiveRank> ?maxRank . "
        f"<{incoming_prio}> <{M.MDA}priorityRank> ?incomingRank . "
        f"FILTER(?maxRank >= ?incomingRank) "
        f"BIND(<{alarm}> AS ?active)"
    )


def _cat2a_ondemand_aggregate_branch(alarm: str, incoming_prio) -> str | None:
    """Same domain condition as _cat2a_aggregate_branch (does some OTHER
    currently-active alarm on this alarm's own (patient, process) key
    have priorityRank >= mine), but computed as a genuinely fresh MAX
    aggregate SPARQL query per alarm — no standing shadowMaxActiveRank
    rule, nothing incrementally maintained, nothing for RDFox to pay a
    rescan cost for on every DELETE WHERE that evicts an expired alarm
    from the group. A third design point, distinct from both the standing
    aggregate rule ("cat2a", AGGREGATE_PROMOTED_RULES) and the
    already-tried-and-confirmed-worse on-demand self-join (the plan's own
    "cat2a >=8x faster... than the self-join above, which is what
    actually made that patient's replay stall") — this keeps the
    self-join's absence of standing-rule state but expresses the "does
    anything qualify" check as a single aggregate rather than a per-row
    filter+limit-1, on the theory that RDFox's planner may handle a native
    MAX differently (e.g. index-assisted) than the equivalent filtered
    self-join. Unverified until timed against real patient 2826 — that's
    the whole point of this variant existing as its own selectable rule
    name rather than replacing "cat2a" outright.

    ?process is bound once, from the arriving alarm's own chain, and
    reused (not re-bound) on the active side — so the subquery's implicit
    single group already corresponds to exactly this alarm's own
    (patient, process) key; no explicit GROUP BY needed, mirroring how
    _cat2a_aggregate_branch's ppKey plays the identical role.
    """
    if incoming_prio is None:
        return None
    return (
        f"{{ SELECT (MAX(?activeRank) AS ?maxRank) WHERE {{ "
        f"GRAPH ?gArrCond {{ <{alarm}> <{M.MDA}hasMessage> ?msg . ?msg <{M.MDA}triggeredBy> ?device . }} "
        f"GRAPH ?gArrStruct {{ ?device <{M.MDA}hasSensor> ?sensor . "
        f"?sensor <{M.MDA}sensorProducesSignal> ?signal . ?signal <{M.MDA}analyzedBy> ?analysis . }} "
        f"GRAPH ?gArrCond {{ ?analysis <{M.MDA}producesMetric> ?metric . ?metric <{M.MDA}approximates> ?property . }} "
        f"?property <{M.MDA}isPropertyOf> ?process . "
        f"GRAPH ?gCandCond {{ "
        f"?candidate <{M.MDA}hasCategory> <https://w3id.org/mda/vocab/alarm-category/Physiological> . "
        f"?candidate <{M.MDA}hasMessage> ?activeMsg . "
        f"?activeMsg <{M.MDA}triggeredBy> ?activeDevice . "
        f"?candidate <{M.MDA}hasPriority> ?activePrio . "
        f"}} "
        f"GRAPH ?gCandStruct {{ ?activeDevice <{M.MDA}hasSensor> ?activeSensor . "
        f"?activeSensor <{M.MDA}sensorProducesSignal> ?activeSignal . ?activeSignal <{M.MDA}analyzedBy> ?activeAnalysis . }} "
        f"GRAPH ?gCandCond {{ ?activeAnalysis <{M.MDA}producesMetric> ?activeMetric . "
        f"?activeMetric <{M.MDA}approximates> ?activeProperty . }} "
        f"?activeProperty <{M.MDA}isPropertyOf> ?process . "
        f"?activePrio <{M.MDA}priorityRank> ?activeRank . "
        f"}} }} "
        f"<{incoming_prio}> <{M.MDA}priorityRank> ?incomingRank . "
        f"FILTER(?maxRank >= ?incomingRank) "
        f"BIND(<{alarm}> AS ?active)"
    )


def _cat2b_aggregate_branch(alarm: str, patient_iri: str) -> str:
    """Same ONE-graph-variable-per-co-asserted-group fix as
    _cat2a_aggregate_branch — see its docstring for the confirmed
    mechanism (hasSensor/sensorProducesSignal/analyzedBy always co-minted
    into one alarm's own persistent graph; hasCategory/hasMessage/
    triggeredBy/producesMetric/rdf:type always co-minted into that same
    alarm's own transient graph)."""
    return (
        f"GRAPH ?gCond {{ "
        f"<{alarm}> <{M.MDA}hasCategory> <https://w3id.org/mda/vocab/alarm-category/Physiological> . "
        f"<{alarm}> <{M.MDA}hasMessage> ?msg . "
        f"?msg <{M.MDA}triggeredBy> ?device . "
        f"}} "
        f"GRAPH ?gStruct {{ ?device <{M.MDA}hasSensor> ?sensorIn . "
        f"?sensorIn <{M.MDA}sensorProducesSignal> ?signalIn . ?signalIn <{M.MDA}analyzedBy> ?analysisIn . }} "
        f"GRAPH ?gCond {{ "
        f"?analysisIn <{M.MDA}producesMetric> ?metricIn . "
        f"?metricIn <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> ?metricType . "
        f"}} "
        f'BIND(IRI(CONCAT(STR(<{patient_iri}>), "|", STR(?metricType))) AS ?mtKey) '
        f"?mtKey <{M.MDA}shadowSensorCount> ?c . "
        f"FILTER(?c >= 2) "
        f"BIND(<{alarm}> AS ?active)"
    )


# Metric types representation/rules/approximates_bridge.dlog actually
# derives mda:approximates for (its 14 rule heads) — MUST be kept in sync
# BY HAND if that file's coverage ever changes. Any metric type NOT in
# this set can never satisfy cat2a's join at all: cat2a's arriving-side
# ?metric -> approximates -> ?property hop can never bind for it, so the
# whole query is guaranteed empty by construction.
#
# WHY THIS IS CHECKED HERE, IN PYTHON, RATHER THAN LEFT FOR RDFOX TO
# DISCOVER: confirmed directly this is a real, live gap — patient 2826's
# alarm 3499 (metric type ArterialBloodPressure_Mean, not in this set)
# made cat2a's on-demand query take 100+ seconds to prove what this
# lookup proves in microseconds: there is no possible match. RDFox's own
# query planner does NOT discover the dead end cheaply on its own — tried
# and empirically ruled out BOTH reordering the query body so the
# arriving side's approximates hop comes first (100.19s, no improvement)
# AND wrapping the arriving side in a SPARQL subquery to force it to
# materialize before the join (still hadn't finished after 180s — worse).
# Neither purely-SPARQL restructuring changed RDFox's chosen plan, which
# is why this decision has to be made outside the query entirely, in
# Python, before the query is even generated.
APPROXIMATES_COVERED_METRIC_TYPES = {
    "SpO2", "ArterialBloodPressure", "VenousBloodPressure", "PulseFlowIndex",
    "HeartRate", "PulseRate", "CO2", "RespirationRate", "RespirationVolume",
    "AirwayPressure", "CPAP", "AirTemperature", "Temperature", "Humidity",
}


def _alarm_metric_types(kb, event) -> set:
    """Every distinct Metric-kind concept name (e.g. {"ArterialBloodPressure_Mean"})
    M.ground_chain would mint for this alarm's own archetype — used only to
    decide whether cat2a's on-demand query can possibly match (see
    APPROXIMATES_COVERED_METRIC_TYPES above), not part of any minted
    output itself. Cheap: archetype_structure is memoized on `kb`
    (kb.archetype_cache), so this is a small, already-cached tree walk,
    safe to call once per alarm."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return set()
    arch = M.archetype_structure(kb, type_iri)
    found = set()

    def walk(cls):
        concept = arch.concept(cls)
        if concept is not None and "vocab/metric/" in str(concept):
            found.add(str(concept).rsplit("/", 1)[-1])
        for child_cls, link in kb.tree.items():
            if link and link[0] == cls:
                walk(child_cls)

    walk(M.MDA.Device)
    return found


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = width if total <= 0 else min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def build_script(kb, patients: dict, scratch_dir: Path, enabled_rules=None,
                  progress: bool = True, verify_identity: bool = False,
                  dstore: str = "poc") -> tuple:
    """`enabled_rules`: iterable of RULE_FILES keys to import, or None for
    all of them (every rule enabled — the default validation behaviour).

    `dstore`: ONE dstore shared by every patient in `patients`, created
    and populated with the framework/rule files exactly once — not one
    dstore per patient (the original Phase 2 design). Import time itself
    was never the cost (single-digit ms/file, per RDFox's own logging);
    the problem was memory: at the project's 3500-patient target, 3500
    separate dstores each holding an independent copy of the ~3000-triple
    static framework multiplies that memory ~3500x for data that's
    identical across all of them. Safe to share now that every minted
    entity/alarm/message IRI is patient-scoped (see mint.py's MINTING
    section header) — before that fix, two patients sharing a real-corpus
    device_id would have collided onto one IRI in a shared dstore.

    `progress`: print a per-patient header line, then an in-place
    (carriage-return-updated) progress bar as that patient's own alarms
    are minted — never one line per alarm (a real patient can carry tens
    of thousands, see _progress_bar's own call site). Historically
    load-bearing when per-alarm minting ran a real OWL-RL closure
    (~2-3s/call, since removed — see mint.py's module docstring); kept on
    by default since a multi-thousand-patient run is still worth showing
    progress for even at the much lower per-alarm cost minting has now.

    `verify_identity`: development-only — also run the batch
    M.resolve_identity(kb, events_so_far) per alarm and assert it matches
    M.IdentityTracker's incremental result, mirroring how Timeline.
    observe() (op_knowledge.py) was itself originally validated. Doubles
    identity-resolution cost, so leave off by default; only turn on to
    re-confirm the equivalence after touching either implementation.

    KNOWN, HARMLESS FALSE-POSITIVE with this flag: when two of a
    patient's alarms share the exact same `start` instant (real corpus
    data — confirmed on alexKim_03), `events_so_far = [ev for ev in
    events_sorted if ev.start <= e.start]` includes BOTH tied alarms
    for the batch computation, while the incremental tracker has only
    processed the first of the two (in `events_sorted`'s stable order) at
    this exact check point — so the two can briefly disagree on an entry
    for the *other* tied alarm's device_id. Confirmed inert: each alarm's
    own grounding (`particular_iri`) only ever reads its OWN device_id's
    identity entry, never a different device's, and the tracker converges
    to the same content as soon as the second tied alarm is itself
    processed one iteration later. Verified end-to-end (not just via this
    assertion) by re-running poc_entry.py's exact same real-corpus sample
    before and after this port and confirming identical fire counts."""
    rule_names = list(RULE_FILES) if enabled_rules is None else list(enabled_rules)
    lines = []
    checks = []
    file_counter = itertools.count(1)
    t0 = time.monotonic()
    num_patients = len(patients)

    lines.append(f"dstore create {dstore}")
    lines.append(f"active {dstore}")
    for f in FRAMEWORK_FILES:
        lines.append(f"import {f}")
    for name in rule_names:
        if name in ONDEMAND_RULE_NAMES:
            continue  # queried on demand per alarm below, not a standing rule
        lines.append(f"import {RULE_FILES[name]}")
    # cat2a/cat2b's promoted aggregate rewrite — standing rules (see
    # AGGREGATE_PROMOTED_RULES's own comment), loaded instead of
    # RULE_FILES["cat2a"/"cat2b"] (never loaded — self-join rules stay
    # on-demand-only per ONDEMAND_RULE_NAMES, and these ARE the on-demand
    # replacement for their check bodies, not additional standing rules
    # alongside them).
    if "cat2a" in rule_names:
        lines.append(f"import {CAT2A_AGGREGATE_RULE}")
    if "cat2b" in rule_names:
        lines.append(f"import {CAT2B_AGGREGATE_RULE}")

    # Which on-demand rules are enabled for THIS run, grouped by which
    # check/predicate they feed — computed once, not per-alarm, since
    # `rule_names` is fixed for the whole call.
    flagged_active = [name for name in ("cat1a", "cat1b") if name in rule_names]
    silenced_active = [name for name in ("cat2a", "cat2b", "cat2a_ondemand_agg") if name in rule_names]
    # impliesClinicalEvent's 3 rules stay STANDING (see ONDEMAND_RULE_NAMES's
    # own comment on why) — any_clinical just gates whether that check is
    # emitted at all, same as before either conversion.
    any_clinical = bool(CLINICAL_RULE_NAMES & set(rule_names))

    # CAT3a/CAT3b: see RULE_FILES' own comment and CAT3_DEPENDENCIES.
    # Fail fast rather than silently emitting a check that can never
    # match — enabling cat3a/cat3b without their building-block inputs
    # is a real misconfiguration a SETTINGS dict edit could hit, not a
    # hypothetical.
    cat3a_active = "cat3a" in rule_names
    cat3b_active = "cat3b" in rule_names
    for name, active in (("cat3a", cat3a_active), ("cat3b", cat3b_active)):
        if active and not CAT3_DEPENDENCIES[name] <= set(rule_names):
            missing = CAT3_DEPENDENCIES[name] - set(rule_names)
            raise ValueError(f"{name} requires {sorted(CAT3_DEPENDENCIES[name])} also enabled "
                              f"(missing: {sorted(missing)})")

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        events_sorted = sorted(events, key=lambda ev: ev.start)
        if progress:
            print(f"  [{time.monotonic() - t0:7.1f}s] minting patient {pi}/{num_patients} "
                  f"({patient}): {len(events_sorted)} alarm(s)")

        # RDFox's own `echo <token>` shell command (confirmed via `help
        # echo`: "Prints the tokens specified... separated by a single
        # space" — an exact, undecorated line, nothing to disambiguate)
        # gives execute_script an unambiguous per-patient/per-alarm marker
        # to match live in RDFox's streamed stdout — see execute_script's
        # own docstring for why this is the RDFox-execution counterpart to
        # this function's own per-alarm minting progress bar above.
        lines.append(f"echo PATIENT_START:{patient}")

        driver = Driver(kb, scratch_dir, file_counter)
        n_events = len(events_sorted)
        # Update at most ~100 times per patient, not once per alarm — a
        # real patient can carry tens of thousands of alarms (confirmed
        # directly: one real-corpus patient had 25,793), and printing a
        # full line per alarm at that scale floods the terminal with
        # scrollback rather than showing progress. An in-place bar
        # (carriage return, no newline until the patient is done) shows
        # the same real-time signal in one line instead.
        report_every = max(1, n_events // 100)
        for ei, e in enumerate(events_sorted, start=1):
            if progress and (ei == 1 or ei == n_events or ei % report_every == 0):
                bar = _progress_bar(ei, n_events)
                print(f"\r    {bar} {ei}/{n_events} alarms "
                      f"[{time.monotonic() - t0:7.1f}s]", end="", flush=True)
            driver.flush_due(e.start)
            M.update_identity(kb, e, driver.identity_tracker)
            identity = driver.identity_tracker.identity
            if verify_identity:
                events_so_far = [ev for ev in events_sorted if ev.start <= e.start]
                batch_identity = M.resolve_identity(kb, events_so_far)
                assert identity == batch_identity, (
                    f"incremental identity tracker diverged from resolve_identity's batch "
                    f"computation for {patient} at {e.label}@{e.start}")
            pending = driver.insert_alarm(e, identity)
            lines.extend(driver.commands)
            driver.commands.clear()

            # Check THIS alarm's own firing status right at its arrival —
            # matching assess.py's own timing discipline (mda_poc_
            # assessment.py calls assess() once per arriving alarm, against
            # the situation AT THAT INSTANT). Checking only once at the very
            # end of the whole patient timeline (the original version of
            # this driver did) is wrong: by then every alarm's transient
            # graph — and eventually its persistent graph — has already
            # been dropped, so nothing is left to match against at all.
            #
            # This runs BETWEEN insert_alarm()'s two phases — the arriving
            # alarm's own hasPriority triple isn't inserted yet (see
            # insert_alarm's own docstring for why cat2a's aggregate check
            # needs that) — but every check below is unaffected by
            # hasPriority's absence, so nothing else needs to know or care.
            alarm = pending["alarm"]
            patient_iri = pending["patient"]
            now_literal = f'"{e.start.isoformat()}"^^{XSD_DATETIME}'
            # None of cat1a/cat1b/cat2a/cat2b/the 3 clinical rules are
            # standing rules anymore — see ONDEMAND_RULE_NAMES's own
            # module-level comment for why. Each predicate is instead
            # checked via a UNION'd, on-demand query over whichever
            # contributing rules are enabled. Per the plan's §3, each
            # on-demand body now carries its own validUntil FILTER
            # (ONDEMAND_QUERY_BODIES/_append_validity_filters) bound to
            # ?now here — physical graph presence is no longer what makes
            # a match valid, now that eviction can be batched (plan's §4)
            # and may lag a graph's own logical expiry.
            #
            # flaggedLikelyFalsePositive/silencedBy are alarm-scoped
            # (?alarm bound via VALUES to THIS alarm's own IRI).
            # impliesClinicalEvent is NOT alarm-scoped — its subject is a
            # PhysiologicalProcess, not an alarm, so (unlike the other two)
            # it asks "is ANY clinical event currently implied ANYWHERE in
            # the store" rather than "did THIS alarm/patient cause one".
            #
            # KNOWN IMPRECISE UNDER A SHARED DSTORE, NOT A REGRESSION THIS
            # PHASE INTRODUCES: physiologicalProcess:X (e.g.
            # CardiacContraction) is a STANDING, patient-independent
            # CONCEPT — impliesClinicalEvent's own domain is Referential
            # (one shared IRI globally), so this check can return a hit
            # "belonging" to a DIFFERENT patient's concurrently-active
            # alarm. This is the exact already-documented Phase 3 gap the
            # design doc's own "Grounding facts" section describes (SKOLEM
            # minting not yet implemented) — not something this phase
            # needs to solve, since no CAT rule consumes
            # impliesClinicalEvent. Revisit once Phase 3 lands.
            #
            # Each group is only emitted when at least one contributing
            # rule is enabled — with none enabled there's nothing to check
            # (matches the old behaviour of the predicate simply never
            # being derived).
            # Emitted as SEPARATE queries per rule, not UNIONed — mirrors
            # cat2a/cat2b's own fix (see this_alarm_silenced's comment
            # below) for the same reason: keeps each rule individually
            # timed/counted for the per-rule instrumentation in
            # execute_script, and avoids relying on RDFox's planner to
            # handle a UNION of differently-shaped bodies well.
            for name in flagged_active:
                branch = f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }} {ONDEMAND_QUERY_BODIES[name]}"
                lines.append(f"select distinct ?evidence where {{ {branch} }}")
                checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
            # cat2a can only ever match if THIS alarm's own metric type has
            # an mda:approximates mapping at all (see
            # APPROXIMATES_COVERED_METRIC_TYPES's own comment — harmless to
            # keep even now that cat2a is aggregate-based: the arriving-side
            # chain hop still can't bind for an uncovered metric type, so
            # this remains a correct, if now purely cosmetic, short-circuit).
            # cat2b needs no such check: it joins on the metric's own
            # rdf:type directly, a base fact that's never missing.
            this_alarm_silenced = silenced_active
            if "cat2a" in this_alarm_silenced or "cat2a_ondemand_agg" in this_alarm_silenced:
                metric_types = _alarm_metric_types(kb, e)
                if metric_types and not (metric_types & APPROXIMATES_COVERED_METRIC_TYPES):
                    this_alarm_silenced = [n for n in this_alarm_silenced
                                            if n not in ("cat2a", "cat2a_ondemand_agg")]
            # cat2a's and cat2b's aggregate checks are emitted as SEPARATE
            # queries, NOT combined via UNION into one — confirmed
            # empirically (real patient 2826, alarms 0-900): each alone
            # (and both loaded as standing rules but only one checked) ran
            # in ~2-3s for 900 alarms; UNIONing their two branches together
            # into one query reproducibly cost ~90-97s for the same 900
            # alarms. RDFox's planner produces a badly inefficient plan for
            # this specific combination — not something either branch does
            # on its own. checks gets a 5-tuple: name as the 4th element so
            # cat2a's and cat2b's entries don't collide as the SAME dict
            # key in execute_script's `dict(zip(checks, counts))` (which
            # would silently drop one), and each alarm's own per-patient
            # sequence index (`ei`) as the 5th so two alarms sharing the
            # exact same `start` instant — a real, non-rare occurrence in
            # the real corpus (confirmed on alexKim_03, see build_script's
            # own `verify_identity` docstring) — don't ALSO collide with
            # each other, which silently dropped one of their results
            # before this index was added. See run()'s and poc_entry.py's
            # report()'s own "at most one per alarm" grouping for how
            # cat2a/cat2b are recombined into a single silencedBy verdict
            # per alarm, matching the old single-UNIONed-query semantics —
            # unaffected by the extra tuple element, since it only reads
            # k[0]/k[1].
            for name in this_alarm_silenced:
                if name == "cat2a":
                    branch = _cat2a_aggregate_branch(alarm, patient_iri, pending["incoming_prio"])
                    if branch is None:
                        continue
                elif name == "cat2a_ondemand_agg":
                    branch = _cat2a_ondemand_aggregate_branch(alarm, pending["incoming_prio"])
                    if branch is None:
                        continue
                elif name == "cat2b":
                    branch = _cat2b_aggregate_branch(alarm, patient_iri)
                else:
                    branch = (
                        f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }} {ONDEMAND_QUERY_BODIES[name]}"
                    )
                lines.append(f"select ?active where {{ {branch} }} limit 1")
                checks.append((patient, e.start, "silencedBy", name, ei))
            if any_clinical:
                # Standing rule (see ONDEMAND_RULE_NAMES's own comment for
                # why this one stays materialized, unlike everything else
                # in this function) — a read against RDFox's own
                # incrementally-maintained answer. A standing Datalog rule
                # has no notion of "now" (it's maintained against whatever
                # the store currently contains), so per the plan's §3 the
                # validity check has to live HERE, at the read site, not in
                # any .dlog rule body — every clinical rule already keeps
                # its head tied to its deciding antecedent's own graph
                # variable (see e.g. cardiac_arrest.dlog's own header), so
                # re-deriving that graph and checking its validUntil is
                # enough; no .dlog file needs to change.
                lines.append(
                    f"select ?process ?event where {{ "
                    f"VALUES ?now {{ {now_literal} }} "
                    f"GRAPH ?g {{ ?process <{M.MDA}impliesClinicalEvent> ?event }} "
                    f"?g <{M.MDA}validUntil> ?until . FILTER(?now <= ?until) }}"
                )
                checks.append((patient, e.start, "impliesClinicalEvent", "clinical", ei))
            if cat3a_active:
                # CAT3a — cardiorespiratory arrest: are CardiacArrest and
                # RespiratoryArrest (cardiac_arrest.dlog/respiratory_arrest.
                # dlog's own standing tags) BOTH currently valid, right now.
                # No ?alarm anchor, deliberately, same as the older rdflib
                # POC's cat3a_cardiorespiratory_arrest.rq: this is a genuine
                # temporal-overlap check between two independent conditions,
                # not something scoped to the arriving alarm. `limit 1` —
                # only "does at least one such pair currently hold" matters;
                # see count_cat3_episodes' own docstring for why a boolean
                # is enough here (PhysiologicalProcess is Referential — one
                # shared IRI per concept, so there is at most one such pair
                # per patient at any instant, not a genuine set to dedupe
                # over the way the older POC's open_pairs did).
                #
                # KNOWN, batch_size-DEPENDENT IMPRECISION: like
                # impliesClinicalEvent's own check above, this can in
                # principle match a DIFFERENT patient's concurrently-active
                # tags under a shared dstore (PhysiologicalProcess carries
                # no patient scoping — see that check's own comment). Inert
                # under this project's current SETTINGS['batch_size']: 1
                # (one patient's alarms in the dstore at a time) — would
                # need real per-patient grounding of Process nodes
                # ("Phase 3") before this is safe at batch_size > 1.
                lines.append(
                    f"select ?cardiacNode ?respiratoryNode where {{ "
                    f"VALUES ?now {{ {now_literal} }} "
                    f"GRAPH ?g1 {{ ?cardiacNode <{M.MDA}impliesClinicalEvent> <{_CLINICAL_EVENT}CardiacArrest> }} "
                    f"?g1 <{M.MDA}validUntil> ?until1 . FILTER(?now <= ?until1) "
                    f"GRAPH ?g2 {{ ?respiratoryNode <{M.MDA}impliesClinicalEvent> <{_CLINICAL_EVENT}RespiratoryArrest> }} "
                    f"?g2 <{M.MDA}validUntil> ?until2 . FILTER(?now <= ?until2) }} limit 1"
                )
                checks.append((patient, e.start, "cat3aCoincidence", "cat3a", ei))
            if cat3b_active:
                # CAT3b — ventilation failure: a mechanical ventilator
                # currently reporting a non-Enabled hasDeviceOperationState,
                # AND ReducedPulmonaryFunction (reduced_pulmonary_function.
                # dlog's own standing tag) both currently valid. Ported from
                # cat3b_ventilation_failure.rq — device side matched
                # directly (hasDeviceOperationState, not the more general
                # hasOperationState the old rdflib POC used: that property's
                # owl:propertyChainAxiom promotion was never ported to
                # RDFox, and isn't needed — hasDeviceOperationState is
                # grounded straight into the PERSISTENT/background graph by
                # mint.py's background_for_key, specifically so it survives
                # the 15-minute post-alarm window on its own; see that
                # function's own comment for why this needed fixing first).
                # device:MechanicalVentilator is matched as the device
                # node's OWN asserted rdf:type, not via subclass reasoning —
                # confirmed directly (data/kg_generated.ttl) that archetype
                # instances are typed with this base class already; the
                # manufacturer-specific SKOS concepts in vocab_generated.ttl
                # are never the asserted rdf:type, only hasDeviceType's
                # value, so no subClassOf hop is needed.
                # Same no-?alarm-anchor, `limit 1`, batch_size-dependent
                # patient-scoping caveats as cat3a above.
                lines.append(
                    f"select ?deviceNode ?processNode where {{ "
                    f"VALUES ?now {{ {now_literal} }} "
                    f"GRAPH ?g1 {{ ?deviceNode <{_RDF_TYPE}> <{_DEVICE}MechanicalVentilator> . "
                    f"?deviceNode <{M.MDA}hasDeviceOperationState> ?state . "
                    f"FILTER(?state != <{_OPERATION_STATE}Enabled>) }} "
                    f"?g1 <{M.MDA}validUntil> ?until1 . FILTER(?now <= ?until1) "
                    f"GRAPH ?g2 {{ ?processNode <{M.MDA}impliesClinicalEvent> <{_CLINICAL_EVENT}ReducedPulmonaryFunction> }} "
                    f"?g2 <{M.MDA}validUntil> ?until2 . FILTER(?now <= ?until2) }} limit 1"
                )
                checks.append((patient, e.start, "cat3bCoincidence", "cat3b", ei))
            driver.complete_alarm(pending)
            lines.extend(driver.commands)
            driver.commands.clear()
            lines.append(f"echo ALARM_DONE:{patient}")
        if progress:
            print()  # finalize this patient's in-place progress line
        driver.flush_all()
        lines.extend(driver.commands)

    if progress:
        print(f"  [{time.monotonic() - t0:7.1f}s] minting done for {num_patients} patient(s)")
    lines.append("quit")
    return "\n".join(lines), checks


def _reader_thread(pipe, q: queue.Queue) -> None:
    """Runs in a background thread: forward every line RDFox prints to
    `q`, then a final None sentinel on EOF. Needed (rather than just
    iterating `pipe` in the main thread) so execute_script can still
    enforce a wall-clock timeout even if RDFox goes completely silent —
    an in-loop time check only runs between lines received, which never
    fires at all if no more lines ever arrive."""
    for line in pipe:
        q.put(line.rstrip("\n"))
    q.put(None)


def execute_script(script_text: str, checks: list, scratch: Path, timeout: int = 600,
                    progress: bool = True, patients: dict | None = None) -> tuple:
    """Run `script_text` against a real RDFox instance and return
    ({check_key: answer_count}, {check_key: seconds}) for every entry in
    `checks`, in order — the second dict is per-statement wall-clock time
    as RDFox itself reports it ("Total statement evaluation time"), used
    by summarize_rule_timings for per-rule trigger-count/duration
    analysis.

    Shared by run() (the fixed regression test below) and poc_entry.py
    (the general-purpose runner) so the RDFox invocation/output-parsing
    logic — both non-obvious, see the comments inline — lives in exactly
    one place.

    `progress`: render an in-place, per-patient progress bar as RDFox
    actually executes the script — the counterpart to build_script's own
    per-alarm minting bar, for the step that follows it. Confirmed
    directly (not assumed) that this is possible at all: RDFox flushes
    its stdout per-line even when piped, not just when attached to a
    TTY — verified with an `echo`-then-`sleep 3000`-then-`echo` script,
    where the first echo arrived immediately and the second only after
    the full 3s, ruling out RDFox block-buffering its own output until
    exit (the common failure mode that would have made "live" progress
    silently do nothing until the process ends anyway). Driven by the
    `echo PATIENT_START:<patient>` / `echo ALARM_DONE:<patient>` marker
    lines build_script emits into the script for exactly this purpose —
    `echo`'s own RDFox semantics (`help echo`: "Prints the tokens
    specified... separated by a single space") make these unambiguous,
    exact lines to match on, unlike inferring progress from counting
    select/DELETE-WHERE result lines (which still need the lookahead
    disambiguation below, and don't carry a patient identity at all).

    `patients`: the same {patient: events} dict build_script was called
    with, used only to size the progress bar (alarm count per patient).
    Optional and derived from `checks` when omitted — but `checks` is
    empty whenever every rule is disabled (a real, useful diagnostic
    run — see poc_entry.py's SETTINGS['enabled_rules']), which previously
    collapsed the progress bar to "patient N/0 ... /0 alarms" since it
    had nothing to size itself from. Pass `patients` to keep the bar
    correct in that case.
    """
    script_path = scratch / "replay.rdfox"
    script_path.write_text(script_text)

    if patients is not None:
        total_per_patient = {p: len(evs) for p, evs in patients.items()}
    else:
        total_per_patient = {}
        for check in checks:
            patient, ts = check[0], check[1]
            total_per_patient.setdefault(patient, set()).add(ts)
        total_per_patient = {p: len(s) for p, s in total_per_patient.items()}
    num_patients = len(total_per_patient)

    # RDFox's CLI has no "run this script file" positional argument — any
    # argument after <root> in sandbox/shell mode is itself a SHELL COMMAND
    # (confirmed via `RDFox -help`: "all supplied commands are executed").
    # The working pattern is piping the script's own text via stdin, which
    # also closes stdin on EOF (no interactive prompt to hang on) without
    # needing an explicit `< /dev/null`.
    t0 = time.monotonic()
    script_file = script_path.open()
    process = subprocess.Popen(
        [str(RDFOX_BIN), "sandbox", str(scratch)],
        env={"RDFOX_LICENSE_FILE": str(LICENSE)},
        stdin=script_file, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(process.stdout, q), daemon=True)
    reader.start()

    out_lines: list = []
    current_patient = None
    patient_index = 0
    alarms_done = 0
    timed_out = False
    deadline = t0 + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Used to raise TimeoutExpired here, which meant a stalling
            # run produced ZERO diagnostic data — exactly the case where
            # per-rule timing (summarize_rule_timings) matters most.
            # Instead: kill the process, and fall through to the same
            # parsing logic below on whatever output was captured before
            # the kill, so every check/alarm that DID complete still gets
            # counted and timed. The caller can tell a partial result from
            # a complete one via the printed warning below (there's no
            # separate return signal — the point is graceful degradation,
            # not a new error-handling contract every caller must adopt).
            timed_out = True
            process.kill()
            break
        try:
            line = q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        out_lines.append(line)

        if not progress:
            continue
        if line.startswith("PATIENT_START:"):
            current_patient = line.split(":", 1)[1]
            patient_index += 1
            alarms_done = 0
            total = total_per_patient.get(current_patient, 0)
            print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                  f"{_progress_bar(0, total)} 0/{total} alarms "
                  f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)
        elif line.startswith("ALARM_DONE:"):
            total = total_per_patient.get(current_patient, 0)
            report_every = max(1, total // 100)
            alarms_done += 1
            if alarms_done == total or alarms_done % report_every == 0:
                print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                      f"{_progress_bar(alarms_done, total)} {alarms_done}/{total} alarms "
                      f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)

    process.wait()
    script_file.close()
    if progress and num_patients:
        print()  # finalize the in-place line
    if timed_out:
        print(f"  [{time.monotonic() - t0:7.1f}s] *** TIMED OUT after {timeout}s — "
              f"RDFox killed mid-execution. Parsing partial output below: every check "
              f"that DID complete before the kill is still counted/timed; anything "
              f"after the stall is simply absent from the result. ***")
    else:
        print(f"  [{time.monotonic() - t0:7.1f}s] RDFox execution finished "
              f"({len(checks)} check(s) run)")
    output = "\n".join(out_lines)

    # "Number of query answers:" is printed by BOTH `select` and
    # `DELETE WHERE` (the latter as its WHERE-clause match count, always
    # followed within a few lines by "Number of attempted deletions:") —
    # only lines NOT followed by that marker are real SELECT results.
    # Each statement (select or delete) also prints its OWN "Total
    # statement evaluation time: X s" a few lines after its own "Number
    # of query answers" — confirmed directly from RDFox's own output
    # shape (both spike-test and production runs) — so the same lookahead
    # window that already isolates a real select's answer count also
    # captures that select's own timing, giving per-check duration
    # without needing any extra RDFox-side instrumentation.
    select_counts = []
    select_timings = []
    for i, line in enumerate(out_lines):
        m = re.match(r"Number of query answers:\s+(\d+)", line)
        if not m:
            continue
        lookahead = out_lines[i:i + 6]
        if any("Number of attempted deletions" in l for l in lookahead):
            continue
        select_counts.append(int(m.group(1)))
        t = None
        for l in lookahead:
            tm = re.match(r"Total statement evaluation time:\s+([\d.]+)\s*s", l)
            if tm:
                t = float(tm.group(1))
                break
        select_timings.append(t)

    if "rror" in output:
        error_lines = [l for l in out_lines if "rror" in l]
        if error_lines:
            print("--- errors seen in RDFox output ---")
            for l in error_lines:
                print(" ", l)

    counts_by_check = dict(zip(checks, select_counts))
    timings_by_check = dict(zip(checks, select_timings))
    return counts_by_check, timings_by_check


def run_batched(kb, patients: dict, scratch_root: Path, batch_size: "int | None" = None,
                 enabled_rules=None, progress: bool = True) -> tuple:
    """Process `patients` in bounded-size batches instead of one script for
    every patient in the run, returning the merged
    ({check_key: answer_count}, {check_key: seconds}) across all batches.

    `batch_size`: None (or >= len(patients)) processes everyone in a single
    batch — today's behaviour, unchanged. A smaller number bounds how much
    is held in memory/disk at once (every batch's trig files, its script
    text) and how many patients' worth of data live in the shared dstore
    simultaneously, which matters at the project's 3500-patient target —
    holding the entire run as one script/dstore doesn't scale the way it
    does for a handful of patients.

    Deliberately NOT the long-lived-streaming-subprocess design (a
    persistent RDFox process fed incrementally) — that needs its own
    spike first (unverified I/O territory: every RDFox invocation tested
    so far is one-shot blocking, write-then-close-stdin-then-read-all-of-
    stdout; a persistent open-stdin process risks output buffering and
    writer/reader deadlock that hasn't been exercised at all). This
    batches across separate, already-proven `subprocess.run` calls
    instead — zero new subprocess-I/O risk, and each batch still gets its
    own shared dstore (item 2), just scoped to that batch's patients
    rather than the whole run.
    """
    items = list(patients.items())
    size = batch_size if batch_size else len(items)
    counts_by_check: dict = {}
    timings_by_check: dict = {}
    num_batches = -(-len(items) // size) if items else 0  # ceil div
    for bi in range(0, len(items), size):
        batch = dict(items[bi:bi + size])
        batch_num = bi // size + 1
        if progress:
            print(f"[batch {batch_num}/{num_batches}] {len(batch)} patient(s)")
        batch_scratch = scratch_root / f"batch_{batch_num:04d}"
        if batch_scratch.exists():
            shutil.rmtree(batch_scratch)
        batch_scratch.mkdir(parents=True)
        script_text, checks = build_script(kb, batch, batch_scratch,
                                            enabled_rules=enabled_rules, progress=progress)
        # Scaled by TOTAL ALARM COUNT in the batch, not patient count —
        # real-corpus patients have wildly uneven alarm density (confirmed
        # directly: one real patient carried 25,793 alarms against ~10-16
        # for the small fabricated/excerpt datasets), so a handful of
        # patients can still mean a huge script. A patient-count-scaled
        # timeout (30s/patient) genuinely timed out RDFox mid-execution on
        # a real 5-patient/44,375-alarm batch at its 150s cap. ~10ms/alarm
        # gives real headroom over that observed case without being
        # wastefully large for small batches.
        #
        # Floor raised 120s -> 600s -> 1500s (this session): even single-patient
        # batches (batch_size=1) were still timing out at 120s on patients
        # with a large concurrently-active cluster on one device (e.g.
        # patient 2826's ABP-verkleinen storm, 59 concurrent alarms) —
        # cat1a/cat1b's cross-alarm consolidation (see cat1a_signal_
        # quality.dlog's own header) genuinely needs independent per-hop
        # graph variables for correctness, so unlike cat2a/cat2b this cost
        # has no query-shape fix yet. 600s is a stopgap to let those
        # patients actually finish instead of silently truncating results —
        # not a fix for the underlying cost.
        total_alarms = sum(len(events) for events in batch.values())
        timeout = max(1500, total_alarms // 100)
        batch_counts, batch_timings = execute_script(script_text, checks, batch_scratch,
                                                      timeout=timeout, patients=batch)
        counts_by_check.update(batch_counts)
        timings_by_check.update(batch_timings)
    return counts_by_check, timings_by_check


def count_alarms_fired(check_items, kind: str) -> int:
    """"flaggedLikelyFalsePositive" and "silencedBy" can each have
    MULTIPLE checks entries for the SAME alarm (one per contributing
    rule — cat1a/cat1b, cat2a/cat2b respectively), emitted as separate
    queries rather than UNIONed into one (see build_script's own comment
    on why — a real, confirmed RDFox planner regression for at least the
    cat2a/cat2b combination, not a style choice). Every checks tuple is
    now 5-element — (patient, start, kind, rule_name, ei) — the rule_name
    so cat1a/cat1b and cat2a/cat2b don't collide as the same dict key,
    and the per-alarm sequence index `ei` so two alarms sharing the exact
    same `start` instant don't either (see build_script's own comment at
    its checks.append call sites). This counts each alarm AT MOST ONCE
    regardless of how many of its rules fired, matching the old
    single-UNIONed-query "limit 1" semantics exactly. "impliesClinicalEvent"
    still has exactly one checks entry per alarm, so summing its values
    directly is already correct — this helper is a no-op harmless superset
    for that case."""
    fired = set()
    for k, v in check_items:
        if k[2] != kind or v <= 0:
            continue
        fired.add((k[0], k[1]))
    return len(fired)


def count_cat3_episodes(counts_by_check: dict, kind: str) -> dict:
    """Rising-edge episode count per patient for a CAT3 coincidence check
    (kind="cat3aCoincidence" or "cat3bCoincidence") — mirrors the older
    rdflib POC's open_pairs dedup (CODE/evaluation_poc/mda_poc_
    assessment.py's run()): the underlying coincidence re-fires on EVERY
    arrival for as long as both conditions hold, so counting every hit
    would count one arrest/failure many times over (once per alarm that
    happens to arrive while it's still true). This counts only FALSE->TRUE
    transitions — one per genuine episode — matching that module's own
    "a patient who arrests, recovers, and arrests again hours later had
    TWO events, not one" reasoning.

    Relies on counts_by_check's insertion order matching each patient's own
    alarm arrival order — true for every checks entry build_script ever
    appends (one patient fully processed before the next starts, alarms
    within a patient always in .start order; Python dicts preserve
    insertion order).

    Simplification versus the older POC's per-(cardiacNode,respiratoryNode)
    -pair (or (deviceNode,processNode)-pair) dedup: tracks one boolean per
    patient, not a set of pairs. Safe here because both check queries
    already `limit 1` (build_script) — a genuine set of SIMULTANEOUSLY
    open, DISTINCT pairs for one patient is not something this project's
    current single-Referential-concept-per-condition domain (one shared
    physiologicalProcess:CardiacContraction-style IRI per concept) can
    actually produce; there is at most one possible pair open at a time."""
    episodes: dict = {}
    open_state: dict = {}
    for key, count in counts_by_check.items():
        if key[2] != kind:
            continue
        patient = key[0]
        now_open = count > 0
        if now_open and not open_state.get(patient, False):
            episodes[patient] = episodes.get(patient, 0) + 1
        open_state[patient] = now_open
    return episodes


def summarize_rule_timings(counts_by_check: dict, timings_by_check: dict, print_it: bool = True) -> dict:
    """Per-rule breakdown across a whole run: how many times each
    on-demand check (cat1a/cat1b/cat2a/cat2b) or the combined standing-
    rule read (impliesClinicalEvent, tagged "clinical") was evaluated,
    how many of those evaluations actually matched ("hits"), total time
    RDFox itself reports spending on that check's queries, and average
    time per invocation vs. average time per hit — the latter is what
    actually answers "does this rule get slower when it fires, or is
    cost independent of outcome." Keyed by (kind, name) — e.g.
    ("silencedBy", "cat2a") — since every checks tuple now carries a rule
    name as its 4th element (see build_script's own comment on why:
    keeping every on-demand check as its own separate query, one rule
    name per query, both to avoid the confirmed cat2a/cat2b UNION
    regression and to make exactly this kind of per-rule instrumentation
    possible without extra bookkeeping)."""
    groups: dict = {}
    for key, t in timings_by_check.items():
        kind, name = key[2], key[3]
        g = groups.setdefault((kind, name), {"invocations": 0, "hits": 0, "total_s": 0.0, "hit_s": 0.0})
        g["invocations"] += 1
        if t is not None:
            g["total_s"] += t
        n = counts_by_check.get(key, 0)
        if n and n > 0:
            g["hits"] += 1
            if t is not None:
                g["hit_s"] += t

    if print_it:
        print("\nPer-rule timing summary:")
        print(f"  {'rule':<28} {'invocations':>12} {'hits':>8} {'total_s':>10} "
              f"{'avg_s/call':>12} {'avg_s/hit':>10}")
        for (kind, name), g in sorted(groups.items()):
            avg_call = g["total_s"] / g["invocations"] if g["invocations"] else 0.0
            avg_hit = g["hit_s"] / g["hits"] if g["hits"] else 0.0
            print(f"  {kind + '/' + name:<28} {g['invocations']:>12} {g['hits']:>8} "
                  f"{g['total_s']:>10.3f} {avg_call:>12.5f} {avg_hit:>10.5f}")
    return groups


def run():
    scratch = ROOT / "MDA-POC-RDFox" / "engine" / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    kb = M.load_kb()

    events = load_events(DATASET)
    groups = group_by_patient(events)
    in_scope = {p: evs for p, evs in groups.items() if p in EXPECTED}

    script_text, checks = build_script(kb, in_scope, scratch)
    counts_by_check, timings_by_check = execute_script(script_text, checks, scratch, patients=in_scope)
    summarize_rule_timings(counts_by_check, timings_by_check)

    # Computed once across the whole run, not per patient — see
    # count_cat3_episodes' own docstring on why it relies on
    # counts_by_check's insertion order (patient-then-arrival-order), which
    # a per-patient filtered slice would still preserve, but there is no
    # reason to recompute it once per patient when one pass already answers
    # every patient's episode count.
    cat3a_episodes = count_cat3_episodes(counts_by_check, "cat3aCoincidence")
    cat3b_episodes = count_cat3_episodes(counts_by_check, "cat3bCoincidence")

    total = passed = 0
    for patient in in_scope:
        per_patient = {k: v for k, v in counts_by_check.items() if k[0] == patient}
        n_flag = count_alarms_fired(per_patient.items(), "flaggedLikelyFalsePositive")
        n_silence = count_alarms_fired(per_patient.items(), "silencedBy")
        fired = (n_flag > 0) or (n_silence > 0)
        expected = EXPECTED[patient]
        total += 1
        status = "PASS" if fired == expected else "FAIL"
        passed += status == "PASS"
        print(f"[{status}] {patient}: expected fire={expected}, got flagged={n_flag} silenced={n_silence}")

        if patient in EXPECTED_CAT3A:
            n_cat3a = cat3a_episodes.get(patient, 0)
            fired3a = n_cat3a > 0
            expected3a = EXPECTED_CAT3A[patient]
            total += 1
            status3a = "PASS" if fired3a == expected3a else "FAIL"
            passed += status3a == "PASS"
            print(f"[{status3a}] {patient} (cat3a): expected fire={expected3a}, got episodes={n_cat3a}")

        if patient in EXPECTED_CAT3B:
            n_cat3b = cat3b_episodes.get(patient, 0)
            fired3b = n_cat3b > 0
            expected3b = EXPECTED_CAT3B[patient]
            total += 1
            status3b = "PASS" if fired3b == expected3b else "FAIL"
            passed += status3b == "PASS"
            print(f"[{status3b}] {patient} (cat3b): expected fire={expected3b}, got episodes={n_cat3b}")

    print(f"\n{passed}/{total} patients matched expected outcome")


if __name__ == "__main__":
    run()
