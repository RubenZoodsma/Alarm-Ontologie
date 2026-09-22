"""
windows.py — the window operators (RSP-QL: windows over the stream).

Each alarm has two named graphs:
  <alarm#transient>   message and condition: from arrival until its end.
  <alarm#persistent>  structure and isMonitoredBy: from arrival until the
                      post-alarm window (ontology) after its end arrives.
A graph is valid exactly while it is in the store; there is no validity
metadata, so no query can see an alarm's future. An alarm's graphs hold
only what it reported, and no other alarm edits them: a shared node (a
sensor, a metric) carries one state per alarm reporting it.

Insert: a TriG file via `import`. Drop: `DELETE WHERE { GRAPH <g> {...} }`.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import mint as M
from stream import AlarmArrival, AlarmEnd


def graph_iri(alarm: str, suffix: str) -> str:
    return f"<{alarm}#{suffix}>"


def graph_triples(g) -> list:
    """rdflib Graph -> N-Triples lines (rdflib does the escaping)."""
    text = g.serialize(format="nt")
    return [line.strip() for line in text.splitlines() if line.strip()]


def subject_predicate(triple: str) -> tuple:
    """(subject, predicate) of an N-Triples line."""
    parts = triple.rstrip(" .").split(" ", 2)
    return parts[0], parts[1]


@dataclass
class OpenAlarm:
    """What the window operator remembers of an alarm between its arrival
    and the expiry of its persistent graph."""
    alarm: str          # the alarm IRI
    tgraph: str         # <alarm#transient>
    pgraph: str         # <alarm#persistent>
    label: str
    device_id: str
    kinds: frozenset    # episode kinds its graphs can be evidence for


class WindowOperator:
    """The windows of ONE patient's stream: RDFox commands for each
    transition; the processor orders them. `file_counter` is shared across
    patients, who write TriG files into one scratch directory."""

    def __init__(self, kb, scratch_dir: Path, file_counter):
        self.kb = kb
        self.scratch = scratch_dir
        self._file_counter = file_counter
        self.identity_tracker = M.new_identity_tracker(kb)
        self.open: dict = {}        # alarm_id -> OpenAlarm, until its end
        self.expiries: list = []    # (due, seq, OpenAlarm): persistent graphs
        self._seq = itertools.count()

    def _write_trig(self, blocks: dict) -> str:
        path = self.scratch / f"tx_{next(self._file_counter):04d}.trig"
        with path.open("w") as f:
            for graph, triples in blocks.items():
                if not triples:
                    continue
                f.write(f"GRAPH {graph} {{\n")
                for t in triples:
                    f.write(f"  {t}\n")
                f.write("}\n")
        return str(path)

    def on_arrive(self, arrival: AlarmArrival, identity: dict, kinds: frozenset) -> tuple:
        """(commands, pending). Phase 1 of the insert: everything except the
        alarm's hasPriority triple, held back until its checks have run
        (complete()), so it cannot justify its own CAT2a silence."""
        kb = self.kb
        alarm_uri = M.alarm_iri(arrival)
        alarm = str(alarm_uri)
        tgraph = graph_iri(alarm, "transient")
        pgraph = graph_iri(alarm, "persistent")

        msg_graph = M.alarm_message(kb, arrival, identity)
        M.add_triggered_by(msg_graph, [arrival], kb, identity)
        cond_graph = M.condition_for_event(kb, arrival.patient, arrival.label, arrival.device_id, identity)
        bg_graph = M.background_for_key(kb, arrival.patient, arrival.label, arrival.device_id, identity)

        incoming_prio = msg_graph.value(alarm_uri, M.MDA.hasPriority)
        priority_pred = f"<{M.MDA}hasPriority>"
        priority_triple = None
        phase1_msg_triples = []
        for t in graph_triples(msg_graph):
            _, pred = subject_predicate(t)
            if pred == priority_pred and priority_triple is None:
                priority_triple = t
            else:
                phase1_msg_triples.append(t)

        patient = M.patient_iri(arrival.patient)
        dev = M.device_iri(arrival.patient, arrival.device_id)
        persistent_triples = graph_triples(bg_graph) + [f"<{patient}> <{M.MDA}isMonitoredBy> <{dev}> ."]
        path = self._write_trig({tgraph: phase1_msg_triples + graph_triples(cond_graph),
                                 pgraph: persistent_triples})
        self.open[arrival.alarm_id] = OpenAlarm(alarm, tgraph, pgraph, arrival.label, arrival.device_id,
                                                frozenset(kinds))
        commands = [f"# arrive (phase1): {arrival.label} @ {arrival.device_id} {arrival.start.isoformat()}",
                    f"import {path}"]
        pending = {"alarm": alarm, "tgraph": tgraph,
                   "priority_triple": priority_triple, "incoming_prio": incoming_prio}
        return commands, pending

    @staticmethod
    def complete(pending: dict) -> list:
        """Phase 2: insert the held-back hasPriority triple, if any."""
        if pending["priority_triple"] is None:
            return []
        return [f"# arrive (phase2): complete {pending['alarm']}",
                f"INSERT DATA {{ GRAPH {pending['tgraph']} {{ {pending['priority_triple']} }} }}"]

    def on_end(self, end: AlarmEnd) -> tuple:
        """(commands, OpenAlarm): drop the transient graph and start the
        persistent graph's post-alarm window."""
        alarm = self.open.pop(end.alarm_id)
        self.expiries.append((end.time + self.kb.window, next(self._seq), alarm))
        commands = [f"# end: {alarm.label} @ {alarm.device_id} {end.time.isoformat()}",
                    f"DELETE WHERE {{ GRAPH {alarm.tgraph} {{ ?s ?p ?o }} }}"]
        return commands, alarm

    def due(self, up_to: datetime | None = None) -> list:
        """Remove and return the windows closing at or before `up_to` (all
        when None), as [(due, [OpenAlarm, ...])] in time order."""
        closing = sorted(e for e in self.expiries if up_to is None or e[0] <= up_to)
        self.expiries = [e for e in self.expiries if not (up_to is None or e[0] <= up_to)]
        grouped: list = []
        for due, _, alarm in closing:
            if grouped and grouped[-1][0] == due:
                grouped[-1][1].append(alarm)
            else:
                grouped.append((due, [alarm]))
        return grouped

    @staticmethod
    def expire(alarm: OpenAlarm) -> list:
        """Drop the alarm's persistent graph: its window closed."""
        return [f"# window-expiry: {alarm.label} @ {alarm.device_id}",
                f"DELETE WHERE {{ GRAPH {alarm.pgraph} {{ ?s ?p ?o }} }}"]
