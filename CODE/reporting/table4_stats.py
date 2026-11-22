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

Memory: computed one patient at a time (chunks of CHUNK_SIZE patients, split
from the dataset in one streaming pass); the full run fits in a few GB.

Usage:  python3 CODE/reporting/table4_stats.py
"""

import csv
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
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


CHUNK_SIZE = 100            # patients per pass; memory holds one chunk
WORK_DIR = ROOT / "CODE" / "reporting" / "_scratch_table4"


def alarm_id(e) -> str:
    return str(M.alarm_iri(e)).rsplit("/", 1)[-1]


def read_chunk(path: Path) -> dict:
    """{patient: [(Event, department), ...]} in input order (the stream's
    order at one instant)."""
    out = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        col = {name: i for i, name in enumerate(next(reader))}
        get = lambda row, name: row[col[name]] if name in col else ""  # noqa: E731
        for row in reader:
            if not row:
                continue
            e = S.Event(get(row, "patientID"), get(row, "label"), get(row, "device_id"),
                        datetime.fromisoformat(get(row, "start")), datetime.fromisoformat(get(row, "end")),
                        get(row, "alarm_id"), get(row, "device_name"), get(row, "priority"))
            out[e.patient].append((e, get(row, "department")))
    return out


def main():
    """Every Table 4 number, computed one patient at a time: the dataset is
    split into chunks of CHUNK_SIZE patients in one streaming pass, and each
    patient's counts are added to its unit's totals. Memory holds one chunk
    and the firing log (indexed by alarm), never the whole run."""
    run = json.loads(MANIFEST.read_text())
    if not run.get("complete", False):
        print("WARNING: the run manifest says the run was incomplete; numbers are partial.")
    for key in ("firing_log", "event_log", "arrival_times"):
        p = Path(run[key])
        if not p.exists() or p.stat().st_size == 0:
            raise SystemExit(f"{p.name} is empty or missing: the run has not written its logs yet "
                             f"(complete: {run.get('complete')}). Wait for poc_main.py to finish.")
    kb = M.load_kb()
    window = parse_window(run["post_alarm_validity"])
    dataset = Path(run["dataset"])
    patients = list(run["patients"])
    if "department" not in pd.read_csv(dataset, sep=";", dtype=str, nrows=0).columns:
        raise SystemExit(f"{dataset.name} has no 'department' column: re-export with export_rdata.R")

    # firings: subject alarm -> rules; lifts and withdrawals with their time
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

    # per unit: counts, per-patient rates, the patients with each kind of hit
    tot = defaultdict(Counter)            # unit -> key -> n
    cases = defaultdict(lambda: defaultdict(set))   # unit -> key -> {patient}
    rates = defaultdict(lambda: ([], []))           # unit -> (without, with)
    unit_of = {}

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    chunks = [patients[i:i + CHUNK_SIZE] for i in range(0, len(patients), CHUNK_SIZE)]
    paths = S.partition_by_patient(dataset, chunks, WORK_DIR)
    for k, path in enumerate(paths, start=1):
        for pid, rows in read_chunk(path).items():
            unit = Counter(d for _, d in rows).most_common(1)[0][0]
            unit_of[pid] = unit
            evs = [e for e, _ in rows]
            rep = [e for e in evs if M.type_of(kb, e) is not None]
            t = tot[unit]
            t["corpus"] += len(evs)
            t["replayed"] += len(rep)
            if not rep:
                continue

            # non-empty state, in arrival order
            reach = None   # latest (end + window) among earlier arrivals
            for e in sorted(rep, key=lambda x: x.start):   # stable: input order at one instant
                if reach is not None and reach > e.start:
                    t["nonempty"] += 1
                r = e.end + window
                reach = r if reach is None or r > reach else reach

            def lifted(a, end, rule) -> bool:
                if rule == "1b":
                    return a in withdrawn_at and withdrawn_at[a] < end
                if rule in ("2a", "2b"):
                    return a in lifted_at and lifted_at[a] < end
                return False

            silenced_any, n_removed = set(), 0
            for e in rep:
                a = alarm_id(e)
                rs = rules_of.get(a, ())
                for rule in rs:
                    t[f"{rule}_n"] += 1
                    cases[unit][rule].add(pid)
                    if rule != "1a" and lifted(a, e.end, rule):
                        t[f"{rule}_lifted"] += 1
                if rs:
                    silenced_any.add(a)
                    if any(not lifted(a, e.end, rule) for rule in rs):
                        n_removed += 1
                filtered = (norm(e.priority) in FILTER_PRIORITIES
                            and (e.end - e.start).total_seconds() < FILTER_SECONDS)
                if rs and filtered:
                    t["filter_also"] += 1
                elif rs:
                    t["filter_passed"] += 1
                    if norm(e.priority) == "hoog":
                        t["filter_passed_high"] += 1
                elif filtered:
                    t["filter_only"] += 1
            t["distinct"] += len(silenced_any)
            if silenced_any:
                cases[unit]["distinct"].add(pid)

            # alarm rate per alarm-active clock hour
            hours = set()
            for e in rep:
                h = e.start.replace(minute=0, second=0, microsecond=0)
                while h <= e.end:
                    hours.add(h)
                    h += timedelta(hours=1)
            rates[unit][0].append(len(rep) / len(hours))
            rates[unit][1].append((len(rep) - n_removed) / len(hours))
        print(f"  chunk {k}/{len(paths)} ({len(unit_of)} patient(s))", flush=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    # clinical events and processing times, streamed and counted per unit
    ev_n, ev_cases = defaultdict(Counter), defaultdict(lambda: defaultdict(set))
    with Path(run["event_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["kind"] in EVENT_RULES and r["patient"] in unit_of:
                u = unit_of[r["patient"]]
                ev_n[u][r["kind"]] += 1
                ev_cases[u][r["kind"]].add(r["patient"])
    hist = defaultdict(Counter)
    with Path(run["arrival_times"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["patient"] in unit_of:
                hist[unit_of[r["patient"]]][float(r["ms"])] += int(r["alarms"])

    units = sorted(set(unit_of.values()))
    for src in (tot, ev_n, hist):          # ALL = the sum over the units
        src["ALL"] = sum((src[u] for u in units), Counter())
    for src in (cases, ev_cases):
        for key in {k for u in units for k in src[u]}:
            src["ALL"][key] = set().union(*(src[u][key] for u in units))
    rates["ALL"] = ([v for u in units for v in rates[u][0]], [v for u in units for v in rates[u][1]])

    rows = []
    for unit in units + ["ALL"]:
        t, c = tot[unit], cases[unit]
        n_corpus, n_rep, n_ne = t["corpus"], t["replayed"], t["nonempty"]
        pm, pq1, pq3 = weighted_quartiles(hist[unit])
        rows += [
            (unit, "corpus_n", n_corpus, "Alarms of the replayed patients, before exclusion"),
            (unit, "excluded_n", n_corpus - n_rep, "Alarms without a blueprint (outside annotated strata or not annotatable)"),
            (unit, "excluded_pct", pct(n_corpus - n_rep, n_corpus), "Without a blueprint, % of corpus (0-100)"),
            (unit, "replayed_n", n_rep, "Alarms with a blueprint, replayed"),
            (unit, "nonempty_n", n_ne, "Alarms arriving in a non-empty patient state"),
            (unit, "nonempty_pct", pct(n_ne, n_rep), "Non-empty, % of replayed alarms (0-100)"),
            (unit, "proc_median", pm, "Processing time per alarm, ms, median"),
            (unit, "proc_q1", pq1, "Processing time per alarm, ms, Q1"),
            (unit, "proc_q3", pq3, "Processing time per alarm, ms, Q3"),
        ]
        for rule in ("1a", "1b", "2a", "2b"):
            n = t[f"{rule}_n"]
            rows += [
                (unit, f"{rule}_n", n, f"Rule {rule}: alarms silenced"),
                (unit, f"{rule}_pct", pct(n, n_ne), f"Rule {rule}: % of non-empty alarms (0-100)"),
                (unit, f"{rule}_cases", len(c[rule]), f"Rule {rule}: cases affected"),
            ]
            if rule != "1a":
                rows.append((unit, f"{rule}_lifted", t[f"{rule}_lifted"],
                             f"Rule {rule}: silences lifted before the alarm ended"))
        d = t["distinct"]
        rows += [
            (unit, "distinct_n", d, "Invalidation + redundancy: distinct alarms silenced"),
            (unit, "distinct_pct", pct(d, n_ne), "Distinct alarms, % of non-empty alarms (0-100)"),
            (unit, "distinct_cases", len(c["distinct"]), "Distinct alarms: cases affected"),
        ]
        for name, vals, desc in (("rate_without", rates[unit][0], "without silences"),
                                 ("rate_with", rates[unit][1], "with silences")):
            m, q1, q3 = quartiles(vals)
            rows += [(unit, f"{name}_median", m, f"Alarms per alarm-active clock hour {desc}, median"),
                     (unit, f"{name}_q1", q1, f"Alarm rate {desc}, Q1"),
                     (unit, f"{name}_q3", q3, f"Alarm rate {desc}, Q3")]
        also, passed, high = t["filter_also"], t["filter_passed"], t["filter_passed_high"]
        rows += [
            (unit, "filter_also_n", also, "Silenced alarms also suppressed by the 5-s delay filter"),
            (unit, "filter_also_pct", pct(also, d), "Also suppressed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_n", passed, "Silenced alarms the filter would pass"),
            (unit, "filter_passed_pct", pct(passed, d), "Passed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_high_n", high, "Passed by the filter, of which high priority"),
            (unit, "filter_passed_high_pct", pct(high, passed), "High priority, % of passed (0-100)"),
            (unit, "filter_only_n", t["filter_only"], "Alarms suppressed by the filter but by no rule"),
        ]
        for kind, rule in EVENT_RULES.items():
            rows += [(unit, f"{rule}_n", ev_n[unit][kind], f"Rule {rule}: clinical events identified"),
                     (unit, f"{rule}_cases", len(ev_cases[unit][kind]), f"Rule {rule}: cases affected")]

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
