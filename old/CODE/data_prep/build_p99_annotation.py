"""
build_p99_annotation.py

Builds the p99 annotation file from the p99 corpus, carrying over the p75
consensus annotation:

    in:   DATA/CORPUS/Annotation_corpus_99.csv
          DATA/ANNOTATION/consensus/p75_annotated.csv
          DATA/ANNOTATION/device_models.csv
    out:  DATA/ANNOTATION/consensus/p99_annotated.csv

One row per distinct alarm of the p99 corpus: device model + label + priority
(the blueprint identity of build_framework.py, same normalisation; departments
merged). Per row:

  1. p75 has a row with the same identity: copied verbatim.
  2. Otherwise p75 has a row for the same model + label with priority
     'Unknown' (the adult ICU before its source priorities were added): the
     annotation is copied with the new priority. When that Unknown row maps to
     one row here, its reviewStatus is kept; when it maps to several (the label
     occurs at more than one priority), every copy gets reviewStatus
     REVIEW-PRIO for the team to verify (an EXCL row stays EXCL).
  3. Otherwise: a new row with alarmLabel, alarmPriority and Device.Type
     pre-filled, the rest blank (as build_annotation_scaffold.py).

Rule 1 wins over rule 2: an Unknown row is not copied onto an identity p75
already annotated under its own priority (reported when they differ). p75 rows
that end up nowhere are reported.

Cells are copied as text (csv module, no type conversion); the output has the
header, encoding (UTF-8, no BOM) and line endings (CRLF) of p75_annotated.csv.
Refuses to overwrite an existing p99_annotated.csv unless --force.

Usage:  python3 CODE/data_prep/build_p99_annotation.py [--force]
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "DATA" / "CORPUS" / "Annotation_corpus_99.csv"
P75 = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p75_annotated.csv"
MODELS = ROOT / "DATA" / "ANNOTATION" / "device_models.csv"
OUT = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p99_annotated.csv"

VERIFY = "REVIEW-PRIO"
ANNOTATION_START = "alarmCategory"   # first column that holds annotation, not identity/bookkeeping


def norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def main():
    if OUT.exists() and "--force" not in sys.argv:
        raise SystemExit(f"{OUT.name} exists; pass --force to overwrite.")

    with MODELS.open(encoding="utf-8", newline="") as fh:
        model_of = {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}

    # distinct alarms of the p99 corpus
    alarms = {}
    with CORPUS.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["Alarm_device"] not in model_of:
                raise SystemExit(f"Gateway name {r['Alarm_device']!r} missing from {MODELS.name}")
            model = model_of[r["Alarm_device"]]
            key = (norm(model), norm(r["Alarm_label"]), norm(r["Alarm_priority"]))
            a = alarms.setdefault(key, {"label": r["Alarm_label"], "priority": r["Alarm_priority"],
                                        "model": model, "depts": set(), "count": 0})
            a["depts"].add(r["Alarm_department"])
            a["count"] += int(r["Quant_count"])

    with P75.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    header, body = rows[0], rows[1:]
    i_lab, i_pri = header.index("alarmLabel"), header.index("alarmPriority")
    i_mod, i_rev = header.index("Device.Type"), header.index("reviewStatus")
    i_ann = header.index(ANNOTATION_START)

    exact, unknown = {}, defaultdict(list)
    for r in body:
        key = (norm(r[i_mod]), norm(r[i_lab]), norm(r[i_pri]))
        if norm(r[i_pri]) == "unknown":
            unknown[key[:2]].append(r)
        else:
            if key in exact:
                raise SystemExit(f"Duplicate identity in {P75.name}: {key}")
            exact[key] = r
    for k, rs in unknown.items():
        if len(rs) > 1:
            raise SystemExit(f"Several Unknown rows in {P75.name} for {k}")

    # where each Unknown row lands (only on identities rule 1 does not cover)
    targets = defaultdict(list)
    for key in alarms:
        if key not in exact and key[:2] in unknown:
            targets[key[:2]].append(key)

    out, used, stats = [], set(), defaultdict(int)
    shadowed = []                                   # Unknown row vs existing exact annotation
    for key, a in alarms.items():
        if key in exact:
            row = list(exact[key])
            used.add(id(exact[key]))
            stats["copied (same identity)"] += 1
            src = unknown.get(key[:2], [None])[0]
            if src is not None and src[i_ann:] != row[i_ann:]:
                shadowed.append((a, src, row))
        elif key[:2] in unknown:
            src = unknown[key[:2]][0]
            used.add(id(src))
            row = list(src)
            row[i_pri] = a["priority"]
            if len(targets[key[:2]]) > 1 and row[i_rev].strip() != "EXCL":
                row[i_rev] = VERIFY
                stats[f"from Unknown, duplicated -> {VERIFY}"] += 1
            else:
                stats["from Unknown, re-keyed (1:1)"] += 1
        else:
            row = [""] * len(header)
            row[i_lab], row[i_pri], row[i_mod] = a["label"], a["priority"], a["model"]
            stats["new (to annotate)"] += 1
        out.append(row)

    out.sort(key=lambda r: (norm(r[i_lab]), norm(r[i_mod]), norm(r[i_pri])))
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter=";", lineterminator="\r\n").writerows([header] + out)

    print(f"{CORPUS.name}: {len(alarms)} distinct alarms -> {OUT.relative_to(ROOT)}")
    for k, v in stats.items():
        print(f"  {k:<36}{v:>5}")
    print(f"  {'total':<36}{len(out):>5}")

    left = [r for r in body if id(r) not in used]
    if left:
        print(f"\np75 rows not carried over ({len(left)}):")
        for r in left:
            why = ("an annotation under its own priority already covers it"
                   if any((k[:2] == (norm(r[i_mod]), norm(r[i_lab]))) for k in exact
                          if k in alarms) and norm(r[i_pri]) == "unknown"
                   else "not in the p99 corpus")
            print(f"  [{r[i_rev] or '-':<6}] {r[i_mod] or '-':<14} {r[i_pri]:<8} {r[i_lab]}  ({why})")
    if shadowed:
        print(f"\nUnknown row differs from the existing annotation it would merge into "
              f"({len(shadowed)}); existing kept:")
        for a, src, row in shadowed:
            print(f"  {a['model'] or '-':<14} {a['priority']:<8} {a['label']}")
            for col, old, new in zip(header[i_ann:], src[i_ann:], row[i_ann:]):
                if old != new:
                    print(f"      {col}: Unknown row {old!r} | kept {new!r}")
    dup = {k: v for k, v in targets.items() if len(v) > 1}
    if dup:
        print(f"\nUnknown rows duplicated over several priorities ({len(dup)}):")
        for (m, lab), keys in sorted(dup.items()):
            print(f"  {m or '-':<14} {alarms[keys[0]]['label']}: "
                  + ", ".join(alarms[k]["priority"] for k in sorted(keys)))


if __name__ == "__main__":
    main()
