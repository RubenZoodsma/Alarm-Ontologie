"""
synthetic_sweep.py — end-to-end test of the full pipeline (poc_main ->
logs -> Table 4 of manuscript_tables.py) on synthetic patients whose outcome is known.

Every verification case (validation/cases/*.yaml, each confirmed by
engine/regression.py) becomes a SEGMENT of a long patient stream, plus a few
segments for what only Table 4 needs (the 5-s filter, alarms without a
blueprint, stream edge cases). Three patients, each a mixed device fleet:

  SWEEP_PICU   department PICU: the cases on PICU/adult devices + extras
  SWEEP_NICU   department NICU: the cases on NICU devices (Medibus,
               LeoniPlus) and the single-monitor CAT1a/CAT3a cases + extras
  SWEEP_ALL    department PICU: every segment, one after the other

Segments are separated by GAP (> the 15-min post-alarm validity), and device
ids repeat across segments (Monitor_00, ServoU_00, ...): a segment can only
reproduce its isolated result if nothing outlives its validity.

Checks (non-zero exit on any failure):
  1. isolated run (each segment its own patient) == the regression's own
     logs of the cases: poc_main reproduces the verified case outcomes;
  2. stitched runs — serial (one chunk per patient), 8 workers, and a run
     killed after its first chunk then resumed — each give, per segment,
     exactly the isolated result shifted in time, and no row mixes alarms
     of two segments (no leakage);
  3. Table 4 (manuscript_tables.py) on the stitched run: the extras land where they should
     (filter, no blueprint, tie), and its counts agree with the logs.

  cd MDA-POC/engine && python3 regression.py      # writes the case events
  cd ..  && python3 validation/synthetic_sweep.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))
import mint as M  # noqa: E402
import stream as S  # noqa: E402

CASES = ROOT / "engine" / "_scratch"            # regression.py output
OUT = ROOT / "_scratch_sweep"
GAP = timedelta(hours=1)
BASE = datetime(2025, 1, 6, 0, 0, 0)
CASE_DAY = datetime(2026, 1, 1)                  # the cases' own date

NICU_DEVICES = {"Medibus", "LeoniPlus"}
NICU_RULES = {"1a", "3a"}

# Segments only Table 4 needs: (label, device_id, device_name, priority,
# start s, end s), times from the segment's start.
EXTRAS = {
    "x_filter": [
        ("PHILIPSMONITOR - Lage hartfreq.", "Monitor_00", "Philipsmonitor", "Medium", 0, 3),        # filtered
        ("PHILIPSMONITOR - Extr. lage hartfreq.", "Monitor_00", "Philipsmonitor", "Hoog", 120, 122),  # high: never
        ("PHILIPSMONITOR - SpO2 lage PFI", "Monitor_00", "Philipsmonitor", "Laag", 1200, 1204),     # filtered
        ("PHILIPSMONITOR - SpO2 lage PFI", "Monitor_00", "Philipsmonitor", "Laag", 2400, 2460),     # 60 s: passes
    ],
    "x_nobp": [
        ("PHILIPSMONITOR - Control. pat-ID", "Monitor_00", "Philipsmonitor", "Medium", 0, 30),     # no blueprint
        ("UNKNOWNGW - Some alarm", "Other_00", "UnknownGateway", "Hoog", 60, 90),                  # unknown gateway
    ],
    "x_edge": [
        ("PHILIPSMONITOR - SpO2 laag", "Monitor_00", "Philipsmonitor", "Medium", 0, 0),            # zero length
        ("PHILIPSMONITOR - SpO2 laag", "Monitor_00", "Philipsmonitor", "Medium", 1200, 1190),      # ends before start
        ("PHILIPSMONITOR - Desaturatie", "Monitor_00", "Philipsmonitor", "Hoog", 2400, 2460),      # tie: two at
        ("PHILIPSMONITOR - Desaturatie", "Monitor_00", "Philipsmonitor", "Hoog", 2400, 2460),      # one instant
    ],
    "x_also": [
        ("PHILIPSMONITOR - Desaturatie", "Monitor_00", "Philipsmonitor", "Hoog", 0, 60),
        ("PHILIPSMONITOR - SpO2 laag", "Monitor_00", "Philipsmonitor", "Medium", 10, 13),          # 2a + filter
    ],
}
HEADER = ["patientID", "label", "device_id", "start", "end", "alarm_id", "device_name", "priority", "department"]


# ── building the datasets ────────────────────────────────────────────────

def load_segments() -> dict:
    """{segment: [row dict, ...]} — the cases (from the regression's
    cases_events.csv) and EXTRAS, times as datetimes."""
    segs: dict = defaultdict(list)
    with (CASES / "cases_events.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            segs[r["patientID"]].append(dict(r, start=datetime.fromisoformat(r["start"]),
                                             end=datetime.fromisoformat(r["end"])))
    t0 = CASE_DAY.replace(hour=8)
    for seg, rows in EXTRAS.items():
        for i, (label, dev, name, prio, s, e) in enumerate(rows, start=1):
            segs[seg].append({"patientID": seg, "label": label, "device_id": dev, "device_name": name,
                              "priority": prio, "alarm_id": f"e{i}",
                              "start": t0 + timedelta(seconds=s), "end": t0 + timedelta(seconds=e)})
    return dict(segs)


def assign(segs: dict) -> dict:
    """{patient: (department, [segment, ...])}."""
    def rule_of(seg):
        import yaml
        p = ROOT / "validation" / "cases" / f"{seg}.yaml"
        return str(yaml.safe_load(p.read_text())["rule"]) if p.exists() else "extra"
    cases = [s for s in segs if s not in EXTRAS]
    nicu = [s for s in cases if {r["device_name"] for r in segs[s]} & NICU_DEVICES or rule_of(s) in NICU_RULES]
    picu = [s for s in cases if s not in nicu]
    return {"SWEEP_PICU": ("PICU", picu + list(EXTRAS)),
            "SWEEP_NICU": ("NICU", nicu + list(EXTRAS)),
            "SWEEP_ALL": ("PICU", cases + list(EXTRAS))}


def write(path: Path, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r["patientID"], r["label"], r["device_id"], r["start"].isoformat(),
                        r["end"].isoformat(), r["alarm_id"], r["device_name"], r["priority"], r["department"]])


def build(segs: dict, patients: dict) -> tuple:
    """Write isolated.csv and stitched.csv; return (offsets, iri_map):
    offsets[patient][segment] = timedelta added to the segment's times;
    iri_map[stitched alarm local name] = (segment, isolated local name)."""
    isolated, stitched = [], []
    dept_of_seg = {}
    for patient, (dept, seg_list) in patients.items():
        for seg in seg_list:
            dept_of_seg.setdefault(seg, dept)
    for seg, rows in segs.items():
        for r in rows:
            isolated.append(dict(r, patientID=seg, alarm_id=f"{seg}_{r['alarm_id']}", department=dept_of_seg[seg]))
    offsets: dict = defaultdict(dict)
    iri_map: dict = {}
    for patient, (dept, seg_list) in patients.items():
        cursor = BASE
        for seg in seg_list:
            rows = segs[seg]
            first = min(r["start"] for r in rows)
            off = cursor - first
            offsets[patient][seg] = off
            for r in rows:
                s = dict(r, patientID=patient, alarm_id=f"{seg}_{r['alarm_id']}", department=dept,
                         start=r["start"] + off, end=r["end"] + off)
                stitched.append(s)
                iso = dict(r, patientID=seg, alarm_id=f"{seg}_{r['alarm_id']}")
                iri_map[_local(s)] = (seg, _local(iso))
            last = max(max(r["start"], r["end"]) for r in rows) + off
            cursor = last + GAP
    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "isolated.csv", isolated)
    write(OUT / "stitched.csv", stitched)
    return offsets, iri_map


def _local(r: dict) -> str:
    ev = S.Event(r["patientID"], r["label"], r["device_id"], r["start"], r["end"], r["alarm_id"],
                 r["device_name"], r["priority"])
    return str(M.alarm_iri(ev)).rsplit("/", 1)[-1]


# ── running poc_main (each run in its own process) ───────────────────────

def child(dataset: str, workers: str, chunk: str, out: str, resume: str) -> None:
    import poc_main as PM
    out_dir = Path(out)
    PM.SETTINGS.update(dataset=Path(dataset), n_patients="all", workers=int(workers), chunk_size=int(chunk),
                       resume=resume == "resume", event_log=out_dir / "clinical_events.csv",
                       firing_log=out_dir / "rule_firings.csv", arrival_times=out_dir / "arrival_times.csv",
                       run_manifest=out_dir / "run.json")
    PM.run()


def run_poc(name: str, dataset: Path, workers: int, chunk: int, resume: bool = False,
            kill_after_chunks: int | None = None) -> Path:
    out = OUT / name
    if not resume and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, __file__, "child", str(dataset), str(workers), str(chunk), str(out),
           "resume" if resume else "-"]
    with (out / ("resume.txt" if resume else "run.txt")).open("w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        if kill_after_chunks is not None:
            while proc.poll() is None:
                time.sleep(0.05)
                try:
                    done = json.loads((out / "run.json").read_text())["chunks_done"]
                except (OSError, ValueError, KeyError):
                    continue
                if len(done) >= kill_after_chunks:
                    time.sleep(0.1)                       # into the next chunk
                    os.kill(proc.pid, signal.SIGKILL)
                    proc.wait()
                    print(f"  {name}: killed after chunk(s) {done}")
                    return out
        if proc.wait() != 0:
            raise SystemExit(f"{name}: poc_main failed, see {out}")
    if "INCOMPLETE" in (out / ("resume.txt" if resume else "run.txt")).read_text():
        raise SystemExit(f"{name}: incomplete run, see {out}")
    return out


# ── comparing logs ───────────────────────────────────────────────────────

def read_log(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _shift(iso: str, delta: timedelta) -> str:
    return (datetime.fromisoformat(iso) - delta).isoformat()


def normalise_stitched(run: Path, offsets: dict, iri_map: dict, problems: list) -> dict:
    """{(log, segment): Counter of rows} in isolated form: patient -> segment,
    times shifted back, alarm ids mapped. A row naming alarms of two
    segments is a leak."""
    out: dict = defaultdict(Counter)
    for log, time_keys in (("rule_firings", ("time",)), ("clinical_events", ("start", "end"))):
        for r in read_log(run / f"{log}.csv"):
            ids = r["alarm_ids"].split()
            mapped = [iri_map.get(i) for i in ids]
            if None in mapped:
                problems.append(f"{run.name}/{log}: unknown alarm id in {r}")
                continue
            segs = {s for s, _ in mapped}
            if len(segs) != 1:
                problems.append(f"{run.name}/{log}: LEAK, one row names segments {sorted(segs)}: {r}")
                continue
            seg = segs.pop()
            off = offsets[r["patient"]][seg]
            row = dict(r, patient=seg, alarm_ids=" ".join(local for _, local in mapped))
            for k in time_keys:
                row[k] = _shift(r[k], off)
            out[(log, seg)][tuple(row.values())] += 1
    return out


def normalise_isolated(run: Path) -> dict:
    out: dict = defaultdict(Counter)
    for log in ("rule_firings", "clinical_events"):
        for r in read_log(run / f"{log}.csv"):
            out[(log, r["patient"])][tuple(r.values())] += 1
    return out


def normalise_regression() -> dict:
    """The regression's case logs, with alarm ids in the isolated form
    (ALARM_ID a1 -> <case>_a1)."""
    out: dict = defaultdict(Counter)
    for log in ("rule_firings", "clinical_events"):
        for r in read_log(CASES / f"{log}.csv"):
            case = r["patient"]
            ids = []
            for local in r["alarm_ids"].split():
                head, aid = local.rsplit("_", 1)
                ids.append(f"{head}_{case}_{aid}")
            row = dict(r, alarm_ids=" ".join(ids))
            out[(log, case)][tuple(row.values())] += 1
    return out


def compare(name: str, got: dict, want: dict, segments: list, problems: list) -> None:
    bad = []
    for seg in segments:
        for log in ("rule_firings", "clinical_events"):
            if got.get((log, seg), Counter()) != want.get((log, seg), Counter()):
                bad.append(f"{seg}/{log}")
    n = sum(sum(c.values()) for (lg, s), c in got.items() if s in segments)
    print(f"  {name}: {len(segments)} segment(s), {n} log row(s): "
          + ("identical" if not bad else f"{len(bad)} differ: {bad[:8]}"))
    problems += [f"{name}: {b} differs" for b in bad]


# ── Table 4 ──────────────────────────────────────────────────────────────

def table4(run: Path, problems: list) -> dict:
    sys.path.insert(0, str(ROOT.parent / "CODE" / "reporting"))
    import manuscript_tables as T
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rows = T.table4_rows(manifest=run / "run.json")
    # as strings, the way they read back from the CSV
    return {(u, k): "" if v is None else str(v) for u, k, v, _ in rows}


def check_table4(stats: dict, run: Path, patients: dict, problems: list) -> None:
    def val(unit, key):
        v = stats.get((unit, key), "")
        return float(v) if v not in ("", None) else None

    events = S.load_events(OUT / "stitched.csv")
    kb = M.load_kb()
    units = {"PICU": {p for p, (d, _) in patients.items() if d == "PICU"},
             "NICU": {p for p, (d, _) in patients.items() if d == "NICU"}}
    firings = read_log(run / "rule_firings.csv")
    cevents = read_log(run / "clinical_events.csv")
    ok = []
    for unit, pats in list(units.items()) + [("ALL", set(patients))]:
        evs = [e for e in events if e.patient in pats]
        nobp = [e for e in evs if M.type_of(kb, e) is None]
        filt = [e for e in evs if M.type_of(kb, e) is not None and e.priority.lower() in ("laag", "medium")
                and (e.end - e.start).total_seconds() < 5]
        checks = {
            "corpus_n": len(evs),
            "excluded_n": len(nobp),
            "replayed_n": len(evs) - len(nobp),
            "3a_n": sum(1 for r in cevents if r["patient"] in pats and r["kind"] == "CardioRespiratoryArrest"),
            "3b_n": sum(1 for r in cevents if r["patient"] in pats and r["kind"] == "VentilationFailure"),
            "filter_also_n+filter_only_n": len(filt),
            "distinct_n": len({(f["patient"], f["alarm_ids"].split()[0]) for f in firings
                               if f["patient"] in pats and f["rule"] in ("cat1a", "cat1b", "cat2a", "cat2b")}),
        }
        for key, want in checks.items():
            got = sum(val(unit, k) or 0 for k in key.split("+")) if "+" in key else val(unit, key)
            if got is None or got != want:
                problems.append(f"table4 {unit}/{key}: {got} (expected {want})")
            else:
                ok.append(f"{unit}/{key}={want:g}")
    # the extras, per patient
    per = {p: [e for e in events if e.patient == p] for p in patients}
    for p, evs in per.items():
        nobp_labels = {e.label for e in evs if M.type_of(kb, e) is None}
        for label in ("PHILIPSMONITOR - Control. pat-ID", "UNKNOWNGW - Some alarm"):
            if label not in nobp_labels:
                problems.append(f"{p}: '{label}' not counted as without a blueprint")
        tie = [f for f in firings if f["patient"] == p and f["rule"] == "cat2a"
               and f["alarm"].strip() == "PHILIPSMONITOR - Desaturatie" and "x_edge_e" in f["alarm_ids"]]
        also = [f for f in firings if f["patient"] == p and f["rule"] == "cat2a" and "x_also_e2" in f["alarm_ids"]]
        if len(also) != 1:
            problems.append(f"{p}: x_also: expected rule 2a to silence the short SpO2 laag, got {also}")
        if len(tie) != 1:
            problems.append(f"{p}: tie at one instant: expected one cat2a silence on the second Desaturatie, got {tie}")
    for unit in ("PICU", "NICU", "ALL"):
        if not (val(unit, "filter_also_n") or 0) >= 1:
            problems.append(f"table4 {unit}/filter_also_n: the silenced-and-filtered path was not exercised")
    print(f"  Table 4: {len(ok)} count(s) agree with the logs and the stream "
          f"({', '.join(ok[:6])}, ...)")


# ── main ─────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.monotonic()
    if not (CASES / "cases_events.csv").exists():
        raise SystemExit("run engine/regression.py first (it writes the case events)")
    if OUT.exists():
        shutil.rmtree(OUT)
    segs = load_segments()
    patients = assign(segs)
    offsets, iri_map = build(segs, patients)
    n_st = sum(1 for _ in open(OUT / "stitched.csv")) - 1
    print(f"Segments: {len(segs)} ({len(segs) - len(EXTRAS)} cases + {len(EXTRAS)} extras); "
          f"patients: " + ", ".join(f"{p} ({d}, {len(s)} segments)" for p, (d, s) in patients.items())
          + f"; {n_st} stitched alarm(s)")
    fleet = sorted({r["device_name"] for rows in segs.values() for r in rows})
    print(f"Fleet: {', '.join(fleet)}")
    problems: list = []

    print("1. isolated run vs the regression's verified case logs")
    iso = run_poc("isolated", OUT / "isolated.csv", workers=1, chunk=100)
    iso_logs = normalise_isolated(iso)
    cases = [s for s in segs if s not in EXTRAS]
    compare("isolated vs regression", iso_logs, normalise_regression(), cases, problems)

    print("2. stitched runs vs the isolated run (per segment, shifted)")
    for name, kw in (("stitched_serial", dict(workers=1, chunk=1)),
                     ("stitched_w8", dict(workers=8, chunk=100))):
        run = run_poc(name, OUT / "stitched.csv", **kw)
        for p, (d, seg_list) in patients.items():
            compare(f"{name} {p}", _patient_view(run, p, offsets, iri_map, problems), iso_logs, seg_list, problems)
    run_poc("stitched_resume", OUT / "stitched.csv", workers=1, chunk=1, kill_after_chunks=1)
    run = run_poc("stitched_resume", OUT / "stitched.csv", workers=1, chunk=1, resume=True)
    import re
    m = re.search(r"Resuming: (\d+)/(\d+)", (run / "resume.txt").read_text())
    if not m or not 1 <= int(m.group(1)) < int(m.group(2)):
        problems.append(f"stitched_resume: did not resume partway ({m.group(0) if m else 'no resume line'})")
    else:
        print(f"  stitched_resume: {m.group(0)} chunk(s) done before the kill")
    for p, (d, seg_list) in patients.items():
        compare(f"stitched_resume {p}", _patient_view(run, p, offsets, iri_map, problems), iso_logs, seg_list,
                problems)

    print("3. Table 4 (manuscript_tables.py) on the stitched run")
    stats = table4(OUT / "stitched_serial", problems)
    check_table4(stats, OUT / "stitched_serial", patients, problems)

    print(f"\n{'PASS' if not problems else 'FAIL'} ({time.monotonic() - t0:.0f}s)")
    for p in problems[:40]:
        print(f"  - {p}")
    sys.exit(1 if problems else 0)


def _patient_view(run: Path, patient: str, offsets: dict, iri_map: dict, problems: list) -> dict:
    """normalise_stitched restricted to one stitched patient's rows."""
    tmp = run / f"_only_{patient}"
    tmp.mkdir(exist_ok=True)
    for log in ("rule_firings", "clinical_events"):
        rows = [r for r in read_log(run / f"{log}.csv") if r["patient"] == patient]
        with (tmp / f"{log}.csv").open("w", newline="", encoding="utf-8") as fh:
            fields = list(rows[0].keys()) if rows else (["patient", "time", "rule", "alarm", "caused_by", "alarm_ids"]
                                                        if log == "rule_firings" else
                                                        ["patient", "kind", "start", "end", "supported_by", "alarm_ids"])
            w = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
            w.writeheader()
            w.writerows(rows)
    view = normalise_stitched(tmp, offsets, iri_map, problems)
    shutil.rmtree(tmp)
    return view


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(*sys.argv[2:7])
    else:
        main()
