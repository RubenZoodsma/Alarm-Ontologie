"""
accumulation_probe.py — does the patient state stay bounded over a long stay?

Replays ONE patient serially (as poc_main does) and, every EVERY arrivals,
records what the run holds:
  store     quads in total; named graphs by kind (#transient, #persistent,
            clinical-event graphs, other); quads in the default graph
  events    open clinical events, their evidencedBy links (total, max)
  python    WindowOperator.open / expiries, identity-tracker keys,
            Processor.checks, LiveLog supports / records / firings
  cost      ms per arrival over the last block; process RSS
By the windows' design (windows.py) the store should hold only the alarms
still active or within the post-alarm window, so every count should go up
and down with the alarm density, not grow with the length of the stay.

  python3 validation/accumulation_probe.py <patientID> [max_arrivals] [every]
Reads the dataset and settings of poc_main.SETTINGS. Writes
_scratch_probe/<patient>.csv.
"""

from __future__ import annotations

import csv
import resource
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT))
import event_log as EL  # noqa: E402
import execution as X  # noqa: E402
import mint as M  # noqa: E402
import poc_main as PM  # noqa: E402
import processor as P  # noqa: E402
import rules as RU  # noqa: E402
import stream as S  # noqa: E402

EVENT_BASE = M.EVENT_BASE


def store_counts(store) -> dict:
    import pyoxigraph as ox
    graphs = Counter()
    quads_by_kind = Counter()
    for q in store.quads_for_pattern(None, None, None, None):
        g = q.graph_name
        if isinstance(g, ox.DefaultGraph):
            kind = "default"
        else:
            v = g.value
            kind = ("transient" if v.endswith("#transient") else "persistent" if v.endswith("#persistent")
                    else "event" if v.startswith(EVENT_BASE) else "other")
        quads_by_kind[kind] += 1
    for g in store.named_graphs():
        v = g.value
        graphs["transient" if v.endswith("#transient") else "persistent" if v.endswith("#persistent")
               else "event" if v.startswith(EVENT_BASE) else "other"] += 1
    ev = list(store.query("""SELECT ?ev (COUNT(?e) AS ?n) WHERE { GRAPH ?g {
        ?ev a <https://w3id.org/mda/ontology#ClinicalEvent> . OPTIONAL { ?ev <https://w3id.org/mda/ontology#evidencedBy> ?e } } }
        GROUP BY ?ev"""))
    ns = [int(s["n"].value) for s in ev]
    return {"quads": sum(quads_by_kind.values()), "quads_default": quads_by_kind["default"],
            "quads_transient": quads_by_kind["transient"], "quads_persistent": quads_by_kind["persistent"],
            "quads_event": quads_by_kind["event"], "quads_other": quads_by_kind["other"],
            "graphs_transient": graphs["transient"], "graphs_persistent": graphs["persistent"],
            "graphs_event": graphs["event"], "graphs_other": graphs["other"],
            "events_open": len(ns), "evidence_links": sum(ns), "evidence_max": max(ns, default=0)}


def main():
    patient = sys.argv[1]
    max_arrivals = int(sys.argv[2]) if len(sys.argv) > 2 else None
    every = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
    kb = M.load_kb(PM.SETTINGS["post_alarm_validity"])
    dataset = PM.resolve_dataset(PM.SETTINGS["dataset"])
    events = [e for e in S.load_events_for_patients(dataset, {patient}) if M.type_of(kb, e) is not None]
    if max_arrivals:
        events = events[:max_arrivals]
    print(f"{patient}: {len(events)} alarm(s) with a blueprint, "
          f"{events[0].start} .. {max(e.end for e in events)}")
    rule_names = sorted(n for n, on in PM.SETTINGS["enabled_rules"].items() if on)
    out_dir = ROOT / "_scratch_probe"
    scratch = out_dir / "engine"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    log = EL.LiveLog({patient: events}, EL.alarm_index(events))
    run = P.LiveRun(kb, scratch, rule_names, log, X.RuleStats())
    rows, arrivals, t_block = [], 0, time.perf_counter()
    path = out_dir / f"{patient}.csv"
    with path.open("w", newline="") as fh:
        w = None
        for unit in P.stream_units(S.replay_stream(events)):
            run.proc.feed(unit)
            run.execute(patient)
            if not isinstance(unit[0], S.AlarmArrival):
                continue
            arrivals += 1
            if arrivals % every:
                continue
            ms = 1000 * (time.perf_counter() - t_block) / every
            win = run.proc._windows.get(patient)
            row = {"arrivals": arrivals, "time": unit[0].time.isoformat(), "ms_per_arrival": round(ms, 2),
                   **store_counts(run.session._store),   # current store (rebuilt every REBUILD_EVERY syncs)
                   "window_open": len(win.open) if win else 0,
                   "window_expiries": len(win.expiries) if win else 0,
                   "identity_keys": sum(len(v) for v in win.identity_tracker.state.values()) if win else 0,
                   "proc_checks": len(run.proc.checks),
                   "log_supports": sum(len(v) for v in log._supports.values()),
                   "log_records": len(log.records), "log_firings": len(log.firings),
                   "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1)}
            if w is None:
                w = csv.DictWriter(fh, fieldnames=list(row), delimiter=";")
                w.writeheader()
            w.writerow(row)
            fh.flush()
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items() if k in (
                "arrivals", "ms_per_arrival", "quads", "graphs_transient", "graphs_persistent", "graphs_event",
                "events_open", "evidence_links", "evidence_max", "log_supports", "rss_mb")), flush=True)
            t_block = time.perf_counter()
    print(f"-> {path}")


if __name__ == "__main__":
    main()
