"""
build_table3_sheet.py

Adds two sheets to the manuscript tables workbook, mirroring Table 4:

  Data_T3  - landing table for the Power Query output of
             MANUSCRIPT/table_drafts/framework_stats.csv (Unit | Key | Value |
             Description), prefilled with that CSV's current values.
  Table 3  - the formatted "Framework summary statistics" table (manuscript
             3.3.1); every cell looks its value up in Data_T3 by Unit + Key and
             shows N when the value is missing or empty.

Run framework_stats_table.py first. If Excel has the workbook open (lock file
present), the result is written to Table4_occurrence_T3.xlsx instead, so
Excel's next save cannot overwrite it.

Usage:  python3 CODE/reporting/build_table3_sheet.py
"""

import csv
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parents[2]
DRAFTS = ROOT / "MANUSCRIPT" / "table_drafts"
WORKBOOK = DRAFTS / "Table4_occurrence.xlsx"
CSV_PATH = DRAFTS / "framework_stats.csv"
DATA, TABLE = "Data_T3", "Table 3"
LAST = 300  # lookup range depth in Data_T3

M_CODE = f'''let
    Source = Csv.Document(File.Contents("{CSV_PATH}"),
        [Delimiter=";", Columns=4, Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    // "en-US": the CSV uses a decimal point; a Dutch locale would read 10.9 as 109
    Typed = Table.TransformColumnTypes(Promoted,
        {{{{"Unit", type text}}, {{"Key", type text}}, {{"Value", type number}}, {{"Description", type text}}}}, "en-US")
in
    Typed'''

HOWTO = [
    "1. Data > Get Data > From Other Sources > Blank Query > Advanced Editor; paste the M code below.",
    "2. Name the query tblTable3; Close & Load To... > Table > Existing worksheet: Data_T3!$A$1",
    "   (delete the prefilled table first, or load elsewhere and copy).",
    "3. Refresh after rerunning CODE/reporting/framework_stats_table.py.",
    "4. Table 3 looks values up by Unit + Key in A2:C300; a missing or empty Value shows as N.",
    "5. OOPS! rows are manual entry: type the counts in column C (or add them to the CSV).",
    "Values below are REAL outputs of framework_stats_table.py, not dummy data.",
    "",
    "Power Query (M):",
] + M_CODE.splitlines()


# ── formula helpers (same conventions as Table 4: no locale-dependent formats) ──

def look(unit, key):
    return (f'LOOKUP(2,1/(({DATA}!$A$2:$A${LAST}="{unit}")*({DATA}!$B$2:$B${LAST}="{key}")),'
            f'{DATA}!$C$2:$C${LAST})')


def num(unit, key, dec=0):
    v = look(unit, key)
    shown = (f"FIXED({v},0,TRUE)" if dec == 0 else
             f'IF(ROUND({v},{dec})=INT(ROUND({v},{dec})),FIXED({v},0,TRUE),'
             f'SUBSTITUTE(FIXED({v},{dec},TRUE),",","."))')
    return f'IFERROR(IF({v}&""="","N",{shown}),"N")'


def n_pct(unit, k_n, k_pct):
    return f'={num(unit, k_n)}&" ["&{num(unit, k_pct, 1)}&"]"'


def med_iqr(unit, stem):
    return (f'={num(unit, stem + "_median", 1)}&" ["&{num(unit, stem + "_q1", 1)}'
            f'&" - "&{num(unit, stem + "_q3", 1)}&"]"')


def yes_no(unit, key, yes, no):
    v = look(unit, key)
    return f'=IFERROR(IF({v}&""="","N",IF({v}=1,"{yes}","{no}")),"N")'


UNITS = [("NICU", "Neonatal ICU"), ("PICU", "Pediatric ICU"), ("ADULT", "Adult ICU"), ("ALL", "Total")]

# (label, kind, args) ; kind: section | unit (per-unit columns) | all (Total column only)
LAYOUT = [
    ("Corpus", "section", None),
    ("Alarms in corpus, n", "unit", lambda u: "=" + num(u, "corpus_alarms_n")),
    ("Distinct alarms in corpus, n", "unit", lambda u: "=" + num(u, "corpus_distinct_n")),
    ("Devices, n", "unit", lambda u: "=" + num(u, "selected_devices_n")),
    ("Elicitation", "section", None),
    ("Distinct alarms annotated, n [%]", "unit", lambda u: n_pct(u, "selected_distinct_n", "selected_distinct_pct")),
    ("Distinct alarms not annotatable *, n", "unit", lambda u: "=" + num(u, "not_annotatable_n")),
    ("Alarm blueprints generated †, n", "unit", lambda u: "=" + num(u, "blueprints_n")),
    ("Alarm volume covered by a blueprint, n [%]", "unit", lambda u: n_pct(u, "blueprint_volume_n", "blueprint_volume_pct")),
    ("Knowledge base", "section", None),
    ("Blueprints in this unit only, n", "unit", lambda u: "" if u == "ALL" else "=" + num(u, "blueprints_unit_only_n")),
    ("Triples per blueprint, logs only ‡, median [IQR]", "unit", lambda u: med_iqr(u, "triples_logs")),
    ("Triples per blueprint, enriched, median [IQR]", "unit", lambda u: med_iqr(u, "triples_enriched")),
]

FOOTNOTE = ("Distinct alarm: unique combination of device model, alarm label and priority, selected for annotation "
            "per department, device and priority stratum up to 75% of its alarm volume. Totals count a distinct alarm "
            "or device occurring in several units once, so they are not the sum of the units. Distinct alarms "
            "annotated = not annotatable + blueprints generated. * Not annotatable: the label conveys no interpretable "
            "alarm condition (a raw device code, or a metric without direction); no blueprint is generated. "
            "† One blueprint per distinct alarm; it serves that alarm in every unit where it occurs. "
            "‡ Logs only: the blueprint the same builder produces from what an alarm log provides "
            "(label, priority, originating device).")


def read_csv():
    rows = []
    with CSV_PATH.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            v = r["Value"]
            rows.append((r["Unit"], r["Key"], float(v) if v not in ("",) else None, r["Description"]))
    return rows


def build(wb):
    for name in (DATA, TABLE):
        if name in wb.sheetnames:
            del wb[name]
    arial = lambda **k: Font(name="Arial", **{"size": 10, **k})

    # ── Data_T3 ──
    d = wb.create_sheet(DATA)
    d.append(["Unit", "Key", "Value", "Description", None, "How to use"])
    for unit, key, val, desc in read_csv():
        if val is not None and val == int(val):
            val = int(val)
        d.append([unit, key, val, desc])
    for i, line in enumerate(HOWTO, start=2):
        d.cell(row=i, column=6, value=line).font = Font(name="Consolas", size=9)
    n = d.max_row
    tbl = Table(displayName="tblTable3", ref=f"A1:D{len(read_csv()) + 1}")
    tbl.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    d.add_table(tbl)
    for col, w in zip("ABCDEF", (8, 28, 12, 70, 3, 110)):
        d.column_dimensions[col].width = w

    # ── Table 3 ──
    t = wb.create_sheet(TABLE)
    thin, medium = Side(style="thin"), Side(style="medium")
    t["A1"] = "Table XXX.  Framework summary statistics"
    t["A1"].font = arial(bold=True)
    for c in "ABCDE":
        t[f"{c}1"].border = Border(bottom=medium)
    for j, (_, head) in enumerate(UNITS, start=2):
        cell = t.cell(row=2, column=j, value=head)
        cell.font = arial(bold=True)
        cell.alignment = Alignment(horizontal="center")
        cell.border = Border(bottom=thin)
    t.cell(row=2, column=1).border = Border(bottom=thin)

    r = 3
    for label, kind, arg in LAYOUT:
        t.cell(row=r, column=1, value=label).font = arial(bold=(kind == "section"))
        if kind == "unit":
            for j, (u, _) in enumerate(UNITS, start=2):
                t.cell(row=r, column=j, value=arg(u))
        elif kind == "all":
            t.cell(row=r, column=5, value=arg)
        for j in range(2, 6):
            t.cell(row=r, column=j).alignment = Alignment(horizontal="center")
            t.cell(row=r, column=j).font = arial()
        r += 1
    for c in range(1, 6):
        t.cell(row=r - 1, column=c).border = Border(bottom=medium)
    t.cell(row=r, column=1, value=FOOTNOTE).font = arial(italic=True, size=8)
    t.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    t.cell(row=r, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    t.row_dimensions[r].height = 60
    t.column_dimensions["A"].width = 50
    for c in "BCDE":
        t.column_dimensions[c].width = 20


def main():
    wb = openpyxl.load_workbook(WORKBOOK)
    build(wb)
    locked = (WORKBOOK.parent / f"~${WORKBOOK.name}").exists()
    out = WORKBOOK.with_name("Table4_occurrence_T3.xlsx") if locked else WORKBOOK
    wb.save(out)
    print(f"Wrote {out.relative_to(ROOT)}" + ("  (workbook open in Excel: wrote a copy)" if locked else ""))


if __name__ == "__main__":
    main()
