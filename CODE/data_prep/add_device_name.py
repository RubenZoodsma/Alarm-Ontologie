"""
add_device_name.py  (one-off, FRAMEWORK/PLAN_blueprint_identity.md step A1)

Makes a blueprint's identity device model + label + priority in the consensus
annotation file:

  1. Names the unnamed marker column `reviewStatus` (x / EXCL / REVIEW).
  2. Fills `Device.Type` with the device model, mapped from the gateway's
     device name in the selected corpus (`Alarm_device`) through
     DATA/ANNOTATION/device_models.csv. Where the file already holds a
     different model, it is replaced and reported (agreed 2026-11-19:
     Hulbus -> LeoniPlus, Datexohmedacom -> CarescapeR860). Gateway names with
     no known model (Medibus, Philipsmonitor) leave Device.Type as annotated.
  3. Splits a row whose label + priority occurs on several devices into one
     row per device. Every copy keeps the original annotation; the copy for
     the device with the highest alarm volume keeps its status, the others get
     `REVIEW` for the team to annotate (Track B).

Cells other than reviewStatus and Device.Type are copied verbatim (csv module,
no type conversion). Refuses to run on a file that already has `reviewStatus`.

Usage:  python3 CODE/data_prep/add_device_name.py
"""

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANNOTATED = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p75_annotated.csv"
CORPUS = ROOT / "DATA" / "CORPUS" / "Annotation_corpus_75.csv"
MODELS = ROOT / "DATA" / "ANNOTATION" / "device_models.csv"


def norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def main():
    with ANNOTATED.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    header, body = rows[0], rows[1:]
    if "reviewStatus" in header:
        raise SystemExit("reviewStatus already present: nothing to do.")
    if header[1] != "":
        raise SystemExit(f"Expected the unnamed marker as column 2, found {header[1]!r}.")
    with MODELS.open(encoding="utf-8", newline="") as fh:
        model_of = {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}

    devices = {}  # label + priority -> {gateway name: volume}
    with CORPUS.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["Alarm_device"] not in model_of:
                raise SystemExit(f"Gateway name {r['Alarm_device']!r} missing from {MODELS.name}")
            key = (norm(r["Alarm_label"]), norm(r["Alarm_priority"]))
            vol = devices.setdefault(key, {})
            vol[r["Alarm_device"]] = vol.get(r["Alarm_device"], 0) + int(r["Quant_count"])

    header = header[:]
    header[1] = "reviewStatus"
    i_prio, i_type = header.index("alarmPriority"), header.index("Device.Type")

    out, split, replaced = [], [], []
    for row in body:
        key = (norm(row[0]), norm(row[i_prio]))
        if key not in devices:
            raise SystemExit(f"Not in the selected corpus: {row[0]!r} / {row[i_prio]!r}")
        ranked = sorted(devices[key].items(), key=lambda kv: -kv[1])
        for rank, (gateway, _) in enumerate(ranked):
            new = row[:]
            model = model_of[gateway]
            if model:
                if new[i_type].strip() and new[i_type].strip() != model:
                    replaced.append((row[0], new[i_type].strip(), model))
                new[i_type] = model
            if rank > 0:
                new[1] = "REVIEW"
                split.append((row[0], row[i_prio], gateway))
            out.append(new)

    with ANNOTATED.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";", lineterminator="\n")
        w.writerow(header)
        w.writerows(out)

    print(f"{len(body)} rows -> {len(out)} rows")
    print(f"{len(split)} new REVIEW rows:")
    for label, prio, gateway in split:
        print(f"  {gateway:10} {label} ({prio})")
    print(f"{len(replaced)} Device.Type values replaced:")
    for label, old, new in replaced:
        print(f"  {old} -> {new}   {label}")


if __name__ == "__main__":
    main()
