"""
cases_appendix.py — the verification cases and their latest results as one
Markdown document (for the manuscript's appendix), per rule: each case's
purpose, rationale, alarms, and every assertion with its outcome. Each rule
is linked to the reasoning competency question it answers (CQ8-10,
manuscript Table 1; RULE_CQ below).

Reads validation/cases/*.yaml and validation/results/cases_results.csv
(written by engine/regression.py; run that first). Writes
validation/results/cases_appendix.md.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

import cases as C  # noqa: E402

SETTINGS = {
    # Include expected-fail cases (behavior the rules do not have yet).
    "include_expected_fail": True,
    # Include the rationale text per case.
    "include_rationale": True,
}

RULE_TITLES = {
    "1a": "Rule 1a: invalidation by the alarm's own sensing pathway",
    "1b": "Rule 1b: invalidation of an asystole by an arterial line",
    "2a": "Rule 2a: redundancy by the same physiological property",
    "2b": "Rule 2b: redundancy by the same metric from another sensor",
    "3a": "Rule 3a: cardiorespiratory arrest",
    "3b": "Rule 3b: ventilation failure",
    "events": "Clinical events from single alarms",
    "engine": "Engine behavior",
    "scenario": "Illustrative scenario (Fig. 3 and 4)",
}
ORDER = list(RULE_TITLES)

# The reasoning competency questions (manuscript Table 1) are answered by the
# rules, not by standalone queries; each rule file's header names its CQ.
RULE_CQ = {
    "1a": "CQ8", "1b": "CQ8",
    "2a": "CQ9", "2b": "CQ9",
    "3a": "CQ10", "3b": "CQ10",
    "events": "CQ10 (constituent events)",
}
CQ_TEXT = {
    "CQ8": "Which clinical alarms coincide with alarms carrying a technical condition "
           "that reduces the validity of that sensing pathway?",
    "CQ9": "Which alarms coincide with other alarms that likely already convey "
           "equivalent clinical or technical information?",
    "CQ10": "Can coinciding alarms be identified that together form a different "
            "clinical meaning than their independent meaning, and for which no "
            "individual alarm exists?",
}


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def main() -> None:
    results_csv = C.RESULTS_DIR / "cases_results.csv"
    if not results_csv.exists():
        sys.exit(f"{results_csv} not found: run engine/regression.py first")
    with results_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    by_case = defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)
    meta = json.loads((C.RESULTS_DIR / "cases_run.json").read_text(encoding="utf-8"))

    cases = C.load_cases()
    if not SETTINGS["include_expected_fail"]:
        cases = [c for c in cases if c.status != "expected-fail"]
    groups = defaultdict(list)
    for c in cases:
        groups[c.rule].append(c)

    out = ["# Verification cases", ""]
    out.append(f"Run {meta['run']} on commit {meta['commit']}"
               + (" (with uncommitted changes)" if meta["uncommitted_changes"] else "")
               + f"; framework {meta['framework_hash']}, rules {meta['rules_hash']}, cases {meta['cases_hash']}.")
    out.append(f"{meta['passed']}/{meta['assertions']} assertions passed in {meta['active_cases']} active cases, "
               "including the engine-wide invariants.")
    out += ["", "The reasoning competency questions (Table 1) are answered by the rules:", ""]
    for cq, text in CQ_TEXT.items():
        rules = ", ".join(r for r, q in RULE_CQ.items() if q == cq)
        out.append(f"- **{cq}** ({text}): rules {rules}")
    out += ["", "| Rule | CQ | Cases | Assertions | Passed | Review pending |", "|---|---|---|---|---|---|"]
    for rule in ORDER:
        cs = [c for c in groups.get(rule, []) if c.status == "active"]
        if not cs:
            continue
        rs = [r for c in cs for r in by_case.get(c.id, [])]
        out.append(f"| {rule} | {RULE_CQ.get(rule, '')} | {len(cs)} | {len(rs)} | {sum(r['result'] == 'PASS' for r in rs)} | "
                   f"{sum(c.review == 'pending' for c in cs)} |")
    engine_rows = by_case.get("(engine)", [])
    out += ["", "Engine-wide invariants, checked over all cases:", ""]
    for r in engine_rows:
        out.append(f"- **{r['assertion']}**: {r['result']}")
    out.append("")

    for rule in ORDER:
        if rule not in groups:
            continue
        out += [f"## {RULE_TITLES[rule]}", ""]
        if rule in RULE_CQ:
            out += [f"*Competency question:* {RULE_CQ[rule]}.", ""]
        for c in groups[rule]:
            status = c.status + ("" if c.review == "done" else ", review pending")
            out += [f"### {c.id}: {c.title}", "", f"*Purpose:* {c.purpose}. *Status:* {status}.", ""]
            if SETTINGS["include_rationale"]:
                for key in ("clinical", "technical"):
                    text = (c.rationale.get(key) or "").strip()
                    if text:
                        out += [f"*{key.capitalize()} rationale:* {text}", ""]
            out += ["| Alarm | Device | Label | Start | End |", "|---|---|---|---|---|"]
            for a in c.stream:
                out.append(f"| {a['id']} | {_cell(a['device'])} | {_cell(a['label'])} | "
                           f"{c.show(a['start'])} | {c.show(a['end'])} |")
            out += ["", "| # | Expected | Result | Observed |", "|---|---|---|---|"]
            if c.status == "draft":
                out.append("| | not run (draft) | | |")
            for r in by_case.get(c.id, []):
                out.append(f"| {r['n']} | {_cell(r['assertion'])} | {r['result']} | {_cell(r['observed'])} |")
            out.append("")

    path = C.RESULTS_DIR / "cases_appendix.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"written {path}")


if __name__ == "__main__":
    main()
