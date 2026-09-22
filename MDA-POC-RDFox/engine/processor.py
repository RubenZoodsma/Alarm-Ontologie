"""
processor.py — the query processor (RSP-QL): turns the stream into the
RDFox commands that keep the store and the rules' results up to date.

At an instant t, in this order:
  1. windows closing at or before t: drop those persistent graphs, then
     re-evaluate the episodes they could support, at their closing time;
  2. the AlarmEnds at t: drop all their transient graphs, then lift the
     CAT2 silences they were the last justification for, then re-evaluate
     the episodes once;
  3. each AlarmArrival at t: insert it (two phases), run its CAT1 and CAT2
     checks and store their flags and silences, then evaluate the episodes.

The whole script is written before it runs (execution.py): nothing here
reacts to a query result. Works on one patient's stream or on several
interleaved in one store.
"""

from __future__ import annotations

import itertools
import shutil
import time
from pathlib import Path

import mint as M
from actions import (check, dt, evaluate_commands, flag_insert, flag_withdraw, iri, silence_bindings,
                     silence_insert, silence_lift, values)
from event_log import trace_block
from execution import FRAMEWORK_FILES, SCRIPT_PREAMBLE, _progress_bar, execute_script
from rules import (RULES, _alarm_functional_unit,
                   _alarm_metric_types, enabled_event_rules, kinds_supported_by_metric, relevant_kinds)
from stream import AlarmArrival, AlarmEnd, replay_stream
from windows import WindowOperator


class Processor:
    """Turns stream elements into RDFox commands (`lines`) and records the
    CAT1/CAT2 checks it emitted (`checks`, matched to their results by
    execution.execute_script)."""

    def __init__(self, kb, scratch_dir: Path, rule_names):
        self.kb = kb
        self.scratch = scratch_dir
        self.lines: list = []
        self.checks: list = []
        self._file_counter = itertools.count(1)
        self._windows: dict = {}       # patient -> WindowOperator
        self._arrived: dict = {}       # patient -> number of arrivals so far
        # Which rules are enabled, grouped by what their result does.
        self.flag_rules = [n for n in ("cat1a", "cat1b") if n in rule_names]
        self.silence_rules = [n for n in ("cat2a", "cat2b") if n in rule_names]
        # Episode rules in evaluation order; raises if a combined rule is
        # enabled without its constituents.
        self.event_rules = enabled_event_rules(rule_names)

    def window(self, patient: str) -> WindowOperator:
        if patient not in self._windows:
            self._windows[patient] = WindowOperator(self.kb, self.scratch, self._file_counter)
            self._arrived[patient] = 0
        return self._windows[patient]

    # ----------------------------------------------------------------
    # The stream
    # ----------------------------------------------------------------

    def feed(self, elements, on_arrival=None) -> None:
        """Process `elements` in stream order; one patient's ends at one
        instant are handled together. `on_arrival(patient)`: a progress
        callback."""
        i = 0
        while i < len(elements):
            element = elements[i]
            self.close_windows(element.time)
            if isinstance(element, AlarmEnd):
                j = i
                while (j < len(elements) and isinstance(elements[j], AlarmEnd)
                       and elements[j].time == element.time and elements[j].patient == element.patient):
                    j += 1
                self.on_ends(element.patient, element.time, elements[i:j])
                i = j
            else:
                self.on_arrival(element)
                if on_arrival is not None:
                    on_arrival(element.patient)
                i += 1

    def close_windows(self, up_to=None) -> None:
        """Close every window due at or before `up_to` (all when None),
        across patients, in time order."""
        closing = []
        for patient, window in self._windows.items():
            closing += [(due, patient, alarms) for due, alarms in window.due(up_to)]
        for due, patient, alarms in sorted(closing, key=lambda c: c[0]):
            for alarm in alarms:
                self.lines += WindowOperator.expire(alarm)
            kinds = frozenset().union(*(a.kinds for a in alarms))
            self.lines += evaluate_commands(kinds, self.event_rules, patient, due)

    def on_ends(self, patient: str, when, ends: list) -> None:
        """Step 2 of the module docstring, for one patient at `when`."""
        window = self.window(patient)
        ended = []
        for end in ends:
            commands, alarm = window.on_end(end)
            self.lines += commands
            ended.append(alarm)
        if self.silence_rules:
            for alarm in ended:
                select, delete = silence_lift(alarm.alarm, when)
                self.lines += trace_block(f"lift {patient} {when.isoformat()}", select)
                self.lines.append(delete)
        kinds = frozenset().union(*(a.kinds for a in ended))
        self.lines += evaluate_commands(kinds, self.event_rules, patient, when)

    def on_arrival(self, e: AlarmArrival) -> None:
        """Step 3 of the module docstring."""
        kb, patient = self.kb, e.patient
        window = self.window(patient)
        self._arrived[patient] += 1
        ei = self._arrived[patient]  # this alarm's per-patient sequence number

        M.update_identity(kb, e, window.identity_tracker)
        identity = window.identity_tracker.identity

        event_kinds = (frozenset(relevant_kinds(kb, e.label, _alarm_metric_types(kb, e), self.event_rules))
                       if self.event_rules else frozenset())
        commands, pending = window.on_arrive(e, identity, event_kinds)
        self.lines += commands

        # Checks run between the insert's two phases, bound to this alarm
        # and this moment. One query per rule, never a UNION: each is timed
        # on its own, and RDFox planned a cat2a/cat2b UNION ~30x slower.
        alarm = pending["alarm"]
        now = dt(e.start)
        for name in self.flag_rules:
            self.lines += trace_block(f"check {len(self.checks)}", check(name, values(alarm=iri(alarm), now=now)))
            self.checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
        # Store the flags: clinical events ignore flagged alarms. cat1b is
        # gated on heart-rate alarms; an IBP alarm may withdraw a cat1b flag.
        withdraw_kinds = frozenset()
        if "cat1a" in self.flag_rules:
            self.lines.append(flag_insert("cat1a", alarm, pending["tgraph"], now))
        if "cat1b" in self.flag_rules:
            if "HeartRate" in _alarm_metric_types(kb, e):
                self.lines.append(flag_insert("cat1b", alarm, pending["tgraph"], now))
            if _alarm_functional_unit(kb, e) == "FU_InvasiveBloodPressure":
                select, delete = flag_withdraw(alarm)
                self.lines += trace_block(f"withdraw {patient} {e.start.isoformat()}", select)
                self.lines.append(delete)
                # A withdrawn asystole becomes evidence at this moment.
                withdraw_kinds = kinds_supported_by_metric("HeartRate", self.event_rules)

        prio = pending["incoming_prio"]
        for name in self.silence_rules:
            self.lines += trace_block(f"check {len(self.checks)}",
                                      check(name, silence_bindings(name, alarm, now, prio)))
            self.lines.append(silence_insert(name, alarm, pending["tgraph"], now, prio))
            self.checks.append((patient, e.start, "silencedBy", name, ei))

        self.lines += WindowOperator.complete(pending)
        # This arrival (or a cat1b withdrawal) may start or extend an event.
        self.lines += evaluate_commands(event_kinds | withdraw_kinds, self.event_rules, patient, e.start)
        self.lines.append(f"echo ALARM_DONE:{patient}")


def script_header(dstore: str) -> list:
    """Create the store, load the framework, set output format and prefixes."""
    return ([f"dstore create {dstore}", f"active {dstore}"]
            + [f"import {f}" for f in FRAMEWORK_FILES] + SCRIPT_PREAMBLE)


def build_script(kb, patients: dict, scratch_dir: Path, enabled_rules=None,
                  progress: bool = True, dstore: str = "poc") -> tuple:
    """(script text, checks): each patient's alarms replayed one patient
    after another, in ONE dstore (IRIs are patient-scoped, mint.py).
    `enabled_rules`: rules.RULES names, or None for all."""
    rule_names = list(RULES) if enabled_rules is None else list(enabled_rules)
    proc = Processor(kb, scratch_dir, rule_names)
    lines = script_header(dstore)
    t0 = time.monotonic()
    num_patients = len(patients)

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        n_events = len(events)
        if progress:
            print(f"  [{time.monotonic() - t0:7.1f}s] minting patient {pi}/{num_patients} "
                  f"({patient}): {n_events} alarm(s)")
        # Progress markers for execute_script.
        proc.lines.append(f"echo PATIENT_START:{patient}")
        report_every = max(1, n_events // 100)
        done = itertools.count(1)

        def report(_patient):
            k = next(done)
            if progress and (k == 1 or k == n_events or k % report_every == 0):
                print(f"\r    {_progress_bar(k, n_events)} {k}/{n_events} alarms "
                      f"[{time.monotonic() - t0:7.1f}s]", end="", flush=True)

        proc.feed(replay_stream(events), on_arrival=report)
        proc.close_windows()  # the patient's remaining landmark windows
        if progress:
            print()

    if progress:
        print(f"  [{time.monotonic() - t0:7.1f}s] minting done for {num_patients} patient(s)")
    lines += proc.lines
    lines.append("quit")
    return "\n".join(lines), proc.checks


def run_batched(kb, patients: dict, scratch_root: Path, batch_size: "int | None" = None,
                 enabled_rules=None, progress: bool = True, trace: list | None = None) -> tuple:
    """Run `patients` in batches of `batch_size` (None: one batch), each
    its own script and RDFox process, bounding memory and disk. Returns the
    merged ({check_key: answer_count}, {check_key: seconds})."""
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
        # Timeout: 10 ms per alarm, at least 1500 s. Alarm counts per
        # patient vary widely (up to ~26k), and a burst of concurrent alarms
        # on one device makes the CAT1 checks slow.
        total_alarms = sum(len(events) for events in batch.values())
        timeout = max(1500, total_alarms // 100)
        batch_counts, batch_timings = execute_script(script_text, checks, batch_scratch,
                                                      timeout=timeout, patients=batch,
                                                      trace=trace)
        counts_by_check.update(batch_counts)
        timings_by_check.update(batch_timings)
    return counts_by_check, timings_by_check
