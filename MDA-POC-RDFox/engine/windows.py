"""
windows.py — the window operators (RSP-QL: windows over the stream).

Each alarm contributes two named graphs to the store:
  - transient  <alarm#transient>: its message and condition content —
    valid from its arrival to its end.
  - persistent <alarm#persistent>: its structural/background content plus
    mda:isMonitoredBy — valid from its arrival to 15 minutes after its end
    (a landmark window, compensating for the absence of continuous device
    state telemetry).
Nothing else: an alarm's graphs hold exactly what that alarm reported, and
no other alarm's arrival or end ever edits them. Shared particulars
(device, sensor, signal, metric, ...) are re-minted into each alarm's own
graphs, so a shared node can carry several states at once — one per
active alarm reporting it — and every rule reads a state through the alarm
that reports it.

RDFox mechanics used, all confirmed by direct testing:
  - Named-graph insert: TriG `GRAPH <iri> { ... }` via `import <file>`.
  - Named-graph drop: `DELETE WHERE { GRAPH <iri> { ?s ?p ?o } }`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import mint as M
from actions import XSD_DATETIME, evaluate_commands, silence_lift
from event_log import trace_block
from stream import Event

WINDOW = timedelta(minutes=15)  # ontology.ttl's postAlarmValidityDuration


def graph_iri(alarm: str, suffix: str) -> str:
    return f"<{alarm}#{suffix}>"


VALID_FROM = f"<{M.MDAPOC}validFrom>"
VALID_UNTIL = f"<{M.MDAPOC}validUntil>"


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


class WindowOperator:
    """Builds the RDFox shell script for one patient's full event stream.

    `file_counter` MUST be shared across every patient processed in the
    same run (build_script creates one itertools.count() and passes it to
    each patient's Driver) — one Driver instance per patient restarting
    its own counter at 1 caused every patient after the first to silently
    overwrite an earlier patient's tx_0001.trig/tx_0002.trig in the shared
    scratch directory, since all patients' scripts are generated before
    RDFox ever reads any of them back."""

    def __init__(self, kb, scratch_dir: Path, file_counter, event_rules=(), cat2_lift=False):
        self.kb = kb
        # CAT2a or CAT2b enabled: an alarm's end may lift silences it
        # justified (see actions.silence_lift).
        self.lift_cat2 = cat2_lift
        # Enabled episode rules (rules.EVENT_RULES order).
        # A dropped graph can take away an event's evidence, so each drop of
        # an alarm that is relevant to an event kind re-evaluates that kind
        # at the drop's own logical time — see actions.py's docstring (EPISODE).
        self.event_rules = list(event_rules)
        self.scratch = scratch_dir
        self.commands: list[str] = []
        self.pending: list[tuple] = []  # (when, command_text)
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

    def insert_alarm(self, event: Event, identity: dict, event_kinds: frozenset = frozenset()) -> dict:
        """Phase 1 of a two-phase insert: everything about this alarm
        EXCEPT its own `hasPriority` triple, which is held back and must
        be completed via complete_alarm() after any per-alarm checks have
        run. Until then the arriving alarm has no priority in the store, so
        it can never count as an "active alarm of equal or higher priority"
        in its own CAT2a check (whose incoming priority is passed in via
        VALUES instead). Every other check (cat1a/cat1b/cat2b) and the
        clinical-event updates are unaffected by hasPriority's absence and
        run fine against phase-1-only data.

        Returns the pending state complete_alarm() needs; always
        two-phase now regardless of which rules are enabled — holding
        back one triple and inserting it separately is cheap enough not
        to be worth conditioning on enabled_rules.
        """
        kb = self.kb
        alarm_uri = M.alarm_iri(event)
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
        patient = M.patient_iri(event.patient)
        dev = M.device_iri(event.patient, event.device_id)
        persistent_triples = graph_triples(bg_graph) + [f"<{patient}> <{M.MDA}isMonitoredBy> <{dev}> ."]

        metadata = (
            validity_triples(tgraph, event.start, event.end)
            + validity_triples(pgraph, event.start, event.end + WINDOW)
        )
        path = self._write_trig({tgraph: transient_triples_phase1, pgraph: persistent_triples}, bare=metadata)
        self.commands.append(f"# arrive (phase1): {event.label} @ {event.device_id} {event.start.isoformat()}")
        self.commands.append(f"import {path}")

        self.schedule(event.end, self._with_events(
            self._drop_transient_cmd(event, tgraph), event, event.end, event_kinds))
        self.schedule(event.end + WINDOW, self._with_events(
            self._drop_persistent_cmd(event, pgraph), event, event.end + WINDOW, event_kinds))

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

    def _drop_transient_cmd(self, event: Event, graph: str) -> str:
        """Built at arrival, run at the alarm's end: drop THIS alarm's
        transient graph — only its own content (see the module docstring)."""
        cmd = f"# end: {event.label} @ {event.device_id} {event.end.isoformat()}\n"
        cmd += f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}"
        if self.lift_cat2:
            select, delete = silence_lift(graph[1:].split("#", 1)[0], event.end)
            cmd += "\n" + "\n".join(trace_block(f"lift {event.patient} {event.end.isoformat()}", select))
            cmd += "\n" + delete
        return cmd

    def _with_events(self, cmd: str, event: Event, when: datetime, event_kinds) -> str:
        lines = evaluate_commands(event_kinds, self.event_rules, event.patient, when)
        return "\n".join([cmd] + lines)

    def _drop_persistent_cmd(self, event: Event, graph: str) -> str:
        return (f"# window-expiry: {event.label} @ {event.device_id}\n"
                f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}")
