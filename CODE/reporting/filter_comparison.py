"""
filter_comparison.py

Manuscript V9, 4.4.3 "Replay of real-world alarm streams": the comparison of
the alarms silenced by invalidation and redundancy (rules 1a, 1b, 2a, 2b) with
those a coarse filter, as applied by current alarm management systems, would
suppress (a 5-second delay on low/medium priority alarms).

From one MDA-POC run (poc_main.py) it writes

    MANUSCRIPT/table_drafts/filter_comparison.csv         Unit;Key;Value;Description
    MANUSCRIPT/table_drafts/filter_comparison_rules.csv   per rule and per delay
    MANUSCRIPT/table_drafts/filter_comparison.txt         the V9 paragraph, filled in

and prints all three.

Base case = Table 4. The keys filter_also_*, filter_passed_*, filter_passed_high_*
and filter_only_n use exactly the definitions of table4_stats.py (same run, same
blueprint filter, same unit assignment); when table4_stats.csv exists the base
case is checked against it and any difference is reported.

Additions beyond Table 4
------------------------
Removed vs silenced  Table 4 counts every alarm that is the subject of a 1a/1b/
                     2a/2b firing ("silenced"), including silences lifted (2a/2b)
                     or flags withdrawn (1b) before the alarm ended. Those alarms
                     were delivered in the end. "removed" keeps only alarms still
                     silenced when they ended: the like-for-like comparison with a
                     filter, which removes an alarm for good.
Per rule             the comparison for each rule separately. An alarm silenced by
                     several rules is counted under each. cat1 (1a or 1b) and cat2
                     (2a or 2b) count each alarm once within the category.
Delay sensitivity    the same comparison for delays of DELAYS seconds, and for a
                     filter that also delays alarms of unknown priority.
Priority mix         the priority of the silenced alarms the filter passes
                     (high / medium / low / unknown).

Filter               an alarm is suppressed when its priority is in the filter's
                     set and it lasted less than the delay (end - start < delay s).
                     Default: {low, medium}, 5 s; high and unknown never.

Usage:  python3 CODE/reporting/filter_comparison.py [path/to/run.json]
        (default: MDA-POC/_scratch/run.json)
"""

import csv
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
POC = ROOT / "MDA-POC"
sys.path.insert(0, str(POC / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mint as M  # noqa: E402
import stream as S  # noqa: E402
from table4_stats import CHUNK_SIZE, SILENCE_RULES, alarm_id, norm, pct, read_chunk  # noqa: E402

MANIFEST = Path(sys.argv[1]) if len(sys.argv) > 1 else POC / "_scratch" / "run.json"
OUT_DIR = ROOT / "MANUSCRIPT" / "table_drafts"
OUT_MAIN = OUT_DIR / "filter_comparison.csv"
OUT_RULES = OUT_DIR / "filter_comparison_rules.csv"
OUT_TEXT = OUT_DIR / "filter_comparison.txt"
TABLE4 = OUT_DIR / "table4_stats.csv"

BASE_DELAY = 5
BASE_PRIORITIES = frozenset({"laag", "medium"})
DELAYS = (2, 3, 5, 10, 15, 30)
FILTER_VARIANTS = {
    "lowmed": BASE_PRIORITIES,                         # manuscript definition
    "lowmed_unknown": BASE_PRIORITIES | {"unknown"},   # unknown treated as not high
}
PRIORITY_NAMES = {"hoog": "high", "medium": "medium", "laag": "low", "unknown": "unknown"}
RULES = ("1a", "1b", "2a", "2b")
TABLE4_KEYS = ("filter_also_n", "filter_also_pct", "filter_passed_n", "filter_passed_pct",
               "filter_passed_high_n", "filter_passed_high_pct", "filter_only_n")


WORK_DIR = ROOT / "CODE" / "reporting" / "_scratch_filter"


def load():
    """One patient at a time, as table4_stats.py: the dataset is split into
    chunks of CHUNK_SIZE patients in one streaming pass. Kept per unit: a
    histogram {(priority, duration_s): n} of all replayed alarms (enough for
    every filter variant and delay) and one record per silenced alarm
    (priority, duration, rules, rules still in force at its end)."""
    run = json.loads(MANIFEST.read_text())
    if not run.get("complete", False):
        print("WARNING: the run manifest says the run is incomplete; numbers are partial.\n")
    for key in ("firing_log", "event_log", "arrival_times"):
        p = Path(run[key])
        if not p.exists() or p.stat().st_size == 0:
            raise SystemExit(f"{p.name} is empty or missing: the run has not written its logs yet "
                             f"(complete: {run.get('complete')}). Wait for poc_main.py to finish.")
    kb = M.load_kb()
    dataset = Path(run["dataset"])
    patients = list(run["patients"])
    if "department" not in pd.read_csv(dataset, sep=";", dtype=str, nrows=0).columns:
        raise SystemExit(f"{dataset.name} has no 'department' column: re-export with export_rdata.R")

    rules_of = defaultdict(set)
    lifted_at, withdrawn_at = {}, {}
    with Path(run["firing_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            subject = r["alarm_ids"].split()[0] if r["alarm_ids"] else None
            if subject is None:
                continue
            t = pd.Timestamp(r["time"]).to_pydatetime()
            if r["rule"] in SILENCE_RULES:
                rules_of[subject].add(SILENCE_RULES[r["rule"]])
            elif r["rule"] == "cat2_lifted":
                lifted_at[subject] = min(t, lifted_at.get(subject, t))
            elif r["rule"] == "cat1b_withdrawn":
                withdrawn_at[subject] = min(t, withdrawn_at.get(subject, t))

    def lifted(a, end, rule) -> bool:
        if rule == "1b":
            return a in withdrawn_at and withdrawn_at[a] < end
        if rule in ("2a", "2b"):
            return a in lifted_at and lifted_at[a] < end
        return False

    hist = defaultdict(Counter)    # unit -> (prio, dur) -> n
    recs = defaultdict(list)       # unit -> [(prio, dur, rules, removed_rules)]
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    chunks = [patients[i:i + CHUNK_SIZE] for i in range(0, len(patients), CHUNK_SIZE)]
    paths = S.partition_by_patient(dataset, chunks, WORK_DIR)
    n_pat = 0
    for k, path in enumerate(paths, start=1):
        for pid, rows in read_chunk(path).items():
            n_pat += 1
            unit = Counter(d for _, d in rows).most_common(1)[0][0]
            for e, _ in rows:
                if M.type_of(kb, e) is None:
                    continue
                prio, dur = norm(e.priority), (e.end - e.start).total_seconds()
                hist[unit][(prio, dur)] += 1
                a = alarm_id(e)
                rs = rules_of.get(a)
                if rs:
                    recs[unit].append((prio, dur, frozenset(rs),
                                       frozenset(r for r in rs if not lifted(a, e.end, r))))
        print(f"  chunk {k}/{len(paths)} ({n_pat} patient(s))", flush=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    units = sorted(hist)
    hist["ALL"] = sum((hist[u] for u in units), Counter())
    recs["ALL"] = [r for u in units for r in recs[u]]
    return run, hist, recs, units


def compare(hist: Counter, group: list, delay, prios) -> dict:
    """Filter vs a group of silenced-alarm records, within a unit (its histogram)."""
    filter_n = sum(n for (p, d), n in hist.items() if p in prios and d < delay)
    also = sum(1 for p, d, _, _ in group if p in prios and d < delay)
    mix = Counter(PRIORITY_NAMES.get(p, p or "unknown") for p, d, _, _ in group
                  if not (p in prios and d < delay))
    return {
        "silenced": len(group), "also": also, "passed": len(group) - also,
        "passed_high": mix["high"], "mix": mix, "filter_n": filter_n,
        "filter_only": filter_n - also, "universe": sum(hist.values()),
    }


def main():
    run, hist, recs, units = load()
    units = units + ["ALL"]
    main_rows, rule_rows, res = [], [], {}

    for unit in units:
        h, silenced = hist[unit], recs[unit]
        removed = [r for r in silenced if r[3]]
        base = compare(h, silenced, BASE_DELAY, BASE_PRIORITIES)
        rem = compare(h, removed, BASE_DELAY, BASE_PRIORITIES)
        res[unit] = (base, rem)
        d = BASE_DELAY
        main_rows += [
            # --- Table 4 rows (identical definitions to table4_stats.py)
            (unit, "filter_also_n", base["also"], f"Silenced alarms also suppressed by the {d}-s delay filter"),
            (unit, "filter_also_pct", pct(base["also"], base["silenced"]), "Also suppressed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_n", base["passed"], "Silenced alarms the filter would pass"),
            (unit, "filter_passed_pct", pct(base["passed"], base["silenced"]), "Passed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_high_n", base["passed_high"], "Passed by the filter, of which high priority"),
            (unit, "filter_passed_high_pct", pct(base["passed_high"], base["passed"]), "High priority, % of passed (0-100)"),
            (unit, "filter_only_n", base["filter_only"], "Alarms suppressed by the filter but by no rule"),
            # --- context
            (unit, "replayed_n", base["universe"], "Replayed alarms (with a blueprint)"),
            (unit, "silenced_n", base["silenced"], "Distinct alarms silenced by 1a/1b/2a/2b (Table 4 'distinct')"),
            (unit, "filter_n", base["filter_n"], f"Alarms the {d}-s filter suppresses"),
            (unit, "filter_pct", pct(base["filter_n"], base["universe"]), "Filter-suppressed, % of replayed (0-100)"),
            (unit, "silenced_pct_replayed", pct(base["silenced"], base["universe"]), "Silenced, % of replayed (0-100)"),
            (unit, "union_n", base["silenced"] + base["filter_only"], "Silenced by a rule or suppressed by the filter"),
            (unit, "filter_only_pct_filter", pct(base["filter_only"], base["filter_n"]), "Filter-only, % of filter-suppressed (0-100)"),
        ]
        for p in ("medium", "low", "unknown"):
            main_rows.append((unit, f"filter_passed_{p}_n", base["mix"][p], f"Passed by the filter, of which {p} priority"))
        # --- removed (still silenced at the alarm's end)
        main_rows += [
            (unit, "removed_n", rem["silenced"], "Alarms still silenced when they ended (lifted/withdrawn excluded)"),
            (unit, "removed_also_n", rem["also"], "Removed alarms also suppressed by the filter"),
            (unit, "removed_also_pct", pct(rem["also"], rem["silenced"]), "Also suppressed, % of removed (0-100)"),
            (unit, "removed_passed_n", rem["passed"], "Removed alarms the filter would pass"),
            (unit, "removed_passed_high_n", rem["passed_high"], "Removed and passed, of which high priority"),
            (unit, "removed_passed_high_pct", pct(rem["passed_high"], rem["passed"]), "High priority, % of removed and passed (0-100)"),
            (unit, "removed_filter_only_n", rem["filter_only"], "Suppressed by the filter, not removed by any rule"),
        ]

        # per rule x delay x filter variant
        for variant, prios in FILTER_VARIANTS.items():
            for delay in DELAYS:
                groups = [("any", silenced, "silenced"), ("any", removed, "removed")]
                groups += [(r, [x for x in silenced if r in x[2]], "silenced") for r in RULES]
                groups += [(cat, [x for x in silenced if x[2] & rs], "silenced")   # union: counted once
                           for cat, rs in (("cat1", {"1a", "1b"}), ("cat2", {"2a", "2b"}))]
                for rule, sset, basis in groups:
                    c = compare(h, sset, delay, prios)
                    rule_rows.append({
                        "Unit": unit, "Rule": rule, "Basis": basis, "Filter": variant, "Delay_s": delay,
                        "n": c["silenced"], "also_n": c["also"], "also_pct": pct(c["also"], c["silenced"]),
                        "passed_n": c["passed"], "passed_high_n": c["passed_high"],
                        "passed_high_pct": pct(c["passed_high"], c["passed"]),
                        "filter_n": c["filter_n"], "filter_only_n": c["filter_only"],
                    })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_MAIN.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Unit", "Key", "Value", "Description"])
        w.writerows(main_rows)
    pd.DataFrame(rule_rows).to_csv(OUT_RULES, sep=";", index=False)

    # consistency with Table 4 (only meaningful when both come from the same run)
    if TABLE4.exists():
        t4 = pd.read_csv(TABLE4, sep=";", dtype=str)
        t4 = {(u, k): v for u, k, v in zip(t4.Unit, t4.Key, t4.Value)}
        mine = {(u, k): str(v) for u, k, v, _ in main_rows if k in TABLE4_KEYS}
        diff = [(uk, mine[uk], t4.get(uk)) for uk in mine if t4.get(uk) is not None and t4[uk] != mine[uk]]
        if diff:
            print(f"NOTE: {len(diff)} base-case values differ from {TABLE4.name} "
                  "(different run, or table4_stats.py changed):")
            for (u, k), a, b in diff:
                print(f"  {u:5} {k:24} here {a:>10}  table4 {b:>10}")
            print()

    # V9 paragraph (PICU first, then NICU, as in the manuscript)
    order = [u for u in ("PICU", "NICU") if u in res] or [u for u in units if u != "ALL"]

    def both(f):
        return " and ".join(str(f(*res[u])) for u in order)

    also_pct = both(lambda b, r: f"{pct(b['also'], b['silenced'])}%")
    high_pct = both(lambda b, r: f"{pct(b['passed_high'], b['passed'])}%")
    only_n = both(lambda b, r: b["filter_only"])
    text = (
        f"A filter would also have suppressed {also_pct} of the silenced alarms. "
        f"Among the silenced alarms that a filter would have passed, {high_pct} were of high priority. "
        f"Conversely, a filter would have suppressed {only_n} alarms that no rule concerned."
    )
    supp = "\n".join(
        f"  {u}: silenced {b['silenced']}, of which removed {r['silenced']} "
        f"({pct(r['also'], r['silenced'])}% of removed also filtered; "
        f"{pct(r['passed_high'], r['passed'])}% of removed-and-passed high priority). "
        f"Filter suppresses {b['filter_n']} of {b['universe']} replayed alarms ({pct(b['filter_n'], b['universe'])}%)."
        for u in order for b, r in [res[u]]
    )
    OUT_TEXT.write_text(
        f"Run: {MANIFEST}  (complete: {run.get('complete')}, patients: {len(run['patients'])})\n"
        f"Filter: {BASE_DELAY}-s delay on {sorted(BASE_PRIORITIES)}; units: {' and '.join(order)}\n\n"
        f"{text}\n\nRemoved vs silenced (not in V9):\n{supp}\n",
        encoding="utf-8",
    )

    for r in main_rows:
        print(f"{r[0]:5} {r[1]:26} {r[2]!s:>10}  {r[3]}")
    print("\nPer rule, base filter (silenced basis):")
    df = pd.DataFrame(rule_rows)
    sel = df[(df.Filter == "lowmed") & (df.Delay_s == BASE_DELAY) & (df.Basis == "silenced")]
    print(sel.drop(columns=["Basis", "Filter", "Delay_s"]).to_string(index=False))
    print("\nDelay sensitivity (any rule, silenced basis, ALL units):")
    sel = df[(df.Unit == "ALL") & (df.Rule == "any") & (df.Basis == "silenced")]
    print(sel[["Filter", "Delay_s", "also_pct", "passed_high_pct", "filter_n", "filter_only_n"]].to_string(index=False))
    print("\n" + OUT_TEXT.read_text())
    for p in (OUT_MAIN, OUT_RULES, OUT_TEXT):
        print(f"Wrote {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
