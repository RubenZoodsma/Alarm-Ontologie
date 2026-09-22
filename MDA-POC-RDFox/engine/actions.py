"""
actions.py — what happens with a rule's result (RSP-QL: R2R, the effect).

Every command is an action template (representation/actions/) with this
moment's bindings and, where it applies, a rule's condition
(representation/rules/) — see rules.compose. This module only decides
which template, with which bindings.

FLAG (CAT1). A CAT1 result is STORED, in the flagged alarm's own transient
graph (so a flag disappears when its alarm ends), naming the node the
verdict rests on. Two consumers read it:
  - clinical events: a flagged alarm is no evidence for any condition
    (agreed 2026-09-21). The processor runs an arrival's CAT1 checks before
    its clinical-event evaluation, so a flag is in place before the alarm
    could ever count.
  - CAT1b's withdrawal (rules/cat1b_withdraw.rq): the flag is withdrawn when
    the IBP pathway that justified it raises an alarm while the asystole is
    still active — traced ("withdraw <patient> <time>") and logged as a
    cat1b_withdrawn firing.

SILENCE (CAT2). Every active alarm that justified a silence at arrival,
under either rule, is stored as `incoming mdapoc:silencedBy active` in the
incoming alarm's own transient graph. When an active alarm ends, its
silencedBy links are removed; an alarm left with none, and still active,
has its silence lifted (rules/cat2_lift.rq) — traced ("lift <patient>
<time>") and logged as a cat2_lifted firing.

EPISODE (clinical events, CAT3). A clinical event is a node of its own,
one per episode, for one patient:

    event  a mda:ClinicalEvent, clinicalEvent:<Kind>
    event  mda:concernsPatient  patient
    event  mda:evidencedBy      evidence      (one or more)
    event  mda:hasStart         time
    event  mda:hasEnd           time          (only in the moment it ends)

An event outlives the individual facts that support it (a second heart
rate alarm keeps a cardiac arrest going after the first one ends), so it
cannot be a derived Datalog fact: RDFox would retract it the moment the
first alarm's graph is dropped, and a derived fact cannot carry an end
time. The per-(patient, kind) state machine therefore runs as guarded
updates:

    inactive + evidence   -> START:  create the event (hasStart = now)
    active   + evidence   -> EXTEND: add evidencedBy for new evidence
    active   + none left  -> END:    set hasEnd = now, log it, remove it

"Active" is simply "an event of this kind for this patient exists in the
store": an ended event is removed in the same step it ends, so the store
holds only current belief.

Evidence can only appear or disappear when a relevant alarm is inserted
(arrival) or one of its graphs is dropped (end, window expiry) — no alarm
ever edits another alarm's graphs. evaluate_commands() is emitted at
exactly those moments, with `now` set to that moment's logical time, so
hasStart and hasEnd are exact, never "when the driver happened to notice".
For a combined event this gives the later of its constituents' starts and
the earliest of their ends.

Order inside one evaluation: END for every kind (single-alarm kinds before
combined ones, so a combined event sees its constituent already ended),
then log and remove the ended events, then EXTEND/START (again single
before combined, so a combined event can start on a constituent that
started in this same step).

LOGGING. The log is not part of the graph of belief. After every
START/EXTEND, each active event is printed with what currently supports it
("support"); the events that end in a step are printed with start and end
("ended"), just before they are removed. event_log.py turns these into
records after the run.
"""

from __future__ import annotations

from datetime import datetime

import mint as M
from event_log import trace_block
from rules import ALARMPRIO, CAT1B_WITHDRAW, CAT2_LIFT, RULE_BY_KIND, RULES, compose

XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"
CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"


def iri(value) -> str:
    return f"<{value}>"


def dt(when: datetime) -> str:
    """An xsd:dateTime literal."""
    return f'"{when.isoformat()}"^^{XSD_DATETIME}'


def values(**bindings) -> str:
    """A one-row VALUES clause: values(alarm=iri(a), now=dt(t))."""
    names = " ".join(f"?{name}" for name in bindings)
    return f"VALUES ({names}) {{ ({' '.join(bindings.values())}) }}"


# --------------------------------------------------------------------
# CAT1 and CAT2, at an alarm's arrival
# --------------------------------------------------------------------

def check(rule: str, bindings: str) -> str:
    """The rule's check query, for the firing log."""
    return compose("check", bindings, RULES[rule].condition)


def flag_insert(rule: str, alarm: str, tgraph: str, now: str) -> str:
    """Store `rule`'s (cat1a or cat1b) flag on `alarm`, if it holds."""
    return compose("flag_insert", values(alarm=iri(alarm), now=now, tgraph=tgraph), RULES[rule].condition)


def flag_withdraw(alarm: str) -> tuple:
    """(select, delete): the stored cat1b flags `alarm` withdraws, and their
    removal."""
    bindings = values(new=iri(alarm))
    return (compose("flag_withdraw_select", bindings, CAT1B_WITHDRAW),
            compose("flag_withdraw_delete", bindings, CAT1B_WITHDRAW))


def _incoming_prio(incoming_prio) -> str:
    """The incoming alarm's priority as a binding. An alarm without one is
    bound as alarmprio:Unknown — what the rules then do with it is in the
    rule file (cat2a_process_priority.rq)."""
    return iri(incoming_prio if incoming_prio is not None else f"{ALARMPRIO}Unknown")


def silence_bindings(rule: str, alarm: str, now: str, incoming_prio=None) -> str:
    """The check's bindings; cat2a also needs the incoming alarm's priority,
    which is not in the store yet at its own check."""
    if rule == "cat2a":
        return values(alarm=iri(alarm), now=now, incomingPrio=_incoming_prio(incoming_prio))
    return values(alarm=iri(alarm), now=now)


def silence_insert(rule: str, alarm: str, tgraph: str, now: str, incoming_prio=None) -> str:
    """Store `rule`'s (cat2a or cat2b) silence on `alarm`: one silencedBy
    link per active alarm justifying it."""
    if rule == "cat2a":
        bindings = values(alarm=iri(alarm), now=now, incomingPrio=_incoming_prio(incoming_prio), tgraph=tgraph)
    else:
        bindings = values(alarm=iri(alarm), now=now, tgraph=tgraph)
    return compose("silence_insert", bindings, RULES[rule].condition)


def silence_lift(ended_alarm: str, when: datetime) -> tuple:
    """(select, delete) run when `ended_alarm` stops being active: the
    still-active alarms it was the LAST justification for (their silence
    is lifted), and the removal of every silencedBy link to it."""
    return (compose("silence_lift_select", values(ended=iri(ended_alarm), now=dt(when)), CAT2_LIFT),
            compose("silence_lift_delete", values(ended=iri(ended_alarm))))


# --------------------------------------------------------------------
# Episodes: clinical events and CAT3
# --------------------------------------------------------------------

def evaluate_commands(kinds: set, rules: list, patient_id: str, now: datetime) -> list:
    """RDFox script lines that bring this patient's events of `kinds` up to
    date at logical time `now`. Empty when no kind is relevant."""
    ordered = [r.kind for r in rules if r.kind in kinds]
    if not ordered:
        return []
    patient = iri(M.patient_iri(patient_id))

    def state(kind):
        return values(patient=patient, now=dt(now), kind=iri(CLINICAL_EVENT + kind))

    lines = [f"# clinical events: {', '.join(ordered)} @ {now.isoformat()}"]
    for kind in ordered:
        lines.append(compose("episode_end", state(kind), RULE_BY_KIND[kind].condition))
    lines += trace_block("ended", compose("episode_ended_log", values(patient=patient)))
    lines.append(compose("episode_remove", values(patient=patient)))
    for kind in ordered:
        condition = RULE_BY_KIND[kind].condition
        event = M.event_iri(kind, patient_id, now)
        start = values(patient=patient, now=dt(now), kind=iri(CLINICAL_EVENT + kind),
                       event=iri(event), eventGraph=iri(f"{event}#event"))
        lines.append(compose("episode_extend", state(kind), condition))
        lines.append(compose("episode_start", start, condition))
        lines += trace_block("support", compose("episode_support", state(kind), condition))
    return lines
