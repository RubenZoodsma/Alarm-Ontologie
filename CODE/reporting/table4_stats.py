"""
table4_stats.py

Every number of Table 4 ("Expressibility and occurrence per rule", manuscript
4.3.3) from one MDA-POC run, as one long table for Power Query:

    MANUSCRIPT/table_drafts/table4_stats.csv      Unit;Key;Value;Description

Unit is NICU, PICU (from the events file's `department` column) or ALL. The
keys are those of the workbook's Data sheet (tblTable4), which Table 4 looks
up by Unit + Key.

Inputs: the run manifest MDA-POC/_scratch/run.json (written by poc_main.py:
dataset, patients, logs) and validation/results/rule_coverage_summary.csv
(label combinations).

Definitions
-----------
Replayed alarm   an alarm with a blueprint (device model + label + priority).
                 Alarms without one are ignored throughout, as in the POC;
                 they appear only in "without a blueprint".
Unit             a patient's unit is the department of most of its alarms;
                 every count of that patient goes to that unit.
Non-empty state  at arrival, another replayed alarm of the patient arrived
                 earlier and ended less than the post-alarm window ago (or is
                 still active): exactly what the engine's patient state holds.
Silenced by rule the alarm is the subject of a firing of that rule (1a, 1b,
                 2a, 2b) at any time; lifted = its silence lifted (2a, 2b) or
                 its flag withdrawn (1b) before the alarm ended.
Removed          silenced, and still silenced when it ended: a 1a flag, a 1b
                 flag not withdrawn, or a 2a/2b silence not lifted before its end.
Alarm rate       per patient: replayed alarms / clock hours with at least one
                 active replayed alarm (denominator from the complete stream,
                 before silencing); "with silences" leaves out removed alarms.
                 Median [IQR] over the unit's patients.
Filter           a 5-second delay: low or medium priority alarms shorter than
                 5 s are suppressed; high and unknown priority never.
Processing time  per arriving alarm (engine/execution.py, wall clock),
                 median [IQR] over the unit's alarms.

Usage:  python3 CODE/reporting/table4_stats.py
"""

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
POC = ROOT / "MDA-POC"
sys.path.insert(0, str(POC / "engine"))
import mint as M  # noqa: E402
import stream as S  # noqa: E402

MANIFEST = POC / "_scratch" / "run.json"
COVERAGE = POC / "validation" / "results" / "rule_coverage_summary.csv"
OUT_PATH = ROOT / "MANUSCRIPT" / "table_drafts" / "table4_stats.csv"

FILTER_SECONDS = 5
FILTER_PRIORITIES = {"laag", "medium"}
SILENCE_RULES = {"cat1a": "1a", "cat1b": "1b", "cat2a": "2a", "cat2b": "2b"}
EVENT_RULES = {"CardioRespiratoryArrest": "3a", "VentilationFailure": "3b"}


def norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def pct(n, d):
    return round(100 * n / d, 1) if d else ""


def quartiles(values: list):
    """Median, Q1, Q3 (linear interpolation); empty when no values."""
    if not values:
        return "", "", ""
    s = pd.Series(values, dtype=float)
    return round(s.median(), 2), round(s.quantile(0.25), 2), round(s.quantile(0.75), 2)


def weighted_quartiles(hist: Counter):
    """Median, Q1, Q3 of a histogram {value: count} (nearest rank)."""
    total = sum(hist.values())
    if not total:
        return "", "", ""
    out = []
    for q in (0.5, 0.25, 0.75):
        rank, seen = max(1, -(-q * total // 1)), 0
        for v in sorted(hist):
            seen += hist[v]
            if seen >= rank:
                out.append(v)
                break
    return tuple(out)


def parse_window(s: str) -> timedelta:
    h, m, sec = (float(x) for x in str(s).split(":"))
    return timedelta(hours=h, minutes=m, seconds=sec)


def main():
    run = json.loads(MANIFEST.read_text())
    if not run.get("complete", False):
        print("WARNING: the run manifest says the run was incomplete; numbers are partial.")
    kb = M.load_kb()
    window = parse_window(run["post_alarm_validity"])
    dataset = Path(run["dataset"])
    patients = set(run["patients"])

    events = S.load_events_for_patients(dataset, patients)
    dept_col = pd.read_csv(dataset, sep=";", dtype=str, nrows=0).columns
    if "department" not in dept_col:
        raise SystemExit(f"{dataset.name} has no 'department' column: re-export with export_rdata.R")
    raw = pd.read_csv(dataset, sep=";", dtype=str, usecols=["patientID", "department"])
    raw = raw[raw.patientID.isin(patients)]
    unit_of = raw.groupby("patientID").department.agg(lambda x: x.value_counts().index[0]).to_dict()

    replayed = [e for e in events if M.type_of(kb, e) is not None]
    by_id = {str(M.alarm_iri(e)).rsplit("/", 1)[-1]: e for e in replayed}

    # firings: subject alarm -> rules; lifts and withdrawals with their time
    rules_of = defaultdict(set)
    lifted_at, withdrawn_at = {}, {}
    with Path(run["firing_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            subject = r["alarm_ids"].split()[0] if r["alarm_ids"] else None
            if subject not in by_id:
                continue
            t = pd.Timestamp(r["time"]).to_pydatetime()
            if r["rule"] in SILENCE_RULES:
                rules_of[subject].add(SILENCE_RULES[r["rule"]])
            elif r["rule"] == "cat2_lifted":
                lifted_at[subject] = min(t, lifted_at.get(subject, t))
            elif r["rule"] == "cat1b_withdrawn":
                withdrawn_at[subject] = min(t, withdrawn_at.get(subject, t))

    def lifted(a, rule) -> bool:
        end = by_id[a].end
        if rule == "1b":
            return a in withdrawn_at and withdrawn_at[a] < end
        if rule in ("2a", "2b"):
            return a in lifted_at and lifted_at[a] < end
        return False

    def removed(a) -> bool:
        return any(not lifted(a, r) for r in rules_of[a])

    events_log = pd.read_csv(run["event_log"], sep=";", dtype=str)
    times = pd.read_csv(run["arrival_times"], sep=";", dtype={"patient": str})

    # non-empty state per replayed alarm, per patient in arrival order
    nonempty = set()
    per_patient = defaultdict(list)
    for e in replayed:
        per_patient[e.patient].append(e)
    for pid, evs in per_patient.items():
        reach = None   # latest (end + window) among earlier arrivals
        for e in sorted(evs, key=lambda x: x.start):   # stable: input order at one instant
            if reach is not None and reach > e.start:
                nonempty.add(str(M.alarm_iri(e)).rsplit("/", 1)[-1])
            r = e.end + window
            reach = r if reach is None or r > reach else reach

    units = sorted(set(unit_of.values())) + ["ALL"]
    rows = []
    for unit in units:
        in_unit = (lambda p: True) if unit == "ALL" else (lambda p, u=unit: unit_of.get(p) == u)
        all_u = [e for e in events if in_unit(e.patient)]
        rep_u = [e for e in replayed if in_unit(e.patient)]
        ids_u = {str(M.alarm_iri(e)).rsplit("/", 1)[-1] for e in rep_u}
        ne_u = ids_u & nonempty
        n_corpus, n_rep = len(all_u), len(rep_u)
        hist = Counter()
        for p, ms, n in zip(times.patient, times.ms, times.alarms):
            if in_unit(p):
                hist[float(ms)] += int(n)
        pm, pq1, pq3 = weighted_quartiles(hist)
        rows += [
            (unit, "corpus_n", n_corpus, "Alarms of the replayed patients, before exclusion"),
            (unit, "excluded_n", n_corpus - n_rep, "Alarms without a blueprint (outside annotated strata or not annotatable)"),
            (unit, "excluded_pct", pct(n_corpus - n_rep, n_corpus), "Without a blueprint, % of corpus (0-100)"),
            (unit, "replayed_n", n_rep, "Alarms with a blueprint, replayed"),
            (unit, "nonempty_n", len(ne_u), "Alarms arriving in a non-empty patient state"),
            (unit, "nonempty_pct", pct(len(ne_u), n_rep), "Non-empty, % of replayed alarms (0-100)"),
            (unit, "proc_median", pm, "Processing time per alarm, ms, median"),
            (unit, "proc_q1", pq1, "Processing time per alarm, ms, Q1"),
            (unit, "proc_q3", pq3, "Processing time per alarm, ms, Q3"),
        ]
        silenced_any = set()
        for rule in ("1a", "1b", "2a", "2b"):
            subj = {a for a in ids_u if rule in rules_of[a]}
            silenced_any |= subj
            rows += [
                (unit, f"{rule}_n", len(subj), f"Rule {rule}: alarms silenced"),
                (unit, f"{rule}_pct", pct(len(subj), len(ne_u)), f"Rule {rule}: % of non-empty alarms (0-100)"),
                (unit, f"{rule}_cases", len({by_id[a].patient for a in subj}), f"Rule {rule}: cases affected"),
            ]
            if rule != "1a":
                rows.append((unit, f"{rule}_lifted", sum(lifted(a, rule) for a in subj),
                             f"Rule {rule}: silences lifted before the alarm ended"))
        rows += [
            (unit, "distinct_n", len(silenced_any), "Invalidation + redundancy: distinct alarms silenced"),
            (unit, "distinct_pct", pct(len(silenced_any), len(ne_u)), "Distinct alarms, % of non-empty alarms (0-100)"),
            (unit, "distinct_cases", len({by_id[a].patient for a in silenced_any}), "Distinct alarms: cases affected"),
        ]

        # alarm rate per alarm-active clock hour, per patient
        rate_without, rate_with = [], []
        for pid, evs in per_patient.items():
            if not in_unit(pid):
                continue
            hours = set()
            for e in evs:
                h = e.start.replace(minute=0, second=0, microsecond=0)
                while h <= e.end:
                    hours.add(h)
                    h += timedelta(hours=1)
            n_removed = sum(removed(str(M.alarm_iri(e)).rsplit("/", 1)[-1]) for e in evs)
            rate_without.append(len(evs) / len(hours))
            rate_with.append((len(evs) - n_removed) / len(hours))
        for name, vals, desc in (("rate_without", rate_without, "without silences"),
                                 ("rate_with", rate_with, "with silences")):
            m, q1, q3 = quartiles(vals)
            rows += [(unit, f"{name}_median", m, f"Alarms per alarm-active clock hour {desc}, median"),
                     (unit, f"{name}_q1", q1, f"Alarm rate {desc}, Q1"),
                     (unit, f"{name}_q3", q3, f"Alarm rate {desc}, Q3")]

        # 5-second delay filter
        filtered = {str(M.alarm_iri(e)).rsplit("/", 1)[-1] for e in rep_u
                    if norm(e.priority) in FILTER_PRIORITIES
                    and (e.end - e.start).total_seconds() < FILTER_SECONDS}
        also, passed = silenced_any & filtered, silenced_any - filtered
        high = {a for a in passed if norm(by_id[a].priority) == "hoog"}
        rows += [
            (unit, "filter_also_n", len(also), "Silenced alarms also suppressed by the 5-s delay filter"),
            (unit, "filter_also_pct", pct(len(also), len(silenced_any)), "Also suppressed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_n", len(passed), "Silenced alarms the filter would pass"),
            (unit, "filter_passed_pct", pct(len(passed), len(silenced_any)), "Passed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_high_n", len(high), "Passed by the filter, of which high priority"),
            (unit, "filter_passed_high_pct", pct(len(high), len(passed)), "High priority, % of passed (0-100)"),
            (unit, "filter_only_n", len(filtered - silenced_any), "Alarms suppressed by the filter but by no rule"),
        ]

        # clinical events
        ev_u = events_log[events_log.patient.map(in_unit)]
        for kind, rule in EVENT_RULES.items():
            k = ev_u[ev_u.kind == kind]
            rows += [(unit, f"{rule}_n", len(k), f"Rule {rule}: clinical events identified"),
                     (unit, f"{rule}_cases", k.patient.nunique(), f"Rule {rule}: cases affected")]

    # label combinations (unit-independent)
    with COVERAGE.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            rows.append(("ALL", f"combos_{r['rule']}", int(r["pairs"]),
                         f"Rule {r['rule']}: label combinations a list would need (Section 4.3.2)"))
    rows.append(("ALL", "run_patients_n", len(patients), "Patients in the run"))
    rows.append(("ALL", "run_complete", int(bool(run.get("complete"))), "1 if the engine run completed without errors"))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Unit", "Key", "Value", "Description"])
        w.writerows(rows)
    for r in rows:
        print(f"{r[0]:5} {r[1]:24} {r[2]!s:>10}  {r[3]}")
    print(f"\nWrote {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
