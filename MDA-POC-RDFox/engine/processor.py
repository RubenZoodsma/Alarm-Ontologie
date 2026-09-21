"""
processor.py — the query processor (RSP-QL): consumes the stream
(stream.py) and emits, per element, the RDFox commands that bring the
store and the rules' results up to date, in order.

At an instant t, in this order:
  1. landmark windows closing at or before t: drop those persistent graphs
     (windows.py), then re-evaluate the episodes they could support, at
     their own closing time;
  2. the AlarmEnds at t (stream.py orders them first): drop ALL their
     transient graphs, then lift the CAT2 silences they were the last
     justification for, then re-evaluate the episodes once;
  3. each AlarmArrival at t: insert it (two phases), run its CAT1 and CAT2
     checks with their flags and silences, then evaluate the episodes.
Ending every alarm at t before any lift reproduces "the silenced alarm is
still active strictly after t" without knowing anyone's end in advance.

The script is written in full before anything runs (execution.py), so
this module never reacts to a query result. The same Processor handles one
patient's stream (build_script, the replay) or several patients interleaved
in one store (validation/clinical_events_cross_patient.py) — as a live feed
would deliver them.
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
from rules import (ALARMPRIO, RULES, _alarm_functional_unit,
                   _alarm_metric_types, enabled_event_rules, kinds_supported_by_metric, relevant_kinds)
from stream import AlarmArrival, AlarmEnd, replay_stream
from windows import WindowOperator


class Processor:
    """Turns stream elements into RDFox commands (`lines`) and records the
    CAT1/CAT2 checks it emitted (`checks`, matched to their results by
    execution.execute_script)."""

    def __init__(self, kb, scratch_dir: Path, rule_names, verify_identity: bool = False):
        self.kb = kb
        self.scratch = scratch_dir
        self.lines: list = []
        self.checks: list = []
        self.verify_identity = verify_identity
        self._file_counter = itertools.count(1)
        self._windows: dict = {}       # patient -> WindowOperator
        self._arrived: dict = {}       # patient -> [AlarmArrival] so far
        # Which rules are enabled, grouped by what their result does.
        self.flag_rules = [n for n in ("cat1a", "cat1b") if n in rule_names]
        self.silence_rules = [n for n in ("cat2a", "cat2b") if n in rule_names]
        # Episode rules in evaluation order; raises if a combined rule is
        # enabled without its constituents.
        self.event_rules = enabled_event_rules(rule_names)

    def window(self, patient: str) -> WindowOperator:
        if patient not in self._windows:
            self._windows[patient] = WindowOperator(self.kb, self.scratch, self._file_counter)
            self._arrived[patient] = []
        return self._windows[patient]

    # ----------------------------------------------------------------
    # The stream
    # ----------------------------------------------------------------

    def feed(self, elements, on_arrival=None) -> None:
        """Process `elements` (in stream order). Consecutive AlarmEnds of one
        patient at one instant are handled together. `on_arrival(patient)`
        is called after each arrival (progress reporting)."""
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
        """Close every landmark window due at or before `up_to` (all when
        None), across patients, in time order."""
        closing = []
        for patient, window in self._windows.items():
            closing += [(due, patient, alarms) for due, alarms in window.due(up_to)]
        for due, patient, alarms in sorted(closing, key=lambda c: c[0]):
            for alarm in alarms:
                self.lines += WindowOperator.expire(alarm)
            kinds = frozenset().union(*(a.kinds for a in alarms))
            self.lines += evaluate_commands(kinds, self.event_rules, patient, due)

    def on_ends(self, patient: str, when, ends: list) -> None:
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
        kb, patient = self.kb, e.patient
        window = self.window(patient)
        arrived = self._arrived[patient]
        arrived.append(e)
        ei = len(arrived)  # this alarm's per-patient sequence number

        M.update_identity(kb, e, window.identity_tracker)
        identity = window.identity_tracker.identity
        if self.verify_identity:
            batch_identity = M.resolve_identity(kb, arrived)
            assert identity == batch_identity, (
                f"incremental identity tracker diverged from resolve_identity's batch "
                f"computation for {patient} at {e.label}@{e.start}")

        event_kinds = (frozenset(relevant_kinds(kb, e.label, _alarm_metric_types(kb, e), self.event_rules))
                       if self.event_rules else frozenset())
        commands, pending = window.on_arrive(e, identity, event_kinds)
        self.lines += commands

        # Every rule is evaluated on demand, at this moment (rules.py's
        # docstring: why not standing Datalog), bound to THIS alarm (?alarm)
        # and this moment (?now). This runs BETWEEN the insert's two phases:
        # the arriving alarm's own hasPriority triple is not in the store
        # yet. Each rule is its own query, never a UNION of rules: that
        # keeps each rule individually timed (summarize_rule_timings), and
        # RDFox's planner handled a cat2a/cat2b UNION badly (patient 2826,
        # alarms 0-900: ~2-3 s each alone, ~90-97 s as one UNION). A check
        # key carries the rule name and `ei`, so neither two rules nor two
        # alarms sharing a start instant collide.
        alarm = pending["alarm"]
        now = dt(e.start)
        for name in self.flag_rules:
            self.lines += trace_block(f"check {len(self.checks)}", check(name, values(alarm=iri(alarm), now=now)))
            self.checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
        # Store the flags (actions.flag_insert): clinical events ignore
        # flagged alarms, and a later alarm on a cat1b flag's IBP pathway
        # withdraws it. Only heart-rate alarms can be an asystole — a cheap
        # gate for cat1b; the condition decides.
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

        silence_rules = self.silence_rules
        prio = pending["incoming_prio"]
        if "cat2a" in silence_rules:
            # An Unknown priority cannot be shown to be equal or lower than
            # anything: never silenced by CAT2a (agreed 2026-09-21).
            if prio is None or str(prio) == f"{ALARMPRIO}Unknown":
                silence_rules = [n for n in silence_rules if n != "cat2a"]
        for name in silence_rules:
            self.lines += trace_block(f"check {len(self.checks)}",
                                      check(name, silence_bindings(name, alarm, now, prio)))
            self.lines.append(silence_insert(name, alarm, pending["tgraph"], now, prio))
            self.checks.append((patient, e.start, "silencedBy", name, ei))

        self.lines += WindowOperator.complete(pending)
        # This arrival may start or extend a clinical event of this patient
        # (or, through a cat1b withdrawal, let a heart-rate alarm count).
        self.lines += evaluate_commands(event_kinds | withdraw_kinds, self.event_rules, patient, e.start)
        self.lines.append(f"echo ALARM_DONE:{patient}")


def script_header(dstore: str) -> list:
    """Create the store, load the framework, set the output format and the
    prefixes."""
    return ([f"dstore create {dstore}", f"active {dstore}"]
            + [f"import {f}" for f in FRAMEWORK_FILES] + SCRIPT_PREAMBLE)


def build_script(kb, patients: dict, scratch_dir: Path, enabled_rules=None,
                  progress: bool = True, verify_identity: bool = False,
                  dstore: str = "poc") -> tuple:
    """(script text, checks) for replaying each patient's alarms, one
    patient after another, in ONE dstore.

    `enabled_rules`: iterable of rules.RULES names, or None for all.

    `dstore`: shared by every patient in `patients` and populated with the
    framework once. Memory, not import time, was the cost of one dstore per
    patient (a copy of the ~3000-triple framework each). Safe because every
    minted entity/alarm/message IRI is patient-scoped (mint.py).

    `progress`: a per-patient header line, then an in-place progress bar
    (a real patient can carry tens of thousands of alarms; one real-corpus
    patient had 25,793).

    `verify_identity`: development-only — also run the batch
    M.resolve_identity over the alarms arrived so far and assert it matches
    the incremental tracker. Doubles identity-resolution cost; only turn on
    to re-confirm the equivalence after touching either implementation."""
    rule_names = list(RULES) if enabled_rules is None else list(enabled_rules)
    proc = Processor(kb, scratch_dir, rule_names, verify_identity)
    lines = script_header(dstore)
    t0 = time.monotonic()
    num_patients = len(patients)

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        n_events = len(events)
        if progress:
            print(f"  [{time.monotonic() - t0:7.1f}s] minting patient {pi}/{num_patients} "
                  f"({patient}): {n_events} alarm(s)")
        # RDFox's `echo` prints its tokens as one exact line: an unambiguous
        # per-patient/per-alarm marker for execute_script's live progress.
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
        # quality.rq's own header) genuinely needs independent per-hop
        # graph variables for correctness, so unlike cat2a/cat2b this cost
        # has no query-shape fix yet. 600s is a stopgap to let those
        # patients actually finish instead of silently truncating results —
        # not a fix for the underlying cost.
        total_alarms = sum(len(events) for events in batch.values())
        timeout = max(1500, total_alarms // 100)
        batch_counts, batch_timings = execute_script(script_text, checks, batch_scratch,
                                                      timeout=timeout, patients=batch,
                                                      trace=trace)
        counts_by_check.update(batch_counts)
        timings_by_check.update(batch_timings)
    return counts_by_check, timings_by_check
