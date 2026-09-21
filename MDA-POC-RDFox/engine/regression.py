"""
regression.py — the fixed regression test: every rule against the
fabricated, known-outcome patients in DATA/CAT_evaluation/events_data.csv
(documented in that folder's README.md), checked against the expected
outcomes below.

Usage
-----
  cd MDA-POC-RDFox/engine
  python3 regression.py
"""

from __future__ import annotations

import re
import shutil

import rules as RU
import event_log as EL
import mint as M
from execution import execute_script, summarize_rule_timings
from paths import ENGINE_DIR
from processor import build_script
from stream import DATASET, group_by_patient, load_events

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
    #   priority. Exercises the AirwayPressure_Peak bridge (decision 39):
    #   without it the peak-pressure alarm reaches no process at all.
    "cat2a_peak": True,
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


def lookahead_possible() -> list:
    """Ways an alarm's future could reach the store at its arrival: an end
    on the arrival element, an end read by the modules that handle
    arrivals, or validity metadata in a rule or action file. Empty when the
    stream model rules lookahead out by construction."""
    import dataclasses
    import stream as S
    from paths import ACTIONS_DIR, RULES_DIR
    problems = []
    if "end" in {f.name for f in dataclasses.fields(S.AlarmArrival)}:
        problems.append("stream.AlarmArrival has an end field")
    for name in ("mint.py", "windows.py", "processor.py", "actions.py", "rules.py"):
        for i, line in enumerate((ENGINE_DIR / name).read_text().splitlines(), start=1):
            if re.search(r"\.end\b", line.split("#", 1)[0]):
                problems.append(f"{name}:{i}: reads .end: {line.strip()[:80]}")
    for folder in (RULES_DIR, ACTIONS_DIR):
        for path in sorted(folder.glob("*.rq")):
            if "validUntil" in path.read_text():
                problems.append(f"{path.name}: validUntil")
    return problems


def run():
    scratch = ENGINE_DIR / "_scratch"
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

    problems = lookahead_possible()
    total += 1
    passed += not problems
    print(f"[{'FAIL' if problems else 'PASS'}] no lookahead: an alarm's end cannot reach the store at arrival"
          + "".join(f"\n    {p}" for p in problems))

    hits = rule_logic_outside_rule_files()
    total += 1
    passed += not hits
    print(f"[{'FAIL' if hits else 'PASS'}] no rule logic in Python sources"
          + "".join(f"\n    {h}" for h in hits))

    print(f"\n{passed}/{total} checks matched expected outcome")


if __name__ == "__main__":
    run()
