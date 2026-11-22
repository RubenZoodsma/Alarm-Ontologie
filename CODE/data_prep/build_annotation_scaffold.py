"""
Build the annotation scaffold for a given Pareto cutoff.

Reads DATA/CORPUS/Annotation_corpus_<cutoff>.csv (paths resolved relative to
the Restructured root, not the current working directory) and writes, into
DATA/ANNOTATION/initial/:

- p<cutoff>_annotation.csv: same columns as the consensus annotation file
  the build reads (p75_annotated.csv). alarmLabel, alarmPriority and
  Device.Type are pre-filled; the other ontology-derived columns are left
  blank for annotators to fill in.
- p<cutoff>_annotation_axioms.md: one entry per alarm with label/device/
  priority/department noted, followed by a placeholder line for
  annotators to record the axioms they infer from that alarm.

One entry per distinct alarm: device + label + priority, the identity of a
blueprint (FRAMEWORK/PLAN_blueprint_identity.md). The device is the corpus's
gateway name, mapped to the device model through
DATA/ANNOTATION/device_models.csv and pre-filled in Device.Type. The same
alarm raised in several departments is merged into one entry (departments
combined, Quant_count summed); the same label on different devices is NOT
merged (until 2026-11-19 it was, which gave e.g. an incubator and a radiant
warmer one shared annotation).

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
# Column template: the live annotation schema (the older data.csv template is
# tab-separated and lacks columns the build now reads).
SCAFFOLD_PATH = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p75_annotated.csv"
DEVICE_MODELS_PATH = ROOT / "DATA" / "ANNOTATION" / "device_models.csv"


def load_device_models():
    with open(DEVICE_MODELS_PATH, encoding="utf-8", newline="") as f:
        return {r["gateway_name"]: r["model"] for r in csv.DictReader(f, delimiter=";")}
OUT_DIR = ROOT / "DATA" / "ANNOTATION" / "initial"


def load_merged_corpus(cutoff):
    corpus_path = ROOT / "DATA" / "CORPUS" / f"Annotation_corpus_{cutoff}.csv"
    with open(corpus_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    model_of = load_device_models()
    unknown = sorted({r["Alarm_device"] for r in rows} - set(model_of))
    if unknown:
        raise SystemExit(f"Gateway name(s) missing from {DEVICE_MODELS_PATH.name}: {unknown}")

    groups = defaultdict(list)
    for row in rows:
        groups[(row["Alarm_device"], row["Alarm_label"], row["Alarm_priority"])].append(row)

    merged = []
    for (device, label, priority), group in groups.items():
        merged.append(
            {
                "Alarm_label": label,
                "Alarm_priority": priority,
                "Alarm_department": sorted({r["Alarm_department"] for r in group}),
                "Alarm_device": [device],
                "Device_model": model_of[device],
                "Quant_count": sum(int(r["Quant_count"]) for r in group),
            }
        )

    merged.sort(key=lambda r: (r["Alarm_label"], r["Alarm_device"], r["Alarm_priority"]))
    return rows, merged


def write_csv(cutoff, merged):
    with open(SCAFFOLD_PATH, encoding="utf-8-sig") as f:
        header = next(csv.reader(f, delimiter=";"))
    if "alarmLabel" not in header:
        raise SystemExit(f"{SCAFFOLD_PATH.name} has no alarmLabel column")

    output_path = OUT_DIR / f"p{cutoff}_annotation.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)
        for row in merged:
            out_row = [""] * len(header)
            out_row[header.index("alarmLabel")] = row["Alarm_label"]
            out_row[header.index("alarmPriority")] = row["Alarm_priority"]
            if "Device.Type" in header:
                out_row[header.index("Device.Type")] = row["Device_model"]
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
