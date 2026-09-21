"""
actions.py — what happens with a rule's result (RSP-QL: R2R, the effect).

CAT1 results are stored as flags, CAT2 results as silences; both live in
the affected alarm's own transient graph, so they end with it.
"""

from __future__ import annotations

from datetime import datetime

import mint as M
from rules import ONDEMAND_QUERY_BODIES

XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"


# CAT1 flags are STORED, in the flagged alarm's own transient graph (so a
# flag disappears when its alarm ends), naming the node the verdict rests
# on. Two consumers read them:
#   - clinical events (clinical_events.py): a flagged alarm is no evidence
#     for any condition (agreed 2026-09-21). build_script runs the CAT1
#     checks before an arrival's clinical-event evaluation, so a flag is in
#     place before the alarm could ever count.
#   - CAT1b's withdrawal: the flag is withdrawn when the IBP pathway that
#     justified it raises an alarm while the asystole is still active
#     (agreed 2026-09-21) — in a genuine asystole the pressure alarm
#     naturally follows the ECG alarm by seconds, so the flag set at onset
#     must not stand. An alarm triggered by that functional unit deletes it;
#     the deletion is traced ("withdraw <patient> <time>") and logged as a
#     cat1b_withdrawn firing (event_log.py). A CAT1a flag rests on a signal
#     or sensor, never a functional unit, so a withdrawal never touches it.
_FLAGGED = f"<{M.MDAPOC}flaggedLikelyFalsePositive>"


def cat1_flag_insert(rule: str, alarm: str, tgraph: str, now_literal: str) -> str:
    """Store `rule`'s (cat1a or cat1b) flag on `alarm`, if it holds."""
    return (f"INSERT {{ GRAPH {tgraph} {{ ?alarm {_FLAGGED} ?evidence }} }} WHERE {{ "
            f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }} {ONDEMAND_QUERY_BODIES[rule]} }}")


def cat1b_withdraw(alarm: str) -> tuple:
    """(select, delete): the stored cat1b flags resting on the functional
    unit that triggered `alarm`, and their removal."""
    where = (f"VALUES ?new {{ <{alarm}> }} "
             f"GRAPH ?gNew {{ ?new <{M.MDA}hasMessage> ?newMsg . ?newMsg <{M.MDA}triggeredBy> ?fu . }} "
             f"GRAPH ?gFlag {{ ?flagged {_FLAGGED} ?fu }}")
    return (f"select distinct ?flagged ?new where {{ {where} }}",
            f"DELETE {{ GRAPH ?gFlag {{ ?flagged {_FLAGGED} ?fu }} }} WHERE {{ {where} }}")


# A CAT2 silence (CAT2a or CAT2b) holds only while an active alarm justifies
# it (agreed 2026-09-21): every active alarm that justified it at arrival,
# under either rule, is stored as `incoming mdapoc:silencedBy active` in the
# incoming alarm's own transient graph (so it disappears when that alarm
# ends). When an active alarm's transient graph is dropped, its silencedBy
# links are removed; an alarm left with none from either rule, and still
# active, has its silence lifted — traced ("lift <patient> <time>") and
# logged as a cat2_lifted firing. One silence per alarm, whatever the rules
# behind it: that is how it is presented to a clinician.
_SILENCED_BY = f"<{M.MDAPOC}silencedBy>"


def cat2a_values(alarm: str, now_literal: str, incoming_prio) -> str:
    return f"VALUES (?alarm ?now ?incomingPrio) {{ (<{alarm}> {now_literal} <{incoming_prio}>) }}"


def cat2b_values(alarm: str, now_literal: str) -> str:
    return f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }}"


def cat2_silence_insert(rule: str, values: str, tgraph: str) -> str:
    return (f"INSERT {{ GRAPH {tgraph} {{ ?alarm {_SILENCED_BY} ?active }} }} WHERE {{ "
            f"{values} {ONDEMAND_QUERY_BODIES[rule]} }}")


def cat2_lift(ended_alarm: str, when: datetime) -> tuple:
    """(select, delete) run when `ended_alarm` stops being active: the
    still-active alarms it was the LAST justification for (their silence
    is lifted), and the removal of every silencedBy link to it."""
    when_literal = f'"{when.isoformat()}"^^{XSD_DATETIME}'
    link = f"GRAPH ?gS {{ ?silenced {_SILENCED_BY} <{ended_alarm}> }}"
    select = (f"select distinct ?silenced ?ended where {{ {link} "
              f"?gS <{M.MDAPOC}validUntil> ?gS_until . FILTER({when_literal} < ?gS_until) "
              f"FILTER NOT EXISTS {{ GRAPH ?gS2 {{ ?silenced {_SILENCED_BY} ?other }} "
              f"FILTER(?other != <{ended_alarm}>) }} "
              f"BIND(<{ended_alarm}> AS ?ended) }}")
    delete = f"DELETE {{ {link} }} WHERE {{ {link} }}"
    return select, delete
