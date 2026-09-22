"""
actions.py — what a rule's result does (RSP-QL: R2R, the effect). Picks the
action template and its bindings; rules.compose builds the command.

FLAG (CAT1). Stored in the flagged alarm's transient graph, so it ends with
the alarm. A flagged alarm is no evidence for a clinical event (CAT1 runs
before the episode evaluation). A cat1b flag is withdrawn when its IBP
pathway alarms while the asystole is active (rules/cat1b_withdraw.rq).

SILENCE (CAT2). One `incoming mdapoc:silencedBy active` link per justifying
alarm, in the incoming alarm's transient graph. When the last justifying
alarm ends while the silenced one is still active, the silence is lifted
(rules/cat2_lift.rq).

EPISODE (clinical events, CAT3). One event node per episode:

    event  a mda:ClinicalEvent, clinicalEvent:<Kind>
    event  mda:concernsPatient  patient
    event  mda:evidencedBy      evidence      (one or more)
    event  mda:hasStart         time
    event  mda:hasEnd           time          (only in the moment it ends)

An event outlives its first evidence and carries an end, so it is a
guarded update, not a derived fact:

    inactive + evidence   -> START:  create the event (hasStart = now)
    active   + evidence   -> EXTEND: add evidencedBy for new evidence
    active   + none left  -> END:    set hasEnd = now, log it, remove it

Evaluated only when evidence can change (an insert or a graph drop), at
that logical time, so start and end are exact. Order: END (single kinds
before combined), log and remove ended events, then EXTEND/START (single
before combined). The "support" and "ended" trace blocks feed event_log.py.
"""

from __future__ import annotations

from datetime import datetime

import mint as M
from event_log import trace_block
from rules import ALARMPRIO, CAT1B_WITHDRAW, CAT2_LIFT, RULE_BY_KIND, RULES, compose

XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"
CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"


def iri(value) -> str:
    """An IRI term."""
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
    """The rule's check query (for the firing log)."""
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
    """The incoming alarm's priority term; none -> alarmprio:Unknown."""
    return iri(incoming_prio if incoming_prio is not None else f"{ALARMPRIO}Unknown")


def silence_bindings(rule: str, alarm: str, now: str, incoming_prio=None) -> str:
    """A CAT2 check's bindings; cat2a also binds the incoming priority
    (not in the store yet at its own check)."""
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
    """(select, delete) when `ended_alarm` ends: the alarms whose silence
    it was the last justification for, and removal of its silencedBy links."""
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
