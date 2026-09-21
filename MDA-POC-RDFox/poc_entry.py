"""
poc_entry.py — run the MDA-POC-RDFox window operator over a configurable
subset of patients and a configurable subset of rules.

This is the general-purpose "run the POC" entry point — distinct from
engine/regression.py, which is a FIXED regression test
against the 46 fabricated CAT1–CAT3 patients in DATA/CAT_evaluation/
events_data.csv, checked against a hand-authored expected-outcome table.
Real patients (the default dataset here) have no such ground truth, so
this script reports what fired instead of pass/fail.

Reuses the engine modules (engine/processor.py, engine/mint.py, ...) as-is — no rule or
grounding logic is duplicated here; this file is orchestration only
(dataset/patient/rule selection, execution, reporting).

No command-line arguments — edit SETTINGS below and run the file directly
(e.g. VS Code's Run Python File), the same way the rest of this project's
one-off scripts work.

LOADING THE FULL 14M-ALARM CORPUS (.rData)
--------------------------------------------------------------------
SETTINGS['dataset'] can point at DATA/POC_EVENTS/DATA_LOCKED.rData
directly — the real, full corpus (13.8M rows, 3299 patients), not the
41-alarm/8-patient events_data.csv excerpt. That file is a full saved R
workspace (60+ objects), not a plain data.frame, and its label text isn't
valid UTF-8 — confirmed directly that both pure-Python .rData readers
(pyreadr, rdata) fail on it (a UnicodeDecodeError on real label text, and
a bytecode-parsing error, respectively) — so conversion happens once via
a small R script (data/tools/export_rdata.R) instead of fighting either
library's limitations. That script's own header documents the exact
column mapping (patientID/conditie/bed_naam+device_naam/alarm_start/
alarm_eind -> patient/label/device_id/start/end) and the real, confirmed
data-quality issue it corrects (~30% of labels carry stray whitespace
that would otherwise silently fail exact-string lookup against
kg_generated.ttl's catalogue).

Converting the full file costs ~70s (dominated by R deserializing the
whole 200MB workspace — confirmed the sampling step itself is not what's
slow, so there's nothing to gain by re-running R per sample size) and is
cached: resolve_dataset() only re-runs the R script when the cached CSV
is missing or older than the .rData source. Needs R installed
(`brew install r`) and `Rscript` on PATH; nothing else here depends on R.

Even reading the cached ~1.2GB/13.8M-row CSV in full just to keep 5
patients out of 3299 would cost ~58s for no reason (measured directly:
building every Event/parsing every date dominates that cost, not file
I/O — a patientID-only scan alone takes ~15s). So patient selection
happens BEFORE any Event is built: scan_patient_ids() reads just the
patientID column, the sample is chosen from that, and
load_events_for_patients() then only constructs Events for the chosen
patients' own rows. This runs unconditionally (not just for the .rData
path) — it costs nothing extra on the small fabricated/POC_EVENTS CSVs
and means N-patient selection cost scales with N, not with corpus size,
for any dataset this script is pointed at.
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
import clinical_events as CE
import event_log as EL
import mint as M
import execution as X
import processor as P
import rules as RU
import stream as S

RDATA_EXPORT_SCRIPT = ROOT / "data" / "tools" / "export_rdata.R"

SETTINGS = {
    # Which events file to replay. Three shapes work here:
    #   - the small real-corpus excerpt: DATA/POC_EVENTS/events_data.csv
    #   - the fabricated, known-outcome CAT1–CAT3 fixtures:
    #     ROOT.parent / "DATA/CAT_evaluation/events_data.csv"
    #   - the FULL 14M-alarm corpus: DATA/POC_EVENTS/DATA_LOCKED.rData
    #     (converted+cached to CSV automatically the first time — see
    #     this module's own docstring).
    ### locked dataset - real-world corpus of 14m alarms over 3299 patients
    "dataset": ROOT.parent / "DATA" / "POC_EVENTS" / "DATA_LOCKED.csv",
    ### trial dataset - small excerpt of the real-world corpus, 41 alarms over 8 patients
    # "dataset": ROOT.parent / "DATA" / "POC_EVENTS" / "events_data.csv",
    ### fabricated dataset - 46 known-outcome patients (CAT1–CAT3), for regression testing
    # "dataset": ROOT.parent / "DATA/CAT_evaluation" / "events_data.csv",

    # "all" (or None) replays every patient in the dataset. An int (e.g.
    # 5 or 10) instead randomly samples that many patients rather than
    # the entire set — the point of this setting against the full
    # 14M-alarm corpus: try a handful of patients cheaply instead of
    # paying for all 3299.
    "n_patients": 5,

    # Fixes WHICH patients get sampled when n_patients is a number, so a
    # run is reproducible. Change it to get a different random subset.
    "seed": 42,

    # One switch per rule (engine/rules.RULE_FILES' entries) —
    # flip any of these to False to exclude that rule from the run.
    #"enabled_rules": {name: True for name in RU.RULE_FILES},
    "enabled_rules": {
    "cardiac_arrest": True,
    "respiratory_arrest": True,
    "reduced_pulmonary_function": True,
    "cat1a": True,
    "cat1b": True,
    "cat2a": True,
    "cat2b": True,
    # cardiac_arrest/respiratory_arrest/reduced_pulmonary_function and
    # cat3a (cardiorespiratory arrest)/cat3b (ventilation failure) maintain
    # clinical events per patient (engine/clinical_events.py). A combined
    # rule needs its constituents enabled too (cat3a: cardiac_arrest +
    # respiratory_arrest; cat3b: reduced_pulmonary_function) — build_script
    # raises a clear error otherwise. Every event is scoped to one patient,
    # so any batch_size is safe. Covered by the regression fixtures in
    # DATA/CAT_evaluation/events_data.csv (engine/regression.py).
    "cat3a": True,
    "cat3b": True,
},
    # None processes every selected patient in a single batch/dstore —
    # fine at this dataset's scale. An int (e.g. 200) processes patients
    # in bounded-size batches instead, each its own RDFox run — bounds
    # memory/disk for a run large enough that holding everyone in one
    # script/dstore stops being practical (see engine/processor.
    # run_batched's own docstring). Worth setting once n_patients is
    # "all" against the full 14M-alarm corpus's 3299 patients.
    "batch_size": 1,

    # Where the logs are written: one row per clinical event (start, end,
    # supporting alarms), and one row per CAT1/CAT2 firing (arriving alarm,
    # causing alarms). See engine/event_log.py.
    "event_log": ROOT / "_scratch" / "clinical_events.csv",
    "firing_log": ROOT / "_scratch" / "rule_firings.csv",
}


def resolve_dataset(path: Path) -> Path:
    """If `path` is an .rData file, convert it to this project's own CSV
    shape via data/tools/export_rdata.R and return the (cached) CSV path
    instead. Otherwise return `path` unchanged. See this module's own
    docstring for why conversion is a separate R step and why it's
    cached rather than re-run per sample."""
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
    if n is None or n == "all" or n >= len(all_ids):
        return all_ids
    rng = random.Random(seed)
    return rng.sample(all_ids, n)


def report(patients: dict, rule_names: list, firings: list, records: list) -> None:
    event_kinds = [r.kind for r in CE.enabled_event_rules(rule_names)]
    episodes = {kind: EL.episodes_by_patient(records, kind) for kind in event_kinds}
    total_flagged = total_silenced = total_managed = total_alarms = 0
    total_events = {kind: 0 for kind in event_kinds}
    # Counted from the firing log, not the raw check counts: a cat1b flag
    # can be withdrawn after its check fired (event_log.flagged_alarms).
    flagged = EL.flagged_alarms(firings)
    silenced = EL.silenced_alarms(firings)
    for patient in sorted(patients):
        fired_flag = {a for p, a in flagged if p == patient}
        fired_silence = {a for p, a in silenced if p == patient}
        n_flag = len(fired_flag)
        n_silence = len(fired_silence)
        # cat1+cat2 combined: an alarm counts once here even if BOTH
        # flaggedLikelyFalsePositive and silencedBy fired for it — a
        # straight n_flag+n_silence sum would double-count that alarm,
        # overstating how many DISTINCT alarms the framework actually
        # managed. `patients[patient]` is already the post-coverage-filter
        # (unsupported-label-dropped) event list run() passes in here, so
        # this denominator is the same "fair %" basis run()'s own coverage
        # line uses — not the raw pre-filter alarm count.
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
    t_start = time.monotonic()

    unknown = set(SETTINGS["enabled_rules"]) - set(RU.RULE_FILES)
    if unknown:
        raise ValueError(f"Unknown rule name(s) in SETTINGS['enabled_rules']: {sorted(unknown)} "
                          f"— valid names are {sorted(RU.RULE_FILES)}")
    rule_names = sorted(name for name, on in SETTINGS["enabled_rules"].items() if on)
    print(f"Rules enabled ({len(rule_names)}/{len(RU.RULE_FILES)}): "
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

    # Coverage is ALARM-COUNT weighted, not label-count weighted: a
    # handful of rare unresolved labels covering a tiny fraction of
    # actual alarm volume is a very different situation from a common
    # label going unresolved, and only the alarm-weighted number answers
    # "how much of what actually happened does the framework account
    # for" — the question that actually matters for this POC's results.
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

    # Drop unresolved-label alarms BEFORE minting — mint.py's own
    # alarm_message/condition_for_event/background_for_key already return
    # an empty Graph() for these (kb.type_index has no entry for the
    # label), so they contribute zero triples either way. Without this
    # filter they still cost a real RDFox `import`, two scheduled
    # DELETE WHERE drops, and 2-3 guaranteed-zero `select` checks each —
    # pure overhead in the generated script for content that was never
    # going to be there. Computed after the coverage report above so that
    # report still reflects the full, unfiltered picture.
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
