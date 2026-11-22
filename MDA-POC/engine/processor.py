"""
processor.py — the query processor (RSP-QL): turns the stream into the
engine commands that keep the store and the rules' results up to date.

At an instant t, in this order:
  1. windows closing at or before t: drop those persistent graphs, then
     re-evaluate the episodes they could support, at their closing time;
  2. the AlarmEnds at t: drop all their transient graphs, then lift the
     CAT2 silences they were the last justification for, then re-evaluate
     the episodes once;
  3. each AlarmArrival at t: insert it (two phases), run its CAT1 and CAT2
     checks and store their flags and silences, then evaluate the episodes.

Live, in lockstep (run_live): each stream element — an arrival, or one
patient's ends at one instant — is minted, sent to Oxigraph and executed
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
                     silence_insert, silence_lift, silence_readback, values)
from event_log import LiveLog, alarm_index, trace_block
from execution import (FRAMEWORK_FILES, SCRIPT_PREAMBLE, RuleStats, _progress_bar, make_session, read_blocks,
                       timed)
from rules import (RULES, _alarm_functional_unit,
                   _alarm_metric_types, enabled_event_rules, kinds_supported_by_metric, relevant_kinds)
from stream import AlarmArrival, AlarmEnd, replay_stream
from windows import WindowOperator


class Processor:
    """Turns stream elements into engine commands (`lines`, taken by
    take_lines) and records the CAT1/CAT2 checks it emitted (`checks`,
    removed once LiveRun has read their results)."""

    def __init__(self, kb, scratch_dir: Path, rule_names):
        self.kb = kb
        self.scratch = scratch_dir
        self.lines: list = []
        self.checks: dict = {}                    # check number -> key, until its result is read
        self._check_numbers = itertools.count()
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

        event_kinds = (frozenset(relevant_kinds(kb, e, _alarm_metric_types(kb, e), self.event_rules))
                       if self.event_rules else frozenset())
        commands, pending = window.on_arrive(e, identity, event_kinds)
        self.lines += commands

        # Checks run between the insert's two phases, bound to this alarm
        # and this moment. One query per rule, never a UNION: each is timed
        # on its own, and the former engine (RDFox) planned a cat2a/cat2b UNION ~30x slower.
        alarm = pending["alarm"]
        now = dt(e.start)
        invocation = next(self._invocations)
        # Check, then store the flag: clinical events ignore flagged alarms.
        # cat1b is gated on heart-rate alarms (its condition needs one).
        for name in self.flag_rules:
            if name == "cat1b" and "HeartRate" not in _alarm_metric_types(kb, e):
                continue
            bindings = values(alarm=iri(alarm), now=now)
            number = next(self._check_numbers)
            self.lines += timed(name, invocation,
                                [*trace_block(f"check {number}", check(name, bindings)),
                                 flag_insert(name, alarm, pending["tgraph"], now)])
            self.checks[number] = (patient, e.start, "flaggedLikelyFalsePositive", name, ei)
        # An alarm on an IBP pathway may withdraw a cat1b flag; the asystole
        # then becomes evidence at this moment.
        withdraw_kinds = frozenset()
        if "cat1b" in self.flag_rules and _alarm_functional_unit(kb, e) == "FU_InvasiveBloodPressure":
            select, delete = flag_withdraw(alarm)
            self.lines += timed("cat1b_withdraw", invocation,
                                [*trace_block(f"withdraw {patient} {e.start.isoformat()}", select), delete])
            withdraw_kinds = kinds_supported_by_metric("HeartRate", self.event_rules)

        # CAT2: the condition is evaluated once, by the insert; the links it
        # stored are then read back for the firing log (always, so the
        # script does not depend on the result).
        prio = pending["incoming_prio"]
        for name in self.silence_rules:
            number = next(self._check_numbers)
            self.lines += timed(name, invocation,
                                [silence_insert(name, alarm, pending["tgraph"], now, prio),
                                 *trace_block(f"check {number}",
                                              silence_readback(name, alarm, pending["tgraph"]))])
            self.checks[number] = (patient, e.start, "silencedBy", name, ei)

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


def script_header() -> list:
    """Load the framework into the default graph; declare the prefixes."""
    return [f"import {f}" for f in FRAMEWORK_FILES] + SCRIPT_PREAMBLE


class LiveRun:
    """A live run: one Processor and one Oxigraph store. Each stream element
    is minted, executed, and its results added to `log` and
    `stats`, before the next element is taken. Raises if the engine cannot load
    the framework."""

    def __init__(self, kb, scratch_dir: Path, rule_names, log: LiveLog, stats: RuleStats,
                 timeout: float = 600, audit_log: bool = False):
        self.proc = Processor(kb, scratch_dir, rule_names)
        self.session = make_session(scratch_dir, timeout, audit_log)
        self.log, self.stats = log, stats
        self.errors = 0
        self.proc.lines += script_header()
        if not self.execute("loading the framework") or self.errors:
            self.session.close()
            raise RuntimeError(f"the engine could not load the framework ({self.session.stopped or 'errors above'})")

    def execute(self, where: str) -> bool:
        """Send the pending commands, run them, and read their results. An
        engine error is reported with `where` it happened.
        False once the session has stopped."""
        self.session.send(self.proc.take_lines())
        self._read(self.session.sync(), where)
        return self.session.stopped is None

    def _read(self, lines: list, where: str) -> None:
        for i, line in enumerate(lines):
            if line.startswith("An error occurred") or "cannot be resolved to a file" in line:
                self.errors += 1
                print(f"\n  engine error at {where}: {' '.join(lines[i:i + 2])}")
        self.stats.add(lines)
        self.log.add(read_blocks(lines, self.proc.checks))

    def stream(self, elements: list, on_arrival=None) -> bool:
        """Feed `elements` unit by unit, each executed before the next.
        `on_arrival()`: a progress callback. False once the session has
        stopped."""
        for unit in stream_units(elements):
            t = time.perf_counter()
            self.proc.feed(unit)
            if not self.execute(f"{unit[0].patient} {unit[0].time.isoformat()}"):
                return False
            if isinstance(unit[0], AlarmArrival):
                self.stats.add_arrival(unit[0].patient, time.perf_counter() - t)
            if on_arrival is not None and isinstance(unit[0], AlarmArrival):
                on_arrival()
        return True

    def close(self) -> bool:
        """Close the store. True if the run completed without engine errors."""
        self._read(self.session.close(), "the end of the run")
        complete = self.session.stopped is None
        if complete and self.proc.checks:
            print(f"  WARNING: {len(self.proc.checks)} check(s) without a result — results are incomplete.")
            complete = False
        if self.errors:
            print(f"  WARNING: {self.errors} engine error(s) during the run (reported above).")
        return complete and not self.errors


def run_live(kb, patients: dict, scratch_dir: Path, log: LiveLog, stats: RuleStats, enabled_rules=None,
             progress: bool = True, timeout: float = 600, audit_log: bool = False) -> bool:
    """Replay each patient's stream, one patient after another, through ONE
    Oxigraph store, live (LiveRun); results go to `log` and
    `stats` as they happen. `enabled_rules`: rules.RULES names, or None for
    all. `timeout`: kept for interface stability (statements run in-process).
    `audit_log`: write replay.oxigraph. True if the run completed without
    engine errors."""
    rule_names = list(RULES) if enabled_rules is None else list(enabled_rules)
    run = LiveRun(kb, scratch_dir, rule_names, log, stats, timeout, audit_log)
    t0 = time.monotonic()

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        n_events, done = len(events), itertools.count(1)
        report_every = max(1, n_events // 100)

        def report():
            k = next(done)
            if progress and (k == 1 or k == n_events or k % report_every == 0):
                print(f"\r  patient {pi}/{len(patients)} ({patient}) {_progress_bar(k, n_events)} "
                      f"{k}/{n_events} alarms [{time.monotonic() - t0:7.1f}s]", end="", flush=True)

        ok = run.stream(replay_stream(events), on_arrival=report)
        if ok:
            run.proc.close_windows()  # the patient's remaining windows
            ok = run.execute(f"{patient}, closing its windows")
        if progress:
            print()
        if not ok:
            print(f"  *** engine {run.session.stopped} during patient {patient}: the run stops here; "
                  f"results are partial. ***")
            break

    return run.close()


def run_batched(kb, patients: dict, scratch_root: Path, log: LiveLog, stats: RuleStats,
                batch_size: "int | None" = None, enabled_rules=None, progress: bool = True,
                timeout: float = 600, audit_log: bool = False) -> bool:
    """Run `patients` in batches of `batch_size` (None: one batch), each its
    own live run (run_live) and Oxigraph store, sharing `log` and `stats`.
    A batch that stops is reported and the next batch still runs. True if
    every batch completed without engine errors."""
    items = list(patients.items())
    size = batch_size if batch_size else len(items)
    num_batches = -(-len(items) // size) if items else 0  # ceil div
    complete = True
    for bi in range(0, len(items), size):
        batch = dict(items[bi:bi + size])
        batch_num = bi // size + 1
        if progress:
            print(f"[batch {batch_num}/{num_batches}] {len(batch)} patient(s)")
        batch_scratch = scratch_root / f"batch_{batch_num:04d}"
        if batch_scratch.exists():
            shutil.rmtree(batch_scratch)
        batch_scratch.mkdir(parents=True)
        complete &= run_live(kb, batch, batch_scratch, log, stats, enabled_rules=enabled_rules,
                             progress=progress, timeout=timeout, audit_log=audit_log)
    return complete


# --------------------------------------------------------------------
# Parallel runs: patients are independent, so they can be replayed in
# separate processes, each with its own stores. Results are written in the
# input order of the patients, so the logs match a serial run.
# --------------------------------------------------------------------

_WORKER: dict = {}


def _worker_init(enabled_rules, scratch_root: str, audit_log: bool, window) -> None:
    """Load the knowledge base once per worker process."""
    _WORKER.update(kb=M.load_kb(window), rules=enabled_rules, root=Path(scratch_root), audit=audit_log)


def _worker_patient(item) -> tuple:
    """Replay one patient; return (index, records, firings, rule stats, complete)."""
    from execution import RuleStats as _RuleStats
    index, patient, events = item
    scratch = _WORKER["root"] / f"patient_{index:05d}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    log = LiveLog({patient: events}, alarm_index(events))
    stats = _RuleStats()
    ok = run_live(_WORKER["kb"], {patient: events}, scratch, log, stats,
                  enabled_rules=_WORKER["rules"], progress=False, audit_log=_WORKER["audit"])
    log.close()
    if not _WORKER["audit"]:
        shutil.rmtree(scratch, ignore_errors=True)
    return index, log.records, log.firings, stats, ok


def run_parallel(patients: dict, scratch_root: Path, log: LiveLog, stats: RuleStats, workers: int,
                 enabled_rules=None, progress: bool = True, audit_log: bool = False,
                 window: str | None = None) -> bool:
    """Replay `patients`, one task per patient, over `workers` processes.
    Each worker loads the knowledge base once (`window`: see
    mint.load_kb); each patient gets its own store. Records and firings are written to `log` in the input order of
    the patients; rule statistics are merged into `stats`. True if every
    patient completed without engine errors."""
    from concurrent.futures import ProcessPoolExecutor, as_completed
    rule_names = list(RULES) if enabled_rules is None else list(enabled_rules)
    items = [(i, p, evs) for i, (p, evs) in enumerate(patients.items())]
    # Largest patients first, so one long stay does not run alone at the
    # end. Scheduling only: each patient is still replayed on its own, and
    # the logs are written in input order below.
    items.sort(key=lambda item: -len(item[2]))
    results: dict = {}
    complete, t0 = True, time.monotonic()
    with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init,
                             initargs=(rule_names, str(scratch_root), audit_log, window)) as pool:
        futures = [pool.submit(_worker_patient, item) for item in items]
        for done, future in enumerate(as_completed(futures), start=1):
            index, records, firings, worker_stats, ok = future.result()
            results[index] = (records, firings)
            stats.merge(worker_stats)
            complete &= ok
            if progress:
                print(f"\r  {_progress_bar(done, len(items))} {done}/{len(items)} patients "
                      f"[{time.monotonic() - t0:7.1f}s]", end="", flush=True)
    if progress:
        print()
    for index in range(len(items)):
        records, firings = results[index]
        log.extend(records, firings)
    return complete
