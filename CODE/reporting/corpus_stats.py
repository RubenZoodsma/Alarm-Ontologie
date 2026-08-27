"""
corpus_stats.py

Generates the alarm-corpus / annotation-characteristics statistics for the
manuscript table "Alarm corpus specifications and annotation
characteristics" (Neonatal ICU / Paediatric ICU / Adult ICU / Total study
population), and writes it as a ready-to-paste Markdown table to
DOCS/tables/corpus_stats.md.

Two corpus stages, three ICUs
------------------------------
  Initial alarms            the full alarm log, before any feasibility
                             selection: DATA/CORPUS/Total_corpus.csv
  Alarm corpus after         the Pareto-selected subset that has actually
  feasibility split          been annotated so far: the 50th-percentile
  (50th percentile)          cutoff (DATA/CORPUS/Annotation_corpus_
                             50.csv), the only cutoff with a completed
                             annotation pass (DATA/ANNOTATION/consensus/
                             p50_annotated.csv). 95th/99th-percentile
                             cutoffs exist as corpus files but are not yet
                             annotated, so they cannot populate the
                             "Annotation characteristics" section and are
                             not used here.

An "alarm type" is a (label, priority) pair throughout, matching the
identity already used elsewhere in the pipeline (build_annotation_scaffold.py's
merge key, build_framework.py's alarmtype_iri). The same alarm type can be raised in more than one ICU
(e.g. the same device model deployed in two departments); it is counted
once per ICU it occurs in, but only once in the Total column, so Total is
NOT the sum of the three ICU columns (see the table's footnote). The
"Alarm priority" rows in both corpus-stage sections count unique alarm
types per priority (not occurrence volume) — since priority is part of
the identity key, this always sums to that section's "Unique alarm
labels, n".

Device categories are not hardcoded
------------------------------------
"Physiological monitor", "Mechanical ventilator", "Thermoregulator", etc.
are read straight off the p50_annotated.csv 'Device' column (the
controlled-vocabulary class an annotator assigned to each alarm's device)
and propagated to every corpus row sharing that device model — verified at
runtime that each device model maps to exactly one category. A device
model with no annotated occurrence at all falls into an "Unclassified"
bucket that is reported, never silently dropped or guessed at.

Annotation characteristics
----------------------------
Relevant concepts / Clinically enriched / Technically enriched / Annotated
triples are per-alarm counts of filled columns in p50_annotated.csv:

  Technically enriched = filled technical columns   (Device*, Component*,
                                                       Sensor*, Signal*,
                                                       Metric*)
  Clinically enriched  = filled clinical columns     (PhysiologicalProperty,
                                                       PhysiologicalProcess,
                                                       TherapeuticModality,
                                                       Organ, OrganSystem,
                                                       Patient)
  Relevant concepts    = all filled vocabulary columns, technical +
                          clinical + alarmCategory
  Annotated triples    = Technically enriched + Clinically enriched

Only rows that match an alarm in the 50th-percentile corpus contribute;
unmatched annotated rows (e.g. a label corrected during annotation) are
reported and excluded rather than fuzzy-matched.

Usage
-----
  python3 corpus_stats.py
"""

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent / "framework_build"))
from build_framework import COLUMN_SCHEMES

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR          = Path(__file__).resolve().parent
ROOT                = SCRIPT_DIR.parent.parent   # CODE/reporting -> CODE -> Restructured
TOTAL_CORPUS_PATH   = ROOT / "DATA" / "CORPUS" / "Total_corpus.csv"
CUTOFF_CORPUS_PATH  = ROOT / "DATA" / "CORPUS" / "Annotation_corpus_50.csv"
ANNOTATED_PATH      = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p50_annotated.csv"
OUT_PATH            = ROOT / "DOCS" / "tables" / "corpus_stats.md"

FEASIBILITY_CUTOFF_LABEL = "50th percentile"

# ── Department / priority display ────────────────────────────────────────────

DEPARTMENTS = ["Neonatal intensive care", "Paediatric intensive care", "Adult intensive care"]
TOTAL_KEY   = "__total__"

ICU_LABEL = {
    "Neonatal intensive care":   "Neonatal ICU",
    "Paediatric intensive care": "Paediatric ICU",
    "Adult intensive care":      "Adult ICU",
    TOTAL_KEY:                   "Total study population*",
}
COLUMN_ORDER = DEPARTMENTS + [TOTAL_KEY]

PRIORITY_LABEL = {"Laag": "Low", "Medium": "Medium", "Hoog": "High", "Unknown": "Unknown"}
PRIORITY_ORDER = ["Laag", "Medium", "Hoog", "Unknown"]

UNCLASSIFIED = "Unclassified"

# ── Annotation column groups ─────────────────────────────────────────────────
# The clinical branch of the ontology's two perspectives (patient/
# physiology); everything else the annotated CSV shares with COLUMN_SCHEMES
# is technical (device/sensor/signal/metric). alarmCategory and alarmPriority
# are metadata about the alarm itself, not enrichment, so neither group.

CLINICAL_COLUMNS = {
    "PhysiologicalProperty", "PhysiologicalProcess", "TherapeuticModality",
    "Organ", "OrganSystem", "Patient",
}

# Alarm metadata, not enrichment: alarmPriority is the alarm's own identity
# (like alarmLabel); alarmCategory counts only towards "relevant concepts"
# (see annotation_counts), not towards either enrichment group.
METADATA_COLUMNS = {"alarmPriority", "alarmCategory"}


def norm_key(label: str, priority: str) -> tuple:
    """Whitespace/case-normalized (label, priority) join key."""
    return (" ".join(label.split()).casefold(), " ".join(priority.split()).casefold())


# ── Corpus loading ────────────────────────────────────────────────────────────

def load_corpus(path: Path) -> list:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        r["Alarm_label"]      = r["Alarm_label"].strip()
        r["Alarm_priority"]   = r["Alarm_priority"].strip()
        r["Alarm_department"] = r["Alarm_department"].strip()
        r["Alarm_device"]     = r["Alarm_device"].strip()
        r["Quant_count"]      = int(r["Quant_count"])
    return rows


def load_annotated(path: Path) -> list:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        r["alarmLabel"]    = (r.get("alarmLabel") or "").strip()
        r["alarmPriority"] = (r.get("alarmPriority") or "").strip()
    return rows


def alarm_types(rows: list) -> dict:
    """
    Merge corpus rows into {(label, priority): {priority, departments, devices,
    count}}, the same (label, priority) identity used by build_annotation_
    scaffold.py and build_framework.py's alarmtype_iri. Priority is part of
    that identity key, so every unique alarm type has exactly one priority.
    """
    types = {}
    for r in rows:
        key = (r["Alarm_label"], r["Alarm_priority"])
        t = types.setdefault(key, {"priority": r["Alarm_priority"],
                                    "departments": set(), "devices": set(), "count": 0})
        t["departments"].add(r["Alarm_department"])
        t["devices"].add(r["Alarm_device"])
        t["count"] += r["Quant_count"]
    return types


# ── Device → category, derived from the annotation, not hardcoded ───────────

def build_device_category_map(annotated_rows: list, cutoff_rows: list):
    label_prio_to_devices = defaultdict(set)
    for r in cutoff_rows:
        label_prio_to_devices[norm_key(r["Alarm_label"], r["Alarm_priority"])].add(r["Alarm_device"])

    category_by_device = defaultdict(set)
    unmatched = []
    for r in annotated_rows:
        device_class = (r.get("Device") or "").strip()
        if not device_class:
            continue
        devices = label_prio_to_devices.get(norm_key(r["alarmLabel"], r["alarmPriority"]))
        if not devices:
            unmatched.append((r["alarmLabel"], r["alarmPriority"]))
            continue
        for dev in devices:
            category_by_device[dev].add(device_class)

    conflicts = {d: c for d, c in category_by_device.items() if len(c) > 1}
    if conflicts:
        raise ValueError(
            "Device model(s) annotated with more than one category — "
            f"category derivation assumes exactly one per model: {conflicts}"
        )

    resolved = {d: next(iter(c)) for d, c in category_by_device.items()}
    return resolved, unmatched


def categories_of(devices: set, category_by_device: dict) -> set:
    return {category_by_device.get(d, UNCLASSIFIED) for d in devices}


# ── Corpus-stage statistics (Initial alarms / Alarm corpus after split) ─────

def corpus_section(rows: list, category_by_device: dict) -> dict:
    """
    Per ICU + total: occurrence count, unique alarm-type count, unique
    alarm-type count broken down by device category, and unique alarm-type
    count broken down by priority (counting distinct (label, priority)
    alarm types, not alarm-occurrence volume — each type carries exactly
    one priority by construction, so this always sums to n_unique).
    """
    types = alarm_types(rows)
    section = {}
    for col in COLUMN_ORDER:
        if col == TOTAL_KEY:
            scoped_types = list(types.values())
            n_alarms = sum(r["Quant_count"] for r in rows)
        else:
            scoped_types = [t for t in types.values() if col in t["departments"]]
            n_alarms = sum(r["Quant_count"] for r in rows if r["Alarm_department"] == col)

        by_category = Counter()
        by_priority = Counter()
        for t in scoped_types:
            for cat in categories_of(t["devices"], category_by_device):
                by_category[cat] += 1
            by_priority[t["priority"]] += 1

        section[col] = {
            "n_alarms": n_alarms,
            "n_unique": len(scoped_types),
            "by_category": by_category,
            "by_priority": by_priority,
        }
    return section


# ── Annotation characteristics ───────────────────────────────────────────────

def match_departments(annotated_rows: list, cutoff_rows: list):
    label_prio_to_departments = defaultdict(set)
    for r in cutoff_rows:
        label_prio_to_departments[norm_key(r["Alarm_label"], r["Alarm_priority"])].add(r["Alarm_department"])

    matched, unmatched = [], []
    for r in annotated_rows:
        depts = label_prio_to_departments.get(norm_key(r["alarmLabel"], r["alarmPriority"]))
        if not depts:
            unmatched.append(r)
        else:
            matched.append((r, depts))
    return matched, unmatched


def annotation_counts(row: dict, technical_columns: list, clinical_columns: list) -> dict:
    def n_filled(cols):
        return sum(1 for c in cols if (row.get(c) or "").strip())

    technical = n_filled(technical_columns)
    clinical  = n_filled(clinical_columns)
    return {
        "relevant_concepts":    n_filled(["alarmCategory"] + technical_columns + clinical_columns),
        "technically_enriched": technical,
        "clinically_enriched":  clinical,
        "annotated_triples":    technical + clinical,
    }


def median_iqr(values: list) -> str:
    if not values:
        return "–"
    s = pd.Series(values, dtype=float)
    med, q1, q3 = s.median(), s.quantile(0.25), s.quantile(0.75)
    return f"{med:.0f}[{q1:.0f}-{q3:.0f}]"


def annotation_section(annotated_rows: list, cutoff_rows: list, technical_columns: list, clinical_columns: list):
    matched, unmatched = match_departments(annotated_rows, cutoff_rows)

    per_alarm = []
    for row, depts in matched:
        per_alarm.append((depts, annotation_counts(row, technical_columns, clinical_columns)))

    metrics = ["relevant_concepts", "technically_enriched", "clinically_enriched", "annotated_triples"]
    section = {}
    for col in COLUMN_ORDER:
        scoped = [c for depts, c in per_alarm if col == TOTAL_KEY or col in depts]
        section[col] = {m: median_iqr([c[m] for c in scoped]) for m in metrics}
    return section, unmatched


# ── Markdown rendering ───────────────────────────────────────────────────────

def render_markdown(initial: dict, split: dict, annotation: dict) -> str:
    header = ["", ICU_LABEL["Neonatal intensive care"], ICU_LABEL["Paediatric intensive care"],
              ICU_LABEL["Adult intensive care"], ICU_LABEL[TOTAL_KEY]]
    lines = [
        "Table XXX. Alarm corpus specifications and annotation characteristics.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]

    def row(label, values):
        lines.append("| " + " | ".join([label] + [str(v) for v in values]) + " |")

    all_categories = sorted({cat for col in COLUMN_ORDER for cat in initial[col]["by_category"]}
                             | {cat for col in COLUMN_ORDER for cat in split[col]["by_category"]})

    row("**Initial alarms, n**", [initial[c]["n_alarms"] for c in COLUMN_ORDER])
    row("Unique alarm labels, n", [initial[c]["n_unique"] for c in COLUMN_ORDER])
    for cat in all_categories:
        row(f"&nbsp;&nbsp;{cat}", [initial[c]["by_category"].get(cat, "-") for c in COLUMN_ORDER])
    lines.append("| Alarm priority, n (unique alarms) | | | | |")
    for prio in PRIORITY_ORDER:
        row(f"&nbsp;&nbsp;{PRIORITY_LABEL[prio]}",
            [initial[c]["by_priority"].get(prio, "-") for c in COLUMN_ORDER])

    lines.append(f"| **Alarm corpus after feasibility split ({FEASIBILITY_CUTOFF_LABEL})** | | | | |")
    row("Unique alarm labels, n", [split[c]["n_unique"] for c in COLUMN_ORDER])
    for cat in all_categories:
        row(f"&nbsp;&nbsp;{cat}", [split[c]["by_category"].get(cat, "-") for c in COLUMN_ORDER])
    lines.append("| Alarm priority, n (unique alarms) | | | | |")
    for prio in PRIORITY_ORDER:
        row(f"&nbsp;&nbsp;{PRIORITY_LABEL[prio]}",
            [split[c]["by_priority"].get(prio, "-") for c in COLUMN_ORDER])

    lines.append("| **Annotation characteristics** | | | | |")
    row("Relevant concepts, median[IQR]",    [annotation[c]["relevant_concepts"] for c in COLUMN_ORDER])
    row("Clinically enriched, median[IQR]",  [annotation[c]["clinically_enriched"] for c in COLUMN_ORDER])
    row("Technically enriched, median[IQR]", [annotation[c]["technically_enriched"] for c in COLUMN_ORDER])
    row("Annotated triples, median[IQR]",    [annotation[c]["annotated_triples"] for c in COLUMN_ORDER])

    lines.append("")
    lines.append(
        "*The total study population alarm count is not equal to the sum of "
        "the three individual ICUs due to overlap in alarm occurrence between them."
    )
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    total_rows     = load_corpus(TOTAL_CORPUS_PATH)
    cutoff_rows    = load_corpus(CUTOFF_CORPUS_PATH)
    annotated_rows = load_annotated(ANNOTATED_PATH)

    print(f"[load]   {len(total_rows)} row(s) from {TOTAL_CORPUS_PATH.name}")
    print(f"[load]   {len(cutoff_rows)} row(s) from {CUTOFF_CORPUS_PATH.name}")
    print(f"[load]   {len(annotated_rows)} row(s) from {ANNOTATED_PATH.name}")

    category_by_device, unmatched_devices = build_device_category_map(annotated_rows, cutoff_rows)
    print(f"[device] {len(category_by_device)} device model(s) categorized from annotation:")
    for dev, cat in sorted(category_by_device.items()):
        print(f"         {dev:<16} -> {cat}")
    all_devices = {r["Alarm_device"] for r in total_rows}
    unclassified = sorted(all_devices - set(category_by_device))
    if unclassified:
        print(f"[WARN]   Device model(s) with no annotated occurrence, reported as "
              f"'{UNCLASSIFIED}': {', '.join(unclassified)}")
    if unmatched_devices:
        print(f"[WARN]   {len(unmatched_devices)} annotated row(s) did not match any corpus_50 "
              f"row by (label, priority), skipped for device categorization:")
        for label, prio in unmatched_devices:
            print(f"         {label!r} ({prio})")

    initial = corpus_section(total_rows, category_by_device)
    split   = corpus_section(cutoff_rows, category_by_device)

    header = [c.strip() for c in pd.read_csv(ANNOTATED_PATH, sep=";", nrows=0).columns]
    technical_columns = [c for c in header
                         if c in COLUMN_SCHEMES and c not in CLINICAL_COLUMNS and c not in METADATA_COLUMNS]
    clinical_columns  = [c for c in header if c in CLINICAL_COLUMNS]
    print(f"[cols]   technical: {technical_columns}")
    print(f"[cols]   clinical:  {clinical_columns}")

    annotation, unmatched_annotation = annotation_section(annotated_rows, cutoff_rows,
                                                            technical_columns, clinical_columns)
    if unmatched_annotation:
        print(f"[WARN]   {len(unmatched_annotation)} annotated row(s) did not match any corpus_50 "
              f"row by (label, priority), excluded from Annotation characteristics:")
        for r in unmatched_annotation:
            print(f"         {r['alarmLabel']!r} ({r['alarmPriority']})")

    markdown = render_markdown(initial, split, annotation)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"\n[out]    Wrote {OUT_PATH.relative_to(ROOT)}")
    print()
    print(markdown)


if __name__ == "__main__":
    main()
