"""
Build the annotation scaffold for a given Pareto cutoff.

Reads DATA/CORPUS/Annotation_corpus_<cutoff>.csv (paths resolved relative to
the Restructured root, not the current working directory) and writes, into
DATA/ANNOTATION/initial/:

- p<cutoff>_annotation.csv: same column scaffold as data.csv. alarmLabel
  and alarmPriority are pre-filled; the ontology-derived columns
  (alarmCategory, Device, Sensor, Signal, Metric, ...) are left blank
  for annotators to fill in, exactly as in data.csv.
- p<cutoff>_annotation_axioms.md: one entry per alarm with label/device/
  priority/department noted, followed by a placeholder line for
  annotators to record the axioms they infer from that alarm.

Alarms sharing the same (Alarm_label, Alarm_priority) but appearing in
multiple departments/devices in the source corpus (e.g. the same alarm
raised on different device models across departments) are merged into a
single entry; their departments and devices are each combined into a
sorted, de-duplicated list and their Quant_count values are summed.

Usage:
    python build_annotation_scaffold.py <cutoff>
    python build_annotation_scaffold.py 50
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent   # CODE/data_prep -> CODE -> Restructured
SCAFFOLD_PATH = ROOT / "DATA" / "ANNOTATION" / "consensus" / "data.csv"
OUT_DIR = ROOT / "DATA" / "ANNOTATION" / "initial"


def load_merged_corpus(cutoff):
    corpus_path = ROOT / "DATA" / "CORPUS" / f"Annotation_corpus_{cutoff}.csv"
    with open(corpus_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    groups = defaultdict(list)
    for row in rows:
        groups[(row["Alarm_label"], row["Alarm_priority"])].append(row)

    merged = []
    for (label, priority), group in groups.items():
        merged.append(
            {
                "Alarm_label": label,
                "Alarm_priority": priority,
                "Alarm_department": sorted({r["Alarm_department"] for r in group}),
                "Alarm_device": sorted({r["Alarm_device"] for r in group}),
                "Quant_count": sum(int(r["Quant_count"]) for r in group),
            }
        )

    merged.sort(key=lambda r: r["Alarm_label"])
    return rows, merged


def write_csv(cutoff, merged):
    with open(SCAFFOLD_PATH, encoding="utf-8-sig") as f:
        header = next(csv.reader(f, delimiter=";"))

    output_path = OUT_DIR / f"p{cutoff}_annotation.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        for row in merged:
            out_row = [""] * len(header)
            out_row[header.index("alarmLabel")] = row["Alarm_label"]
            out_row[header.index("alarmPriority")] = row["Alarm_priority"]
            writer.writerow(out_row)
    return output_path


def write_axioms_md(cutoff, merged):
    output_path = OUT_DIR / f"p{cutoff}_annotation_axioms.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# P{cutoff} Annotation Axioms\n\n")
        for row in merged:
            f.write(f"## {row['Alarm_label']}\n")
            f.write(
                f"Device: {', '.join(row['Alarm_device'])}; "
                f"Priority: {row['Alarm_priority']}; "
                f"Department: {', '.join(row['Alarm_department'])}\n\n"
            )
            f.write("<!-- axioms: -->\n\n")
            f.write("---\n\n")
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cutoff",
        type=int,
        help="Pareto cutoff to build from (e.g. 50, 95, 99), matching "
        "Annotation_corpus_<cutoff>.csv",
    )
    args = parser.parse_args()

    raw_rows, merged = load_merged_corpus(args.cutoff)
    csv_path = write_csv(args.cutoff, merged)
    md_path = write_axioms_md(args.cutoff, merged)

    print(f"Input rows:  {len(raw_rows)}")
    print(f"Merged rows: {len(merged)}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
