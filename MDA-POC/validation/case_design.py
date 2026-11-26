"""
case_design.py — the case tables of validation/case_design.xlsx, as CSV.

The workbook has one sheet per rule (named by the rule id: 1a, 1b, ...),
hand-written: the rule, its premises (column A holds the premise id) and
open questions. This script never writes the workbook (openpyxl would drop
its Power Query definitions); it only reads the premises from it, and
writes two CSVs that the workbook loads with Power Query
(validation/case_design.pq):

  validation/results/case_design_cases.csv      one row per case
      Rule, Case, Title, Premise, Purpose, Pairs with, Status, Review,
      Outcome, Check, Run
  validation/results/case_design_coverage.csv   one row per premise
      Rule, Premise, Confirm cases, Falsify cases, Note

Premises are named by id only (P1, P2, ...); their statements are on the
rule's sheet. Outcome is PASS or FAIL from the latest regression run ("not run" without
one, "draft (not run)" for drafts). A FAIL is a finding, not an error in the
case: cases state what the rule should conclude, whether or not it does.

Check (design warnings, not errors): a falsify case names exactly one
premise and pairs with an existing confirm case of the same rule; a confirm
case names at least one premise. Note: a premise without a confirm or a
falsify case.

Reads validation/cases/<rule>/*.yaml and validation/results/cases_results.csv
and cases_run.json (written by engine/regression.py).

  cd MDA-POC/engine && python3 regression.py
  cd .. && python3 validation/case_design.py      # then Refresh All in Excel
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import cases as C  # noqa: E402

CASES_CSV = C.RESULTS_DIR / "case_design_cases.csv"
COVERAGE_CSV = C.RESULTS_DIR / "case_design_coverage.csv"
CASE_COLUMNS = ["Rule", "Case", "Title", "Premise", "Purpose", "Pairs with", "Status", "Review", "Outcome",
                "Check", "Run"]
COVERAGE_COLUMNS = ["Rule", "Premise", "Confirm cases", "Falsify cases", "Note"]


def read_results() -> tuple:
    """({case id: [result, ...]}, run metadata or {})."""
    path = C.RESULTS_DIR / "cases_results.csv"
    by_case = defaultdict(list)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                by_case[r["case"]].append(r["result"])
    meta_path = C.RESULTS_DIR / "cases_run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return by_case, meta


def outcome(case, results: list) -> str:
    if case.status == "draft":
        return "draft (not run)"
    if not results:
        return "not run"
    return "PASS" if all(r in ("PASS", "XPASS", "pass") for r in results) else "FAIL"


def checks(case, by_id: dict) -> str:
    problems = []
    if case.purpose == "falsify":
        if len(case.premise) != 1:
            problems.append(f"falsify should break exactly one premise (names {len(case.premise)})")
        pair = by_id.get(case.pairs_with)
        if not case.pairs_with:
            problems.append("no pairs_with")
        elif pair is None:
            problems.append(f"pairs_with '{case.pairs_with}' is not a case")
        elif pair.purpose != "confirm" or pair.rule != case.rule:
            problems.append(f"pairs_with '{case.pairs_with}' is not a confirm case of rule {case.rule}")
    elif case.purpose == "confirm" and not case.premise:
        problems.append("names no premise")
    return "; ".join(problems)


def main() -> None:
    cases = C.load_cases()
    by_id = {c.id: c for c in cases}
    by_case, meta = read_results()
    run = (f"{meta['run']} on {meta['commit']}" + (" (uncommitted changes)" if meta.get("uncommitted_changes") else "")
           if meta else "no regression run")

    by_rule = defaultdict(list)
    for c in cases:
        by_rule[c.rule].append(c)

    case_rows, coverage_rows = [], []
    for rule in sorted(by_rule):
        for case in sorted(by_rule[rule], key=lambda c: (c.purpose != "confirm", c.id)):
            case_rows.append([rule, case.id, case.title, ", ".join(case.premise), case.purpose, case.pairs_with,
                              case.status, case.review, outcome(case, by_case.get(case.id, [])),
                              checks(case, by_id), run])

    for rule in sorted(set(by_rule) | {r for r in C.RULES if C.load_premises(r)}):
        cs = by_rule.get(rule, [])
        for pid in C.load_premises(rule):
            confirm = [c.id for c in cs if pid in c.premise and c.purpose == "confirm"]
            falsify = [c.id for c in cs if pid in c.premise and c.purpose == "falsify"]
            note = "; ".join(n for n, missing in (("no confirm case", not confirm), ("no falsify case", not falsify))
                             if missing)
            coverage_rows.append([rule, pid, ", ".join(confirm), ", ".join(falsify), note])

    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for path, header, rows in ((CASES_CSV, CASE_COLUMNS, case_rows), (COVERAGE_CSV, COVERAGE_COLUMNS, coverage_rows)):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
        print(f"written {path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
