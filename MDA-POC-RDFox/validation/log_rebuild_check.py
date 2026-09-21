"""
log_rebuild_check.py — the logs name enough to rebuild the evidence.

For every row of clinical_events.csv and rule_firings.csv, look up the
logged alarm ids in the replayed input, re-mint ONLY those alarms with
engine/mint.py, and check that the rebuilt facts contain what the rule
needs:

  CardiacArrest              a HeartRate metric with hasRhythm Absent
  RespiratoryArrest          a RespirationRate metric with hasRhythm Absent
  ReducedPulmonaryFunction   a RespirationVolume_Minute metric with
                             hasValueState Decreased
  CardioRespiratoryArrest    both of the first two
  VentilationFailure         the third, plus a MechanicalVentilator in a
                             fault state
  cat1a / cat1b / cat2a / cat2b   every logged alarm id resolves to an input
                             alarm (the rule's own facts are re-minted from
                             those alarms by the same code)

Run engine/replay_driver.py first (it writes both logs into its scratch
folder). No command-line arguments — edit SETTINGS and run the file.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
import event_log as EL  # noqa: E402
import mint as M  # noqa: E402
import replay_driver as R  # noqa: E402

SETTINGS = {
    "events": R.DATASET,
    "logs": ROOT / "engine" / "_scratch",
}

RDF_TYPE = M.RDF.type
METRIC = M.Namespace("https://w3id.org/mda/vocab/metric/")
RHYTHM = M.Namespace("https://w3id.org/mda/vocab/metric-rhythm/")
VALUE = M.Namespace("https://w3id.org/mda/vocab/metric-value-state/")
OPSTATE = M.Namespace("https://w3id.org/mda/vocab/operation-state/")
DEVICE = M.Namespace("https://w3id.org/mda/vocab/device/")
FAULTS = {OPSTATE.Disabled, OPSTATE.Disconnected, OPSTATE.Malfunction, OPSTATE.Warning}


def rebuild(kb, alarms) -> M.Graph:
    """The facts these alarms mint — condition and background content."""
    g = M.Graph()
    for e in alarms:
        identity = M.resolve_identity(kb, [e])
        g += M.condition_for_event(kb, e.patient, e.label, e.device_id, identity)
        g += M.background_for_key(kb, e.patient, e.label, e.device_id, identity)
    return g


def has_metric(g, metric_type, prop, value) -> bool:
    return any((m, prop, value) in g for m in g.subjects(RDF_TYPE, metric_type))


def has_ventilator_fault(g) -> bool:
    return any(g.value(d, M.MDA.hasDeviceOperationState) in FAULTS
               for d in g.subjects(RDF_TYPE, DEVICE.MechanicalVentilator))


CRITERIA = {
    "CardiacArrest": lambda g: has_metric(g, METRIC.HeartRate, M.MDA.hasRhythm, RHYTHM.Absent),
    "RespiratoryArrest": lambda g: has_metric(g, METRIC.RespirationRate, M.MDA.hasRhythm, RHYTHM.Absent),
    "ReducedPulmonaryFunction": lambda g: has_metric(
        g, METRIC.RespirationVolume_Minute, M.MDA.hasValueState, VALUE.Decreased),
}
CRITERIA["CardioRespiratoryArrest"] = lambda g: (CRITERIA["CardiacArrest"](g)
                                                 and CRITERIA["RespiratoryArrest"](g))
CRITERIA["VentilationFailure"] = lambda g: (CRITERIA["ReducedPulmonaryFunction"](g)
                                            and has_ventilator_fault(g))


def main() -> int:
    kb = M.load_kb()
    by_id = {iri.rsplit("/", 1)[-1]: e
             for iri, e in EL.alarm_index(R.load_events(SETTINGS["events"])).items()}
    failures = 0

    with (SETTINGS["logs"] / "clinical_events.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ids = row["alarm_ids"].split()
            alarms = [by_id[i] for i in ids if i in by_id]
            ok = len(alarms) == len(ids) and CRITERIA[row["kind"]](rebuild(kb, alarms))
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] event  {row['patient']:10s} {row['kind']:25s} "
                  f"rebuilt from {len(alarms)}/{len(ids)} alarm(s): {row['supported_by']}")

    with (SETTINGS["logs"] / "rule_firings.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            ids = row["alarm_ids"].split()
            ok = bool(ids) and all(i in by_id for i in ids)
            failures += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] firing {row['patient']:10s} {row['rule']:25s} "
                  f"{len(ids)} alarm id(s) resolved: {row['alarm']} <- {row['caused_by'] or '(none named)'}")

    print(f"\n{'all rows rebuildable' if not failures else f'{failures} row(s) NOT rebuildable'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
