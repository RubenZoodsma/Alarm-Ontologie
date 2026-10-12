"""
regression.py — every rule against the fabricated, known-outcome patients
in DATA/CAT_evaluation/events_data.csv (see that folder's README.md).

Checks per patient: flagged/silenced or not, CAT3a/CAT3b episodes, exact
event times, expected and forbidden firings; plus no lookahead (prefix
invariance, no engine needed) and no rule logic in Python. Writes both logs
to _scratch/.

  cd MDA-POC-RDFox/engine && python3 regression.py
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from datetime import datetime
from pathlib import Path

import rules as RU
import event_log as EL
import mint as M
import stream as S
from execution import RuleStats, summarize_invocations
from paths import ACTIONS_DIR, ENGINE_DIR, RULES_DIR
from processor import Processor, run_live
from stream import DATASET, group_by_patient, load_events

SETTINGS = {
    # Write every command sent to the engine to _scratch/replay.* (debugging).
    "audit_logging": False,
}

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
    #   vs heart rate). See rules/cat1a_signal_quality.rq.
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
    # CAT2a, agreed decisions 2026-09-21 (rules/cat2a_process_priority.rq):
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
    # CAT2b, agreed decisions 2026-09-21 (rules/cat2b_metric_sensor.rq):
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
    #   priority. Exercises the AirwayPressure_Peak approximates axiom
    #   in inference.ttl (decision 39):
    #   without it the peak-pressure alarm reaches no process at all.
    "cat2a_peak": True,
    # Reduced pulmonary function through oxygenation (revised 2026-10-12):
    # cat3b_spo2_flagged: the low SpO2 on a weak signal is CAT1a-flagged.
    # fig2_scenario: the manuscript's Fig. 2 — one CAT1a flag (episode 1)
    #   and one CAT2b silence (episode 2).
    "cat3b_spo2": False, "cat3b_desat": False, "cat3b_spo2_flagged": True,
    "cat3b_rr": False, "fig2_scenario": True,
}

# CAT3a (cardiorespiratory arrest): a cardiac arrest (Asystolie) and a
# respiratory arrest (Apneu) present at the same time — no tolerance window
# (rules/cat3a_cardiorespiratory_arrest.rq).
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
#     state still persists, but its alarm is over: negative.
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
                  "cat3b_swap": False, "cat3b_datalink": False, "cat3b_cpap": True,
                  # oxygenation branch (revised 2026-10-12): a low SpO2 or a
                  # desaturation on the monitor counts; a flagged SpO2 and a
                  # low respiratory rate do not.
                  "cat3b_spo2": True, "cat3b_desat": True, "cat3b_spo2_flagged": False,
                  "cat3b_rr": False, "fig2_scenario": True}

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
    # cat3b_spo2: reduced pulmonary function from the low SpO2 alone, and
    #   ventilation failure while the leak is also active.
    "cat3b_spo2": {
        ("ReducedPulmonaryFunction", "08:00:30", "08:01:30", 1),
        ("VentilationFailure", "08:00:30", "08:01:30", 2),
    },
    # cat3b_spo2_flagged / cat3b_rr: no event at all.
    "cat3b_spo2_flagged": set(),
    "cat3b_rr": set(),
    # fig2_scenario: the episode-1 SpO2 laag (flagged) supports nothing;
    #   the episode-3 SpO2 laag supports both events.
    "fig2_scenario": {
        ("ReducedPulmonaryFunction", "08:40:30", "08:41:40", 1),
        ("VentilationFailure", "08:40:30", "08:41:40", 2),
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
    "cat3b_spo2_flagged": {("cat1a", "PHILIPSMONITOR - SpO2 laag", "PHILIPSMONITOR - SpO2 zwak signaal")},
    "fig2_scenario": {
        ("cat1a", "PHILIPSMONITOR - SpO2 laag", "PHILIPSMONITOR - SpO2 zwak signaal"),
        ("cat2b", "SERVOU - Ademfrequentie laag", "PHILIPSMONITOR - Lage ademfreq."),
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
    # fig2_scenario: exactly the conclusions of the manuscript's Fig. 2 —
    #   neither low respiratory rate is silenced by CAT2a, the monitor's is
    #   not silenced at all, and no SpO2 laag is silenced.
    "fig2_scenario": {("cat2a", "PHILIPSMONITOR - Lage ademfreq."),
                      ("cat2a", "SERVOU - Ademfrequentie laag"),
                      ("cat2b", "PHILIPSMONITOR - Lage ademfreq."),
                      ("cat2a", "PHILIPSMONITOR - SpO2 laag"),
                      ("cat2b", "PHILIPSMONITOR - SpO2 laag")},
}


def rule_logic_outside_rule_files() -> list:
    """Python lines that look like rule logic: a select, a FILTER, a
    negation, or a pattern over the flag/silence predicates. Rules live in
    representation/rules/ and actions in representation/actions/ (rules.py);
    Python only binds and orders them. Graph drops and the priority INSERT
    DATA (windows.py) are window mechanics and do not match. Comments and
    docstring lines are skipped."""
    pattern = re.compile(r"(?i:select\s+(distinct\s+)?\?)|FILTER\s*\(|NOT EXISTS|silencedBy>|flaggedLikelyFalsePositive>")
    hits = []
    for path in sorted(ENGINE_DIR.glob("*.py")):
        if path.name == "regression.py":
            continue
        in_docstring = False
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.count('"""') % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            if pattern.search(stripped.split("  #", 1)[0]):
                hits.append(f"{path.name}:{i}: {stripped[:100]}")
    return hits


def lookahead_by_construction() -> list:
    """Structural guards: an arrival carries no end, and no rule or action
    file uses validity metadata."""
    problems = []
    if "end" in {f.name for f in dataclasses.fields(S.AlarmArrival)}:
        problems.append("stream.AlarmArrival has an end field")
    for folder in (RULES_DIR, ACTIONS_DIR):
        for path in sorted(folder.glob("*.rq")):
            if "validUntil" in path.read_text():
                problems.append(f"{path.name}: validUntil")
    return problems


UNKNOWN_END = datetime.max


def _instants(elements: list) -> list:
    """Stream elements grouped by instant, in order: [(t, [element, ...])]."""
    grouped: list = []
    for element in elements:
        if grouped and grouped[-1][0] == element.time:
            grouped[-1][1].append(element)
        else:
            grouped.append((element.time, [element]))
    return grouped


def _normalised(lines: list) -> list:
    """Script lines with each TriG import replaced by the file's content,
    so two runs compare on what they mint, not on file paths."""
    return [Path(line[len("import "):]).read_text() if line.startswith("import ") else line
            for line in lines]


def script_depends_on_future(kb, groups: dict, scratch: Path) -> list:
    """Prefix invariance. For every patient and every instant t, the script
    emitted up to t by a run over the whole stream must equal the script of
    a run that knows only the stream up to t: arrivals after t removed,
    ends after t unknown. Any difference means something after t leaked
    into what the engine did at or before t. The whole script covers the
    minted triples, every rule invocation and every window drop; the engine
    executes it deterministically, so the store and every decision up to t
    are identical too."""
    rule_names = list(RU.RULES)
    problems = []
    for patient, events in groups.items():
        base = scratch / "lookahead" / M._clean(patient)
        (base / "full").mkdir(parents=True)
        full = Processor(kb, base / "full", rule_names)
        boundaries = []
        for t, elements in _instants(S.replay_stream(events)):
            full.feed(elements)
            boundaries.append((t, len(full.lines)))
        full_script = _normalised(full.lines)

        for n, (t, emitted) in enumerate(boundaries):
            known = [e if e.end <= t else dataclasses.replace(e, end=UNKNOWN_END)
                     for e in events if e.start <= t]
            (base / f"cut{n}").mkdir()
            cut = Processor(kb, base / f"cut{n}", rule_names)
            cut.feed([element for element in S.replay_stream(known) if element.time <= t])
            if _normalised(cut.lines) != full_script[:emitted]:
                problems.append(f"{patient}: the script up to {t.isoformat()} depends on the stream after it")
                break
    shutil.rmtree(scratch / "lookahead")
    return problems


def run():
    """Replay the fixtures, print both logs and a PASS/FAIL line per check."""
    scratch = ENGINE_DIR / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    kb = M.load_kb()

    events = load_events(DATASET)
    groups = group_by_patient(events)
    in_scope = {p: evs for p, evs in groups.items() if p in EXPECTED}

    alarms = EL.alarm_index(e for evs in in_scope.values() for e in evs)
    log = EL.LiveLog(in_scope, alarms, scratch / "clinical_events.csv", scratch / "rule_firings.csv")
    stats = RuleStats()
    complete = run_live(kb, in_scope, scratch, log, stats, audit_log=SETTINGS["audit_logging"])
    log.close()
    if not complete:
        print("*** INCOMPLETE RUN: the engine stopped or reported errors (see above). ***")
    summarize_invocations(stats)
    records, firings = log.records, log.firings
    for name in ("clinical_events.csv", "rule_firings.csv"):
        print(f"\n--- {name} ---")
        print((scratch / name).read_text().rstrip())
    print()

    cat3a_episodes = EL.episodes_by_patient(records, RU.KIND_BY_RULE["cat3a"])
    cat3b_episodes = EL.episodes_by_patient(records, RU.KIND_BY_RULE["cat3b"])

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

    problems = lookahead_by_construction() + script_depends_on_future(kb, in_scope, scratch)
    total += 1
    passed += not problems
    print(f"[{'FAIL' if problems else 'PASS'}] no lookahead: at every instant, the script so far is "
          f"independent of the stream after it"
          + "".join(f"\n    {p}" for p in problems))

    hits = rule_logic_outside_rule_files()
    total += 1
    passed += not hits
    print(f"[{'FAIL' if hits else 'PASS'}] no rule logic in Python sources"
          + "".join(f"\n    {h}" for h in hits))

    print(f"\n{passed}/{total} checks matched expected outcome")


if __name__ == "__main__":
    run()
