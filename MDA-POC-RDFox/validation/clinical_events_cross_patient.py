"""
clinical_events_cross_patient.py — clinical events never mix patients.

The regression replays patients one after another, so it never has two
patients' alarms in the store at once. This check interleaves five
patients' alarms in one store and compares the event log with the
expected one.

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

import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
import event_log as EL  # noqa: E402
import mint as M  # noqa: E402
import execution as X  # noqa: E402
import rules as RU  # noqa: E402
import stream as S  # noqa: E402
import processor as P  # noqa: E402

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
    """One stream of all patients' alarms, interleaved in time, through the
    engine's own processor into ONE store — as a live feed would arrive."""
    proc = P.Processor(kb, scratch, rule_names)
    proc.feed(S.replay_stream(EVENTS))
    proc.close_windows()
    return "\n".join(P.script_header("xp") + proc.lines + ["quit"])


def main() -> int:
    """Run the scenarios; non-zero on a missing, unexpected or foreign event."""
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
