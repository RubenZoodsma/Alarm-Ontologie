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
import windows as W

RDATA_EXPORT_SCRIPT = ROOT / "data" / "tools" / "export_rdata.R"

SETTINGS = {
    # The events file (.csv, or .rData: converted once to `dataset_cache`).
    ### locked dataset - real-world corpus, 14.45M alarms over 3358 patients
    ### (PICU + NICU, Aug 2023 - Jul 2025), locked 2026-10-16
    "dataset": Path("/Volumes/Onderzoek/Onderzoek_Overig/24U-0113_SAS-ICU/E_ResearchData/2_ResearchData/"
                    "oldCode/pipeline/locked/20261016/DATA_FINAL_20261016.rData"),
    ### trial dataset - small excerpt of the real-world corpus, 41 alarms over 8 patients
    # "dataset": ROOT.parent / "DATA" / "POC_EVENTS" / "events_data.csv",
    ### fabricated dataset - 46 known-outcome patients (CAT1–CAT3), for regression testing
    # "dataset": ROOT.parent / "DATA/CAT_evaluation" / "events_data.csv",

    # Where an .rData dataset's CSV conversion is cached: local and
    # gitignored (*.csv), never next to the locked source. None: DATA/POC_EVENTS/<name>.csv.
    "dataset_cache": None,

    # "all" (or None), or a number of randomly sampled patients.
    "n_patients": "all",

    # Leave out patients with more alarms than this before sampling (None:
    # keep all). A selection on stay length only, for quick runs; the
    # replay itself is unchanged. Must be None for the manuscript run.
    "max_alarms_per_patient": None,

    # Seed of the patient sample.
    "seed": 24,

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
    # Post-alarm validity of structural knowledge, as an xsd:duration
    # ("PT5M", "PT30M", "PT1H"), for the sensitivity analysis; None: the
    # ontology's mda:postAlarmValidityDuration (PT15M).
    "post_alarm_validity": None,

    # Parallel worker processes (patients are independent; each gets its
    # own Oxigraph store). 1: serial, in batches of `batch_size`.
    "workers": 8,

    # Patients per engine run when serial; None: all in one run.
    "batch_size": 1,

    # Patients per chunk: the dataset is split once into chunk files and
    # processed chunk by chunk, so memory holds one chunk (about 2.7 KB per
    # alarm), and the logs are appended after each chunk. None: one chunk.
    "chunk_size": 100,

    # Continue an interrupted run: the chunks recorded as done in the run
    # manifest are skipped, the logs cut back to their size after the last
    # completed chunk. Refused when the manifest's settings, commit or
    # framework files differ from this run's.
    "resume": False,

    # Seconds one stream element may take (kept for compatibility; the
    # engine runs in-process).
    "element_timeout_s": 600,

    # Write every command sent to the engine to replay.oxigraph per run.
    # Large; for debugging only.
    "audit_logging": False,

    # The two logs (engine/event_log.py).
    "event_log": ROOT / "_scratch" / "clinical_events.csv",
    "firing_log": ROOT / "_scratch" / "rule_firings.csv",
    # For Table 4 (MANUSCRIPT/scripts/manuscript_tables.py): processing time per
    # arriving alarm (histogram per patient), and what was run.
    "arrival_times": ROOT / "_scratch" / "arrival_times.csv",
    "run_manifest": ROOT / "_scratch" / "run.json",
}


def resolve_dataset(path: Path) -> Path:
    """`path`, or for an .rData file its CSV conversion (cached; redone
    when older than the source)."""
    if path.suffix.lower() not in (".rdata",):
        return path

    cache = SETTINGS["dataset_cache"] or ROOT.parent / "DATA" / "POC_EVENTS" / f"{path.stem}.csv"
    if not path.exists():
        if cache.exists():
            print(f"WARNING: {path} not found (research share not mounted?); using the cached conversion "
                  f"{cache.name} without checking it against the source.")
            return cache
        raise FileNotFoundError(f"{path} not found (is the research share mounted?), and no cached conversion")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        print(f"Using cached CSV conversion: {cache.name} "
              f"(delete it, or touch {path.name}, to force reconversion)")
        return cache

    if shutil.which("Rscript") is None:
        raise RuntimeError(
            f"{path.name} is an R data file and needs `Rscript` to convert — "
            f"install R (e.g. `brew install r`) and ensure Rscript is on PATH."
        )
    print(f"Converting {path.name} -> {cache} via R (full corpus: a few minutes, cached after this)...")
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


def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def report(kb, patients: dict, rule_names: list, firings: list, records: list, totals: Counter) -> None:
    """Per patient: flagged, silenced, episodes per kind, the share of
    alarms CAT1 or CAT2 managed, the share that arrived with context
    (another alarm of the patient still in the store), and the managed
    share of those. Adds the patient's counts to `totals` (print_totals)."""
    event_kinds = [r.kind for r in RU.enabled_event_rules(rule_names)]
    episodes = {kind: EL.episodes_by_patient(records, kind) for kind in event_kinds}
    with_context = W.alarms_with_context([e for evs in patients.values() for e in evs], kb.window)
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
        context = {str(M.alarm_iri(e)) for e in patients[patient]} & with_context
        n_context = len(context)
        n_managed_in_context = len((fired_flag | fired_silence) & context)
        totals.update(flagged=n_flag, silenced=n_silence, managed=n_managed, alarms=n_alarms,
                      context=n_context, managed_in_context=n_managed_in_context, patients=1)
        parts = [f"flaggedLikelyFalsePositive={n_flag}", f"silencedBy={n_silence}"]
        for kind in event_kinds:
            n = episodes[kind].get(patient, 0)
            totals["event:" + kind] += n
            parts.append(f"{kind}={n}")
        parts.append(f"cat1+cat2 hit rate={n_managed}/{n_alarms} ({_pct(n_managed, n_alarms):.1f}%)")
        parts.append(f"with context={n_context}/{n_alarms} ({_pct(n_context, n_alarms):.1f}%)")
        parts.append(f"cat1+cat2 hit rate with context={n_managed_in_context}/{n_context} "
                     f"({_pct(n_managed_in_context, n_context):.1f}%)")
        print(f"  {patient}: {', '.join(parts)}")


def print_totals(totals: Counter, rule_names: list) -> None:
    event_kinds = [r.kind for r in RU.enabled_event_rules(rule_names)]
    t = totals
    print(f"\nTotals across {t['patients']} patient(s) (this session): "
          f"flaggedLikelyFalsePositive={t['flagged']}, silencedBy={t['silenced']}"
          + "".join(f", {kind}={t['event:' + kind]}" for kind in event_kinds)
          + f", cat1+cat2 hit rate={t['managed']}/{t['alarms']} ({_pct(t['managed'], t['alarms']):.1f}%)"
          + f", with context={t['context']}/{t['alarms']} ({_pct(t['context'], t['alarms']):.1f}%)"
          + f", cat1+cat2 hit rate with context={t['managed_in_context']}/{t['context']} "
            f"({_pct(t['managed_in_context'], t['context']):.1f}%)")
    print("  with context: another alarm of the patient was still in the store at arrival — active,\n"
          "  or within the post-alarm window after its end; without it CAT1/CAT2 have nothing to compare to.")


def code_hash() -> str:
    """A hash of everything that decides a run's conclusions: the framework
    files, the rule and action queries, the engine and this script. A
    resumed run must match it; the commit may differ (work elsewhere in the
    repository, e.g. on the reporting scripts, does not block a resume)."""
    import hashlib
    h = hashlib.sha256()
    import paths as P
    framework = [P.ONTOLOGY, P.INFERENCE, P.CATALOGUE, P.VOCAB, P.CLINICAL_EVENT_VOCAB, P.PRIORITY_RANK]
    paths = (framework + sorted((ROOT / "data").glob("*.ttl")) + sorted((ROOT / "representation").rglob("*.rq"))
             + sorted((ROOT / "engine").glob("*.py")) + [ROOT / "poc_main.py"])
    for path in paths:
        h.update(str(path.relative_to(ROOT.parent)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def git_state() -> dict:
    """The commit and whether MDA-POC had uncommitted changes: recorded, not
    compared on resume."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "-c", "core.fileMode=false", "status", "--porcelain",
                                     "--untracked-files=no", "--", "."],
                                    cwd=ROOT, capture_output=True, text=True).stdout.strip())
    except OSError:
        commit, dirty = "", True
    return {"commit": commit, "uncommitted_changes": dirty}


def _write_manifest(path: Path, manifest: dict) -> None:
    """Atomically: a crash never leaves a half-written manifest."""
    import json
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.replace(path)


LOG_KEYS = ("event_log", "firing_log", "arrival_times")


def run():
    """Load, sample, split into chunks, replay chunk by chunk (appending
    both logs), report."""
    import json
    t_start = time.monotonic()

    unknown = set(SETTINGS["enabled_rules"]) - set(RU.RULES)
    if unknown:
        raise ValueError(f"Unknown rule name(s) in SETTINGS['enabled_rules']: {sorted(unknown)} "
                          f"— valid names are {sorted(RU.RULES)}")
    rule_names = sorted(name for name, on in SETTINGS["enabled_rules"].items() if on)
    print(f"Rules enabled ({len(rule_names)}/{len(RU.RULES)}): "
          f"{', '.join(rule_names) or '(none)'}")

    t = time.monotonic()
    kb = M.load_kb(SETTINGS["post_alarm_validity"])
    print(f"[{time.monotonic() - t_start:7.1f}s] loaded knowledge base "
          f"({time.monotonic() - t:.1f}s); post-alarm validity {kb.window}"
          + ("" if SETTINGS["post_alarm_validity"] is None else " (SETTINGS override)"))

    dataset = resolve_dataset(SETTINGS["dataset"])

    t = time.monotonic()
    counts = S.scan_patient_counts(dataset)
    all_ids = sorted(counts)
    print(f"[{time.monotonic() - t_start:7.1f}s] scanned {len(all_ids)} patient ID(s) "
          f"in {dataset.name} ({time.monotonic() - t:.1f}s)")
    if SETTINGS["max_alarms_per_patient"]:
        cap = SETTINGS["max_alarms_per_patient"]
        all_ids = [p for p in all_ids if counts[p] <= cap]
        print(f"-- max_alarms_per_patient={cap}: {len(all_ids)}/{len(counts)} patient(s) eligible "
              f"(a quick-run selection, not for the manuscript run)")
    chosen_ids = choose_patient_ids(all_ids, SETTINGS["n_patients"], SETTINGS["seed"])
    size = SETTINGS["chunk_size"] or len(chosen_ids)
    chunks = [chosen_ids[i:i + size] for i in range(0, len(chosen_ids), size)]

    scratch = ROOT / "_scratch"
    chunk_dir = scratch / "chunks"
    manifest_path = SETTINGS["run_manifest"]
    # What must match for a resumed run to continue this one.
    identity = {
        "dataset": str(dataset), "patients": sorted(chosen_ids),
        "n_patients": SETTINGS["n_patients"], "seed": SETTINGS["seed"],
        "max_alarms_per_patient": SETTINGS["max_alarms_per_patient"],
        "enabled_rules": rule_names, "post_alarm_validity": str(kb.window),
        "chunk_size": size, "code_sha256": code_hash(),
    }
    manifest = None
    if SETTINGS["resume"] and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        differs = [k for k in identity if manifest.get(k) != identity[k]]
        if differs:
            raise RuntimeError(f"resume refused: {', '.join(differs)} differ(s) from the interrupted run "
                               f"({manifest_path}). Set resume False to start over.")
        # Cut each log back to its size after the last completed chunk: rows
        # of a chunk that was interrupted are dropped, and the chunk reruns.
        for key in LOG_KEYS:
            path, size_done = Path(manifest[key]), manifest["log_sizes"].get(key, 0)
            if path.exists():
                with path.open("r+b") as f:
                    f.truncate(size_done)
        manifest.setdefault("resumed", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **git_state()})
        manifest["chunks_failed"] = []      # retried below
        print(f"Resuming: {len(manifest['chunks_done'])}/{len(chunks)} chunk(s) already done.")
    if manifest is None:
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True)
        for key in LOG_KEYS:
            if SETTINGS[key].exists():
                SETTINGS[key].unlink()
        t = time.monotonic()
        S.partition_by_patient(dataset, chunks, chunk_dir)
        print(f"[{time.monotonic() - t_start:7.1f}s] split {len(chosen_ids)} patient(s) into "
              f"{len(chunks)} chunk(s) of up to {size} ({time.monotonic() - t:.1f}s)")
        manifest = {**identity, "event_log": str(SETTINGS["event_log"]),
                    "firing_log": str(SETTINGS["firing_log"]),
                    "arrival_times": str(SETTINGS["arrival_times"]),
                    "chunks": len(chunks), "chunks_done": [], "chunks_failed": [], "log_sizes": {},
                    "complete": False, "git": git_state(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        _write_manifest(manifest_path, manifest)

    key_of = lambda e: f"{e.device_name} | {e.label} | {e.priority}"  # noqa: E731
    unresolved_counts, n_total = Counter(), 0
    totals, session_stats = Counter(), X.RuleStats()
    complete = True
    # Progress over the whole run, by raw alarms (known from the scan).
    chunk_alarms = [sum(counts[p] for p in ids) for ids in chunks]
    todo = [k for k in range(len(chunks)) if k not in manifest["chunks_done"]]
    total_todo, done_alarms, t_run = sum(chunk_alarms[k] for k in todo), 0, time.monotonic()
    for k, ids in enumerate(chunks):
        if k in manifest["chunks_done"]:
            continue
        t = time.monotonic()
        events = S.load_events_for_patients(chunk_dir / f"chunk_{k:05d}.csv", set(ids))
        n_total += len(events)
        # Coverage: alarms without a blueprint (device model + label +
        # priority) mint nothing; drop them before they cost engine commands.
        unresolved = [e for e in events if M.type_of(kb, e) is None]
        unresolved_counts.update(key_of(e) for e in unresolved)
        if unresolved:
            events = [e for e in events if M.type_of(kb, e) is not None]
        groups = S.group_by_patient(events)
        print(f"[{time.monotonic() - t_start:7.1f}s] chunk {k + 1}/{len(chunks)}: {len(groups)} patient(s), "
              f"{len(events)} alarm(s) with a blueprint")

        log = EL.LiveLog(groups, EL.alarm_index(events), SETTINGS["event_log"], SETTINGS["firing_log"],
                         append=True)
        stats = X.RuleStats()
        engine_scratch = scratch / "engine"
        if engine_scratch.exists():
            shutil.rmtree(engine_scratch)
        engine_scratch.mkdir(parents=True)
        if SETTINGS["workers"] > 1:
            ok = P.run_parallel(groups, engine_scratch, log, stats, SETTINGS["workers"],
                                enabled_rules=rule_names, audit_log=SETTINGS["audit_logging"],
                                window=SETTINGS["post_alarm_validity"])
        else:
            ok = P.run_batched(kb, groups, engine_scratch, log, stats, batch_size=SETTINGS["batch_size"],
                               enabled_rules=rule_names, timeout=SETTINGS["element_timeout_s"],
                               audit_log=SETTINGS["audit_logging"])
        log.close()
        stats.write_arrival_times(SETTINGS["arrival_times"], append=True)
        session_stats.merge(stats)
        report(kb, groups, rule_names, log.firings, log.records, totals)
        if ok and not manifest["chunks_failed"]:
            manifest["chunks_done"].append(k)
        elif not ok:
            # The chunk is not done. A resume cuts the logs back to log_sizes
            # (frozen at the last chunk before the first failure) and reruns
            # every chunk not done: this one and all after it, which still run
            # now (their rows are provisional) but are not recorded as done.
            complete = False
            manifest["chunks_failed"].append(k)
            print(f"*** chunk {k + 1} did not complete (engine errors above); recorded as failed, the run "
                  f"goes on. Resume reruns it. ***")
        if not manifest["chunks_failed"]:
            manifest["log_sizes"] = {key: SETTINGS[key].stat().st_size for key in LOG_KEYS}
        manifest["complete"] = len(manifest["chunks_done"]) == len(chunks)
        _write_manifest(manifest_path, manifest)
        done_alarms += chunk_alarms[k]
        elapsed = time.monotonic() - t_run
        eta = elapsed / done_alarms * (total_todo - done_alarms) if done_alarms else 0
        print(f"== chunk {k + 1}/{len(chunks)} {'done' if ok else 'FAILED'} ({time.monotonic() - t:.0f}s) | "
              f"run: {len(manifest['chunks_done'])}/{len(chunks)} chunks, {done_alarms:,}/{total_todo:,} raw alarms, "
              f"elapsed {P._hms(elapsed)}, remaining ~{P._hms(eta)}", flush=True)

    covered = n_total - sum(unresolved_counts.values())
    print(f"\n-- Coverage (this session): {_pct(covered, n_total):.1f}% - {covered}/{n_total} alarm(s)")
    if unresolved_counts:
        top = unresolved_counts.most_common(3)
        print(f"WARNING: {len(unresolved_counts)} device | label | priority combination(s) without a blueprint. "
              f"Top {len(top)} by alarm count:")
        for label, n in top:
            print(f"  - {label} ({n} alarm(s))")
        if len(unresolved_counts) > len(top):
            print(f"  ... and {len(unresolved_counts) - len(top)} more")
    print_totals(totals, rule_names)
    print(f"Clinical-event log -> {SETTINGS['event_log']}")
    print(f"Rule-firing log -> {SETTINGS['firing_log']}")
    X.summarize_invocations(session_stats)
    print(f"Processing times -> {SETTINGS['arrival_times']}; run manifest -> {manifest_path}")
    print(f"\nTotal wall-clock time: {time.monotonic() - t_start:.1f}s")
    if not (complete and manifest["complete"]):
        print("*** INCOMPLETE RUN: the engine stopped or reported errors (see above); results are partial. "
              "Rerun with resume True to continue. ***")


if __name__ == "__main__":
    run()
