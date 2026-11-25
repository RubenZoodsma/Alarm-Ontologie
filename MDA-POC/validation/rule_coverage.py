"""
rule_coverage.py — which alarm-type combinations each rule covers, derived
from the knowledge base by the engine itself (manuscript 4.3.2).

Every ordered pair of alarm types (A, B) in the knowledge base is replayed as
one synthetic patient, in two arrangements:

- concurrent: B is active (0s to 2m) when A arrives (10s to 1m). This covers
  the rules that relate an arriving alarm to an active one (1a, 2a, 2b) and
  the combined events (3a, 3b).
- after: B has ended (0s to 5s) when A arrives (1m to 2m), so B's structural
  knowledge is still valid but B raises no alarm. This covers rule 1b.

An alarm type is a blueprint: device model + label + priority, taken from the
selected corpus as (gateway device name, label, priority). A device in the
synthetic patient is identified by its gateway name (e.g. Caleo, Medibus).
Pairs that would place two different devices of the same type on one
patient (two ventilators, two patient monitors) are skipped: such a
configuration does not occur, so a label list would never need the pair.
This extends the assumption of 4.2.2 (no two devices of the same type and
manufacturer) to devices of the same type.

A pair is covered by a rule when the rule silences A citing B (1a, 1b, 2a,
2b), or adds its event with A and B as evidence (3a, 3b). The covered pairs
are the label combinations an enumerated list would need to state. The same
pairs, split by the department the alarm types come from, show what the
unchanged rules cover in the adult ICU, whose alarms the replay of 4.3.3
does not contain.

Writes validation/results/rule_coverage_pairs.csv (one row per covered pair)
and rule_coverage_summary.csv. Configure through SETTINGS.
"""

from __future__ import annotations

import csv
import itertools
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

import event_log as EL  # noqa: E402
import mint as M  # noqa: E402
from execution import RuleStats  # noqa: E402
from processor import run_parallel  # noqa: E402
from stream import Event  # noqa: E402

SETTINGS = {
    # Parallel worker processes.
    "workers": 8,
    # Only the first N ordered pairs (None: all); for a quick test.
    "limit": None,
    # The annotated corpus, for the department(s) of each alarm type.
    "corpus": ROOT.parent / "DATA" / "CORPUS" / "Annotation_corpus_99.csv",
    "out_dir": ROOT / "validation" / "results",
}

BASE = datetime(2026, 1, 1, 8, 0, 0)
ARRANGEMENTS = {
    # name: (B start, B end, A start, A end), seconds from BASE
    "concurrent": (0, 120, 10, 60),
    "after": (0, 5, 60, 120),
}
SILENCE_RULES = {"cat1a": "1a", "cat1b": "1b", "cat2a": "2a", "cat2b": "2b"}
EVENT_RULES = {"CardioRespiratoryArrest": "3a", "VentilationFailure": "3b"}
RULE_ORDER = ["1a", "1b", "2a", "2b", "3a", "3b"]
DEPARTMENTS = {"Adult intensive care": "adult", "Paediatric intensive care": "pediatric",
               "Neonatal intensive care": "neonatal"}


def type_id(gateway: str, label: str, priority: str) -> str:
    return f"{gateway} | {label.strip()} | {priority}"


def alarm_types(kb, corpus: Path) -> dict:
    """{type id: (gateway, label, priority, blueprint IRI, departments)} for
    every distinct alarm of the selected corpus that has a blueprint."""
    out = {}
    with corpus.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            g, lbl, prio = row["Alarm_device"], row["Alarm_label"].strip(), row["Alarm_priority"]
            iri = M.type_of(kb, Event("", lbl, "", BASE, BASE, "", g, prio))
            if iri is None:          # not annotatable: no blueprint
                continue
            t = type_id(g, lbl, prio)
            if t not in out:
                out[t] = (g, lbl, prio, iri, set())
            out[t][4].add(DEPARTMENTS[row["Alarm_department"]])
    return out


def device_types(kb, atypes: dict) -> dict:
    """{type id: device category concept of its blueprint, or None}."""
    return {t: M.archetype_structure(kb, v[3]).concept(M.MDA.Device) for t, v in atypes.items()}


def device_of(t: str, atypes: dict) -> str:
    """Device identity in the synthetic patient: the gateway device name."""
    return atypes[t][0]


def plausible(a: str, b: str, dtypes: dict, atypes: dict) -> bool:
    """False when A and B would be two different devices of the same type."""
    ta, tb = dtypes[a], dtypes[b]
    return ta is None or ta != tb or device_of(a, atypes) == device_of(b, atypes)


def patients(labels: list, dtypes: dict, atypes: dict) -> dict:
    """{patient id: (arrangement, A, B, events)} for every plausible ordered pair."""
    out = {}
    pairs = [p for p in itertools.permutations(labels, 2) if plausible(*p, dtypes, atypes)]
    if SETTINGS["limit"]:
        pairs = pairs[: SETTINGS["limit"]]
    ev = lambda pid, t, s, e, role: Event(pid, atypes[t][1], atypes[t][0], s, e, role,  # noqa: E731
                                          atypes[t][0], atypes[t][2])
    for name, (bs, be, as_, ae) in ARRANGEMENTS.items():
        for i, (a, b) in enumerate(pairs):
            pid = f"{name}_{i:05d}"
            t = lambda s: BASE + timedelta(seconds=s)  # noqa: E731
            out[pid] = (name, a, b, [ev(pid, b, t(bs), t(be), "b"), ev(pid, a, t(as_), t(ae), "a")])
    return out


def main() -> None:
    kb = M.load_kb()
    atypes = alarm_types(kb, SETTINGS["corpus"])
    labels = sorted(atypes)
    dept = {t: v[4] for t, v in atypes.items()}
    dtypes = device_types(kb, atypes)
    skipped = sum(1 for p in itertools.permutations(labels, 2) if not plausible(*p, dtypes, atypes))
    pts = patients(labels, dtypes, atypes)
    streams = {pid: evs for pid, (_, _, _, evs) in pts.items()}
    print(f"{len(labels)} alarm types, {len(pts)} synthetic patients "
          f"({skipped} ordered pairs skipped: two devices of one type)")

    scratch = ROOT / "_scratch" / "rule_coverage"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    log = EL.LiveLog(streams, EL.alarm_index(e for evs in streams.values() for e in evs))
    t0 = time.monotonic()
    ok = run_parallel(streams, scratch, log, RuleStats(), SETTINGS["workers"], progress=False)
    shutil.rmtree(scratch)
    if not ok:
        sys.exit("engine errors: coverage incomplete")
    print(f"replayed in {time.monotonic() - t0:.0f}s")

    role = {pid: {str(M.alarm_iri(e)): e.alarm_id for e in evs} for pid, evs in streams.items()}
    covered = set()   # (rule, A, B)
    for f in log.firings:
        rule = SILENCE_RULES.get(f.rule)
        name, a, b, _ = pts[f.patient]
        ids = role[f.patient]
        if rule and ids.get(f.alarm) == "a" and {ids.get(c) for c in f.causes} >= {"b"}:
            if rule == "1b" and name != "after":
                continue
            covered.add((rule, a, b))
    for r in log.records:
        rule = EVENT_RULES.get(r.kind)
        name, a, b, _ = pts[r.patient]
        if rule and name == "concurrent" and {role[r.patient].get(x) for x in r.alarms} >= {"a", "b"}:
            covered.add((rule, a, b))

    out_dir = SETTINGS["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "rule_coverage_pairs.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rule", "alarm", "other_alarm", "alarm_departments", "other_departments"])
        for rule, a, b in sorted(covered, key=lambda x: (RULE_ORDER.index(x[0]), x[1], x[2])):
            w.writerow([rule, a, b, ",".join(sorted(dept[a])), ",".join(sorted(dept[b]))])

    adult = {lbl for lbl in labels if "adult" in dept[lbl]}
    replayed = {lbl for lbl in labels if dept[lbl] & {"pediatric", "neonatal"}}
    adult_only = adult - replayed
    rows = []
    for rule in RULE_ORDER:
        pairs = {(a, b) for r, a, b in covered if r == rule}
        if rule in ("3a", "3b"):   # the combination is unordered
            pairs = {tuple(sorted(p)) for p in pairs}
        types = {x for p in pairs for x in p}
        adult_pairs = {p for p in pairs if set(p) <= adult}
        adult_types = {x for p in adult_pairs for x in p}
        rows.append({
            "rule": rule,
            "pairs": len(pairs),
            "pairs_replayed_units": sum(1 for p in pairs if set(p) <= replayed),
            "pairs_adult_icu": len(adult_pairs),
            "pairs_adult_icu_new": sum(1 for p in adult_pairs if not set(p) <= replayed),
            "pairs_across_units": sum(1 for p in pairs if not (set(p) <= replayed or set(p) <= adult)),
            "adult_only_types": len(adult_types & adult_only),
            "adult_only_devices": len({device_of(x, atypes) for x in adult_types & adult_only}),
        })
    with (out_dir / "rule_coverage_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    print(f"{len(labels)} alarm types: {len(replayed)} in the replayed units, {len(adult)} in the adult ICU, "
          f"{len(adult_only)} adult ICU only ({len({device_of(x, atypes) for x in adult_only})} devices)")
    keys = list(rows[0])
    print(";".join(keys))
    for r in rows:
        print(";".join(str(r[k]) for k in keys))

if __name__ == "__main__":
    main()
