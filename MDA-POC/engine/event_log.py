"""
event_log.py — what fired and which alarms it rests on (RSP-QL: R2S).
Never written back into the store.

  clinical_events.csv   per clinical event: kind, start, end, and every
                        alarm that supported it.
  rule_firings.csv      per CAT1/CAT2 firing (and withdrawal, lift): the
                        alarm, and the alarm(s) that caused it.

Alarms are written as labels (for reading) and ids (the local name of the
alarm IRI, for rebuilding: validation/log_rebuild_check.py).

Written live, per stream element (LiveLog), from the trace blocks of that
element's output, as (tag, check_key, rows):
  "ended"                     event, kind, start, end
  "support"                   event, support (an alarm graph, or a
                              constituent event)
  "check"                     alarm, witness (an alarm graph or alarm)
  "withdraw <patient> <time>" flagged alarm, arriving alarm (cat1b)
  "lift <patient> <time>"     silenced alarm, ended alarm (CAT2)
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import mint as M

# Every select whose answers matter runs inside a trace block: the tag says
# what asked, the rows are the answers.
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
    """Every trace block in the engine's output, as (tag, rows, seconds):
    answer rows only, and its "Total statement evaluation time"."""
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
    """(labels joined with " + ", ids joined with " "), ordered by start,
    label and ALARM_ID: the id breaks ties between identical alarms at one
    instant, so the log does not depend on set order (which varies between
    processes)."""
    known = sorted((alarms[a] for a in alarm_iris if a in alarms),
                   key=lambda e: (e.start, e.label, e.alarm_id))
    ids = [str(M.alarm_iri(e)).rsplit("/", 1)[-1] for e in known]
    return " + ".join(e.label for e in known), " ".join(ids)


@dataclass
class EventRecord:
    """One ended clinical event."""
    event: str
    kind: str
    patient: str
    start: datetime
    end: datetime
    alarms: set = field(default_factory=set)


@dataclass
class FiringRecord:
    """One firing: a check that matched, a withdrawal or a lift."""
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


def flagged_alarms(firings: list) -> set:
    """(patient, alarm) of every alarm flagged and not withdrawn."""
    withdrawn = {(f.patient, f.alarm) for f in firings if f.rule == WITHDRAWN_RULE}
    return {(f.patient, f.alarm) for f in firings
            if f.kind == "flaggedLikelyFalsePositive"
            and not (f.rule == "cat1b" and (f.patient, f.alarm) in withdrawn)}


def silenced_alarms(firings: list) -> set:
    """(patient, alarm) of every alarm silenced and not lifted."""
    lifted = {(f.patient, f.alarm) for f in firings if f.rule == LIFTED_RULE}
    return {(f.patient, f.alarm) for f in firings
            if f.kind == "silencedBy" and (f.patient, f.alarm) not in lifted}


def episodes_by_patient(records: list, kind: str) -> Counter:
    """Episodes of `kind` per patient."""
    return Counter(r.patient for r in records if r.kind == kind)


EVENT_LOG_HEADER = ["patient", "kind", "start", "end", "supported_by", "alarm_ids"]
FIRING_LOG_HEADER = ["patient", "time", "rule", "alarm", "caused_by", "alarm_ids"]


class LiveLog:
    """Both logs, kept up to date per stream element: add() takes the trace
    blocks of one element's output (execution.read_blocks). A firing is
    recorded at its check; a clinical event when it ends, with every alarm
    that supported it, directly or through a constituent event. The only
    state kept is what supports the events still active. With paths, rows
    are written as they happen.

    `patients`: the patient ids (event IRIs carry them cleaned).
    `alarms`: {alarm IRI: Event} (alarm_index), to write labels and ids."""

    def __init__(self, patients, alarms: dict, event_path: Path | None = None,
                 firing_path: Path | None = None, append: bool = False):
        self._original = {M._clean(p): p for p in patients}
        self._alarms = alarms
        self._supports: dict = {}   # active event -> its supports so far
        self.records: list = []     # EventRecord, in the order events end
        self.firings: list = []     # FiringRecord, in the order they fire
        self._event_file, self._event_csv = self._open(event_path, EVENT_LOG_HEADER, append)
        self._firing_file, self._firing_csv = self._open(firing_path, FIRING_LOG_HEADER, append)

    @staticmethod
    def _open(path: Path | None, header: list, append: bool = False) -> tuple:
        """`append`: continue an existing log (a chunked or resumed run);
        the header is written only to a new or empty file."""
        if path is None:
            return None, None
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not append or not path.exists() or path.stat().st_size == 0
        f = path.open("w" if not append else "a", newline="", encoding="utf-8")
        writer = csv.writer(f, delimiter=";")
        if fresh:
            writer.writerow(header)
        return f, writer

    def add(self, blocks: list) -> None:
        for tag, key, rows in blocks:
            if tag == "support":
                for ev, support in rows:
                    self._supports.setdefault(ev, set()).add(support)
            elif tag == "ended":
                # Resolve every ended event first: a combined event ends in
                # the same step as its constituent, and reads its supports.
                ended = [self._event_record(*row) for row in rows]
                for record in ended:
                    self._supports.pop(record.event, None)
                    self._add_event(record)
            else:
                for firing in self._firing_records(tag, key, rows):
                    self._add_firing(firing)
        for f in (self._event_file, self._firing_file):
            if f is not None:
                f.flush()

    def extend(self, records: list, firings: list) -> None:
        """Add the records and firings of another log (a parallel worker's),
        writing them as if they had happened here."""
        for r in firings:
            self._add_firing(r)
        for r in records:
            self._add_event(r)
        for f in (self._event_file, self._firing_file):
            if f is not None:
                f.flush()

    def _alarms_behind(self, ev: str, seen: tuple = ()) -> set:
        found = set()
        for s in self._supports.get(ev, ()):
            if s.startswith(M.EVENT_BASE):
                if s not in seen:
                    found |= self._alarms_behind(s, seen + (ev,))
            else:
                found.add(alarm_of(s))
        return found

    def _event_record(self, ev: str, kind: str, start: str, end: str) -> EventRecord:
        kind_local = kind.rsplit("/", 1)[-1]
        cleaned = ev[len(M.EVENT_BASE) + len(kind_local) + 1:].rsplit("_", 2)[0]  # drop start, number
        return EventRecord(ev, kind_local, self._original.get(cleaned, cleaned),
                           datetime.fromisoformat(start), datetime.fromisoformat(end),
                           self._alarms_behind(ev))

    @staticmethod
    def _firing_records(tag: str, key, rows: list) -> list:
        """A matching check, a withdrawn cat1b flag (WITHDRAWN_RULE) or a
        lifted CAT2 silence (LIFTED_RULE), as FiringRecords."""
        for prefix, kind, rule in ((WITHDRAW_TAG, "flagWithdrawn", WITHDRAWN_RULE),
                                   (LIFT_TAG, "silenceLifted", LIFTED_RULE)):
            if tag.startswith(prefix):
                patient, when = tag[len(prefix):].rsplit(" ", 1)
                return [FiringRecord(patient, datetime.fromisoformat(when), kind, rule, subject, {cause})
                        for subject, cause in rows]
        if tag != "check" or not rows:
            return []
        patient, time, kind, rule = key[0], key[1], key[2], key[3]
        causes = {alarm_of(w) for _a, w in rows if w}
        return [FiringRecord(patient, time, kind, rule, rows[0][0], causes)]

    def _add_event(self, r: EventRecord) -> None:
        self.records.append(r)
        if self._event_csv is not None:
            labels, ids = _describe(r.alarms, self._alarms)
            self._event_csv.writerow([r.patient, r.kind, r.start.isoformat(), r.end.isoformat(), labels, ids])

    def _add_firing(self, r: FiringRecord) -> None:
        self.firings.append(r)
        if self._firing_csv is not None:
            alarm_label, alarm_id = _describe({r.alarm}, self._alarms)
            labels, ids = _describe(r.causes - {r.alarm}, self._alarms)
            self._firing_csv.writerow([r.patient, r.time.isoformat(), r.rule, alarm_label, labels,
                                       " ".join(filter(None, [alarm_id, ids]))])

    def close(self) -> None:
        for f in (self._event_file, self._firing_file):
            if f is not None:
                f.close()
