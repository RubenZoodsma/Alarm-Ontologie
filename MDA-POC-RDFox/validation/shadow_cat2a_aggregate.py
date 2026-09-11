"""
shadow_cat2a_aggregate.py — SHADOW, EXPERIMENTAL. NOT wired into
engine/replay_driver.py's production path; nothing in poc_entry.py or the
live pipeline imports this file. Standalone, following the project's own
"Phase 0 shadow" precedent (validation/phase0_shadow_reification.py):
build the redesign in parallel, validate it by equivalence against the
existing, already-trusted pipeline, before ever proposing to promote it.

What this replaces: cat2a_process_priority.dlog's on-demand 14-atom
self-join ("does an active alarm with rank >= mine exist for the same
physiological process") — confirmed this session to be the actual
concurrency-scaling cost driver behind the patient-2826 replay pathology
(isolated cleanly: disabling only cat2a/cat2b reproduces the exact
climbing-cost pattern; disabling everything else does not).

What it uses instead: representation/rules/shadow/
cat2a_priority_aggregate.dlog — a standing AGGREGATE rule maintaining
MAX(activeRank) per (patient, process), confirmed empirically (this
session's spike work) to update in genuinely constant time regardless of
how many alarms currently share that key (flat ~2-3ms per churn cycle
from group sizes 10 through 1500, including with graph-scoped members
retracted via real whole-graph DELETE WHERE). The arriving alarm's own
`hasPriority` triple is deliberately held back (a two-phase insert) until
AFTER the aggregate is checked, because a MAX aggregate can't be
decomposed after the fact to "exclude me" the way a COUNT can — see the
shadow rule file's own header for the full reasoning, and cat1b/cat2b's
shadow rule headers (representation/rules/shadow/) for why THEY don't
need this two-phase treatment.

Deliberately does NOT inherit engine/replay_driver.py's Driver class or
its GC-batching mechanism (a separate, not-yet-validated redesign) — this
harness reimplements plain, immediate per-alarm transient/persistent
graph drops (the pre-batching semantics) so the cat2a aggregate rewrite
is validated in isolation, not confounded with a second, independent
experimental change.

Validates:
  1. Correctness — cat2a_pos/cat2a_neg fabricated fixtures (DATA/
     CAT_evaluation/events_data.csv) must fire/not-fire identically to
     the production on-demand query.
  2. Performance — replayed against the same real patient-2826 window
     that exposed the concurrency-scaling cost (alarms ~3636-3680),
     compared to the production on-demand query's timing on the
     identical window, with cat1a/cat1b/cat2b disabled on both sides so
     the comparison isolates cat2a specifically.
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "MDA-POC-RDFox" / "engine"))
import mint as M  # noqa: E402
import replay_driver as R  # noqa: E402

SHADOW_RULES_DIR = ROOT / "MDA-POC-RDFox" / "representation" / "rules" / "shadow"
SHADOW_CAT2A_RULE = SHADOW_RULES_DIR / "cat2a_priority_aggregate.dlog"
WINDOW = R.WINDOW


class ShadowDriver:
    """Standalone -- see this module's own docstring for why it does not
    reuse engine.replay_driver.Driver's (GC-batched) implementation."""

    def __init__(self, kb, scratch_dir: Path, file_counter):
        self.kb = kb
        self.scratch = scratch_dir
        self.commands: list[str] = []
        self.pending: list[tuple] = []
        self.last_wins_stack: dict = {}
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

    def insert_alarm_shadow_cat2a(self, event: R.Event, identity: dict) -> None:
        """Two-phase insert + standing-aggregate check for cat2a's
        silencedBy. Emits: phase-1 import (everything except the arriving
        alarm's own hasPriority triple), the shadow check query, phase-2
        import (just the withheld hasPriority triple), then the usual
        scheduled drops -- mirroring Driver.insert_alarm's shape exactly,
        minus GC batching (see module docstring)."""
        kb = self.kb
        alarm_uri = M.alarm_iri(event.patient, event.device_id, event.start)
        alarm = str(alarm_uri)
        tgraph = R.graph_iri(alarm, "transient")
        pgraph = R.graph_iri(alarm, "persistent")

        msg_graph = M.alarm_message(kb, event, identity)
        M.add_triggered_by(msg_graph, [event], kb, identity)
        cond_graph = M.condition_for_event(kb, event.patient, event.label, event.device_id, identity)
        bg_graph = M.background_for_key(kb, event.patient, event.label, event.device_id, identity)

        incoming_prio = msg_graph.value(alarm_uri, M.MDA.hasPriority)

        all_msg_triples = R.graph_triples(msg_graph)
        priority_triple = None
        phase1_msg_triples = []
        prio_pred = f"<{M.MDA}hasPriority>"
        for t in all_msg_triples:
            _, pred = R.subject_predicate(t)
            if pred == prio_pred:
                priority_triple = t
            else:
                phase1_msg_triples.append(t)

        transient_triples_phase1 = phase1_msg_triples + R.graph_triples(cond_graph)
        full_transient_triples = all_msg_triples + R.graph_triples(cond_graph)
        patient = M.patient_iri(event.patient)
        dev = M.device_iri(event.patient, event.device_id)
        persistent_triples = R.graph_triples(bg_graph) + [f"<{patient}> <{M.MDA}isMonitoredBy> <{dev}> ."]

        # last_wins conflict handling, identical in shape to Driver's own
        # (replay_driver.py:266-275) -- unaffected by holding hasPriority
        # back, since hasPriority is never a last_wins-tagged predicate.
        for triple in list(full_transient_triples):
            subj, pred = R.subject_predicate(triple)
            if pred not in self.kb.last_wins_str:
                continue
            key = (subj, pred)
            stack = self.last_wins_stack.setdefault(key, [])
            if stack:
                _, old_graph, old_triple = stack[-1]
                self.commands.append(f"DELETE WHERE {{ GRAPH {old_graph} {{ {old_triple} }} }}")
            stack.append((event.start, tgraph, triple))

        metadata = R.validity_triples(tgraph, event.start, event.end) + \
            R.validity_triples(pgraph, event.start, event.end + WINDOW)
        path1 = self._write_trig({tgraph: transient_triples_phase1, pgraph: persistent_triples}, bare=metadata)
        self.commands.append(f"# arrive (phase1): {event.label} @ {event.device_id} {event.start.isoformat()}")
        self.commands.append(f"import {path1}")

        if priority_triple is not None:
            check_query = (
                f"select ?maxRank ?incomingRank where {{ "
                f"GRAPH ?g1 {{ <{alarm}> <{M.MDA}hasMessage> ?msg }} "
                f"GRAPH ?g2 {{ ?msg <{M.MDA}triggeredBy> ?device }} "
                f"GRAPH ?g3 {{ ?device <{M.MDA}hasSensor> ?sensor }} "
                f"GRAPH ?g4 {{ ?sensor <{M.MDA}sensorProducesSignal> ?signal }} "
                f"GRAPH ?g5 {{ ?signal <{M.MDA}analyzedBy> ?analysis }} "
                f"GRAPH ?g6 {{ ?analysis <{M.MDA}producesMetric> ?metric }} "
                f"GRAPH ?g7 {{ ?metric <{M.MDA}approximates> ?property }} "
                f"?property <{M.MDA}isPropertyOf> ?process . "
                f'BIND(IRI(CONCAT(STR(<{patient}>), "|", STR(?process))) AS ?ppKey) '
                f"?ppKey <{M.MDA}shadowMaxActiveRank> ?maxRank . "
                f"<{incoming_prio}> <{M.MDA}priorityRank> ?incomingRank . "
                f"FILTER(?maxRank >= ?incomingRank) "
                f"}} limit 1"
            )
            self.commands.append(check_query)
            self.commands.append(f"# complete (phase2): {event.label} @ {event.device_id}")
            self.commands.append(f"INSERT DATA {{ GRAPH {tgraph} {{ {priority_triple} }} }}")
        else:
            # No hasPriority minted for this archetype at all -- cat2a can
            # never fire for it (mirrors APPROXIMATES_COVERED_METRIC_TYPES's
            # own dead-end short-circuit). Emit a query guaranteed to
            # answer 0 rows, so checks/answer-count bookkeeping stays
            # aligned 1:1 with the production run's check list either way.
            self.commands.append(f"select ?x where {{ FILTER(false) }}")

        self.schedule(event.end, self._drop_transient_cmd(event, tgraph, full_transient_triples))
        self.schedule(event.end + WINDOW, self._drop_persistent_cmd(event, pgraph))

    def _drop_transient_cmd(self, event: R.Event, graph: str, transient_triples: list) -> str:
        restores = []
        for triple in transient_triples:
            subj, pred = R.subject_predicate(triple)
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

    def _drop_persistent_cmd(self, event: R.Event, graph: str) -> str:
        return (f"# window-expiry: {event.label} @ {event.device_id}\n"
                f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}")


def build_shadow_script(kb, patients: dict, scratch_dir: Path, dstore: str = "shadow") -> tuple[str, list]:
    import itertools
    file_counter = itertools.count(1)
    lines = [f"dstore create {dstore}", f"active {dstore}"]
    for f in R.FRAMEWORK_FILES:
        lines.append(f"import {f}")
    lines.append(f"import {SHADOW_CAT2A_RULE}")

    checks = []
    for patient, events in patients.items():
        driver = ShadowDriver(kb, scratch_dir, file_counter)
        events_sorted = sorted(events, key=lambda ev: ev.start)
        lines.append(f"echo PATIENT_START:{patient}")
        for e in events_sorted:
            driver.flush_due(e.start)
            M.update_identity(kb, e, driver.identity_tracker)
            identity = driver.identity_tracker.identity
            driver.insert_alarm_shadow_cat2a(e, identity)
            lines.extend(driver.commands)
            driver.commands.clear()
            checks.append((patient, e.start, "silencedBy"))
            lines.append(f"echo ALARM_DONE:{patient}")
        driver.flush_all()
        lines.extend(driver.commands)
    lines.append("quit")
    return "\n".join(lines), checks


def load_real_events(dataset_path: Path, kb, patient_id: str, start_idx: int, end_idx: int) -> list:
    events = []
    with open(dataset_path, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader)
        for row in reader:
            patient, label, device_id, start, end = row
            if patient != patient_id or label not in kb.type_index:
                continue
            events.append(R.Event(patient, label, device_id,
                                   datetime.fromisoformat(start), datetime.fromisoformat(end)))
    events.sort(key=lambda e: e.start)
    return events[start_idx:end_idx]


def run_correctness_check(kb, scratch_root: Path) -> bool:
    """cat2a_pos/cat2a_neg fabricated fixtures, shadow rewrite only
    (cat1a/cat1b/cat2b irrelevant to these two fixtures' expectation)."""
    events = R.load_events(R.DATASET)
    groups = R.group_by_patient(events)
    in_scope = {p: evs for p, evs in groups.items() if p in ("cat2a_pos", "cat2a_neg")}

    scratch = scratch_root / "correctness"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    script_text, checks = build_shadow_script(kb, in_scope, scratch)
    counts = R.execute_script(script_text, checks, scratch, progress=False)

    ok = True
    for patient, expected in (("cat2a_pos", True), ("cat2a_neg", False)):
        n = sum(v for k, v in counts.items() if k[0] == patient and k[2] == "silencedBy")
        fired = n > 0
        status = "PASS" if fired == expected else "FAIL"
        ok = ok and (status == "PASS")
        print(f"[{status}] {patient}: expected fire={expected}, got silencedBy_count={n}")
    return ok


def run_performance_comparison(kb, scratch_root: Path, patient_id: str, start_idx: int, end_idx: int):
    dataset = ROOT / "DATA" / "POC_EVENTS" / "DATA_LOCKED.csv"
    events = load_real_events(dataset, kb, patient_id, start_idx, end_idx)
    print(f"performance window: patient {patient_id} [{start_idx}:{end_idx}] ({len(events)} alarms)")

    # --- shadow (aggregate rewrite) ---
    scratch_shadow = scratch_root / "perf_shadow"
    if scratch_shadow.exists():
        shutil.rmtree(scratch_shadow)
    scratch_shadow.mkdir(parents=True)
    script_shadow, checks_shadow = build_shadow_script(kb, {patient_id: events}, scratch_shadow)
    t0 = time.monotonic()
    R.execute_script(script_shadow, checks_shadow, scratch_shadow, timeout=180, progress=False)
    t_shadow = time.monotonic() - t0
    print(f"[shadow/aggregate] total={t_shadow:.2f}s for {len(events)} alarms")

    # --- production (cat2a only, cat1a/1b/2b disabled, matching this
    # session's earlier cat2_only isolation methodology) ---
    scratch_prod = scratch_root / "perf_prod"
    if scratch_prod.exists():
        shutil.rmtree(scratch_prod)
    scratch_prod.mkdir(parents=True)
    script_prod, checks_prod = R.build_script(kb, {patient_id: events}, scratch_prod,
                                               enabled_rules={"cat2a"}, progress=False)
    t0 = time.monotonic()
    R.execute_script(script_prod, checks_prod, scratch_prod, timeout=180, progress=False)
    t_prod = time.monotonic() - t0
    print(f"[production/self-join] total={t_prod:.2f}s for {len(events)} alarms")

    print(f"\nRESULT: shadow={t_shadow:.2f}s  production={t_prod:.2f}s  "
          f"speedup={t_prod / max(t_shadow, 0.001):.1f}x")


if __name__ == "__main__":
    kb = M.load_kb()
    scratch_root = ROOT / "MDA-POC-RDFox" / "_scratch" / "shadow_cat2a"

    print("=== correctness: cat2a_pos/cat2a_neg ===")
    ok = run_correctness_check(kb, scratch_root)
    print()

    if ok:
        print("=== performance: patient 2826, alarms [3600:3700] ===")
        run_performance_comparison(kb, scratch_root, "2826", 3600, 3700)
    else:
        print("Correctness check failed -- not running the performance comparison.")
