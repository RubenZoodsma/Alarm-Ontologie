"""
shadow_common.py — shared plumbing for the shadow aggregate-rewrite
harnesses (shadow_cat2a_aggregate.py, shadow_cat1b_aggregate.py,
shadow_cat2b_aggregate.py). SHADOW, EXPERIMENTAL. Not imported by
engine/replay_driver.py or poc_entry.py.

ShadowDriver mints transient/persistent graphs exactly like
engine.replay_driver.Driver (same mint.py calls, same graph IRIs,
same last_wins handling), but deliberately WITHOUT that module's GC
batching (a separate, not-yet-validated redesign — see the plan at
/Users/rzoodsm2/.claude/plans/toasty-finding-mitten.md) and without any
per-rule check logic baked in, so each rule's harness only has to supply
its own check-query shape and ordering (cat2a's two-phase insert is the
one rule whose check can't just be "insert everything, then look up the
aggregate" — see its own harness for why).
"""
from __future__ import annotations

import csv
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "MDA-POC-RDFox" / "engine"))
import mint as M  # noqa: E402
import replay_driver as R  # noqa: E402

SHADOW_RULES_DIR = ROOT / "MDA-POC-RDFox" / "representation" / "rules" / "shadow"
WINDOW = R.WINDOW


class ShadowDriver:
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

    def insert_alarm_full(self, event: R.Event, identity: dict) -> tuple:
        """Ordinary, single-phase insert — everything about this alarm
        lands in the store atomically, exactly like production
        Driver.insert_alarm (minus GC batching). Returns
        (alarm, tgraph, pgraph, patient_iri_str) for the caller to build
        its own check query against the now-fully-present data. Use this
        for any rule whose check is safe to run AFTER a normal insert
        (cat1b, cat2b — see their own harnesses for why); cat2a needs its
        own two-phase variant instead (see shadow_cat2a_aggregate.py)."""
        kb = self.kb
        alarm_uri = M.alarm_iri(event.patient, event.device_id, event.start)
        alarm = str(alarm_uri)
        tgraph = R.graph_iri(alarm, "transient")
        pgraph = R.graph_iri(alarm, "persistent")

        msg_graph = M.alarm_message(kb, event, identity)
        M.add_triggered_by(msg_graph, [event], kb, identity)
        cond_graph = M.condition_for_event(kb, event.patient, event.label, event.device_id, identity)
        bg_graph = M.background_for_key(kb, event.patient, event.label, event.device_id, identity)

        transient_triples = R.graph_triples(msg_graph) + R.graph_triples(cond_graph)
        patient = M.patient_iri(event.patient)
        dev = M.device_iri(event.patient, event.device_id)
        persistent_triples = R.graph_triples(bg_graph) + [f"<{patient}> <{M.MDA}isMonitoredBy> <{dev}> ."]

        for triple in list(transient_triples):
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
        path = self._write_trig({tgraph: transient_triples, pgraph: persistent_triples}, bare=metadata)
        self.commands.append(f"# arrive: {event.label} @ {event.device_id} {event.start.isoformat()}")
        self.commands.append(f"import {path}")

        self.schedule(event.end, self._drop_transient_cmd(event, tgraph, transient_triples))
        self.schedule(event.end + WINDOW, self._drop_persistent_cmd(event, pgraph))

        return alarm, tgraph, pgraph, str(patient)

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


def build_shadow_script(kb, patients: dict, scratch_dir: Path, shadow_rule_path: Path,
                         check_query_builder, check_kind: str, dstore: str = "shadow") -> tuple[str, list]:
    """Generic per-alarm loop shared by every single-phase-insert shadow
    rule (cat1b, cat2b). `check_query_builder(alarm, tgraph, pgraph,
    patient_iri_str) -> str` returns the full SPARQL check-query text for
    one alarm, run immediately after that alarm's own full insert."""
    import itertools
    file_counter = itertools.count(1)
    lines = [f"dstore create {dstore}", f"active {dstore}"]
    for f in R.FRAMEWORK_FILES:
        lines.append(f"import {f}")
    lines.append(f"import {shadow_rule_path}")

    checks = []
    for patient, events in patients.items():
        driver = ShadowDriver(kb, scratch_dir, file_counter)
        events_sorted = sorted(events, key=lambda ev: ev.start)
        lines.append(f"echo PATIENT_START:{patient}")
        for e in events_sorted:
            driver.flush_due(e.start)
            M.update_identity(kb, e, driver.identity_tracker)
            identity = driver.identity_tracker.identity
            alarm, tgraph, pgraph, patient_iri = driver.insert_alarm_full(e, identity)
            lines.extend(driver.commands)
            driver.commands.clear()
            lines.append(check_query_builder(alarm, tgraph, pgraph, patient_iri))
            checks.append((patient, e.start, check_kind))
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


def run_correctness_check(kb, scratch_root: Path, patients: tuple, expected: dict, check_kind: str,
                           shadow_rule_path: Path, check_query_builder) -> bool:
    events = R.load_events(R.DATASET)
    groups = R.group_by_patient(events)
    in_scope = {p: evs for p, evs in groups.items() if p in patients}

    scratch = scratch_root / "correctness"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    script_text, checks = build_shadow_script(kb, in_scope, scratch, shadow_rule_path,
                                               check_query_builder, check_kind)
    counts = R.execute_script(script_text, checks, scratch, progress=False)

    ok = True
    for patient in patients:
        n = sum(v for k, v in counts.items() if k[0] == patient and k[2] == check_kind)
        fired = n > 0
        status = "PASS" if fired == expected[patient] else "FAIL"
        ok = ok and (status == "PASS")
        print(f"[{status}] {patient}: expected fire={expected[patient]}, got {check_kind}_count={n}")
    return ok


def run_performance_comparison(kb, scratch_root: Path, patient_id: str, start_idx: int, end_idx: int,
                                shadow_rule_path: Path, check_query_builder, check_kind: str,
                                production_enabled_rule: str, timeout: int = 180):
    dataset = ROOT / "DATA" / "POC_EVENTS" / "DATA_LOCKED.csv"
    events = load_real_events(dataset, kb, patient_id, start_idx, end_idx)
    print(f"performance window: patient {patient_id} [{start_idx}:{end_idx}] ({len(events)} alarms)")

    scratch_shadow = scratch_root / "perf_shadow"
    if scratch_shadow.exists():
        shutil.rmtree(scratch_shadow)
    scratch_shadow.mkdir(parents=True)
    script_shadow, checks_shadow = build_shadow_script(kb, {patient_id: events}, scratch_shadow,
                                                         shadow_rule_path, check_query_builder, check_kind)
    t0 = time.monotonic()
    try:
        R.execute_script(script_shadow, checks_shadow, scratch_shadow, timeout=timeout, progress=False)
        t_shadow = time.monotonic() - t0
        print(f"[shadow/aggregate] total={t_shadow:.2f}s for {len(events)} alarms")
    except Exception as exc:
        t_shadow = time.monotonic() - t0
        print(f"[shadow/aggregate] DID NOT FINISH within {timeout}s ({exc})")
        t_shadow = None

    scratch_prod = scratch_root / "perf_prod"
    if scratch_prod.exists():
        shutil.rmtree(scratch_prod)
    scratch_prod.mkdir(parents=True)
    script_prod, checks_prod = R.build_script(kb, {patient_id: events}, scratch_prod,
                                               enabled_rules={production_enabled_rule}, progress=False)
    t0 = time.monotonic()
    try:
        R.execute_script(script_prod, checks_prod, scratch_prod, timeout=timeout, progress=False)
        t_prod = time.monotonic() - t0
        print(f"[production] total={t_prod:.2f}s for {len(events)} alarms")
    except Exception as exc:
        t_prod = time.monotonic() - t0
        print(f"[production] DID NOT FINISH within {timeout}s ({exc})")
        t_prod = None

    if t_shadow is not None and t_prod is not None:
        print(f"\nRESULT: shadow={t_shadow:.2f}s  production={t_prod:.2f}s  "
              f"speedup={t_prod / max(t_shadow, 0.001):.1f}x")
    else:
        print(f"\nRESULT: shadow={t_shadow}  production={t_prod} (one or both did not finish)")
