"""
compare_logs.py — do two runs log the same firings and clinical events?

Compares both logs of a reference run and a current run as sets of rows,
alarms matched by ALARM_ID. Prints the rows only one side has; non-zero
exit when there are any. Configure through SETTINGS.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SETTINGS = {
    # ROOT / "_baseline" / "regression" against ROOT / "engine" / "_scratch"
    # for the fixture regression; the poc5 pair for the 5-patient sample.
    "reference": ROOT / "_baseline" / "poc5",
    "current": ROOT / "_scratch",
}


def alarm_ids(cell: str) -> tuple:
    """The ALARM_IDs in an `alarm_ids` cell, sorted."""
    return tuple(sorted(a.rsplit("_", 1)[-1] for a in cell.split()))


def firing_rows(path: Path) -> set:
    with path.open(encoding="utf-8") as f:
        return {(r["patient"], r["time"], r["rule"], r["alarm"], r["caused_by"], alarm_ids(r["alarm_ids"]))
                for r in csv.DictReader(f, delimiter=";")}


def event_rows(path: Path) -> set:
    with path.open(encoding="utf-8") as f:
        return {(r["patient"], r["kind"], r["start"], r["end"], alarm_ids(r["alarm_ids"]))
                for r in csv.DictReader(f, delimiter=";")}


def compare(name: str, read) -> int:
    """Print the differences in one log; return their number."""
    ref = read(SETTINGS["reference"] / name)
    cur = read(SETTINGS["current"] / name)
    only_ref, only_cur = sorted(ref - cur), sorted(cur - ref)
    status = "SAME" if not (only_ref or only_cur) else "DIFFERENT"
    print(f"[{status}] {name}: {len(ref)} reference row(s), {len(cur)} current row(s), "
          f"{len(ref & cur)} in both")
    for row in only_ref:
        print(f"  - only in reference: {row}")
    for row in only_cur:
        print(f"  + only in current:   {row}")
    return len(only_ref) + len(only_cur)


def main() -> int:
    differences = compare("rule_firings.csv", firing_rows) + compare("clinical_events.csv", event_rows)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
