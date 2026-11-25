"""
export_annotation_xlsx.py

Writes the annotation workbook for the co-authors from a consensus annotation
CSV (default: p99):

    in:   DATA/ANNOTATION/consensus/p<cutoff>_annotated.csv
          DATA/CORPUS/Annotation_corpus_<cutoff>.csv     (context columns)
          FRAMEWORK/VOCABULARY/{seed/vocab_base,vocab_generated}.ttl  (dropdowns)
    out:  DATA/ANNOTATION/to_authors/p<cutoff>_annotation.xlsx

Sheets
  Instructions  what to do, colour legend, reviewStatus values, one example row
  Annotation    one row per distinct alarm (model + label + priority).
                Locked: the identity (alarmLabel, alarmPriority, Device.Type)
                and read-only context (gateway device, departments, alarm
                count, status). Editable: reviewStatus, every annotation
                column, and a free-text comment. Rows already annotated
                are shaded green, rows to check amber, new rows white.
  Vocabulary    the terms in use per column; the annotation columns offer them
                as a dropdown but accept a new term after a warning (the
                vocabulary is open: build_framework.py stubs new terms).
  _meta         (very hidden) source file + checksum, CSV header, and the
                identity of every row, so import_annotation_xlsx.py can rebuild
                the CSV exactly and detect edited identities.

The sheets are protected without a password: against accidents, not people.

Usage:  python3 CODE/data_prep/export_annotation_xlsx.py [cutoff]
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime

import rdflib
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from annotation_xlsx_common import (
    COMMENT, CONSENSUS_DIR, CONTEXT, CORPUS_DIR, FIRST_ROW, HEADER_ROW, ID_COL, IDENTITY,
    MODELS, REVIEW, REVIEW_VALUES, ROOT, SCHEME, SHEET_DATA, SHEET_HELP, SHEET_META,
    SHEET_VOCAB, VOCAB_FILES, VOCAB_NS, key, read_csv, sha256)

CUTOFF = sys.argv[1] if len(sys.argv) > 1 else "99"
SRC = CONSENSUS_DIR / f"p{CUTOFF}_annotated.csv"
CORPUS = CORPUS_DIR / f"Annotation_corpus_{CUTOFF}.csv"
OUT = ROOT / "DATA" / "ANNOTATION" / "to_authors" / f"p{CUTOFF}_annotation.xlsx"

DEPT_SHORT = {"Neonatal intensive care": "NICU", "Paediatric intensive care": "PICU",
              "Adult intensive care": "Adult ICU"}
GROUPS = [  # (column name test, group label) for the band in row 1
    (lambda c: c in IDENTITY, "Alarm (do not edit)"),
    (lambda c: c in CONTEXT, "Context (read only)"),
    (lambda c: c == REVIEW, "Review"),
    (lambda c: c == "alarmCategory", "Alarm"),
    (lambda c: c.startswith("Device"), "Device"),
    (lambda c: c.startswith("Component") or c == "FunctionalUnit", "Component"),
    (lambda c: c.startswith("Sensor"), "Sensor"),
    (lambda c: c.startswith("Signal"), "Signal"),
    (lambda c: c.startswith("Metric"), "Metric"),
    (lambda c: c in ("TherapeuticModality", "PhysiologicalProperty", "PhysiologicalProcess"),
     "Therapy & physiology"),
    (lambda c: c == COMMENT, "Comment"),
]

FONT = "Arial"
F_BASE, F_BOLD = Font(name=FONT, size=9), Font(name=FONT, size=9, bold=True)
F_MUTED = Font(name=FONT, size=9, italic=True, color="666666")
F_EXCL = Font(name=FONT, size=9, color="999999")
FILL = {k: PatternFill("solid", fgColor=v) for k, v in {
    "identity": "E7E6E6", "context": "F2F2F2", "header": "D9E1F2", "band": "B4C6E7",
    "done": "E2EFDA", "check": "FFF2CC", "new": "FFFFFF"}.items()}
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def vocabulary(header, rows):
    """Terms per annotation column: vocabulary notations of its scheme (minus
    the scheme's root and pre-coordinated shorthands such as
    AirwayPressure_Peak) plus every value already used in that column."""
    g = rdflib.Graph()
    for f in VOCAB_FILES:
        g.parse(f)
    skos = rdflib.namespace.SKOS
    roots = set(g.subjects(skos.topConceptOf, None))
    by_scheme = defaultdict(set)
    for s, o in g.subject_objects(skos.notation):
        s = str(s)
        if s.startswith(VOCAB_NS) and rdflib.URIRef(s) not in roots:
            by_scheme[s[len(VOCAB_NS):].split("/")[0]].add(str(o))
    for scheme, terms in by_scheme.items():
        by_scheme[scheme] = {t for t in terms if not ("_" in t and t.split("_")[0] in terms)}
    vocab = {}
    for col, scheme in SCHEME.items():
        if col not in header:
            continue
        used = {r[header.index(col)].strip() for r in rows if r[header.index(col)].strip()}
        vocab[col] = sorted(by_scheme.get(scheme, set()) | used, key=str.casefold)
    return vocab


def context(header):
    """Gateway device(s), departments and alarm count per identity (corpus)."""
    with MODELS.open(encoding="utf-8", newline="") as fh:
        model_of = {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}
    ctx = defaultdict(lambda: {"gw": set(), "dept": set(), "n": 0})
    with CORPUS.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            c = ctx[key(r["Alarm_label"], r["Alarm_priority"], model_of[r["Alarm_device"]])]
            c["gw"].add(r["Alarm_device"])
            c["dept"].add(DEPT_SHORT.get(r["Alarm_department"], r["Alarm_department"]))
            c["n"] += int(r["Quant_count"])
    return ctx


def status(rec, annotation_cols):
    rv = rec[REVIEW].strip()
    if rv == "REVIEW":
        return "Check: copied from another device", "check"
    if rv == "REVIEW-PRIO":
        return "Check: copied to a new priority", "check"
    if rv == "EXCL" or any(rec[c].strip() for c in annotation_cols):
        return "Annotated", "done"
    return "New: to annotate", "new"


def main():
    header, rows = read_csv(SRC)
    missing = [c for c in IDENTITY + [REVIEW] if c not in header]
    if missing:
        raise SystemExit(f"{SRC.name} lacks column(s) {missing}")
    annotation_cols = [c for c in header if c not in IDENTITY and c != REVIEW]
    sheet_cols = [ID_COL] + IDENTITY + CONTEXT + [REVIEW] + annotation_cols + [COMMENT]
    vocab, ctx = vocabulary(header, rows), context(header)

    wb = Workbook()
    help_ws = wb.active
    help_ws.title = SHEET_HELP
    ws = wb.create_sheet(SHEET_DATA)
    vs = wb.create_sheet(SHEET_VOCAB)
    meta = wb.create_sheet(SHEET_META)

    # ── Annotation sheet ────────────────────────────────────────────────────
    col_of = {c: i + 1 for i, c in enumerate(sheet_cols)}
    # row 1: group band (merged per contiguous group)
    labels = [next((g for test, g in GROUPS if test(c)), "") for c in sheet_cols]
    start = 2
    for i in range(2, len(sheet_cols) + 2):
        if i == len(sheet_cols) + 1 or labels[i - 1] != labels[start - 1]:
            cell = ws.cell(1, start, labels[start - 1])
            cell.font, cell.fill, cell.alignment = F_BOLD, FILL["band"], Alignment(horizontal="center")
            if i - 1 > start:
                ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=i - 1)
            start = i
    # row 2: exact CSV column names (the import maps by these)
    for c, j in col_of.items():
        cell = ws.cell(HEADER_ROW, j, c)
        cell.font, cell.border = F_BOLD, BORDER
        cell.fill = FILL["identity"] if c in IDENTITY else FILL["context"] if c in CONTEXT \
            else FILL["header"]
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        if c in vocab:
            examples = ", ".join(vocab[c][:6]) or "none yet"
            cell.comment = Comment(f"One term per cell. Pick from the dropdown "
                                   f"(sheet Vocabulary) or type a new CamelCase term.\n"
                                   f"Terms: {examples}{', ...' if len(vocab[c]) > 6 else ''}",
                                   "annotation template", width=260, height=110)
    ws.cell(HEADER_ROW, col_of[REVIEW]).comment = Comment(
        "x = annotated and checked; EXCL = not annotatable (label conveys no "
        "interpretable condition); REVIEW / REVIEW-PRIO = copied, to be checked. "
        "Leave empty while not done.", "annotation template", width=260, height=90)

    for n, r in enumerate(rows):
        rec = dict(zip(header, r))
        k = key(rec["alarmLabel"], rec["alarmPriority"], rec["Device.Type"])
        c = ctx.get(k, {"gw": set(), "dept": set(), "n": 0})
        stat, shade = status(rec, annotation_cols)
        values = {ID_COL: n, **{x: rec[x] for x in header},
                  "Gateway device": ", ".join(sorted(c["gw"])),
                  "Department(s)": ", ".join(sorted(c["dept"])),
                  "Alarm count": c["n"], "Status": stat, COMMENT: ""}
        row = FIRST_ROW + n
        for col, j in col_of.items():
            cell = ws.cell(row, j, values[col])
            cell.border = BORDER
            if col in IDENTITY:
                cell.fill, cell.font = FILL["identity"], F_BASE
            elif col in CONTEXT or col == ID_COL:
                cell.fill, cell.font = FILL["context"], F_MUTED
                if col == "Alarm count":
                    cell.number_format = "#,##0"
            else:
                cell.fill = FILL[shade]
                cell.font = F_EXCL if rec[REVIEW].strip() == "EXCL" else F_BASE
                cell.number_format = "@"
                cell.protection = Protection(locked=False)
    last = FIRST_ROW + len(rows) - 1

    # dropdowns: reviewStatus strict; annotation columns warn on a new term
    dv = DataValidation(type="list", formula1='"' + ",".join(REVIEW_VALUES) + '"',
                        allow_blank=True, showErrorMessage=True, errorStyle="stop",
                        errorTitle="reviewStatus", error="Use x, EXCL, REVIEW or REVIEW-PRIO "
                        "(or leave empty).")
    dv.add(f"{get_column_letter(col_of[REVIEW])}{FIRST_ROW}:{get_column_letter(col_of[REVIEW])}{last}")
    ws.add_data_validation(dv)
    for vj, col in enumerate(vocab, start=1):
        letter = get_column_letter(vj)
        vs.cell(1, vj, col).font = F_BOLD
        vs.cell(1, vj).fill = FILL["header"]
        for i, term in enumerate(vocab[col], start=2):
            vs.cell(i, vj, term).font = F_BASE
        vs.column_dimensions[letter].width = max(14, min(40, max(map(len, vocab[col] + [col])) + 2))
        if not vocab[col]:
            continue
        d = DataValidation(type="list", allow_blank=True, showErrorMessage=True,
                           formula1=f"={SHEET_VOCAB}!${letter}$2:${letter}${len(vocab[col]) + 1}",
                           errorStyle="warning", errorTitle="New term?",
                           error="Not in the current vocabulary. Yes = keep it as a new term "
                                 "(CamelCase, one term per cell); No = choose again.")
        cl = get_column_letter(col_of[col])
        d.add(f"{cl}{FIRST_ROW}:{cl}{last}")
        ws.add_data_validation(d)

    widths = {ID_COL: 5, "alarmLabel": 42, "alarmPriority": 9, "Device.Type": 13,
              "Gateway device": 14, "Department(s)": 15, "Alarm count": 10, "Status": 26,
              REVIEW: 9, COMMENT: 40}
    for c, j in col_of.items():
        ws.column_dimensions[get_column_letter(j)].width = widths.get(c, 18)
    ws.column_dimensions[get_column_letter(col_of[ID_COL])].hidden = True
    ws.row_dimensions[HEADER_ROW].height = 30
    ws.freeze_panes = ws.cell(FIRST_ROW, col_of["Device.Type"] + 1)
    ws.auto_filter.ref = f"B{HEADER_ROW}:{get_column_letter(len(sheet_cols))}{last}"
    for s in (ws, vs):
        s.protection.sheet = True
        s.protection.autoFilter = False      # filtering stays allowed
        s.protection.formatColumns = False   # so does resizing columns
        s.protection.formatRows = False
    ws.protection.sort = False

    # ── Instructions ────────────────────────────────────────────────────────
    n_new = sum(status(dict(zip(header, r)), annotation_cols)[1] == "new" for r in rows)
    n_check = sum(status(dict(zip(header, r)), annotation_cols)[1] == "check" for r in rows)
    lines = [
        (f"Alarm annotation – p{CUTOFF} corpus", "title"),
        (f"{len(rows)} distinct alarms (device model + label + source priority): "
         f"{len(rows) - n_new - n_check} already annotated, {n_check} to check, {n_new} new.", None),
        ("", None),
        ("What to do", "h"),
        ("1. Open the sheet 'Annotation'. Each row is one distinct alarm.", None),
        ("2. Fill in the white rows (new) and check the amber rows; green rows are the "
         "earlier consensus – change them only if you disagree, and say why in the comment.", None),
        ("3. Use the dropdowns where you can (terms in the sheet 'Vocabulary'). A term that is "
         "not in the list is allowed after a warning: write it in CamelCase, one term per cell.", None),
        ("4. Set reviewStatus to x when a row is done, or EXCL when the label conveys no "
         "interpretable alarm condition.", None),
        ("5. Put doubts, alternatives or remarks in 'Annotator comment'.", None),
        (f"6. Save as .xlsx with your initials added (e.g. p{CUTOFF}_annotation_AB.xlsx) "
         "and send it back.", None),
        ("", None),
        ("Please do not", "h"),
        ("• add, delete or reorder rows, or rename / move columns (filtering is fine);", None),
        ("• change the grey columns (alarm identity and context) – they link each row back "
         "to the alarm corpus;", None),
        ("• put more than one term in a cell.", None),
        ("", None),
        ("Colours", "h"),
        ("Grey – alarm identity and context: read only", "identity"),
        ("Green – already annotated", "done"),
        ("Amber – copied from another device or priority: please check", "check"),
        ("White – new: to annotate", "new"),
        ("", None),
        ("reviewStatus", "h"),
        ("x – annotated and checked", None),
        ("EXCL – not annotatable: the label conveys no interpretable alarm condition", None),
        ("REVIEW – annotation copied from the same label on another device model: check", None),
        ("REVIEW-PRIO – annotation copied to a new source priority: check", None),
        ("(empty) – not done yet", None),
    ]
    for i, (text, kind) in enumerate(lines, start=1):
        cell = help_ws.cell(i, 1, text)
        cell.font = Font(name=FONT, size=13, bold=True) if kind == "title" else \
            Font(name=FONT, size=10, bold=True) if kind == "h" else Font(name=FONT, size=10)
        if kind in FILL:
            cell.fill = FILL[kind]
    # example row: the most completely annotated reviewed row
    done = [dict(zip(header, r)) for r in rows if dict(zip(header, r))[REVIEW].strip() == "x"]
    ex = max(done, key=lambda d: sum(bool(d[c].strip()) for c in annotation_cols))
    i = len(lines) + 2
    help_ws.cell(i, 1, "Example of a completed row (from the earlier consensus)").font = \
        Font(name=FONT, size=10, bold=True)
    for k, col in enumerate([c for c in IDENTITY + [REVIEW] + annotation_cols if ex[c].strip()]):
        help_ws.cell(i + 1 + k, 1, col).font = F_BOLD
        help_ws.cell(i + 1 + k, 2, ex[col]).font = F_BASE
    help_ws.column_dimensions["A"].width = 30
    help_ws.column_dimensions["B"].width = 40
    help_ws.sheet_view.showGridLines = False
    help_ws.protection.sheet = True

    # ── _meta (very hidden) ─────────────────────────────────────────────────
    meta.append(["source_csv", str(SRC.relative_to(ROOT))])
    meta.append(["source_sha256", sha256(SRC)])
    meta.append(["corpus_csv", str(CORPUS.relative_to(ROOT))])
    meta.append(["exported_at", datetime.now().isoformat(timespec="seconds")])
    meta.append(["n_rows", len(rows)])
    meta.append(["csv_header"] + header)
    meta.append([ID_COL] + IDENTITY)
    for n, r in enumerate(rows):
        rec = dict(zip(header, r))
        meta.append([n] + [rec[c] for c in IDENTITY])
    meta.sheet_state = "veryHidden"

    wb.active = wb.index(ws)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"{SRC.name}: {len(rows)} rows -> {OUT.relative_to(ROOT)}")
    print(f"  annotated {len(rows) - n_new - n_check}, to check {n_check}, new {n_new}")
    print(f"  vocabulary terms per column: "
          + ", ".join(f"{c}={len(v)}" for c, v in vocab.items()))


if __name__ == "__main__":
    main()
