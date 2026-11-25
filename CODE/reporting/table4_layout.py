"""
table4_layout.py

Rearranges the Table 4 sheet of the tables workbook:
  - removes the two alarm-rate rows (the manuscript claims no reduction of
    nurse load or actionability) and the alarm-rate sentence of the footnote;
  - moves the delay-filter rows into their own section below clinical
    inference and the Total row, headed "Silencing comparison to delay filter".

Cell values (formulas included) and styles are moved as they are; the
table's closing bottom border moves from Total to the last filter row.
Stops if the sheet is not in the expected (old) layout.

If Excel has the workbook open (lock file present), the result is written to
Table4_occurrence_layout.xlsx instead.

Usage:  python3 CODE/reporting/table4_layout.py
"""

from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.styles import Border

ROOT = Path(__file__).resolve().parents[2]
WORKBOOK = ROOT / "MANUSCRIPT" / "table_drafts" / "Table4_occurrence.xlsx"
NCOL = 9                     # A..I
FIRST = 16                   # first row rearranged ("Invalidation and redundancy")
EXPECTED = {
    16: "Invalidation and redundancy", 17: "Distinct alarms", 18: "Alarm rate without",
    19: "Alarm rate with", 20: "Also suppressed", 21: "Passed by a filter",
    22: "of which high priority", 23: "Suppressed by a filter only",
    24: "Clinical inference", 25: "3a", 26: "3b", 27: "Total", 28: "* Non-empty",
}
HEADER = "Silencing comparison to delay filter"
RATE_SENTENCE = ("Alarm rate: alarms per clock hour with at least one active alarm, per patient "
                 "(hours from the complete stream), without or with the silences of invalidation "
                 "and redundancy. ")


def snapshot(ws, r):
    return [(ws.cell(r, c).value, copy(ws.cell(r, c)._style)) for c in range(1, NCOL + 1)], \
           ws.row_dimensions[r].height


def put(ws, r, snap):
    cells, height = snap
    for c, (v, st) in enumerate(cells, start=1):
        ws.cell(r, c).value = v
        ws.cell(r, c)._style = copy(st)
    ws.row_dimensions[r].height = height


def main():
    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb["Table 4"]
    for r, start in EXPECTED.items():
        if not str(ws.cell(r, 1).value or "").startswith(start):
            raise SystemExit(f"Table 4 row {r} is {ws.cell(r, 1).value!r}, expected {start!r}...: "
                             f"not the layout this script rearranges.")

    old = {r: snapshot(ws, r) for r in range(FIRST, 29)}
    header = ([(HEADER if c == 1 else None, st) for c, (_, st) in enumerate(old[24][0], start=1)],
              old[24][1])                                   # styled like "Clinical inference"
    order = [old[16], old[17], old[24], old[25], old[26], old[27],
             header, old[20], old[21], old[22], old[23], old[28]]

    ws.unmerge_cells(f"A28:I28")
    for r in range(FIRST, 29):
        for c in range(1, NCOL + 1):
            ws.cell(r, c).value = None
            ws.cell(r, c)._style = copy(ws.cell(1, 30)._style)   # blank style
        ws.row_dimensions[r].height = None
    for i, snap in enumerate(order):
        put(ws, FIRST + i, snap)

    total, last, note = FIRST + 5, FIRST + 10, FIRST + 11
    for c in range(1, NCOL + 1):
        closing = copy(ws.cell(total, c).border.bottom)
        b = ws.cell(total, c).border
        ws.cell(total, c).border = Border(left=b.left, right=b.right, top=b.top,
                                          bottom=copy(ws.cell(total - 1, c).border.bottom))
        b = ws.cell(last, c).border
        ws.cell(last, c).border = Border(left=b.left, right=b.right, top=b.top, bottom=closing)
    ws.merge_cells(f"A{note}:I{note}")
    ws.cell(note, 1).value = str(ws.cell(note, 1).value).replace(RATE_SENTENCE, "")
    if "Alarm rate" in ws.cell(note, 1).value:
        print("WARNING: footnote still mentions the alarm rate; check its wording.")
    ws.print_area = f"A1:I{note}"

    locked = (WORKBOOK.parent / f"~${WORKBOOK.name}").exists()
    out = WORKBOOK.with_name("Table4_occurrence_layout.xlsx") if locked else WORKBOOK
    wb.save(out)
    for r in range(FIRST, note + 1):
        print(f"  {r:2}  {str(ws.cell(r, 1).value)[:70]}")
    print(f"Wrote {out.relative_to(ROOT)}" + ("  (workbook open in Excel: wrote a copy)" if locked else ""))


if __name__ == "__main__":
    main()
