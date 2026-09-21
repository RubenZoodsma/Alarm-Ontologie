"""
replay_driver.py — Phase 2 window operator, replay/validation mode.

Self-contained: imports only engine/mint.py (this folder's own forked copy
of the real MDA-framework grounding pipeline — see mint.py's own docstring
for what it is and why it exists instead of hand-authored data) and the
Python standard library — except scan_patient_ids/load_events_for_patients,
which lazily import pandas (only paid for callers that actually use the
large-corpus loading path; load_events/group_by_patient below stay
stdlib-only for the regression harness).

Reads DATA/CAT_evaluation/events_data.csv (a DATA file — referenced by
path, not duplicated, since it's the input corpus, not framework
knowledge) in arrival order per patient, and replays each alarm's
arrive/end/end+window lifecycle as real RDFox transactions:

  - Alarm arrives: mint its knowledge via mint.py (the real
    extraction+minting pipeline, not reinvented), split into:
      * transient graph: the message (alarm_message + add_triggered_by)
        and condition (condition_for_event) content — valid [start, end].
      * persistent graph: the structural/background content
        (background_for_key) plus mda:isMonitoredBy — valid
        [start, end + PT15M]. Per the plan's own §1 design, EVERY alarm's
        structural content gets the full post-alarm window unconditionally
        (not gated on whether it happens to carry persisting operation
        state — that gate belongs to the legacy rdflib engine's
        revealing_at, which this redesign does not carry over; see the
        plan's Phase 0 "second finding" and correction #1: no tier of
        knowledge here is ever kept around indefinitely, so simply always
        scheduling the drop is what keeps this compliant, not a
        conditional skip).
  - Alarm ends: DROP the transient graph.
  - Alarm end + PT15M: DROP the persistent graph (always scheduled, never
    conditionally skipped — see above).
  - Nothing else. An alarm's graphs hold exactly what that alarm
    reported, from its arrival to its own end (+PT15M); no other alarm's
    arrival or end ever edits them. Shared particulars (device, sensor,
    signal, metric, ...) are re-minted into each alarm's own graphs, so a
    shared node can carry several states at once — one per active alarm
    reporting it — and every rule reads a state through the alarm that
    reports it. (REMOVED 2026-09-21: kb.last_wins retract-on-insert /
    restore-on-end, which edited OTHER alarms' graphs by design. It had
    never taken effect: each alarm's drop command was built at its
    arrival, which popped its own stack entry at once, so the stack was
    always empty — 0 retractions over patient 2826's 5,926 alarms.)

Identity resolution (mint.py's resolve_identity) is re-run per arriving
alarm over that patient's events STRICTLY so far (start <= this alarm's
own start), never the full future history — the same discipline
assess.py's own resolve_identity docstring documents and requires.

Time is LOGICAL, not wall-clock — this replays a fixed historical corpus,
not a live stream. A real production scheduler is out of scope for this
phase — see the plan's Phase 2 entry.

RDFox mechanics used, all confirmed by direct testing:
  - Named-graph insert: TriG `GRAPH <iri> { ... }` via `import <file>`.
  - Named-graph drop: `DELETE WHERE { GRAPH <iri> { ?s ?p ?o } }`.
  - `import`/`DELETE`/`INSERT DATA` all run as plain shell-script lines;
    a script needs a trailing `quit` and must be run with stdin closed.

Usage
-----
  cd MDA-POC-RDFox/engine
  python3 replay_driver.py
"""

from __future__ import annotations

import csv
import itertools
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import clinical_events as CE
import event_log as EL
import mint as M

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET = ROOT / "DATA" / "CAT_evaluation" / "events_data.csv"
LICENSE = ROOT / "RDFox.lic"
RDFOX_BIN = Path.home() / "Downloads" / "RDFox-macOS-arm64-7.6b" / "RDFox"
REPRESENTATION_DIR = Path(__file__).resolve().parent.parent / "representation"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

WINDOW = timedelta(minutes=15)  # ontology.ttl's postAlarmValidityDuration

# CAT1/CAT2 fabricated patients: expected flaggedLikelyFalsePositive/
# silencedBy outcome. The CAT3 patients below are included here too
# (all False) as a sanity check that neither CAT3 fixture accidentally
# also trips a CAT1/CAT2 rule — see EXPECTED_CAT3A/EXPECTED_CAT3B for
# their own, actual expected outcome (whether the clinical event log holds
# a CardioRespiratoryArrest / VentilationFailure event for the patient).
EXPECTED = {
    "cat1a_pos": True, "cat1a_neg": False,
    # cat1a_tech: a technical alarm on a bad signal is never flagged itself.
    # cat1a_sibling: an ECG-signal quality problem does not flag a
    #   respiration-rate alarm (ECG_lead_Impedance) on the same sensor, even
    #   while a heart-rate alarm on the bad signal is active.
    # cat1a_otherfu: an SpO2 fault does not flag a heart-rate alarm.
    # cat1a_sensor_pos/neg: a sensor fault (leads off) flags an alarm on its
    #   own pathway (asystole), not one on another pathway (SpO2 sensor off
    #   vs heart rate). See cat1a_signal_quality.dlog.
    "cat1a_tech": False, "cat1a_sibling": False, "cat1a_otherfu": False,
    "cat1a_sensor_pos": True, "cat1a_sensor_neg": False,
    "cat1b_pos": True, "cat1b_neg": False,
    # identity_collision: two alarms start in the same second on one
    #   monitor; the first to end must not take the other's content with
    #   it (mint.alarm_key). The low MAP still active then silences a low
    #   MAP arriving on a second monitor.
    "identity_collision": True,
    # cat1b_borrow: the asystole is flagged; a heart-rate alarm arriving
    #   while it is active is not (FORBIDDEN_FIRINGS) — it is no asystole.
    #   Still fires overall: the asystole's flag, and a CAT2a silence.
    # cat1b_ecmo: an ECMO circuit pressure is no arterial line — no flag.
    # cat1b_other_monitor: the arterial line may be on another patient
    #   monitor (Draeger vs Philips) — flagged.
    # cat1b_withdraw: flagged at onset, withdrawn when the arterial line
    #   alarms 8 s later — not flagged in the end.
    "cat1b_borrow": True, "cat1b_ecmo": False, "cat1b_other_monitor": True,
    "cat1b_withdraw": False,
    "cat2a_pos": True, "cat2a_neg": False,
    # CAT2a, agreed decisions 2026-09-21 (cat2a_process_priority.dlog):
    # cat2a_pos: severe bradycardia active, bradycardia arrives — same
    #   direction, not more severe, lower priority: redundant, silenced.
    # cat2a_opposite: tachycardia active, bradycardia arrives — reversal.
    # cat2a_escalation: severe bradycardia active, asystole arrives.
    # cat2a_reversal: extreme tachycardia active, asystole arrives.
    # cat2a_hr_map: bradycardia active, low MAP arrives — arterial pressure
    #   belongs to two processes, so it only matches arterial pressure.
    # cat2a_technical: a technical alarm is never the incoming alarm.
    # cat2a_unknown: an Unknown-priority alarm is never silenced.
    # cat2a_gate: low MAP on two monitors — a metric subtype (_Mean) that
    #   the former hand-kept gate excluded; silenced (cat2a and cat2b).
    # cat2a_lift: silenced, lifted when the silencing alarm ends while the
    #   silenced one continues — not silenced in the end.
    # cat2a_lift_last: silenced by two alarms; lifted only when the LAST
    #   of them ends (FORBIDDEN_FIRINGS: not at the first one's end).
    "cat2a_opposite": False, "cat2a_escalation": False, "cat2a_reversal": False,
    "cat2a_hr_map": False, "cat2a_technical": False, "cat2a_unknown": False,
    "cat2a_gate": True, "cat2a_lift": False, "cat2a_lift_last": False,
    "cat2b_pos": True, "cat2b_neg": False,
    # CAT2b, agreed decisions 2026-09-21 (cat2b_metric_sensor.dlog):
    # cat2b_opposite: SpO2 low on one monitor, SpO2 high on another — the
    #   sensors disagree; that is information, not redundancy.
    # cat2b_escalation: low SpO2 active, severe desaturation arrives.
    # cat2b_same_sensor: "Storing in SpO2" refines the sensor (Peripheral)
    #   between two "SpO2 laag" alarms on ONE monitor, splitting one physical
    #   sensor into two identities — not "a different sensor"
    #   (FORBIDDEN_FIRINGS). Still fires overall: a genuine CAT2a silence.
    # cat2b_lift: silenced by another monitor's low SpO2; lifted when it ends.
    "cat2b_opposite": False, "cat2b_escalation": False, "cat2b_same_sensor": True,
    "cat2b_lift": False,
    "cat3a_pos": False, "cat3a_neg": False, "cat3c": False,
    "cat3b_pos": False, "cat3b_neg": False, "cat3d": False, "cat3e": False,
    # cat3f's second asystole comes from another sensor with the same
    # metric type while the first is active — a genuine CAT2b (and CAT2a)
    # silence — but the first ends at 08:01 while the second runs to
    # 08:02, so the silence is lifted: not silenced in the end.
    "cat3f": False,
    # cat3a_flagged: CAT1a flags the asystole (ECG signal impaired).
    # cat3a_withdraw: CAT1b's flag on the asystole is withdrawn when the
    #   arterial line alarms — not flagged in the end.
    "cat3a_flagged": True, "cat3a_withdraw": False,
    "cat3b_leak": False, "cat3b_circuit": False, "cat3b_disconnect": False,
    "cat3b_swap": False, "cat3b_datalink": False,
    # cat3b_cpap: the low minute volume could be a CAT2a redundancy of the
    #   active CPAP low (same process), but its priority is Unknown — never
    #   silenced (see cat2a_unknown).
    "cat3b_cpap": False,
    # cat2a_peak: high tidal volume active, high peak pressure arrives —
    #   same process (pulmonary ventilation), same direction, equal
    #   priority. Exercises the AirwayPressure_Peak bridge (decision 39):
    #   without it the peak-pressure alarm reaches no process at all.
    "cat2a_peak": True,
}

# CAT3a (cardiorespiratory arrest): a cardiac arrest (Asystolie) and a
# respiratory arrest (Apneu) present at the same time — no tolerance window
# (clinical_events.py, EVENT_RULES).
#   cat3a_pos: 08:00:00-08:01:00 / 08:00:30-08:01:30 — genuine 30s overlap.
#   cat3a_neg: 08:00:00-08:00:20 / 08:00:40-08:01:00 — 20s gap, no overlap.
#   cat3c: same overlap as cat3a_pos, arrival order swapped (Apneu first)
#     — confirms order doesn't matter, no ?alarm anchor in the check.
#   cat3a_flagged: the asystole is CAT1a-flagged, hence no evidence — no
#     cardiac arrest, no CAT3a.
#   cat3a_withdraw: the asystole's CAT1b flag is withdrawn at 08:00:20 —
#     CAT3a from then on.
EXPECTED_CAT3A = {"cat3a_pos": True, "cat3a_neg": False, "cat3c": True,
                  "cat3a_flagged": False, "cat3a_withdraw": True}

# CAT3b (ventilation failure): reduced pulmonary function (low minute
# volume) while the ventilation therapy is compromised (a leak, a
# malfunctioning ventilator, a disconnected patient circuit), both at the
# same time (clinicalEvents.ttl, VentilationFailure).
#   cat3b_pos: Ventilator storing 08:00:00-08:01:00 / MV ondergrens
#     08:00:30-08:01:30 — direct overlap.
#   cat3b_neg: fault ends 08:00:20, low minute volume at 08:20:00.
#   cat3d: same overlap as cat3b_pos, arrival order swapped.
#   cat3e: fault ends 08:00:20, low minute volume at 08:10:00 — the device
#     state still persists, but its alarm is over: negative (positive
#     before 2026-09-21, when the fault counted for its 15-minute window).
#   cat3b_leak / cat3b_circuit: a leak / a disconnected circuit on the
#     same ServoU — positive.
#   cat3b_disconnect: Datex "Patient Disconnected" (a patient circuit
#     disconnection) with low minute volume on ANOTHER ventilator —
#     positive, no same-ventilator constraint.
#   cat3b_swap: the ventilator-swap pattern (disconnected from one
#     ventilator, low minute volume on the next 5 min later) — negative.
#   cat3b_datalink: a lost data link is no ventilation failure — negative.
#   cat3b_cpap: a HULBUS in CPAP mode (therapeuticModality:CPAP, a subclass
#     of VentilationTherapy) with a leak, low minute volume on a ServoU —
#     positive; CPAP alarms leave CAT3b intact.
EXPECTED_CAT3B = {"cat3b_pos": True, "cat3b_neg": False, "cat3d": True, "cat3e": False,
                  "cat3b_leak": True, "cat3b_circuit": True, "cat3b_disconnect": True,
                  "cat3b_swap": False, "cat3b_datalink": False, "cat3b_cpap": True}

# Exact clinical events for patients where timing matters — (kind, start,
# end, number of supporting alarms). hasStart/hasEnd must be when the
# criterion started/stopped holding, never when the driver noticed.
#   cat3a_pos: the combined event runs from the LATER constituent start
#     (Apneu 08:00:30) to the EARLIER constituent end (Asystolie 08:01:00).
#   cat3e: the ventilator fault ended at 08:00:20; low minute volume at
#     08:10:00 is reduced pulmonary function on its own, no ventilation
#     failure.
#   cat3b_leak: ventilation failure from the later start (08:00:20) to the
#     earlier end (the leak, 08:01:00).
#   cat3f: two asystole alarms on two devices, 08:00:00–08:01:00 and
#     08:00:30–08:02:00 — ONE cardiac arrest, extended by the second
#     alarm's evidence, ending when the LAST evidence ends (08:02:00).
EXPECTED_EVENTS = {
    "cat3a_pos": {
        ("CardiacArrest", "08:00:00", "08:01:00", 1),
        ("RespiratoryArrest", "08:00:30", "08:01:30", 1),
        ("CardioRespiratoryArrest", "08:00:30", "08:01:00", 2),
    },
    "cat3e": {
        ("ReducedPulmonaryFunction", "08:10:00", "08:10:30", 1),
    },
    "cat3b_leak": {
        ("ReducedPulmonaryFunction", "08:00:20", "08:01:30", 1),
        ("VentilationFailure", "08:00:20", "08:01:00", 2),
    },
    "cat3b_cpap": {
        ("ReducedPulmonaryFunction", "08:00:20", "08:01:30", 1),
        ("VentilationFailure", "08:00:20", "08:01:00", 2),
    },
    "cat3f": {
        ("CardiacArrest", "08:00:00", "08:02:00", 2),
    },
    # cat3a_flagged: the flagged asystole supports nothing; the apnoea's
    #   respiratory arrest stands alone.
    "cat3a_flagged": {
        ("RespiratoryArrest", "08:00:20", "08:01:10", 1),
    },
    # cat3a_withdraw: the asystole counts from the withdrawal (08:00:20),
    #   so the cardiac arrest — and CAT3a — start there, not at its onset.
    "cat3a_withdraw": {
        ("RespiratoryArrest", "08:00:05", "08:01:30", 1),
        ("CardiacArrest", "08:00:20", "08:01:00", 1),
        ("CardioRespiratoryArrest", "08:00:20", "08:01:00", 2),
    },
}

# Firings that must appear in rule_firings.csv, as (rule, arriving alarm,
# causing alarms) — taken from the fixture design (DATA/CAT_evaluation/
# README.md), so the log is checked to name the alarm that actually caused
# each firing. Other firings for the same patient are allowed (and printed).
EXPECTED_FIRINGS = {
    "cat1a_pos": {("cat1a", "PHILIPSMONITOR - SpO2 laag", "PHILIPSMONITOR - Storing in SpO2")},
    "cat1a_sensor_pos": {("cat1a", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - ECG alle leads los")},
    "cat3a_flagged": {("cat1a", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - Niet te leren ECG")},
    "cat1b_pos": {("cat1b", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - ABP verkleinen")},
    "cat2a_pos": {("cat2a", "PHILIPSMONITOR - Lage hartfreq.", "PHILIPSMONITOR - Extr. lage hartfreq.")},
    "cat2a_peak": {("cat2a", "DATEXOHMEDACOMVENTILATOR - Ppeak High", "DATEXOHMEDACOMVENTILATOR - VTexp High")},
    "cat2a_gate": {("cat2a", "PHILIPSMONITOR - ABPm laag", "PHILIPSMONITOR - ABPm laag")},
    "identity_collision": {("cat2a", "PHILIPSMONITOR - ABPm laag", "PHILIPSMONITOR - ABPm laag")},
    "cat2a_lift": {
        ("cat2a", "PHILIPSMONITOR - Lage hartfreq.", "PHILIPSMONITOR - Extr. lage hartfreq."),
        ("cat2_lifted", "PHILIPSMONITOR - Lage hartfreq.", "PHILIPSMONITOR - Extr. lage hartfreq."),
    },
    "cat2a_lift_last": {
        ("cat2a", "PHILIPSMONITOR - Lage hartfreq.",
         "PHILIPSMONITOR - Extr. lage hartfreq. + PHILIPSMONITOR - Asystolie"),
        ("cat2_lifted", "PHILIPSMONITOR - Lage hartfreq.", "PHILIPSMONITOR - Asystolie"),
    },
    "cat2b_pos": {("cat2b", "PHILIPSMONITOR - SpO2 laag", "Monitor - Lage SpO2")},
    "cat3f": {("cat2b", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - Asystolie"),
              ("cat2_lifted", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - Asystolie")},
    "cat2b_lift": {("cat2b", "PHILIPSMONITOR - SpO2 laag", "Monitor - Lage SpO2"),
                   ("cat2_lifted", "PHILIPSMONITOR - SpO2 laag", "Monitor - Lage SpO2")},
    "cat1b_borrow": {("cat1b", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - ABP verkleinen")},
    "cat1b_other_monitor": {("cat1b", "PHILIPSMONITOR - Asystolie", "Monitor - Lage ARTmean")},
    "cat1b_withdraw": {
        ("cat1b", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - ABP verkleinen"),
        ("cat1b_withdrawn", "PHILIPSMONITOR - Asystolie", "PHILIPSMONITOR - ABPm laag"),
    },
}

# Firings that must NOT appear, as (rule, alarm label) or (rule, alarm
# label, causing alarm labels) — for failure modes a patient-level
# fire/no-fire check cannot see, because the patient fires for another,
# legitimate reason (or, for a lift, because the timing is what matters).
FORBIDDEN_FIRINGS = {
    "cat2b_same_sensor": {("cat2b", "PHILIPSMONITOR - SpO2 laag")},
    "cat2a_lift_last": {("cat2_lifted", "PHILIPSMONITOR - Lage hartfreq.",
                         "PHILIPSMONITOR - Extr. lage hartfreq.")},
    "cat1b_borrow": {("cat1b", "PHILIPSMONITOR - Lage hartfreq."),
                     ("cat1b_withdrawn", "PHILIPSMONITOR - Asystolie")},
}


@dataclass
class Event:
    patient: str
    label: str
    device_id: str
    start: datetime
    end: datetime
    # ALARM_ID: this alarm occurrence's own unique identifier, part of its
    # IRI (mint.alarm_key). Taken from the file's `alarm_id` column when
    # present (the corpus: the row number in the locked source data, see
    # data/tools/export_rdata.R), otherwise the 1-based data-row number in
    # the file being read — unique and stable for as long as that file is.
    alarm_id: str


def _alarm_ids(header: list, rows: list) -> list:
    """The ALARM_ID of each data row: its `alarm_id` column, or its
    1-based data-row number when the file has none."""
    if "alarm_id" in header:
        col = header.index("alarm_id")
        return [row[col] for row in rows]
    return [str(i) for i in range(1, len(rows) + 1)]


def load_events(path: Path) -> list:
    with path.open(encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = [row for row in reader if row]
    return [Event(row[0], row[1], row[2], datetime.fromisoformat(row[3]), datetime.fromisoformat(row[4]),
                  alarm_id)
            for row, alarm_id in zip(rows, _alarm_ids(header, rows))]


def group_by_patient(events: list) -> dict:
    groups: dict = {}
    for e in events:
        groups.setdefault(e.patient, []).append(e)
    return groups


def scan_patient_ids(path: Path) -> list:
    """Every distinct patientID in a large events CSV, without building a
    single Event. Uses pandas (a lazy import — the only place in this
    module that needs a dependency beyond the standard library) reading
    just the patientID column: pandas' C parser only tokenizes the one
    column asked for, whereas a plain csv.reader loop still pays full
    per-row tokenization cost for every column even when only row[0] is
    read — confirmed directly the naive version of this function (a
    csv.reader loop reading only row[0]) still took ~34s against pandas'
    ~7s on the real 13.8M-row corpus, because tokenizing all 5 columns
    per row, not date-parsing, is what actually dominates at that scale."""
    import pandas as pd
    ids = pd.read_csv(path, sep=";", usecols=["patientID"], dtype=str)
    return sorted(ids["patientID"].unique())


def load_events_for_patients(path: Path, patient_ids: set) -> list:
    """Build Event objects ONLY for rows whose patientID is in
    `patient_ids`. Also pandas-based, for the same reason as
    scan_patient_ids: reads the whole file (pandas has no way to skip
    rows before parsing them, so this cost doesn't shrink with a smaller
    `patient_ids`), but its C parser does that full read far faster than
    a Python-level csv.reader loop does even when the loop itself skips
    most rows — confirmed directly (~26s pandas vs ~28s csv.reader
    despite the csv.reader version constructing far fewer Event objects)
    on the real 13.8M-row corpus. Pair with scan_patient_ids to choose
    `patient_ids` first."""
    import pandas as pd
    df = pd.read_csv(path, sep=";", dtype=str)
    if "alarm_id" not in df.columns:
        df["alarm_id"] = [str(i) for i in range(1, len(df) + 1)]  # before filtering, like load_events
    df = df[df["patientID"].isin(patient_ids)]
    return [
        Event(row.patientID, row.label, row.device_id,
              datetime.fromisoformat(row.start), datetime.fromisoformat(row.end), row.alarm_id)
        for row in df.itertuples(index=False)
    ]


def graph_iri(alarm: str, suffix: str) -> str:
    return f"<{alarm}#{suffix}>"


VALID_FROM = f"<{M.MDAPOC}validFrom>"
VALID_UNTIL = f"<{M.MDAPOC}validUntil>"
XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"


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


class Driver:
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
        # justified (see cat2_lift).
        self.lift_cat2 = cat2_lift
        # Enabled clinical-event rules (clinical_events.EVENT_RULES order).
        # A dropped graph can take away an event's evidence, so each drop of
        # an alarm that is relevant to an event kind re-evaluates that kind
        # at the drop's own logical time — see clinical_events' docstring.
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
            select, delete = cat2_lift(graph[1:].split("#", 1)[0], event.end)
            cmd += "\n" + "\n".join(CE.trace_block(f"lift {event.patient} {event.end.isoformat()}", select))
            cmd += "\n" + delete
        return cmd

    def _with_events(self, cmd: str, event: Event, when: datetime, event_kinds) -> str:
        lines = CE.evaluate_commands(event_kinds, self.event_rules, event.patient, when)
        return "\n".join([cmd] + lines)

    def _drop_persistent_cmd(self, event: Event, graph: str) -> str:
        return (f"# window-expiry: {event.label} @ {event.device_id}\n"
                f"DELETE WHERE {{ GRAPH {graph} {{ ?s ?p ?o }} }}")


RULES_DIR = REPRESENTATION_DIR / "rules"
CLINICAL_EVENTS_MODULE = Path(__file__).resolve().parent / "clinical_events.py"

FRAMEWORK_FILES = [
    DATA_DIR / "ontology.ttl",
    DATA_DIR / "vocab_generated.ttl",
    DATA_DIR / "clinicalEvent_vocab.ttl",
    DATA_DIR / "inference.ttl",
    DATA_DIR / "priority_rank.ttl",
    # mdapoc: — the POC's own terms (graph validity, silencing, false-
    # positive flags, metric-state order), outside the mda: ontology.
    DATA_DIR / "mdapoc.ttl",
    # Direction/severity of each metric state (mdapoc:) — CAT2's
    # redundancy test.
    DATA_DIR / "metric_state_order.ttl",
    # A .dlog rules file, not framework .ttl data — deliberately mixed in
    # here rather than RULE_FILES below: it's prerequisite infrastructure
    # (mda:approximates, consumed by cat2a), not an
    # optional domain rule a caller would ever want to disable. Replaces
    # the per-alarm owlrl.DeductiveClosure call mint.py used to make (see
    # that file's module docstring) — RDFox derives these natively now.
    RULES_DIR / "approximates_bridge.dlog",
]

# One file per rule (representation/rules/) so a caller (poc_entry.py) can
# enable/disable each independently — split out of the original combined
# clinical_rules.dlog/cat_rules.dlog for exactly that reason.
RULE_FILES = {
    # Clinical-event rules (clinical_events.EVENT_RULES): never imported —
    # they run as guarded SPARQL updates at arrival and drop times. Point at
    # the module that holds them, for traceability.
    "cardiac_arrest": CLINICAL_EVENTS_MODULE,
    "respiratory_arrest": CLINICAL_EVENTS_MODULE,
    "reduced_pulmonary_function": CLINICAL_EVENTS_MODULE,
    "cat1a": RULES_DIR / "cat1a_signal_quality.dlog",
    "cat1b": RULES_DIR / "cat1b_asystole_ibp.dlog",
    "cat2a": RULES_DIR / "cat2a_process_priority.dlog",
    "cat2b": RULES_DIR / "cat2b_metric_sensor.dlog",
    # CAT3a/CAT3b: the combined clinical events (cardiorespiratory arrest,
    # ventilation failure) — same mechanism as the three rules above; each
    # requires its constituent rules (clinical_events.EventRule.requires).
    "cat3a": CLINICAL_EVENTS_MODULE,
    "cat3b": CLINICAL_EVENTS_MODULE,
}


# cat2a/cat2b are NOT imported as standing Datalog rules (unlike every other
# name in RULE_FILES) — build_script instead runs ONDEMAND_QUERY_BODIES below
# as a one-shot `select ... limit 1` at each alarm's own check point. Why:
# both rules' only new predicate (mdapoc:silencedBy) has exactly one consumer —
# that same per-alarm check — so there's no reason to pay for RDFox
# continuously, incrementally re-maintaining their 14-atom self-join against
# every relevant transaction in the WHOLE store for the rule's entire
# lifetime, when the answer is only ever read once, at one specific instant.
# Root cause confirmed by direct RDFox instrumentation (not inferred): the
# expense is RDFox's incremental "delete, then re-derive" maintenance for a
# STANDING rule, triggered by every relevant import/DELETE WHERE anywhere in
# the store — proportional to how many standing rules could be affected by
# what changed, NOT to how much actually matches right now (a fresh,
# stateless query shaped identically to a standing rule's own join,
# re-issued over the exact same accumulated state, answered in 0.000s; the
# structural fan-out at that exact point was trivial — every hop count 1).
# cat1a/cat1b/cardiac_arrest/respiratory_arrest/reduced_pulmonary_function
# join within a SINGLE alarm's own chain (lower combinatorial risk than
# cat2a/cat2b's cross-alarm self-join), but all reference at least one
# `kb.last_wins_str`-tagged predicate (hasQualityState, hasValueState, hasRhythm) — the same
# argument applies: each predicate they read has exactly one consumer (this
# same per-alarm/per-check point), so there's no reason to pay standing
# incremental maintenance for a value nothing else ever reads back.
#
# IMPORTANT: this does NOT touch Driver's physical graph-drop mechanism
# (_drop_transient_cmd/_drop_persistent_cmd) — it stays exactly as the project's own design doc
# (~/.claude/plans/we-re-going-for-the-magical-candy.md) specifies: a
# landmark/sliding-window RDF-stream-processing model where physically
# dropping a graph the instant it's invalid IS the discard mechanism, and
# "currently in the store" and "currently valid" are the same condition by
# construction — the plan's own Phase 1 finding is exactly why NONE of
# these on-demand query bodies below need any validFrom/validUntil interval
# filtering: physical dropping already guarantees it. An earlier version of
# this fix considered replacing physical dropping with an append-only store
# filtered by explicit validity intervals at query time — reconsidered
# because that would make the store grow unboundedly for the life of a
# batch, directly working against the landmark-window discard model that's
# this project's actual RDF-stream-processing design, and turned out to be
# unnecessary once the real root cause (above) was understood: it's
# standing-rule maintenance cost, not physical deletion itself, that's
# expensive.
# HISTORY (2026-09): cardiac_arrest/respiratory_arrest/
# reduced_pulmonary_function were standing .dlog rules tagging a shared
# process concept; they are now patient-scoped clinical-event updates
# (clinical_events.py), gated per alarm by relevance, which removes both
# the cross-patient leak and the unscoped cost described next.
# They were tried
# on-demand too and REVERTED — real-corpus timing (patient 2826) got WORSE,
# not better: multiple new stalls (several seconds to 20s each) plus a
# fresh dead stop near the end, timing out again. Root cause understood,
# not just observed: unlike cat1a/cat1b/cat2a/cat2b, these three checks are
# NOT alarm-scoped (impliesClinicalEvent's subject is a
# PhysiologicalProcess, not an alarm — see the check-emission site's own
# comment) and run UNCONDITIONALLY on every single alarm regardless of
# relevance, with no VALUES-bound candidate set to narrow the search. Under
# a standing rule, that same check is a cheap read against an answer
# RDFox maintains incrementally; on demand, it's a full, unscoped query
# recomputed from scratch on every alarm — the wrong trade for a check
# that's both unconditional and unbounded, even though it was the right
# trade for cat2a/cat2b (checked once each, alarm-scoped, but with a
# 14-atom cross-alarm self-join RDFox had to re-justify on every unrelated
# mutation in the store). On-demand conversion isn't a universal win — it
# only pays off when what's being converted was itself the standing-rule
# maintenance cost, not merely "any rule with a last-wins predicate."
ONDEMAND_RULE_NAMES = {"cat1a", "cat1b", "cat2a", "cat2b"}

# Each flagging body also binds ?witness: the named graph holding the fact
# that made the rule fire (for cat1a, the reported quality state; for
# cat1b, the IBP pathway's link to the patient). Alarm graphs are named
# <alarm#transient>/<alarm#persistent>, so the witness identifies the
# causing alarm — which is all the firing log records (event_log.py).
#
# Hand-translated equivalents of cat2a_process_priority.dlog's/
# cat2b_metric_sensor.dlog's rule BODIES (not the head — only the join
# itself is needed here). NOT auto-generated from those files: confirmed
# directly against the real RDFox 7.6b binary that its bracket quad syntax
# (`[?s,?p,?o] ?g`), used throughout every .dlog file in this project, is
# RULE-file-only — it errors ("Line 1, column 24: Resource expected.")
# inside a plain `select` command, which only accepts standard SPARQL
# (`GRAPH ?g { ?s ?p ?o }`). Translating one syntax into the other is a real
# structural rewrite (bracket atoms -> GRAPH blocks, Datalog's comma
# conjunction -> SPARQL's `.`), not a text substitution, so it's done here
# by hand instead of by a fragile auto-translator.
#
# MUST BE KEPT IN SYNC BY HAND with the corresponding .dlog file if its join
# logic ever changes — there is no automated link between the two. Verified
# equivalent (not just assumed) by running engine/replay_driver.py's own
# run() regression — the same cat2a_pos/cat2a_neg/cat2b_pos/cat2b_neg
# fabricated fixtures that validate the .dlog files themselves — against
# this on-demand version and confirming identical PASS/FAIL.
#
# `?alarm` is bound via a `VALUES` clause at the call site (build_script),
# not string-substituted into this template — avoids any risk of a
# substring match inside a longer variable name (e.g. `?alarmStart`).
_ALARMCAT = "https://w3id.org/mda/vocab/alarm-category/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_METRIC = "https://w3id.org/mda/vocab/metric/"
_METRIC_VALUE_STATE = "https://w3id.org/mda/vocab/metric-value-state/"
_METRIC_RHYTHM = "https://w3id.org/mda/vocab/metric-rhythm/"
_FUNCTIONAL_UNIT = "https://w3id.org/mda/vocab/functional-unit/"
_OPERATION_STATE = "https://w3id.org/mda/vocab/operation-state/"
_QUALITY_STATE = "https://w3id.org/mda/vocab/quality-state/"
_CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"
_DEVICE = "https://w3id.org/mda/vocab/device/"
_ALARMPRIO = "https://w3id.org/mda/vocab/alarm-priority/"
_SENSOR = "https://w3id.org/mda/vocab/sensor/"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_ONDEMAND_BODY_TEMPLATES = {
    # Alarm-scoped (bound via VALUES ?alarm at the call site), projects
    # ?signal — hand-translated from cat1a_signal_quality.dlog's body; see
    # that file for the clause-by-clause reading of the NL rule. Only a
    # PHYSIOLOGICAL alarm is flagged, and only for the signal on its own
    # sensing pathway: ?gT is its transient graph (category, message,
    # triggeredBy, producesMetric), ?gP a persistent copy of its
    # FunctionalUnit -> sensor -> signal -> analysis chain. The former
    # one-free-graph-variable-per-hop shape let hops come from another
    # alarm's chain: it flagged technical alarms, and flagged e.g. a
    # respiration-rate alarm (ECG_lead_Impedance) for an ECG_signal
    # quality problem whenever a heart-rate alarm supplied the missing hop.
    # Two ways the signal can be insufficient (the .dlog's two rules): a
    # quality state on the signal itself, or a fault state on the sensor
    # producing it (not being acquired at all — agreed extension of the NL
    # rule, 2026-09-21).
    "cat1a": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@triggeredBy> ?fu .
                    ?analysis <@MDA@producesMetric> ?metric . }
        GRAPH ?gP { ?fu <@MDA@hasSensor> ?sensor . ?sensor <@MDA@sensorProducesSignal> ?signal .
                    ?signal <@MDA@analyzedBy> ?analysis . }
        {
          GRAPH ?gQ { ?signal <@MDA@hasQualityState> ?quality }
          FILTER(?quality != <@QUALITYSTATE@Good>)
          BIND(?signal AS ?evidence)
        } UNION {
          GRAPH ?gQ { ?sensor <@MDA@hasSensorOperationState> ?sensorState }
          VALUES ?sensorState { <@OPSTATE@Disabled> <@OPSTATE@Disconnected> <@OPSTATE@Malfunction> }
          BIND(?sensor AS ?evidence)
        }
        BIND(?gQ AS ?witness)
    """,
    # Alarm-scoped, projects ?ibpFunctionalUnit — hand-translated from
    # cat1b_asystole_ibp.dlog; see that file for the clause-by-clause
    # reading of the NL rule and the agreed decisions (2026-09-21).
    #   - asystole: the arriving alarm's OWN metric (?gT, its transient
    #     graph) is a heart rate with an absent rhythm — not a metric
    #     borrowed from another alarm's graph.
    #   - IBP present: one IBP alarm's persistent graph (?gIbp, valid until
    #     15 min after that alarm ended) links this patient to an ARTERIAL
    #     transducer on a PATIENT MONITOR's IBP functional unit. ECMO
    #     circuit pressures and venous pressure never count.
    #   - without alarms on that pathway: no currently valid alarm is
    #     triggered by that functional unit — a literal NOT EXISTS, not a
    #     proxy over reported states. Its validity filter is written by
    #     hand: _append_validity_filters skips NOT EXISTS spans.
    # A flag is stored and withdrawn later if an alarm on the same pathway
    # arrives while the asystole is active — see CAT1B_FLAG_INSERT and
    # CAT1B_WITHDRAW below.
    "cat1b": """
        GRAPH ?gT { ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@RDFTYPE@> <@METRIC@HeartRate> .
                    ?metric <@MDA@hasRhythm> <@RHYTHM@Absent> . }
        GRAPH ?gIbp { ?patient <@MDA@isMonitoredBy> ?ibpDevice . ?ibpDevice <@RDFTYPE@> ?ibpDeviceType .
                      ?ibpDevice <@MDA@hasFunctionalUnit> ?ibpFunctionalUnit .
                      ?ibpFunctionalUnit <@RDFTYPE@> <@FUNCTIONALUNIT@FU_InvasiveBloodPressure> .
                      ?ibpFunctionalUnit <@MDA@hasSensor> ?ibpSensor .
                      ?ibpSensor <@RDFTYPE@> <@SENSOR@ABP_transducer> . }
        ?ibpDeviceType <@RDFS@subClassOf>* <@DEVICE@PhysiologicalMonitor> .
        FILTER NOT EXISTS {
          GRAPH ?gOther { ?other <@MDA@hasMessage> ?otherMsg . ?otherMsg <@MDA@triggeredBy> ?ibpFunctionalUnit . }
          ?gOther <@MDAPOC@validUntil> ?gOther_until . FILTER(?now <= ?gOther_until)
        }
        BIND(?ibpFunctionalUnit AS ?evidence)
        BIND(?gIbp AS ?witness)
    """,
    # Hand-translated from cat2a_process_priority.dlog; see that file for the
    # clause-by-clause reading and the agreed decisions (2026-09-21). ?alarm
    # (incoming), ?now and ?incomingPrio are bound via VALUES at the call
    # site: the incoming alarm's own hasPriority is only inserted after its
    # checks (Driver.insert_alarm's two phases), and an Unknown priority is
    # never checked at all. ?gT is the incoming alarm's transient graph, ?gA
    # an active alarm's — each pinned as one group, so no hop is borrowed
    # from another alarm. Returns one row per active alarm that justifies
    # the silence; all of them are stored (cat2_silence_insert) so the
    # silence can be lifted when the last one ends.
    "cat2a": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@MDA@approximates> ?property .
                    ?metric ?stateProp ?state . }
        VALUES ?stateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        ?property <@MDA@isPropertyOf> ?process .
        ?incomingPrio <@MDA@priorityRank> ?incomingRank .
        ?state <@MDAPOC@deviationDirection> ?direction . ?state <@MDAPOC@deviationSeverity> ?severity .

        GRAPH ?gA { ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?active <@MDA@hasMessage> ?activeMsg . ?activeMsg <@MDA@concernsPatient> ?patient .
                    ?active <@MDA@hasPriority> ?activePrio .
                    ?activeAnalysis <@MDA@producesMetric> ?activeMetric .
                    ?activeMetric <@MDA@approximates> ?activeProperty .
                    ?activeMetric ?activeStateProp ?activeState . }
        VALUES ?activeStateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        ?activeProperty <@MDA@isPropertyOf> ?process .
        ?activePrio <@MDA@priorityRank> ?activeRank .
        ?activeState <@MDAPOC@deviationDirection> ?direction . ?activeState <@MDAPOC@deviationSeverity> ?activeSeverity .

        FILTER(?active != ?alarm)
        FILTER(?activePrio != <@ALARMPRIO@Unknown>)
        FILTER(?activeRank >= ?incomingRank)
        FILTER(?activeSeverity >= ?severity)
        FILTER(?property = ?activeProperty || (
          NOT EXISTS { ?property <@MDA@isPropertyOf> ?otherProcess . FILTER(?otherProcess != ?process) } &&
          NOT EXISTS { ?activeProperty <@MDA@isPropertyOf> ?otherProcess2 . FILTER(?otherProcess2 != ?process) }))
        BIND(?gA AS ?witness)
    """,
    # Hand-translated from cat2b_metric_sensor.dlog; see that file for the
    # clause-by-clause reading and the agreed decisions (2026-09-21). ?alarm
    # (incoming) and ?now are bound via VALUES at the call site. ?gT/?gA are
    # the incoming/active alarm's transient graph, ?gP/?gAP a persistent copy
    # of its sensor chain, anchored on its own analysis. "Different sensor"
    # = a different functional unit or a different sensor type: refinements
    # (anatomical position) come only from technical alarms and split one
    # physical sensor into two identities depending on arrival order. One
    # row per active alarm justifying the silence; all are stored.
    "cat2b": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?msg <@MDA@triggeredBy> ?fu .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@RDFTYPE@> ?metricType .
                    ?metric ?stateProp ?state . }
        VALUES ?stateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        GRAPH ?gP { ?fu <@MDA@hasSensor> ?sensor . ?sensor <@MDA@sensorProducesSignal> ?signal .
                    ?signal <@MDA@analyzedBy> ?analysis . ?sensor <@RDFTYPE@> ?sensorType . }
        ?state <@MDAPOC@deviationDirection> ?direction . ?state <@MDAPOC@deviationSeverity> ?severity .

        GRAPH ?gA { ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?active <@MDA@hasMessage> ?activeMsg . ?activeMsg <@MDA@concernsPatient> ?patient .
                    ?activeMsg <@MDA@triggeredBy> ?activeFu .
                    ?activeAnalysis <@MDA@producesMetric> ?activeMetric . ?activeMetric <@RDFTYPE@> ?metricType .
                    ?activeMetric ?activeStateProp ?activeState . }
        VALUES ?activeStateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        GRAPH ?gAP { ?activeFu <@MDA@hasSensor> ?activeSensor .
                     ?activeSensor <@MDA@sensorProducesSignal> ?activeSignal .
                     ?activeSignal <@MDA@analyzedBy> ?activeAnalysis .
                     ?activeSensor <@RDFTYPE@> ?activeSensorType . }
        ?activeState <@MDAPOC@deviationDirection> ?direction . ?activeState <@MDAPOC@deviationSeverity> ?activeSeverity .

        FILTER(?active != ?alarm)
        FILTER(?activeSeverity >= ?severity)
        FILTER(?activeFu != ?fu || ?activeSensorType != ?sensorType)
        BIND(?gA AS ?witness)
    """,
    # The clinical-event rules have no template here: they are updates,
    # not checks — see clinical_events.py.
}
# Plan's §3: replaces "still physically present" with an explicit
# validity check, so a batched/delayed physical eviction (plan's §4) can't
# silently make a stale-but-not-yet-dropped graph look active. Only graph
# variables used OUTSIDE any FILTER NOT EXISTS or OPTIONAL span get a
# filter appended here — a variable scoped entirely inside a negation
# isn't bound in the outer WHERE at all (cat1b's ?g12 is handled by hand,
# inside its own negation, in the template above), and a variable scoped
# inside an OPTIONAL must stay genuinely optional: cat1b's rewritten
# criteria (see its own .dlog header) rely on "unbound passes" — auto-
# injecting a MANDATORY validUntil check for an OPTIONAL graph var would
# force it to always resolve, silently turning "optional" back into
# "required" and reintroducing the exact unsatisfiability bug that
# rewrite exists to fix.
_NOT_EXISTS_RE = re.compile(r"FILTER NOT EXISTS\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_OPTIONAL_RE = re.compile(r"OPTIONAL\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_GRAPH_VAR_RE = re.compile(r"GRAPH\s+(\?g\w*)\s*\{")


def _append_validity_filters(body: str) -> str:
    outer = _OPTIONAL_RE.sub(" ", _NOT_EXISTS_RE.sub(" ", body))
    graph_vars = sorted(set(_GRAPH_VAR_RE.findall(outer)))
    tail = " ".join(
        f"{v} <{M.MDAPOC}validUntil> {v}_until . FILTER(?now <= {v}_until)"
        for v in graph_vars
    )
    return f"{body} {tail}" if tail else body


ONDEMAND_QUERY_BODIES = {
    name: _append_validity_filters(" ".join(
        tmpl.replace("@MDAPOC@", str(M.MDAPOC)).replace("@MDA@", str(M.MDA)).replace("@ALARMCAT@", _ALARMCAT)
            .replace("@RDFTYPE@", _RDF_TYPE).replace("@METRIC@", _METRIC)
            .replace("@VALUESTATE@", _METRIC_VALUE_STATE).replace("@RHYTHM@", _METRIC_RHYTHM)
            .replace("@FUNCTIONALUNIT@", _FUNCTIONAL_UNIT)
            .replace("@QUALITYSTATE@", _QUALITY_STATE).replace("@OPSTATE@", _OPERATION_STATE)
            .replace("@DEVICE@", _DEVICE).replace("@SENSOR@", _SENSOR).replace("@RDFS@", _RDFS)
            .replace("@ALARMPRIO@", _ALARMPRIO)
            .split()
    ))
    for name, tmpl in _ONDEMAND_BODY_TEMPLATES.items()
}


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


def _alarm_functional_unit(kb, event) -> str | None:
    """The functional-unit concept name of this alarm's archetype."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return None
    concept = M.archetype_structure(kb, type_iri).concept(M.MDA.FunctionalUnit)
    return str(concept).rsplit("/", 1)[-1] if concept is not None else None

# cat2a/cat2b are no longer aggregate-based (2026-09-21): their redundancy
# test compares direction and severity (and, for cat2a, priority) of ONE
# active alarm at once, which per-key maxima or counts cannot express. Both
# run as on-demand queries with pinned graph groups (ONDEMAND_QUERY_BODIES);
# the standing rules in representation/rules/shadow/ are no longer loaded.
SHADOW_RULES_DIR = RULES_DIR / "shadow"


# Metric types representation/rules/approximates_bridge.dlog derives
# mda:approximates for. A metric type outside it can never satisfy cat2a's
# ?metric -> approximates -> ?property hop, so cat2a is skipped for it in
# Python: RDFox's planner does not discover that dead end cheaply (patient
# 2826, an ArterialBloodPressure_Mean alarm: 100+ s to prove no match;
# reordering or sub-querying the body did not change its plan).
# Derived from the bridge file itself (every `rdf:type, metric:X` rule
# body), not a hand-kept list: the hand-kept list went stale when the
# bridge gained its _Mean/_Minute/_Tidal/_EndExpiratory rules, silently
# excluding 14 alarm types from CAT2a as the incoming alarm.
APPROXIMATES_COVERED_METRIC_TYPES = set(re.findall(
    r"rdf:type,\s*metric:(\w+)\]", (RULES_DIR / "approximates_bridge.dlog").read_text()))


def _alarm_metric_types(kb, event) -> set:
    """Every distinct Metric-kind concept name (e.g. {"ArterialBloodPressure_Mean"})
    M.ground_chain would mint for this alarm's own archetype — used only to
    decide whether cat2a's on-demand query can possibly match (see
    APPROXIMATES_COVERED_METRIC_TYPES above), not part of any minted
    output itself. Cheap: archetype_structure is memoized on `kb`
    (kb.archetype_cache), so this is a small, already-cached tree walk,
    safe to call once per alarm."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return set()
    arch = M.archetype_structure(kb, type_iri)
    found = set()

    def walk(cls):
        concept = arch.concept(cls)
        if concept is not None and "vocab/metric/" in str(concept):
            found.add(str(concept).rsplit("/", 1)[-1])
        for child_cls, link in kb.tree.items():
            if link and link[0] == cls:
                walk(child_cls)

    walk(M.MDA.Device)
    return found


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = width if total <= 0 else min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def build_script(kb, patients: dict, scratch_dir: Path, enabled_rules=None,
                  progress: bool = True, verify_identity: bool = False,
                  dstore: str = "poc") -> tuple:
    """`enabled_rules`: iterable of RULE_FILES keys to import, or None for
    all of them (every rule enabled — the default validation behaviour).

    `dstore`: ONE dstore shared by every patient in `patients`, created
    and populated with the framework/rule files exactly once — not one
    dstore per patient (the original Phase 2 design). Import time itself
    was never the cost (single-digit ms/file, per RDFox's own logging);
    the problem was memory: at the project's 3500-patient target, 3500
    separate dstores each holding an independent copy of the ~3000-triple
    static framework multiplies that memory ~3500x for data that's
    identical across all of them. Safe to share now that every minted
    entity/alarm/message IRI is patient-scoped (see mint.py's MINTING
    section header) — before that fix, two patients sharing a real-corpus
    device_id would have collided onto one IRI in a shared dstore.

    `progress`: print a per-patient header line, then an in-place
    (carriage-return-updated) progress bar as that patient's own alarms
    are minted — never one line per alarm (a real patient can carry tens
    of thousands, see _progress_bar's own call site). Historically
    load-bearing when per-alarm minting ran a real OWL-RL closure
    (~2-3s/call, since removed — see mint.py's module docstring); kept on
    by default since a multi-thousand-patient run is still worth showing
    progress for even at the much lower per-alarm cost minting has now.

    `verify_identity`: development-only — also run the batch
    M.resolve_identity(kb, events_so_far) per alarm and assert it matches
    M.IdentityTracker's incremental result, mirroring how Timeline.
    observe() (op_knowledge.py) was itself originally validated. Doubles
    identity-resolution cost, so leave off by default; only turn on to
    re-confirm the equivalence after touching either implementation.

    KNOWN, HARMLESS FALSE-POSITIVE with this flag: when two of a
    patient's alarms share the exact same `start` instant (real corpus
    data — confirmed on alexKim_03), `events_so_far = [ev for ev in
    events_sorted if ev.start <= e.start]` includes BOTH tied alarms
    for the batch computation, while the incremental tracker has only
    processed the first of the two (in `events_sorted`'s stable order) at
    this exact check point — so the two can briefly disagree on an entry
    for the *other* tied alarm's device_id. Confirmed inert: each alarm's
    own grounding (`particular_iri`) only ever reads its OWN device_id's
    identity entry, never a different device's, and the tracker converges
    to the same content as soon as the second tied alarm is itself
    processed one iteration later. Verified end-to-end (not just via this
    assertion) by re-running poc_entry.py's exact same real-corpus sample
    before and after this port and confirming identical fire counts."""
    rule_names = list(RULE_FILES) if enabled_rules is None else list(enabled_rules)
    lines = []
    checks = []
    file_counter = itertools.count(1)
    t0 = time.monotonic()
    num_patients = len(patients)

    lines.append(f"dstore create {dstore}")
    lines.append(f"active {dstore}")
    for f in FRAMEWORK_FILES:
        lines.append(f"import {f}")
    lines.extend(CE.SCRIPT_PREAMBLE)
    for name in rule_names:
        if name in ONDEMAND_RULE_NAMES or name in CE.EVENT_RULE_NAMES:
            continue  # queried/updated per alarm below, not a standing rule
        lines.append(f"import {RULE_FILES[name]}")

    # Which on-demand rules are enabled for THIS run, grouped by which
    # check/predicate they feed — computed once, not per-alarm, since
    # `rule_names` is fixed for the whole call.
    flagged_active = [name for name in ("cat1a", "cat1b") if name in rule_names]
    silenced_active = [name for name in ("cat2a", "cat2b") if name in rule_names]
    # Clinical-event rules, in evaluation order; raises if a combined rule
    # is enabled without its constituents.
    event_rules = CE.enabled_event_rules(rule_names)

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        events_sorted = sorted(events, key=lambda ev: ev.start)
        if progress:
            print(f"  [{time.monotonic() - t0:7.1f}s] minting patient {pi}/{num_patients} "
                  f"({patient}): {len(events_sorted)} alarm(s)")

        # RDFox's own `echo <token>` shell command (confirmed via `help
        # echo`: "Prints the tokens specified... separated by a single
        # space" — an exact, undecorated line, nothing to disambiguate)
        # gives execute_script an unambiguous per-patient/per-alarm marker
        # to match live in RDFox's streamed stdout — see execute_script's
        # own docstring for why this is the RDFox-execution counterpart to
        # this function's own per-alarm minting progress bar above.
        lines.append(f"echo PATIENT_START:{patient}")

        driver = Driver(kb, scratch_dir, file_counter, event_rules,
                        cat2_lift=bool({"cat2a", "cat2b"} & set(rule_names)))
        n_events = len(events_sorted)
        # Update at most ~100 times per patient, not once per alarm — a
        # real patient can carry tens of thousands of alarms (confirmed
        # directly: one real-corpus patient had 25,793), and printing a
        # full line per alarm at that scale floods the terminal with
        # scrollback rather than showing progress. An in-place bar
        # (carriage return, no newline until the patient is done) shows
        # the same real-time signal in one line instead.
        report_every = max(1, n_events // 100)
        for ei, e in enumerate(events_sorted, start=1):
            if progress and (ei == 1 or ei == n_events or ei % report_every == 0):
                bar = _progress_bar(ei, n_events)
                print(f"\r    {bar} {ei}/{n_events} alarms "
                      f"[{time.monotonic() - t0:7.1f}s]", end="", flush=True)
            driver.flush_due(e.start)
            M.update_identity(kb, e, driver.identity_tracker)
            identity = driver.identity_tracker.identity
            if verify_identity:
                events_so_far = [ev for ev in events_sorted if ev.start <= e.start]
                batch_identity = M.resolve_identity(kb, events_so_far)
                assert identity == batch_identity, (
                    f"incremental identity tracker diverged from resolve_identity's batch "
                    f"computation for {patient} at {e.label}@{e.start}")
            event_kinds = (frozenset(CE.relevant_kinds(kb, e.label, _alarm_metric_types(kb, e), event_rules))
                           if event_rules else frozenset())
            pending = driver.insert_alarm(e, identity, event_kinds)
            lines.extend(driver.commands)
            driver.commands.clear()

            # Check THIS alarm's own firing status right at its arrival —
            # matching assess.py's own timing discipline (mda_poc_
            # assessment.py calls assess() once per arriving alarm, against
            # the situation AT THAT INSTANT). Checking only once at the very
            # end of the whole patient timeline (the original version of
            # this driver did) is wrong: by then every alarm's transient
            # graph — and eventually its persistent graph — has already
            # been dropped, so nothing is left to match against at all.
            #
            # This runs BETWEEN insert_alarm()'s two phases — the arriving
            # alarm's own hasPriority triple isn't inserted yet (see
            # insert_alarm's own docstring for why cat2a's aggregate check
            # needs that) — but every check below is unaffected by
            # hasPriority's absence, so nothing else needs to know or care.
            alarm = pending["alarm"]
            patient_iri = pending["patient"]
            now_literal = f'"{e.start.isoformat()}"^^{XSD_DATETIME}'
            # None of cat1a/cat1b/cat2a/cat2b are
            # standing rules anymore — see ONDEMAND_RULE_NAMES's own
            # module-level comment for why. Each predicate is instead
            # checked via a UNION'd, on-demand query over whichever
            # contributing rules are enabled. Per the plan's §3, each
            # on-demand body now carries its own validUntil FILTER
            # (ONDEMAND_QUERY_BODIES/_append_validity_filters) bound to
            # ?now here — physical graph presence is no longer what makes
            # a match valid, now that eviction can be batched (plan's §4)
            # and may lag a graph's own logical expiry.
            #
            # flaggedLikelyFalsePositive/silencedBy are alarm-scoped
            # (?alarm bound via VALUES to THIS alarm's own IRI).
            #
            # Each group is only emitted when at least one contributing
            # rule is enabled — with none enabled there's nothing to check
            # (matches the old behaviour of the predicate simply never
            # being derived).
            # Emitted as SEPARATE queries per rule, not UNIONed — mirrors
            # cat2a/cat2b's own fix (see this_alarm_silenced's comment
            # below) for the same reason: keeps each rule individually
            # timed/counted for the per-rule instrumentation in
            # execute_script, and avoids relying on RDFox's planner to
            # handle a UNION of differently-shaped bodies well.
            for name in flagged_active:
                branch = f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }} {ONDEMAND_QUERY_BODIES[name]}"
                lines += CE.trace_block(f"check {len(checks)}",
                                        f"select distinct ?alarm ?witness where {{ {branch} }}")
                checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
            # Store the flags (see cat1_flag_insert): clinical events ignore
            # flagged alarms, and a later alarm on a cat1b flag's IBP
            # pathway withdraws it. Only heart-rate alarms can be an
            # asystole — a cheap gate for cat1b, the query decides.
            withdraw_kinds = frozenset()
            if "cat1a" in flagged_active:
                lines.append(cat1_flag_insert("cat1a", alarm, pending["tgraph"], now_literal))
            if "cat1b" in flagged_active:
                if "HeartRate" in _alarm_metric_types(kb, e):
                    lines.append(cat1_flag_insert("cat1b", alarm, pending["tgraph"], now_literal))
                if _alarm_functional_unit(kb, e) == "FU_InvasiveBloodPressure":
                    select, delete = cat1b_withdraw(alarm)
                    lines += CE.trace_block(f"withdraw {patient} {e.start.isoformat()}", select)
                    lines.append(delete)
                    # A withdrawn asystole becomes evidence at this moment:
                    # re-evaluate what a heart-rate alarm can support.
                    withdraw_kinds = CE.kinds_supported_by_metric("HeartRate", event_rules)
            # cat2a can only ever match if THIS alarm's own metric type has
            # an mda:approximates mapping at all (see
            # APPROXIMATES_COVERED_METRIC_TYPES's own comment — harmless to
            # keep even now that cat2a is aggregate-based: the arriving-side
            # chain hop still can't bind for an uncovered metric type, so
            # this remains a correct, if now purely cosmetic, short-circuit).
            # cat2b needs no such check: it joins on the metric's own
            # rdf:type directly, a base fact that's never missing.
            this_alarm_silenced = silenced_active
            if "cat2a" in this_alarm_silenced:
                metric_types = _alarm_metric_types(kb, e)
                prio = pending["incoming_prio"]
                # An Unknown priority cannot be shown to be equal or lower
                # than anything: never silenced by CAT2a (agreed 2026-09-21).
                if (prio is None or str(prio) == f"{_ALARMPRIO}Unknown"
                        or (metric_types and not (metric_types & APPROXIMATES_COVERED_METRIC_TYPES))):
                    this_alarm_silenced = [n for n in this_alarm_silenced if n != "cat2a"]
            # cat2a's and cat2b's aggregate checks are emitted as SEPARATE
            # queries, NOT combined via UNION into one — confirmed
            # empirically (real patient 2826, alarms 0-900): each alone
            # (and both loaded as standing rules but only one checked) ran
            # in ~2-3s for 900 alarms; UNIONing their two branches together
            # into one query reproducibly cost ~90-97s for the same 900
            # alarms. RDFox's planner produces a badly inefficient plan for
            # this specific combination — not something either branch does
            # on its own. checks gets a 5-tuple: name as the 4th element so
            # cat2a's and cat2b's entries don't collide as the SAME dict
            # key in execute_script's `dict(zip(checks, counts))` (which
            # would silently drop one), and each alarm's own per-patient
            # sequence index (`ei`) as the 5th so two alarms sharing the
            # exact same `start` instant — a real, non-rare occurrence in
            # the real corpus (confirmed on alexKim_03, see build_script's
            # own `verify_identity` docstring) — don't ALSO collide with
            # each other, which silently dropped one of their results
            # before this index was added. See run()'s and poc_entry.py's
            # report()'s own "at most one per alarm" grouping for how
            # cat2a/cat2b are recombined into a single silencedBy verdict
            # per alarm, matching the old single-UNIONed-query semantics —
            # unaffected by the extra tuple element, since it only reads
            # k[0]/k[1].
            for name in this_alarm_silenced:
                if name == "cat2a":
                    values = cat2a_values(alarm, now_literal, pending["incoming_prio"])
                    lines += CE.trace_block(f"check {len(checks)}",
                                            f"select distinct ?alarm ?witness where {{ "
                                            f"{values} {ONDEMAND_QUERY_BODIES['cat2a']} }}")
                    lines.append(cat2_silence_insert("cat2a", values, pending["tgraph"]))
                else:
                    values = cat2b_values(alarm, now_literal)
                    lines += CE.trace_block(f"check {len(checks)}",
                                            f"select distinct ?alarm ?witness where {{ "
                                            f"{values} {ONDEMAND_QUERY_BODIES['cat2b']} }}")
                    lines.append(cat2_silence_insert("cat2b", values, pending["tgraph"]))
                checks.append((patient, e.start, "silencedBy", name, ei))
            driver.complete_alarm(pending)
            lines.extend(driver.commands)
            driver.commands.clear()
            # This arrival may start or extend a clinical event of this patient
            # (or, through a cat1b withdrawal, let a heart-rate alarm count).
            lines.extend(CE.evaluate_commands(event_kinds | withdraw_kinds, event_rules, patient, e.start))
            lines.append(f"echo ALARM_DONE:{patient}")
        if progress:
            print()  # finalize this patient's in-place progress line
        driver.flush_all()
        lines.extend(driver.commands)

    if progress:
        print(f"  [{time.monotonic() - t0:7.1f}s] minting done for {num_patients} patient(s)")
    lines.append("quit")
    return "\n".join(lines), checks


def _reader_thread(pipe, q: queue.Queue) -> None:
    """Runs in a background thread: forward every line RDFox prints to
    `q`, then a final None sentinel on EOF. Needed (rather than just
    iterating `pipe` in the main thread) so execute_script can still
    enforce a wall-clock timeout even if RDFox goes completely silent —
    an in-loop time check only runs between lines received, which never
    fires at all if no more lines ever arrive."""
    for line in pipe:
        q.put(line.rstrip("\n"))
    q.put(None)


def execute_script(script_text: str, checks: list, scratch: Path, timeout: int = 600,
                    progress: bool = True, patients: dict | None = None,
                    trace: list | None = None) -> tuple:
    """Run `script_text` against a real RDFox instance and return
    ({check_key: answer_count}, {check_key: seconds}) for every entry in
    `checks`, in order — the second dict is per-statement wall-clock time
    as RDFox itself reports it ("Total statement evaluation time"), used
    by summarize_rule_timings for per-rule trigger-count/duration
    analysis.

    Shared by run() (the fixed regression test below) and poc_entry.py
    (the general-purpose runner) so the RDFox invocation/output-parsing
    logic — both non-obvious, see the comments inline — lives in exactly
    one place.

    `progress`: render an in-place, per-patient progress bar as RDFox
    actually executes the script — the counterpart to build_script's own
    per-alarm minting bar, for the step that follows it. Confirmed
    directly (not assumed) that this is possible at all: RDFox flushes
    its stdout per-line even when piped, not just when attached to a
    TTY — verified with an `echo`-then-`sleep 3000`-then-`echo` script,
    where the first echo arrived immediately and the second only after
    the full 3s, ruling out RDFox block-buffering its own output until
    exit (the common failure mode that would have made "live" progress
    silently do nothing until the process ends anyway). Driven by the
    `echo PATIENT_START:<patient>` / `echo ALARM_DONE:<patient>` marker
    lines build_script emits into the script for exactly this purpose —
    `echo`'s own RDFox semantics (`help echo`: "Prints the tokens
    specified... separated by a single space") make these unambiguous,
    exact lines to match on, unlike inferring progress from counting
    select/DELETE-WHERE result lines (which don't carry a patient
    identity at all).

    `patients`: the same {patient: events} dict build_script was called
    with, used only to size the progress bar (alarm count per patient).
    Optional and derived from `checks` when omitted — but `checks` is
    empty whenever every rule is disabled (a real, useful diagnostic
    run — see poc_entry.py's SETTINGS['enabled_rules']), which previously
    collapsed the progress bar to "patient N/0 ... /0 alarms" since it
    had nothing to size itself from. Pass `patients` to keep the bar
    correct in that case.

    `trace`: if given, extended with every trace block printed during the
    run, as (tag, check_key or None, rows) — the input of event_log's
    event_records/firing_records. Optional so existing callers are
    unaffected.
    """
    script_path = scratch / "replay.rdfox"
    script_path.write_text(script_text)

    if patients is not None:
        total_per_patient = {p: len(evs) for p, evs in patients.items()}
    else:
        total_per_patient = {}
        for check in checks:
            patient, ts = check[0], check[1]
            total_per_patient.setdefault(patient, set()).add(ts)
        total_per_patient = {p: len(s) for p, s in total_per_patient.items()}
    num_patients = len(total_per_patient)

    # RDFox's CLI has no "run this script file" positional argument — any
    # argument after <root> in sandbox/shell mode is itself a SHELL COMMAND
    # (confirmed via `RDFox -help`: "all supplied commands are executed").
    # The working pattern is piping the script's own text via stdin, which
    # also closes stdin on EOF (no interactive prompt to hang on) without
    # needing an explicit `< /dev/null`.
    t0 = time.monotonic()
    script_file = script_path.open()
    process = subprocess.Popen(
        [str(RDFOX_BIN), "sandbox", str(scratch)],
        env={"RDFOX_LICENSE_FILE": str(LICENSE)},
        stdin=script_file, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(process.stdout, q), daemon=True)
    reader.start()

    out_lines: list = []
    current_patient = None
    patient_index = 0
    alarms_done = 0
    timed_out = False
    deadline = t0 + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Used to raise TimeoutExpired here, which meant a stalling
            # run produced ZERO diagnostic data — exactly the case where
            # per-rule timing (summarize_rule_timings) matters most.
            # Instead: kill the process, and fall through to the same
            # parsing logic below on whatever output was captured before
            # the kill, so every check/alarm that DID complete still gets
            # counted and timed. The caller can tell a partial result from
            # a complete one via the printed warning below (there's no
            # separate return signal — the point is graceful degradation,
            # not a new error-handling contract every caller must adopt).
            timed_out = True
            process.kill()
            break
        try:
            line = q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        out_lines.append(line)

        if not progress:
            continue
        if line.startswith("PATIENT_START:"):
            current_patient = line.split(":", 1)[1]
            patient_index += 1
            alarms_done = 0
            total = total_per_patient.get(current_patient, 0)
            print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                  f"{_progress_bar(0, total)} 0/{total} alarms "
                  f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)
        elif line.startswith("ALARM_DONE:"):
            total = total_per_patient.get(current_patient, 0)
            report_every = max(1, total // 100)
            alarms_done += 1
            if alarms_done == total or alarms_done % report_every == 0:
                print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                      f"{_progress_bar(alarms_done, total)} {alarms_done}/{total} alarms "
                      f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)

    process.wait()
    script_file.close()
    if progress and num_patients:
        print()  # finalize the in-place line
    if timed_out:
        print(f"  [{time.monotonic() - t0:7.1f}s] *** TIMED OUT after {timeout}s — "
              f"RDFox killed mid-execution. Parsing partial output below: every check "
              f"that DID complete before the kill is still counted/timed; anything "
              f"after the stall is simply absent from the result. ***")
    else:
        print(f"  [{time.monotonic() - t0:7.1f}s] RDFox execution finished "
              f"({len(checks)} check(s) run)")
    output = "\n".join(out_lines)

    # Every select runs inside a trace block (clinical_events.trace_block),
    # with output switched on for the whole script, so results are read
    # from the rows each block printed — not from RDFox's "Number of query
    # answers" statistics, which updates and deletes print too. A check's
    # count is its number of rows; its timing is the block's own "Total
    # statement evaluation time".
    blocks = EL.parse_trace_blocks(out_lines)
    counts_by_check, timings_by_check = {}, {}
    collected = []
    for tag, rows, seconds in blocks:
        if tag.startswith("check "):
            key = checks[int(tag.split(" ", 1)[1])]
            counts_by_check[key] = len(rows)
            timings_by_check[key] = seconds
            collected.append(("check", key, rows))
        else:
            collected.append((tag, None, rows))

    error_lines = [l for i, l in enumerate(out_lines)
                   if l.startswith("An error occurred")
                   or (i and out_lines[i - 1].startswith("An error occurred"))]
    if error_lines:
        print("--- errors seen in RDFox output ---")
        for l in error_lines:
            print(" ", l)

    if len(counts_by_check) != len(checks) and not timed_out:
        print(f"  WARNING: {len(counts_by_check)} check result(s) for {len(checks)} check(s) — "
              f"some check blocks are missing; counts below are incomplete.")
    if trace is not None:
        trace.extend(collected)
    return counts_by_check, timings_by_check


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
        # quality.dlog's own header) genuinely needs independent per-hop
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


def count_alarms_fired(check_items, kind: str) -> int:
    """"flaggedLikelyFalsePositive" and "silencedBy" can each have
    MULTIPLE checks entries for the SAME alarm (one per contributing
    rule — cat1a/cat1b, cat2a/cat2b respectively), emitted as separate
    queries rather than UNIONed into one (see build_script's own comment
    on why — a real, confirmed RDFox planner regression for at least the
    cat2a/cat2b combination, not a style choice). Every checks tuple is
    now 5-element — (patient, start, kind, rule_name, ei) — the rule_name
    so cat1a/cat1b and cat2a/cat2b don't collide as the same dict key,
    and the per-alarm sequence index `ei` so two alarms sharing the exact
    same `start` instant don't either (see build_script's own comment at
    its checks.append call sites). This counts each alarm AT MOST ONCE
    regardless of how many of its rules fired, matching the old
    single-UNIONed-query "limit 1" semantics exactly."""
    fired = set()
    for k, v in check_items:
        if k[2] != kind or v <= 0:
            continue
        fired.add((k[0], k[1]))
    return len(fired)


def summarize_rule_timings(counts_by_check: dict, timings_by_check: dict, print_it: bool = True) -> dict:
    """Per-rule breakdown across a whole run: how many times each
    on-demand check (cat1a/cat1b/cat2a/cat2b) was evaluated,
    how many of those evaluations actually matched ("hits"), total time
    RDFox itself reports spending on that check's queries, and average
    time per invocation vs. average time per hit — the latter is what
    actually answers "does this rule get slower when it fires, or is
    cost independent of outcome." Keyed by (kind, name) — e.g.
    ("silencedBy", "cat2a") — since every checks tuple now carries a rule
    name as its 4th element (see build_script's own comment on why:
    keeping every on-demand check as its own separate query, one rule
    name per query, both to avoid the confirmed cat2a/cat2b UNION
    regression and to make exactly this kind of per-rule instrumentation
    possible without extra bookkeeping)."""
    groups: dict = {}
    for key, t in timings_by_check.items():
        kind, name = key[2], key[3]
        g = groups.setdefault((kind, name), {"invocations": 0, "hits": 0, "total_s": 0.0, "hit_s": 0.0})
        g["invocations"] += 1
        if t is not None:
            g["total_s"] += t
        n = counts_by_check.get(key, 0)
        if n and n > 0:
            g["hits"] += 1
            if t is not None:
                g["hit_s"] += t

    if print_it:
        print("\nPer-rule timing summary:")
        print(f"  {'rule':<28} {'invocations':>12} {'hits':>8} {'total_s':>10} "
              f"{'avg_s/call':>12} {'avg_s/hit':>10}")
        for (kind, name), g in sorted(groups.items()):
            avg_call = g["total_s"] / g["invocations"] if g["invocations"] else 0.0
            avg_hit = g["hit_s"] / g["hits"] if g["hits"] else 0.0
            print(f"  {kind + '/' + name:<28} {g['invocations']:>12} {g['hits']:>8} "
                  f"{g['total_s']:>10.3f} {avg_call:>12.5f} {avg_hit:>10.5f}")
    return groups


def run():
    scratch = ROOT / "MDA-POC-RDFox" / "engine" / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    kb = M.load_kb()

    events = load_events(DATASET)
    groups = group_by_patient(events)
    in_scope = {p: evs for p, evs in groups.items() if p in EXPECTED}

    script_text, checks = build_script(kb, in_scope, scratch)
    trace: list = []
    counts_by_check, timings_by_check = execute_script(script_text, checks, scratch, patients=in_scope,
                                                        trace=trace)
    summarize_rule_timings(counts_by_check, timings_by_check)
    alarms = EL.alarm_index(e for evs in in_scope.values() for e in evs)
    records = EL.event_records(trace, in_scope)
    firings = EL.firing_records(trace)
    EL.write_event_log(records, alarms, scratch / "clinical_events.csv")
    EL.write_firing_log(firings, alarms, scratch / "rule_firings.csv")
    for name in ("clinical_events.csv", "rule_firings.csv"):
        print(f"\n--- {name} ---")
        print((scratch / name).read_text().rstrip())
    print()

    cat3a_episodes = EL.episodes_by_patient(records, CE.KIND_BY_RULE["cat3a"])
    cat3b_episodes = EL.episodes_by_patient(records, CE.KIND_BY_RULE["cat3b"])

    flagged = EL.flagged_alarms(firings)
    silenced = EL.silenced_alarms(firings)
    total = passed = 0
    for patient in in_scope:
        # Counted from the firing log, not the raw check counts: a cat1b
        # flag can be withdrawn after its check fired.
        n_flag = sum(1 for p, _a in flagged if p == patient)
        n_silence = sum(1 for p, _a in silenced if p == patient)
        fired = (n_flag > 0) or (n_silence > 0)
        expected = EXPECTED[patient]
        total += 1
        status = "PASS" if fired == expected else "FAIL"
        passed += status == "PASS"
        print(f"[{status}] {patient}: expected fire={expected}, got flagged={n_flag} silenced={n_silence}")

        if patient in EXPECTED_CAT3A:
            n_cat3a = cat3a_episodes.get(patient, 0)
            fired3a = n_cat3a > 0
            expected3a = EXPECTED_CAT3A[patient]
            total += 1
            status3a = "PASS" if fired3a == expected3a else "FAIL"
            passed += status3a == "PASS"
            print(f"[{status3a}] {patient} (cat3a): expected fire={expected3a}, got episodes={n_cat3a}")

        if patient in EXPECTED_CAT3B:
            n_cat3b = cat3b_episodes.get(patient, 0)
            fired3b = n_cat3b > 0
            expected3b = EXPECTED_CAT3B[patient]
            total += 1
            status3b = "PASS" if fired3b == expected3b else "FAIL"
            passed += status3b == "PASS"
            print(f"[{status3b}] {patient} (cat3b): expected fire={expected3b}, got episodes={n_cat3b}")

    for patient, expected_events in EXPECTED_EVENTS.items():
        got = {(r.kind, r.start.strftime("%H:%M:%S"), r.end.strftime("%H:%M:%S"), len(r.alarms))
               for r in records if r.patient == patient}
        total += 1
        status = "PASS" if got == expected_events else "FAIL"
        passed += status == "PASS"
        print(f"[{status}] {patient} (event times): expected {sorted(expected_events)}, got {sorted(got)}")

    for patient, expected_firings in EXPECTED_FIRINGS.items():
        got = set()
        for f in firings:
            if f.patient == patient:
                alarm_label, _ = EL._describe({f.alarm}, alarms)
                cause_labels, _ = EL._describe(f.causes - {f.alarm}, alarms)
                got.add((f.rule, alarm_label, cause_labels))
        total += 1
        status = "PASS" if expected_firings <= got else "FAIL"
        passed += status == "PASS"
        print(f"[{status}] {patient} (firing trace): expected {sorted(expected_firings)}, got {sorted(got)}")

    for patient, forbidden in FORBIDDEN_FIRINGS.items():
        got = set()
        for f in firings:
            if f.patient == patient:
                alarm_label = EL._describe({f.alarm}, alarms)[0]
                got |= {(f.rule, alarm_label),
                        (f.rule, alarm_label, EL._describe(f.causes - {f.alarm}, alarms)[0])}
        total += 1
        status = "PASS" if not (forbidden & got) else "FAIL"
        passed += status == "PASS"
        print(f"[{status}] {patient} (forbidden firings): must not see {sorted(forbidden)}, "
              f"saw {sorted(forbidden & got)}")

    print(f"\n{passed}/{total} checks matched expected outcome")


if __name__ == "__main__":
    run()
