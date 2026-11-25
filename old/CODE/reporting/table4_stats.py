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
Unit             each alarm counts in its own department (the events file's
                 `department` column), as in Table 3 and Total_corpus.csv. A
                 patient with alarms in both units is a case in both (ALL
                 counts it once); a clinical event goes to the unit of its
                 first supporting alarm.
Non-empty state  at arrival, another replayed alarm of the patient arrived
                 earlier and ended less than the post-alarm window ago (or is
                 still active): exactly what the engine's patient state holds.
                 The state is per patient, across units; the alarm counts in
                 its own unit.
Silenced by rule the alarm is the subject of a firing of that rule (1a, 1b,
                 2a, 2b) at any time; lifted = its silence lifted (2a, 2b) or
                 its flag withdrawn (1b) before the alarm ended.
Removed          silenced, and still silenced when it ended: a 1a flag, a 1b
                 flag not withdrawn, or a 2a/2b silence not lifted before its end.
Alarm rate       per patient: replayed alarms / clock hours with at least one
                 active replayed alarm (denominator from the complete stream,
                 before silencing); "with silences" leaves out removed alarms.
                 Per patient-unit stay (a patient's alarms in one unit, hours
                 from those alarms); median [IQR] over the unit's stays (ALL:
                 per patient, all its alarms).
Filter           a 5-second delay: low or medium priority alarms shorter than
                 5 s are suppressed; high and unknown priority never.
Processing time  per arriving alarm (engine/execution.py, wall clock),
                 median [IQR]. The timing log is per patient, not per alarm:
                 a patient's timings go to the unit of most of its alarms.

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


def run_device_models(run: dict, current: dict) -> dict:
    """The gateway -> device model mapping the run used: device_models.csv at
    the run's commit (run.json), so blueprints are matched as the engine
    matched them, even if the mapping changed after the run."""
    import subprocess
    commit = (run.get("git") or {}).get("commit")
    rel = M.DEVICE_MODELS.resolve().relative_to(ROOT).as_posix()
    if not commit:
        print("WARNING: run.json has no commit; using the current device_models.csv.")
        return current
    text = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{rel}"],
                          capture_output=True, text=True, check=True).stdout
    used = {r["gateway_name"]: r["model"] for r in csv.DictReader(text.splitlines(), delimiter=";")}
    if used != current:
        changed = sorted(k for k in used.keys() | current.keys() if used.get(k) != current.get(k))
        print(f"NOTE: device_models.csv changed since the run ({commit[:7]}): {changed}; "
              f"using the run's mapping.")
    return used


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
    kb.model_of = run_device_models(run, kb.model_of)
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

    # clinical events: the supporting alarm whose department places the event
    event_alarm = {}                      # first supporting alarm id -> [kind, ...]
    with Path(run["event_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["kind"] in EVENT_RULES and r["alarm_ids"]:
                event_alarm.setdefault(r["alarm_ids"].split()[0], []).append((r["patient"], r["kind"]))
    ev_n, ev_cases = defaultdict(Counter), defaultdict(lambda: defaultdict(set))

    # per unit: counts, per-stay rates, the patients with each kind of hit
    tot = defaultdict(Counter)            # unit -> key -> n
    cases = defaultdict(lambda: defaultdict(set))   # unit -> key -> {patient}
    rates = defaultdict(lambda: ([], []))           # unit -> (without, with)
    unit_of = {}                          # patient -> unit of most alarms (timing log only)

    def rate(alarms, n_removed):
        hours = set()
        for e in alarms:
            h = e.start.replace(minute=0, second=0, microsecond=0)
            while h <= e.end:
                hours.add(h)
                h += timedelta(hours=1)
        return len(alarms) / len(hours), (len(alarms) - n_removed) / len(hours)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    chunks = [patients[i:i + CHUNK_SIZE] for i in range(0, len(patients), CHUNK_SIZE)]
    paths = S.partition_by_patient(dataset, chunks, WORK_DIR)
    for k, path in enumerate(paths, start=1):
        for pid, rows in read_chunk(path).items():
            unit_of[pid] = Counter(d for _, d in rows).most_common(1)[0][0]
            for e, d in rows:
                tot[d]["corpus"] += 1
            rep = [(e, d) for e, d in rows if M.type_of(kb, e) is not None]
            for e, d in rep:
                tot[d]["replayed"] += 1
            if not rep:
                continue
            for e, d in rep:
                for patient, kind in event_alarm.get(alarm_id(e), ()):
                    ev_n[d][kind] += 1
                    ev_cases[d][kind].add(patient)

            # non-empty state, in arrival order (state per patient, across units)
            reach = None   # latest (end + window) among earlier arrivals
            for e, d in sorted(rep, key=lambda x: x[0].start):   # stable: input order at one instant
                if reach is not None and reach > e.start:
                    tot[d]["nonempty"] += 1
                r = e.end + window
                reach = r if reach is None or r > reach else reach

            def lifted(a, end, rule) -> bool:
                if rule == "1b":
                    return a in withdrawn_at and withdrawn_at[a] < end
                if rule in ("2a", "2b"):
                    return a in lifted_at and lifted_at[a] < end
                return False

            removed = Counter()               # unit -> alarms removed
            stays = defaultdict(list)          # unit -> this patient's alarms there
            for e, d in rep:
                t = tot[d]
                stays[d].append(e)
                a = alarm_id(e)
                rs = rules_of.get(a, ())
                for rule in rs:
                    t[f"{rule}_n"] += 1
                    cases[d][rule].add(pid)
                    if rule != "1a" and lifted(a, e.end, rule):
                        t[f"{rule}_lifted"] += 1
                if rs:
                    t["distinct"] += 1
                    cases[d]["distinct"].add(pid)
                    if any(not lifted(a, e.end, rule) for rule in rs):
                        removed[d] += 1
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

            # alarm rate per alarm-active clock hour: per stay, and per patient for ALL
            for d, alarms in stays.items():
                w, s = rate(alarms, removed[d])
                rates[d][0].append(w)
                rates[d][1].append(s)
            w, s = rate([e for e, _ in rep], sum(removed.values()))
            rates["ALL"][0].append(w)
            rates["ALL"][1].append(s)
        print(f"  chunk {k}/{len(paths)} ({len(unit_of)} patient(s))", flush=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    # processing times: per patient in the log, so by the patient's main unit
    hist = defaultdict(Counter)
    with Path(run["arrival_times"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["patient"] in unit_of:
                hist[unit_of[r["patient"]]][float(r["ms"])] += int(r["alarms"])

    units = sorted(tot)
    for src in (tot, ev_n, hist):          # ALL = the sum over the units
        src["ALL"] = sum((src[u] for u in units), Counter())
    for src in (cases, ev_cases):          # ALL = each patient once
        for key in {k for u in units for k in src[u]}:
            src["ALL"][key] = set().union(*(src[u][key] for u in units))
    # rates["ALL"] holds one value per patient, filled in the patient loop

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
