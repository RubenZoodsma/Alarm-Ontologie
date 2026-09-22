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

Live, in lockstep (run_live): each stream element — an arrival, or one
patient's ends at one instant — is minted, sent to RDFox and executed
before the next element is taken. Nothing here reacts to a query result.
Works on one patient's stream or on several interleaved in one store.
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
from execution import FRAMEWORK_FILES, SCRIPT_PREAMBLE, RDFoxSession, _progress_bar, read_results, timed
from rules import (RULES, _alarm_functional_unit,
                   _alarm_metric_types, enabled_event_rules, kinds_supported_by_metric, relevant_kinds)
from stream import AlarmArrival, AlarmEnd, replay_stream
from windows import WindowOperator


class Processor:
    """Turns stream elements into RDFox commands (`lines`, taken by
    take_lines) and records the CAT1/CAT2 checks it emitted (`checks`,
    matched to their results by execution.read_results)."""

    def __init__(self, kb, scratch_dir: Path, rule_names):
        self.kb = kb
        self.scratch = scratch_dir
        self.lines: list = []
        self.checks: list = []
        self._file_counter = itertools.count(1)
        self._invocations = itertools.count(1)   # numbers the timing groups
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

    def feed(self, elements) -> None:
        """Process `elements` in stream order (see stream_units)."""
        for unit in stream_units(elements):
            self.close_windows(unit[0].time)
            if isinstance(unit[0], AlarmEnd):
                self.on_ends(unit[0].patient, unit[0].time, unit)
            else:
                self.on_arrival(unit[0])

    def take_lines(self) -> list:
        """The commands emitted since the last call."""
        lines, self.lines = self.lines, []
        return lines

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
            self.lines += evaluate_commands(kinds, self.event_rules, patient, due, next(self._invocations))

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
                self.lines += timed("cat2_lift", next(self._invocations),
                                    [*trace_block(f"lift {patient} {when.isoformat()}", select), delete])
        kinds = frozenset().union(*(a.kinds for a in ended))
        self.lines += evaluate_commands(kinds, self.event_rules, patient, when, next(self._invocations))

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
        invocation = next(self._invocations)
        # Check, then store the flag: clinical events ignore flagged alarms.
        # cat1b is gated on heart-rate alarms (its condition needs one).
        for name in self.flag_rules:
            if name == "cat1b" and "HeartRate" not in _alarm_metric_types(kb, e):
                continue
            bindings = values(alarm=iri(alarm), now=now)
            self.lines += timed(name, invocation,
                                [*trace_block(f"check {len(self.checks)}", check(name, bindings)),
                                 flag_insert(name, alarm, pending["tgraph"], now)])
            self.checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
        # An alarm on an IBP pathway may withdraw a cat1b flag; the asystole
        # then becomes evidence at this moment.
        withdraw_kinds = frozenset()
        if "cat1b" in self.flag_rules and _alarm_functional_unit(kb, e) == "FU_InvasiveBloodPressure":
            select, delete = flag_withdraw(alarm)
            self.lines += timed("cat1b_withdraw", invocation,
                                [*trace_block(f"withdraw {patient} {e.start.isoformat()}", select), delete])
            withdraw_kinds = kinds_supported_by_metric("HeartRate", self.event_rules)

        prio = pending["incoming_prio"]
        for name in self.silence_rules:
            self.lines += timed(name, invocation,
                                [*trace_block(f"check {len(self.checks)}",
                                              check(name, silence_bindings(name, alarm, now, prio))),
                                 silence_insert(name, alarm, pending["tgraph"], now, prio)])
            self.checks.append((patient, e.start, "silencedBy", name, ei))

        self.lines += WindowOperator.complete(pending)
        # This arrival (or a cat1b withdrawal) may start or extend an event.
        self.lines += evaluate_commands(event_kinds | withdraw_kinds, self.event_rules, patient, e.start,
                                        invocation)


def stream_units(elements: list) -> list:
    """The stream's processing units, in order: each arrival on its own, and
    one patient's consecutive ends at one instant together."""
    units, i = [], 0
    while i < len(elements):
        j = i + 1
        if isinstance(elements[i], AlarmEnd):
            while (j < len(elements) and isinstance(elements[j], AlarmEnd)
                   and elements[j].time == elements[i].time and elements[j].patient == elements[i].patient):
                j += 1
        units.append(elements[i:j])
        i = j
    return units


def script_header(dstore: str) -> list:
    """Create the store, load the framework, set output format and prefixes."""
    return ([f"dstore create {dstore}", f"active {dstore}"]
            + [f"import {f}" for f in FRAMEWORK_FILES] + SCRIPT_PREAMBLE)


def execute(proc: Processor, session: RDFoxSession, where: str) -> bool:
    """Send the processor's pending commands and wait until RDFox has run
    them. Reports an RDFox error with `where` it happened. False once the
    session has stopped."""
    session.send(proc.take_lines())
    received = session.sync()
    for i, line in enumerate(received):
        if line.startswith("An error occurred"):
            print(f"\n  RDFox error at {where}: {' '.join(received[i:i + 2])}")
    return session.stopped is None


def stream_through(proc: Processor, session: RDFoxSession, elements: list, on_arrival=None) -> bool:
    """Feed `elements` unit by unit, each executed by RDFox before the next.
    `on_arrival()`: a progress callback. False once the session has stopped."""
    for unit in stream_units(elements):
        proc.feed(unit)
        if not execute(proc, session, f"{unit[0].patient} {unit[0].time.isoformat()}"):
            return False
        if on_arrival is not None and isinstance(unit[0], AlarmArrival):
            on_arrival()
    return True


def run_live(kb, patients: dict, scratch_dir: Path, enabled_rules=None, progress: bool = True,
             trace: list | None = None, timeout: float = 600, dstore: str = "poc") -> list:
    """Replay each patient's stream, one patient after another, through ONE
    RDFox process and store, live: every element is executed before the
    next is minted. `enabled_rules`: rules.RULES names, or None for all.
    `timeout`: seconds RDFox may take for one element. Returns the
    invocations (execution.parse_invocations); `trace` is extended with the
    trace blocks."""
    rule_names = list(RULES) if enabled_rules is None else list(enabled_rules)
    proc = Processor(kb, scratch_dir, rule_names)
    session = RDFoxSession(scratch_dir, timeout)
    session.send(script_header(dstore))
    session.sync()
    t0 = time.monotonic()

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        n_events, done = len(events), itertools.count(1)
        report_every = max(1, n_events // 100)

        def report():
            k = next(done)
            if progress and (k == 1 or k == n_events or k % report_every == 0):
                print(f"\r  patient {pi}/{len(patients)} ({patient}) {_progress_bar(k, n_events)} "
                      f"{k}/{n_events} alarms [{time.monotonic() - t0:7.1f}s]", end="", flush=True)

        ok = stream_through(proc, session, replay_stream(events), on_arrival=report)
        if ok:
            proc.close_windows()  # the patient's remaining windows
            ok = execute(proc, session, f"{patient}, closing its windows")
        if progress:
            print()
        if not ok:
            print(f"  *** RDFox {session.stopped} during patient {patient}: the run stops here; "
                  f"results below are partial. ***")
            break

    out_lines = session.close()
    return read_results(out_lines, proc.checks, trace, complete=session.stopped is None)


def run_batched(kb, patients: dict, scratch_root: Path, batch_size: "int | None" = None,
                 enabled_rules=None, progress: bool = True, trace: list | None = None,
                 timeout: float = 600) -> list:
    """Run `patients` in batches of `batch_size` (None: one batch), each its
    own live run (run_live) and RDFox process. Returns every batch's
    invocations."""
    items = list(patients.items())
    size = batch_size if batch_size else len(items)
    invocations: list = []
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
        invocations += run_live(kb, batch, batch_scratch, enabled_rules=enabled_rules,
                                progress=progress, trace=trace, timeout=timeout)
    return invocations
