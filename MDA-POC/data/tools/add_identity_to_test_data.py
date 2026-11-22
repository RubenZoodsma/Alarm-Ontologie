"""
add_identity_to_test_data.py  (one-off, FRAMEWORK/PLAN_blueprint_identity.md step A8)

Gives every alarm in the POC's test data the gateway device name and the
priority that, with its label, identify its blueprint (device model + label +
priority):

  validation/cases/*.yaml                    stream entries: device_name, priority
  DATA/CAT_evaluation/events_data.csv        columns device_name;priority
  DATA/POC_EVENTS/events_data.csv            columns device_name;priority

Resolution per alarm, from the selected corpus (Annotation_corpus_75.csv):
  1. the (gateway name, priority) pairs that carry its label;
  2. if several, keep those whose gateway name occurs in the alarm's device id
     (e.g. Medibus_00 -> Medibus);
  3. if several priorities remain: Unknown when the device id says so
     (cat2aunknown_...), else the non-Unknown priority (the UMCU units);
  4. if several gateways remain (IncubatorDraeger_00 names no model): the one
     with the most alarms for that label.
Every choice made in steps 3-4 is printed. Labels absent from the corpus are
left empty (no blueprint: a coverage gap) and printed.

Usage:  python3 MDA-POC/data/tools/add_identity_to_test_data.py
"""

import csv
import re
from collections import defaultdict
from pathlib import Path

POC = Path(__file__).resolve().parents[2]
ROOT = POC.parent
CORPUS = ROOT / "DATA" / "CORPUS" / "Annotation_corpus_75.csv"
CSV_FILES = [ROOT / "DATA" / "CAT_evaluation" / "events_data.csv",
             ROOT / "DATA" / "POC_EVENTS" / "events_data.csv"]
CASES = POC / "validation" / "cases"


def norm(s):
    return " ".join(str(s).split()).casefold()


volume = defaultdict(lambda: defaultdict(int))   # label -> (gateway, priority) -> alarms
with CORPUS.open(encoding="utf-8-sig", newline="") as fh:
    for r in csv.DictReader(fh, delimiter=";"):
        volume[norm(r["Alarm_label"])][(r["Alarm_device"], r["Alarm_priority"])] += int(r["Quant_count"])

choices = []


def resolve(label: str, device_id: str):
    cands = dict(volume.get(norm(label), {}))
    if not cands:
        choices.append(f"  no blueprint: {label!r} ({device_id})")
        return "", ""
    did = device_id.casefold()
    named = {k: v for k, v in cands.items() if k[0].casefold() in did}
    if named:
        cands = named
    if len({p for _, p in cands}) > 1:
        want_unknown = "unknown" in did
        cands = {k: v for k, v in cands.items() if (k[1] == "Unknown") == want_unknown} or cands
        choices.append(f"  priority {sorted({p for _, p in cands})} for {label!r} ({device_id})")
    if len(cands) > 1:
        best = max(cands, key=cands.get)
        choices.append(f"  gateway {best[0]} (most alarms of {sorted(g for g, _ in cands)}) for {label!r} ({device_id})")
        return best
    return next(iter(cands))


def fill_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
        fields = list(rows[0].keys())
    if "device_name" in fields:
        print(f"{path.name}: already has device_name, skipped")
        return
    for r in rows:
        r["device_name"], r["priority"] = resolve(r["label"], r["device_id"])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + ["device_name", "priority"], delimiter=";",
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{path.relative_to(ROOT)}: {len(rows)} rows")


ENTRY = re.compile(r'^(\s*- \{.*?device: "(?P<dev>[^"]*)".*?label: "(?P<label>[^"]*)")(?P<rest>.*\}\s*)$')


def fill_cases():
    n = 0
    for path in sorted(CASES.glob("*.yaml")):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        out = []
        for line in lines:
            m = ENTRY.match(line)
            if m and "device_name:" not in line:
                g, p = resolve(m["label"], m["dev"])
                line = f'{m.group(1)}, device_name: "{g}", priority: "{p}"{m["rest"]}'
                n += 1
            out.append(line)
        path.write_text("".join(out), encoding="utf-8")
    print(f"validation/cases: {n} stream entries")


if __name__ == "__main__":
    for f in CSV_FILES:
        fill_csv(f)
    fill_cases()
    print("choices:")
    print("\n".join(choices) or "  none")
