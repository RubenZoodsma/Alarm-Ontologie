"""
clinical_events_cross_patient.py — clinical events never mix patients.

engine/processor.build_script processes one patient completely (all
graphs dropped) before starting the next, so its regression fixtures never
have two patients' alarms in the store at the same moment and cannot show
whether an event could be built from another patient's evidence. This
check interleaves several patients' alarms in ONE data store, in real time
order, and compares the resulting event log with the expected one.

Scenarios (all overlapping in time, all in the same store):
  xp_cardiac     Asystolie 08:00:00–08:01:00        -> CardiacArrest only
  xp_respiratory Apneu     08:00:30–08:01:30        -> RespiratoryArrest only
                 (together these two would make a cardiorespiratory
                  arrest if patients were mixed)
  xp_both        Asystolie + Apneu, same patient    -> both, plus
                 CardioRespiratoryArrest 08:00:30–08:01:00 (control: shows
                 the combined event CAN fire in this harness)
  xp_vent_fault  Ventilator storing 08:00:00–08:01:00 -> nothing
  xp_low_mv      MV ondergrens 08:00:30–08:01:30     -> ReducedPulmonaryFunction
                 only (no VentilationFailure from the other patient's
                 ventilator)

No command-line arguments — edit SETTINGS and run the file directly.
"""
from __future__ import annotations

import itertools
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
import actions as A  # noqa: E402
import event_log as EL  # noqa: E402
import mint as M  # noqa: E402
import execution as X  # noqa: E402
import rules as RU  # noqa: E402
import stream as S  # noqa: E402
import windows as W  # noqa: E402

SETTINGS = {
    "scratch": ROOT / "_scratch" / "clinical_events_cross_patient",
    "enabled_rules": sorted(RU.EVENT_RULE_NAMES),
}


def t(hms: str) -> datetime:
    return datetime.fromisoformat(f"2026-01-01T{hms}")


EVENTS = [
    S.Event("xp_cardiac", "PHILIPSMONITOR - Asystolie", "xp_PhilipsMonitor_00", t("08:00:00"), t("08:01:00"), "xp1"),
    S.Event("xp_respiratory", "PHILIPSMONITOR - Apneu", "xp_PhilipsMonitor_00", t("08:00:30"), t("08:01:30"), "xp2"),
    S.Event("xp_both", "PHILIPSMONITOR - Asystolie", "xp_PhilipsMonitor_00", t("08:00:00"), t("08:01:00"), "xp3"),
    S.Event("xp_both", "PHILIPSMONITOR - Apneu", "xp_PhilipsMonitor_00", t("08:00:30"), t("08:01:30"), "xp4"),
    S.Event("xp_vent_fault", "MEDIBUS - Ventilator storing", "xp_Medibus_00", t("08:00:00"), t("08:01:00"), "xp5"),
    S.Event("xp_low_mv", "MEDIBUS - MV ondergrens", "xp_Medibus_00", t("08:00:30"), t("08:01:30"), "xp6"),
]

# (patient, kind, start, end, number of supporting alarms)
EXPECTED = {
    ("xp_cardiac", "CardiacArrest", t("08:00:00"), t("08:01:00"), 1),
    ("xp_respiratory", "RespiratoryArrest", t("08:00:30"), t("08:01:30"), 1),
    ("xp_both", "CardiacArrest", t("08:00:00"), t("08:01:00"), 1),
    ("xp_both", "RespiratoryArrest", t("08:00:30"), t("08:01:30"), 1),
    ("xp_both", "CardioRespiratoryArrest", t("08:00:30"), t("08:01:00"), 2),
    ("xp_low_mv", "ReducedPulmonaryFunction", t("08:00:30"), t("08:01:30"), 1),
}


def build_interleaved_script(kb, scratch: Path, rule_names) -> str:
    rules = RU.enabled_event_rules(rule_names)
    lines = ["dstore create xp", "active xp"]
    lines += [f"import {f}" for f in X.FRAMEWORK_FILES]
    lines += X.SCRIPT_PREAMBLE
    counter = itertools.count(1)
    drivers = {}
    pending = []  # (when, seq, command) across all patients, flushed in time order
    seq = itertools.count()
    for e in sorted(EVENTS, key=lambda ev: ev.start):
        due = sorted(p for p in pending if p[0] <= e.start)
        lines += [cmd for _, _, cmd in due]
        pending = [p for p in pending if p[0] > e.start]

        driver = drivers.setdefault(e.patient, W.WindowOperator(kb, scratch, counter, rules))
        M.update_identity(kb, e, driver.identity_tracker)
        kinds = frozenset(RU.relevant_kinds(kb, e.label, RU._alarm_metric_types(kb, e), rules))
        driver.insert_alarm(e, driver.identity_tracker.identity, kinds)
        lines += driver.commands
        driver.commands.clear()
        lines += A.evaluate_commands(kinds, rules, e.patient, e.start)
        # Take this driver's scheduled drops into the shared, time-ordered queue.
        pending += [(when, next(seq), cmd) for when, cmd in driver.pending]
        driver.pending = []
    lines += [cmd for _, _, cmd in sorted(pending)]
    lines.append("quit")
    return "\n".join(lines)


def main() -> int:
    scratch = SETTINGS["scratch"]
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    kb = M.load_kb()
    script = build_interleaved_script(kb, scratch, SETTINGS["enabled_rules"])
    trace: list = []
    X.execute_script(script, [], scratch, progress=False, trace=trace)
    records = EL.event_records(trace, {e.patient for e in EVENTS})
    alarms = EL.alarm_index(EVENTS)
    got = {(r.patient, r.kind, r.start, r.end, len(r.alarms)) for r in records}
    # Every alarm IRI carries its patient's cleaned id, so a supporting
    # alarm from another patient is visible.
    foreign = [(r.patient, r.kind, a) for r in records for a in r.alarms
               if f"_{M._clean(r.patient)}_" not in a]

    for r in records:
        labels, _ids = EL._describe(r.alarms, alarms)
        print(f"  {r.patient:15s} {r.kind:25s} {r.start.time()}–{r.end.time()}  {labels}")
    missing, unexpected = EXPECTED - got, got - EXPECTED
    for m in sorted(missing):
        print(f"[FAIL] missing:    {m}")
    for u in sorted(unexpected):
        print(f"[FAIL] unexpected: {u}")
    for f in foreign:
        print(f"[FAIL] evidence from another patient: {f}")
    ok = not missing and not unexpected and not foreign
    print("[PASS] no cross-patient events; all expected events present" if ok else "")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
