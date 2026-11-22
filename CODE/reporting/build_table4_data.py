"""
build_table4_data.py

Loads MANUSCRIPT/table_drafts/table4_stats.csv (from table4_stats.py) into the
Data sheet of the tables workbook (table tblTable4, Unit | Key | Value |
Description), replacing its values, and writes the Power Query M code for
refreshing it from the CSV next to it. Also brings Table 4's wording in line
with the definitions in table4_stats.py: the exclusion row, and the filter and
alarm-rate footnotes.

If Excel has the workbook open (lock file present), the result is written to
Table4_occurrence_T4.xlsx instead, so Excel's next save cannot overwrite it.

Usage:  python3 CODE/reporting/build_table4_data.py
"""

import csv
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

ROOT = Path(__file__).resolve().parents[2]
DRAFTS = ROOT / "MANUSCRIPT" / "table_drafts"
WORKBOOK = DRAFTS / "Table4_occurrence.xlsx"
CSV_PATH = DRAFTS / "table4_stats.csv"

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
    "2. Name the query tblTable4; Close & Load To... > Table > Existing worksheet: Data!$A$1",
    "   (delete the prefilled table first, or load elsewhere and copy).",
    "3. Refresh after rerunning MDA-POC (poc_main.py) and CODE/reporting/table4_stats.py.",
    "4. Unit is NICU, PICU or ALL; Table 4 looks values up by Unit + Key; an empty Value shows as N.",
    "Values below come from table4_stats.py for the run named in ALL/run_patients_n (not dummy data).",
    "",
    "Power Query (M):",
] + M_CODE.splitlines()

RELABEL = {
    "Excluded, outside annotated strata, n [%]": "Without a blueprint, n [%]",
}
FOOTNOTE_EDITS = [
    ("‡ Filter: suppresses alarms of low priority or shorter than N seconds.",
     "‡ Filter: a 5-second delay, suppressing low and medium priority alarms shorter than 5 s."),
    ("Alarm rate: alarms per admitted clock hour, without or with the silences of invalidation and redundancy.",
     "Alarm rate: alarms per clock hour with at least one active alarm, per patient (hours from the complete "
     "stream), without or with the silences of invalidation and redundancy. Alarms without a blueprint are "
     "left out throughout."),
]


def main():
    with CSV_PATH.open(encoding="utf-8") as fh:
        rows = [(r["Unit"], r["Key"], r["Value"], r["Description"]) for r in csv.DictReader(fh, delimiter=";")]

    wb = openpyxl.load_workbook(WORKBOOK)
    d = wb["Data"]
    for r in range(2, max(d.max_row, len(rows) + 1) + 1):
        for c in range(1, 7):
            d.cell(row=r, column=c).value = None
    for i, (unit, key, value, desc) in enumerate(rows, start=2):
        v = float(value) if value not in ("",) else None
        d.cell(row=i, column=1, value=unit)
        d.cell(row=i, column=2, value=key)
        d.cell(row=i, column=3, value=int(v) if v is not None and v == int(v) else v)
        d.cell(row=i, column=4, value=desc)
    for i, line in enumerate(HOWTO, start=2):
        d.cell(row=i, column=6, value=line).font = Font(name="Consolas", size=9)
    d.tables["tblTable4"].ref = f"A1:D{len(rows) + 1}"

    t = wb["Table 4"]
    changed = []
    for row in t.iter_rows():
        for c in row:
            if not isinstance(c.value, str) or c.value.startswith("="):
                continue
            new = RELABEL.get(c.value, c.value)
            for old, rep in FOOTNOTE_EDITS:
                new = new.replace(old, rep)
            if new != c.value:
                c.value = new
                changed.append(c.coordinate)

    locked = (WORKBOOK.parent / f"~${WORKBOOK.name}").exists()
    out = WORKBOOK.with_name("Table4_occurrence_T4.xlsx") if locked else WORKBOOK
    wb.save(out)
    print(f"Data: {len(rows)} values; Table 4 cells reworded: {', '.join(changed) or 'none'}")
    print(f"Wrote {out.relative_to(ROOT)}" + ("  (workbook open in Excel: wrote a copy)" if locked else ""))


if __name__ == "__main__":
    main()
