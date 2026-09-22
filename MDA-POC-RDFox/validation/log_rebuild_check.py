"""
log_rebuild_check.py — the logs name enough to rebuild the evidence.

For every row of clinical_events.csv and rule_firings.csv, take ONLY the
alarms the logs name for it (its `alarm_ids`, and their CAT1 history —
below), replay them on their own through the engine — the same processor, rule files and actions as a real run —
and check that the row comes out again:

  clinical event   an event of the same kind, with the same start and end
  firing           the same rule on the same alarm at the same time, with
                   the same causing alarms (this includes cat1b_withdrawn
                   and cat2_lifted rows)

The criterion is therefore the rule itself (representation/rules/*.rq),
not a second copy of it here.

A row's own alarms are extended with what the logs say about them: every
CAT1 row (cat1a, cat1b, cat1b_withdrawn) on one of those alarms, up to the
row's time, adds its alarms too, recursively. A CAT1 flag decides whether
an alarm counts as evidence, and its withdrawal decides from when — so an
event whose start was set by a withdrawal needs the flag's cause and the
withdrawal's cause, which the logs name in the firing rows of that alarm,
not in the event row. Each row is replayed as its own patient
("<patient>_rb<row>"), so rows cannot lend each other evidence; all rows
share one RDFox run.

What passing means: the named alarms are SUFFICIENT for the result. It
does not show that no other alarm contributed, and for a rule on absence
(cat1b: no other alarm on the pathway) a smaller set of alarms can only
make the absence easier to meet.

Run engine/regression.py (or poc_entry.py) first; point SETTINGS at the
events file it replayed and the folder holding its logs. No command-line
arguments — edit SETTINGS and run the file.
"""
from __future__ import annotations

import csv
import dataclasses
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
import event_log as EL  # noqa: E402
import mint as M  # noqa: E402
import stream as S  # noqa: E402
from execution import execute_script  # noqa: E402
from processor import build_script  # noqa: E402

SETTINGS = {
    # The fixture regression: S.DATASET with ROOT / "engine" / "_scratch".
    # A poc_entry.py run: its dataset with ROOT / "_scratch".
    "events": S.DATASET,
    "logs": ROOT / "engine" / "_scratch",
    "scratch": ROOT / "_scratch_rebuild",
}


def alarm_id(iri_or_id: str) -> str:
    """The ALARM_ID at the end of an alarm IRI or of a logged alarm id."""
    return str(iri_or_id).rsplit("_", 1)[-1]


def read_rows(name: str) -> list:
    with (SETTINGS["logs"] / name).open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


CAT1_RULES = {"cat1a", "cat1b", EL.WITHDRAWN_RULE}


def with_cat1_history(patient: str, ids: list, until: datetime, firing_rows: list) -> list:
    """`ids` plus the alarms of every CAT1 firing row on one of them at or
    before `until`, recursively."""
    found, todo = list(ids), list(ids)
    while todo:
        subject = todo.pop()
        for r in firing_rows:
            row_ids = [alarm_id(i) for i in r["alarm_ids"].split()]
            if (r["patient"] == patient and r["rule"] in CAT1_RULES and row_ids and row_ids[0] == subject
                    and datetime.fromisoformat(r["time"]) <= until):
                for i in row_ids:
                    if i not in found:
                        found.append(i)
                        todo.append(i)
    return found


def main() -> int:
    events_rows = read_rows("clinical_events.csv")
    firing_rows = read_rows("rule_firings.csv")
    rows = [("event", r) for r in events_rows] + [("firing", r) for r in firing_rows]

    logged_patients = {r["patient"] for _k, r in rows}
    # With its position in the file: alarms starting at the same instant
    # arrive in input order (stream.replay_stream), and the replay must too.
    by_id = {(e.patient, e.alarm_id): (pos, e)
             for pos, e in enumerate(S.load_events_for_patients(SETTINGS["events"], logged_patients))}

    # One replay patient per row, holding only the alarms the logs name for it.
    patients, unresolved = {}, set()
    for n, (kind, r) in enumerate(rows):
        ids = [alarm_id(i) for i in r["alarm_ids"].split()]
        until = datetime.fromisoformat(r["end"] if kind == "event" else r["time"])
        ids = with_cat1_history(r["patient"], ids, until, firing_rows)
        found = [by_id.get((r["patient"], i)) for i in ids]
        if not ids or None in found:
            unresolved.add(n)
            continue
        replay = f"{r['patient']}_rb{n}"
        patients[replay] = [dataclasses.replace(e, patient=replay) for _pos, e in sorted(found)]

    scratch = SETTINGS["scratch"]
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    trace: list = []
    script_text, checks = build_script(M.load_kb(), patients, scratch, progress=False)
    execute_script(script_text, checks, scratch, progress=False, patients=patients, trace=trace)

    events = {}
    for rec in EL.event_records(trace, patients):
        events.setdefault(rec.patient, set()).add((rec.kind, rec.start, rec.end))
    firings = {}
    for f in EL.firing_records(trace):
        causes = tuple(sorted(alarm_id(a) for a in f.causes - {f.alarm}))
        firings.setdefault(f.patient, set()).add((f.time, f.rule, alarm_id(f.alarm), causes))

    failures = 0
    for n, (kind, r) in enumerate(rows):
        replay = f"{r['patient']}_rb{n}"
        ids = [alarm_id(i) for i in r["alarm_ids"].split()]
        n_replayed = len(patients.get(replay, ()))
        if kind == "event":
            want = (r["kind"], datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"]))
            ok = n not in unresolved and want in events.get(replay, set())
            what = f"event  {r['patient']:18s} {r['kind']:25s} {r['start'][11:]}–{r['end'][11:]}"
            detail = r["supported_by"]
        else:
            want = (datetime.fromisoformat(r["time"]), r["rule"], ids[0], tuple(sorted(ids[1:])))
            ok = n not in unresolved and want in firings.get(replay, set())
            what = f"firing {r['patient']:18s} {r['rule']:25s} {r['time'][11:]}"
            detail = f"{r['alarm']} <- {r['caused_by'] or '(none named)'}"
        failures += not ok
        why = "" if ok else ("  [alarm id not in the events file]" if n in unresolved
                             else "  [not reproduced from these alarms alone]")
        print(f"[{'PASS' if ok else 'FAIL'}] {what} from {n_replayed} alarm(s): {detail}{why}")

    print(f"\n{len(rows)} row(s): "
          + ("all rebuilt from the alarms they name" if not failures
             else f"{failures} NOT rebuilt from the alarms they name"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
