"""
poc_main.py — run the POC on a chosen dataset, patient sample and rule set,
and report what fired (real patients have no ground truth; for pass/fail
see engine/regression.py). Configure through SETTINGS.

The full corpus (DATA_LOCKED.rData, an R workspace) is converted once to
CSV by data/tools/export_rdata.R and cached. Patients are sampled from a
patientID-only scan before any event is built, so cost scales with the
sample, not the corpus.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
import event_log as EL
import mint as M
import execution as X
import processor as P
import rules as RU
import stream as S

RDATA_EXPORT_SCRIPT = ROOT / "data" / "tools" / "export_rdata.R"

SETTINGS = {
    # The events file (.csv, or .rData: converted and cached).
    ### locked dataset - real-world corpus of 14m alarms over 3299 patients
    "dataset": ROOT.parent / "DATA" / "POC_EVENTS" / "DATA_LOCKED.csv",
    ### trial dataset - small excerpt of the real-world corpus, 41 alarms over 8 patients
    # "dataset": ROOT.parent / "DATA" / "POC_EVENTS" / "events_data.csv",
    ### fabricated dataset - 46 known-outcome patients (CAT1–CAT3), for regression testing
    # "dataset": ROOT.parent / "DATA/CAT_evaluation" / "events_data.csv",

    # "all" (or None), or a number of randomly sampled patients.
    "n_patients": 5,

    # Seed of the patient sample.
    "seed": 42,

    # One switch per rule (engine/rules.RULES). A combined rule needs its
    # constituents: cat3a needs cardiac_arrest + respiratory_arrest, cat3b
    # needs reduced_pulmonary_function.
    "enabled_rules": {
    "cardiac_arrest": True,
    "respiratory_arrest": True,
    "reduced_pulmonary_function": True,
    "cat1a": True,
    "cat1b": True,
    "cat2a": True,
    "cat2b": True,
    "cat3a": True,
    "cat3b": True,
},
    # Patients per RDFox run; None: all in one run.
    "batch_size": 1,

    # The two logs (engine/event_log.py).
    "event_log": ROOT / "_scratch" / "clinical_events.csv",
    "firing_log": ROOT / "_scratch" / "rule_firings.csv",
}


def resolve_dataset(path: Path) -> Path:
    """`path`, or for an .rData file its CSV conversion (cached; redone
    when older than the source)."""
    if path.suffix.lower() not in (".rdata",):
        return path

    cache = path.with_suffix(".csv")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        print(f"Using cached CSV conversion: {cache.name} "
              f"(delete it, or touch {path.name}, to force reconversion)")
        return cache

    if shutil.which("Rscript") is None:
        raise RuntimeError(
            f"{path.name} is an R data file and needs `Rscript` to convert — "
            f"install R (e.g. `brew install r`) and ensure Rscript is on PATH."
        )
    print(f"Converting {path.name} -> {cache.name} via R (full corpus: ~70s, cached after this)...")
    t0 = time.monotonic()
    result = subprocess.run(
        ["Rscript", str(RDATA_EXPORT_SCRIPT), str(path), str(cache), "all", "0"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"export_rdata.R failed:\n{result.stdout}\n{result.stderr}")
    print(f"  {result.stdout.strip()} ({time.monotonic() - t0:.1f}s)")
    return cache


def choose_patient_ids(all_ids: list, n, seed: int) -> list:
    """All ids, or a seeded random sample of `n`."""
    if n is None or n == "all" or n >= len(all_ids):
        return all_ids
    rng = random.Random(seed)
    return rng.sample(all_ids, n)


def report(patients: dict, rule_names: list, firings: list, records: list) -> None:
    """Per patient and in total: flagged, silenced, episodes per kind, and
    the share of alarms CAT1 or CAT2 managed."""
    event_kinds = [r.kind for r in RU.enabled_event_rules(rule_names)]
    episodes = {kind: EL.episodes_by_patient(records, kind) for kind in event_kinds}
    total_flagged = total_silenced = total_managed = total_alarms = 0
    total_events = {kind: 0 for kind in event_kinds}
    # From the firing log, so withdrawn flags and lifted silences don't count.
    flagged = EL.flagged_alarms(firings)
    silenced = EL.silenced_alarms(firings)
    for patient in sorted(patients):
        fired_flag = {a for p, a in flagged if p == patient}
        fired_silence = {a for p, a in silenced if p == patient}
        n_flag = len(fired_flag)
        n_silence = len(fired_silence)
        # Distinct alarms flagged or silenced, over the alarms with a known label.
        n_managed = len(fired_flag | fired_silence)
        n_alarms = len(patients[patient])
        pct_managed = 100.0 * n_managed / n_alarms if n_alarms else 0.0
        total_flagged += n_flag
        total_silenced += n_silence
        total_managed += n_managed
        total_alarms += n_alarms
        parts = [f"flaggedLikelyFalsePositive={n_flag}", f"silencedBy={n_silence}"]
        for kind in event_kinds:
            n = episodes[kind].get(patient, 0)
            total_events[kind] += n
            parts.append(f"{kind}={n}")
        parts.append(f"cat1+cat2 hit rate={n_managed}/{n_alarms} ({pct_managed:.1f}%)")
        print(f"  {patient}: {', '.join(parts)}")
    total_pct = 100.0 * total_managed / total_alarms if total_alarms else 0.0
    print(f"\nTotals across {len(patients)} patient(s): "
          f"flaggedLikelyFalsePositive={total_flagged}, silencedBy={total_silenced}"
          + "".join(f", {kind}={n}" for kind, n in total_events.items())
          + f", cat1+cat2 hit rate={total_managed}/{total_alarms} ({total_pct:.1f}%)")


def run():
    """Load, sample, replay, write both logs, report."""
    t_start = time.monotonic()

    unknown = set(SETTINGS["enabled_rules"]) - set(RU.RULES)
    if unknown:
        raise ValueError(f"Unknown rule name(s) in SETTINGS['enabled_rules']: {sorted(unknown)} "
                          f"— valid names are {sorted(RU.RULES)}")
    rule_names = sorted(name for name, on in SETTINGS["enabled_rules"].items() if on)
    print(f"Rules enabled ({len(rule_names)}/{len(RU.RULES)}): "
          f"{', '.join(rule_names) or '(none)'}")

    t = time.monotonic()
    kb = M.load_kb()
    print(f"[{time.monotonic() - t_start:7.1f}s] loaded knowledge base "
          f"({time.monotonic() - t:.1f}s)")

    dataset = resolve_dataset(SETTINGS["dataset"])

    t = time.monotonic()
    all_ids = S.scan_patient_ids(dataset)
    print(f"[{time.monotonic() - t_start:7.1f}s] scanned {len(all_ids)} patient ID(s) "
          f"in {dataset.name} ({time.monotonic() - t:.1f}s)")

    chosen_ids = choose_patient_ids(all_ids, SETTINGS["n_patients"], SETTINGS["seed"])
    t = time.monotonic()
    events = S.load_events_for_patients(dataset, set(chosen_ids))
    groups = S.group_by_patient(events)
    print(f"[{time.monotonic() - t_start:7.1f}s] loaded {len(events)} alarm(s) for "
          f"{len(groups)} patient(s) ({time.monotonic() - t:.1f}s)")
    print(f"-- Patients: {len(groups)}/{len(all_ids)} ")

    # Coverage: the share of alarms whose label the catalogue knows.
    label_counts = Counter(e.label for e in events)
    unresolved_counts = Counter({label: n for label, n in label_counts.items()
                                  if label not in kb.type_index})
    n_total = len(events)
    n_covered = n_total - sum(unresolved_counts.values())
    coverage_pct = 100.0 * n_covered / n_total if n_total else 0.0
    print(f"-- Coverage: {coverage_pct:.1f}% - {n_covered}/{n_total} alarm(s) ")

    if unresolved_counts:
        top = unresolved_counts.most_common(3)
        print(f"WARNING: {len(unresolved_counts)} label(s) with non-matching AlarmType."
              f"Top {len(top)} by alarm count:")
        for label, n in top:
            print(f"  - {label} ({n} alarm(s))")
        if len(unresolved_counts) > len(top):
            print(f"  ... and {len(unresolved_counts) - len(top)} more distinct label(s)")

    # Unknown labels mint nothing; drop them before they cost RDFox commands.
    if unresolved_counts:
        events = [e for e in events if e.label in kb.type_index]
        groups = S.group_by_patient(events)

    scratch = ROOT / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    print(f"[{time.monotonic() - t_start:7.1f}s] processing {len(groups)} patient(s), "
          f"batch_size={SETTINGS['batch_size'] or 'unbounded'}...")
    trace: list = []
    counts_by_check, timings_by_check = P.run_batched(kb, groups, scratch, batch_size=SETTINGS["batch_size"],
                                                        enabled_rules=rule_names, trace=trace)
    alarms = EL.alarm_index(events)
    records = EL.event_records(trace, groups)
    EL.write_event_log(records, alarms, SETTINGS["event_log"])
    firings = EL.firing_records(trace)
    EL.write_firing_log(firings, alarms, SETTINGS["firing_log"])

    print()
    report(groups, rule_names, firings, records)
    print(f"Clinical-event log: {len(records)} event(s) -> {SETTINGS['event_log']}")
    print(f"Rule-firing log -> {SETTINGS['firing_log']}")
    X.summarize_rule_timings(counts_by_check, timings_by_check)
    print(f"\nTotal wall-clock time: {time.monotonic() - t_start:.1f}s")


if __name__ == "__main__":
    run()
