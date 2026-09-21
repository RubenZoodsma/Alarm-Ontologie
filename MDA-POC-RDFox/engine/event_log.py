"""
event_log.py — what fired, and which alarms it rests on. Kept outside the
store.

Two logs, both reduced to ALARMS — enough to rebuild the graph, since every
fact in it is minted from an alarm (patient, label, device, start, end):

  clinical_events.csv   one row per clinical event: its kind, start, end,
                        and the alarms that supported it at any point.
  rule_firings.csv      one row per firing of a CAT1/CAT2 rule: the
                        arriving alarm, and the alarm(s) that caused it.

Each alarm is written twice: as its label, for reading, and as its id
(the local name of its IRI, which encodes patient, device, start, end and
ALARM_ID), for rebuilding — a label alone repeats far too often to
identify an alarm.

Input: the trace blocks execution.execute_script collects (see
trace_block below), as (tag, check_key, rows):
  ("ended", None, rows)     rows: event, kind, start, end
  ("support", None, rows)   rows: event, support — an alarm graph
                            (<alarm#transient|#persistent>) or, for a
                            combined event, a constituent event
  ("check", key, rows)      rows: alarm, witness — the witness is an alarm
                            graph or an alarm (unbound when a rule cannot
                            name one)
  ("withdraw <patient> <time>", None, rows)
                            rows: flagged alarm, arriving alarm — a stored
                            cat1b flag withdrawn because an alarm on its IBP
                            pathway arrived (actions.cat1b_withdraw)
  ("lift <patient> <time>", None, rows)
                            rows: silenced alarm, ended alarm — a CAT2a
                            silence lifted because the last active alarm
                            justifying it ended (actions.cat2_lift)
Nothing here is ever written back into the store.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import mint as M

# Every select whose answers matter runs inside a trace block, with output
# switched on for the whole script (execution.SCRIPT_PREAMBLE): the block's
# tag says what asked, its rows are the answers.
TRACE_BEGIN = "TRACE_BEGIN"
TRACE_END = "TRACE_END"


def trace_block(tag: str, query: str) -> list:
    """`query` wrapped in TRACE_BEGIN <tag> / TRACE_END marker lines."""
    return [f"echo {TRACE_BEGIN} {tag}", query, f"echo {TRACE_END}"]


def _term(token: str) -> str:
    """A TSV answer term as a plain string: an IRI without its brackets, a
    typed literal without its datatype, "" when unbound."""
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    if token.startswith('"'):
        return token[1:token.index('"', 1)]
    return token


def parse_trace_blocks(out_lines: list) -> list:
    """Every TRACE_BEGIN <tag> ... TRACE_END block in RDFox's output, as
    (tag, rows). Answer rows are the lines starting with an IRI or a
    literal; the TSV header (`?var ...`) and RDFox's statistics lines are
    skipped. The statement's own "Total statement evaluation time" is
    returned alongside, for per-check timing."""
    blocks, tag, rows, seconds = [], None, [], None
    begin = TRACE_BEGIN + " "
    for line in out_lines:
        if line.startswith(begin):
            tag, rows, seconds = line[len(begin):], [], None
        elif line == TRACE_END and tag is not None:
            blocks.append((tag, rows, seconds))
            tag = None
        elif tag is not None:
            if line.startswith("<") or line.startswith('"'):
                rows.append([_term(t) for t in line.split("\t")])
            elif line.startswith("Total statement evaluation time"):
                seconds = float(line.split(":", 1)[1].strip().split()[0])
    return blocks


def alarm_of(graph_or_alarm: str) -> str:
    """The alarm IRI behind an alarm graph IRI (<alarm#transient>,
    <alarm#persistent>); an alarm IRI is returned unchanged."""
    return graph_or_alarm.split("#", 1)[0]


def alarm_index(events) -> dict:
    """{alarm IRI: Event} for the replayed input events."""
    return {str(M.alarm_iri(e)): e for e in events}


def _describe(alarm_iris, alarms: dict) -> tuple:
    """(labels joined with " + ", ids joined with " "), ordered by start."""
    known = sorted((alarms[a] for a in alarm_iris if a in alarms), key=lambda e: (e.start, e.label))
    ids = [str(M.alarm_iri(e)).rsplit("/", 1)[-1] for e in known]
    return " + ".join(e.label for e in known), " ".join(ids)


@dataclass
class EventRecord:
    event: str
    kind: str
    patient: str
    start: datetime
    end: datetime
    alarms: set = field(default_factory=set)


def event_records(blocks: list, patients=()) -> list:
    """One EventRecord per ended clinical event, with every alarm that
    supported it — directly, or through a constituent event."""
    original = {M._clean(p): p for p in patients}
    supports: dict = {}
    for tag, _key, rows in blocks:
        if tag == "support":
            for ev, support in rows:
                supports.setdefault(ev, set()).add(support)

    def alarms_behind(ev, seen=()):
        found = set()
        for s in supports.get(ev, ()):
            if s.startswith(M.EVENT_BASE):
                if s not in seen:
                    found |= alarms_behind(s, seen + (ev,))
            else:
                found.add(alarm_of(s))
        return found

    records = {}
    for tag, _key, rows in blocks:
        if tag != "ended":
            continue
        for ev, kind, start, end in rows:
            kind_local = kind.rsplit("/", 1)[-1]
            cleaned = ev[len(M.EVENT_BASE) + len(kind_local) + 1:].rsplit("_", 1)[0]
            records[ev] = EventRecord(ev, kind_local, original.get(cleaned, cleaned),
                                      datetime.fromisoformat(start), datetime.fromisoformat(end),
                                      alarms_behind(ev))
    return sorted(records.values(), key=lambda r: (r.patient, r.start, r.kind))


@dataclass
class FiringRecord:
    patient: str
    time: datetime
    kind: str       # flaggedLikelyFalsePositive / silencedBy
    rule: str       # cat1a / cat1b / cat2a / cat2b / ...
    alarm: str      # the arriving alarm's IRI
    causes: set = field(default_factory=set)


WITHDRAW_TAG = "withdraw "
WITHDRAWN_RULE = "cat1b_withdrawn"
LIFT_TAG = "lift "
LIFTED_RULE = "cat2_lifted"


def firing_records(blocks: list) -> list:
    """One FiringRecord per check that returned at least one row, one per
    withdrawn cat1b flag (rule WITHDRAWN_RULE: `alarm` is the flagged alarm,
    `causes` the alarm whose arrival withdrew it), and one per lifted CAT2
    silence (rule LIFTED_RULE: `alarm` is the silenced alarm, `causes` the
    alarm whose end lifted it)."""
    records = []
    for tag, key, rows in blocks:
        for prefix, kind, rule in ((WITHDRAW_TAG, "flagWithdrawn", WITHDRAWN_RULE),
                                   (LIFT_TAG, "silenceLifted", LIFTED_RULE)):
            if tag.startswith(prefix):
                patient, when = tag[len(prefix):].rsplit(" ", 1)
                for subject, cause in rows:
                    records.append(FiringRecord(patient, datetime.fromisoformat(when),
                                                kind, rule, subject, {cause}))
                break
        else:
            if tag != "check" or not rows:
                continue
            patient, time, kind, rule = key[0], key[1], key[2], key[3]
            causes = {alarm_of(w) for _a, w in rows if w}
            records.append(FiringRecord(patient, time, kind, rule, rows[0][0], causes))
    return records


def flagged_alarms(firings: list) -> set:
    """(patient, alarm) for every alarm still flagged likely false positive
    at the end of the replay: flagged by some rule, and not only by cat1b
    flags that were withdrawn."""
    withdrawn = {(f.patient, f.alarm) for f in firings if f.rule == WITHDRAWN_RULE}
    return {(f.patient, f.alarm) for f in firings
            if f.kind == "flaggedLikelyFalsePositive"
            and not (f.rule == "cat1b" and (f.patient, f.alarm) in withdrawn)}


def silenced_alarms(firings: list) -> set:
    """(patient, alarm) for every alarm still silenced at the end of the
    replay: silenced at arrival (CAT2a and/or CAT2b), and not lifted before
    it ended — a lift ends the silence whatever rules were behind it."""
    lifted = {(f.patient, f.alarm) for f in firings if f.rule == LIFTED_RULE}
    return {(f.patient, f.alarm) for f in firings
            if f.kind == "silencedBy" and (f.patient, f.alarm) not in lifted}


def episodes_by_patient(records: list, kind: str) -> Counter:
    """How many episodes of `kind` each patient had — one record is one
    episode, since an event ends and is removed before a new one of the
    same kind can start."""
    return Counter(r.patient for r in records if r.kind == kind)


def write_event_log(records: list, alarms: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["patient", "kind", "start", "end", "supported_by", "alarm_ids"])
        for r in records:
            labels, ids = _describe(r.alarms, alarms)
            w.writerow([r.patient, r.kind, r.start.isoformat(), r.end.isoformat(), labels, ids])


def write_firing_log(records: list, alarms: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["patient", "time", "rule", "alarm", "caused_by", "alarm_ids"])
        for r in records:
            alarm_label, alarm_id = _describe({r.alarm}, alarms)
            labels, ids = _describe(r.causes - {r.alarm}, alarms)
            w.writerow([r.patient, r.time.isoformat(), r.rule, alarm_label, labels,
                        " ".join(filter(None, [alarm_id, ids]))])
