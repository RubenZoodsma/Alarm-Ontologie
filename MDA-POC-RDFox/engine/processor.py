"""
processor.py — the query processor: turns each patient's alarm stream
into one RDFox script, in arrival order.

Per alarm: due window drops, identity resolution, insertion (two phases),
the CAT1 and CAT2 checks with their flags and silences, and the clinical-
event evaluation. The script is written in full before anything runs
(execution.py), so this module never reacts to a query result.
"""

from __future__ import annotations

import itertools
import shutil
import time
from pathlib import Path

import clinical_events as CE
import mint as M
from actions import XSD_DATETIME, cat1_flag_insert, cat1b_withdraw, cat2a_values, cat2b_values, cat2_silence_insert
from execution import FRAMEWORK_FILES, _progress_bar, execute_script
from rules import (APPROXIMATES_COVERED_METRIC_TYPES, ONDEMAND_QUERY_BODIES, ONDEMAND_RULE_NAMES,
                   RULE_FILES, _ALARMPRIO, _alarm_functional_unit, _alarm_metric_types)
from windows import WindowOperator


def build_script(kb, patients: dict, scratch_dir: Path, enabled_rules=None,
                  progress: bool = True, verify_identity: bool = False,
                  dstore: str = "poc") -> tuple:
    """`enabled_rules`: iterable of RULE_FILES keys to import, or None for
    all of them (every rule enabled — the default validation behaviour).

    `dstore`: ONE dstore shared by every patient in `patients`, created
    and populated with the framework/rule files exactly once — not one
    dstore per patient (the original Phase 2 design). Import time itself
    was never the cost (single-digit ms/file, per RDFox's own logging);
    the problem was memory: at the project's 3500-patient target, 3500
    separate dstores each holding an independent copy of the ~3000-triple
    static framework multiplies that memory ~3500x for data that's
    identical across all of them. Safe to share now that every minted
    entity/alarm/message IRI is patient-scoped (see mint.py's MINTING
    section header) — before that fix, two patients sharing a real-corpus
    device_id would have collided onto one IRI in a shared dstore.

    `progress`: print a per-patient header line, then an in-place
    (carriage-return-updated) progress bar as that patient's own alarms
    are minted — never one line per alarm (a real patient can carry tens
    of thousands, see _progress_bar's own call site). Historically
    load-bearing when per-alarm minting ran a real OWL-RL closure
    (~2-3s/call, since removed — see mint.py's module docstring); kept on
    by default since a multi-thousand-patient run is still worth showing
    progress for even at the much lower per-alarm cost minting has now.

    `verify_identity`: development-only — also run the batch
    M.resolve_identity(kb, events_so_far) per alarm and assert it matches
    M.IdentityTracker's incremental result, mirroring how Timeline.
    observe() (op_knowledge.py) was itself originally validated. Doubles
    identity-resolution cost, so leave off by default; only turn on to
    re-confirm the equivalence after touching either implementation.

    KNOWN, HARMLESS FALSE-POSITIVE with this flag: when two of a
    patient's alarms share the exact same `start` instant (real corpus
    data — confirmed on alexKim_03), `events_so_far = [ev for ev in
    events_sorted if ev.start <= e.start]` includes BOTH tied alarms
    for the batch computation, while the incremental tracker has only
    processed the first of the two (in `events_sorted`'s stable order) at
    this exact check point — so the two can briefly disagree on an entry
    for the *other* tied alarm's device_id. Confirmed inert: each alarm's
    own grounding (`particular_iri`) only ever reads its OWN device_id's
    identity entry, never a different device's, and the tracker converges
    to the same content as soon as the second tied alarm is itself
    processed one iteration later. Verified end-to-end (not just via this
    assertion) by re-running poc_entry.py's exact same real-corpus sample
    before and after this port and confirming identical fire counts."""
    rule_names = list(RULE_FILES) if enabled_rules is None else list(enabled_rules)
    lines = []
    checks = []
    file_counter = itertools.count(1)
    t0 = time.monotonic()
    num_patients = len(patients)

    lines.append(f"dstore create {dstore}")
    lines.append(f"active {dstore}")
    for f in FRAMEWORK_FILES:
        lines.append(f"import {f}")
    lines.extend(CE.SCRIPT_PREAMBLE)
    for name in rule_names:
        if name in ONDEMAND_RULE_NAMES or name in CE.EVENT_RULE_NAMES:
            continue  # queried/updated per alarm below, not a standing rule
        lines.append(f"import {RULE_FILES[name]}")

    # Which on-demand rules are enabled for THIS run, grouped by which
    # check/predicate they feed — computed once, not per-alarm, since
    # `rule_names` is fixed for the whole call.
    flagged_active = [name for name in ("cat1a", "cat1b") if name in rule_names]
    silenced_active = [name for name in ("cat2a", "cat2b") if name in rule_names]
    # Clinical-event rules, in evaluation order; raises if a combined rule
    # is enabled without its constituents.
    event_rules = CE.enabled_event_rules(rule_names)

    for pi, (patient, events) in enumerate(patients.items(), start=1):
        events_sorted = sorted(events, key=lambda ev: ev.start)
        if progress:
            print(f"  [{time.monotonic() - t0:7.1f}s] minting patient {pi}/{num_patients} "
                  f"({patient}): {len(events_sorted)} alarm(s)")

        # RDFox's own `echo <token>` shell command (confirmed via `help
        # echo`: "Prints the tokens specified... separated by a single
        # space" — an exact, undecorated line, nothing to disambiguate)
        # gives execute_script an unambiguous per-patient/per-alarm marker
        # to match live in RDFox's streamed stdout — see execute_script's
        # own docstring for why this is the RDFox-execution counterpart to
        # this function's own per-alarm minting progress bar above.
        lines.append(f"echo PATIENT_START:{patient}")

        driver = WindowOperator(kb, scratch_dir, file_counter, event_rules,
                        cat2_lift=bool({"cat2a", "cat2b"} & set(rule_names)))
        n_events = len(events_sorted)
        # Update at most ~100 times per patient, not once per alarm — a
        # real patient can carry tens of thousands of alarms (confirmed
        # directly: one real-corpus patient had 25,793), and printing a
        # full line per alarm at that scale floods the terminal with
        # scrollback rather than showing progress. An in-place bar
        # (carriage return, no newline until the patient is done) shows
        # the same real-time signal in one line instead.
        report_every = max(1, n_events // 100)
        for ei, e in enumerate(events_sorted, start=1):
            if progress and (ei == 1 or ei == n_events or ei % report_every == 0):
                bar = _progress_bar(ei, n_events)
                print(f"\r    {bar} {ei}/{n_events} alarms "
                      f"[{time.monotonic() - t0:7.1f}s]", end="", flush=True)
            driver.flush_due(e.start)
            M.update_identity(kb, e, driver.identity_tracker)
            identity = driver.identity_tracker.identity
            if verify_identity:
                events_so_far = [ev for ev in events_sorted if ev.start <= e.start]
                batch_identity = M.resolve_identity(kb, events_so_far)
                assert identity == batch_identity, (
                    f"incremental identity tracker diverged from resolve_identity's batch "
                    f"computation for {patient} at {e.label}@{e.start}")
            event_kinds = (frozenset(CE.relevant_kinds(kb, e.label, _alarm_metric_types(kb, e), event_rules))
                           if event_rules else frozenset())
            pending = driver.insert_alarm(e, identity, event_kinds)
            lines.extend(driver.commands)
            driver.commands.clear()

            # Check THIS alarm's own firing status right at its arrival —
            # matching assess.py's own timing discipline (mda_poc_
            # assessment.py calls assess() once per arriving alarm, against
            # the situation AT THAT INSTANT). Checking only once at the very
            # end of the whole patient timeline (the original version of
            # this driver did) is wrong: by then every alarm's transient
            # graph — and eventually its persistent graph — has already
            # been dropped, so nothing is left to match against at all.
            #
            # This runs BETWEEN insert_alarm()'s two phases — the arriving
            # alarm's own hasPriority triple isn't inserted yet (see
            # insert_alarm's own docstring for why cat2a's aggregate check
            # needs that) — but every check below is unaffected by
            # hasPriority's absence, so nothing else needs to know or care.
            alarm = pending["alarm"]
            patient_iri = pending["patient"]
            now_literal = f'"{e.start.isoformat()}"^^{XSD_DATETIME}'
            # None of cat1a/cat1b/cat2a/cat2b are
            # standing rules anymore — see ONDEMAND_RULE_NAMES's own
            # module-level comment for why. Each predicate is instead
            # checked via a UNION'd, on-demand query over whichever
            # contributing rules are enabled. Per the plan's §3, each
            # on-demand body now carries its own validUntil FILTER
            # (ONDEMAND_QUERY_BODIES/_append_validity_filters) bound to
            # ?now here — physical graph presence is no longer what makes
            # a match valid, now that eviction can be batched (plan's §4)
            # and may lag a graph's own logical expiry.
            #
            # flaggedLikelyFalsePositive/silencedBy are alarm-scoped
            # (?alarm bound via VALUES to THIS alarm's own IRI).
            #
            # Each group is only emitted when at least one contributing
            # rule is enabled — with none enabled there's nothing to check
            # (matches the old behaviour of the predicate simply never
            # being derived).
            # Emitted as SEPARATE queries per rule, not UNIONed — mirrors
            # cat2a/cat2b's own fix (see this_alarm_silenced's comment
            # below) for the same reason: keeps each rule individually
            # timed/counted for the per-rule instrumentation in
            # execute_script, and avoids relying on RDFox's planner to
            # handle a UNION of differently-shaped bodies well.
            for name in flagged_active:
                branch = f"VALUES (?alarm ?now) {{ (<{alarm}> {now_literal}) }} {ONDEMAND_QUERY_BODIES[name]}"
                lines += CE.trace_block(f"check {len(checks)}",
                                        f"select distinct ?alarm ?witness where {{ {branch} }}")
                checks.append((patient, e.start, "flaggedLikelyFalsePositive", name, ei))
            # Store the flags (see cat1_flag_insert): clinical events ignore
            # flagged alarms, and a later alarm on a cat1b flag's IBP
            # pathway withdraws it. Only heart-rate alarms can be an
            # asystole — a cheap gate for cat1b, the query decides.
            withdraw_kinds = frozenset()
            if "cat1a" in flagged_active:
                lines.append(cat1_flag_insert("cat1a", alarm, pending["tgraph"], now_literal))
            if "cat1b" in flagged_active:
                if "HeartRate" in _alarm_metric_types(kb, e):
                    lines.append(cat1_flag_insert("cat1b", alarm, pending["tgraph"], now_literal))
                if _alarm_functional_unit(kb, e) == "FU_InvasiveBloodPressure":
                    select, delete = cat1b_withdraw(alarm)
                    lines += CE.trace_block(f"withdraw {patient} {e.start.isoformat()}", select)
                    lines.append(delete)
                    # A withdrawn asystole becomes evidence at this moment:
                    # re-evaluate what a heart-rate alarm can support.
                    withdraw_kinds = CE.kinds_supported_by_metric("HeartRate", event_rules)
            # cat2a can only ever match if THIS alarm's own metric type has
            # an mda:approximates mapping at all (see
            # APPROXIMATES_COVERED_METRIC_TYPES's own comment — harmless to
            # keep even now that cat2a is aggregate-based: the arriving-side
            # chain hop still can't bind for an uncovered metric type, so
            # this remains a correct, if now purely cosmetic, short-circuit).
            # cat2b needs no such check: it joins on the metric's own
            # rdf:type directly, a base fact that's never missing.
            this_alarm_silenced = silenced_active
            if "cat2a" in this_alarm_silenced:
                metric_types = _alarm_metric_types(kb, e)
                prio = pending["incoming_prio"]
                # An Unknown priority cannot be shown to be equal or lower
                # than anything: never silenced by CAT2a (agreed 2026-09-21).
                if (prio is None or str(prio) == f"{_ALARMPRIO}Unknown"
                        or (metric_types and not (metric_types & APPROXIMATES_COVERED_METRIC_TYPES))):
                    this_alarm_silenced = [n for n in this_alarm_silenced if n != "cat2a"]
            # cat2a's and cat2b's aggregate checks are emitted as SEPARATE
            # queries, NOT combined via UNION into one — confirmed
            # empirically (real patient 2826, alarms 0-900): each alone
            # (and both loaded as standing rules but only one checked) ran
            # in ~2-3s for 900 alarms; UNIONing their two branches together
            # into one query reproducibly cost ~90-97s for the same 900
            # alarms. RDFox's planner produces a badly inefficient plan for
            # this specific combination — not something either branch does
            # on its own. checks gets a 5-tuple: name as the 4th element so
            # cat2a's and cat2b's entries don't collide as the SAME dict
            # key in execute_script's `dict(zip(checks, counts))` (which
            # would silently drop one), and each alarm's own per-patient
            # sequence index (`ei`) as the 5th so two alarms sharing the
            # exact same `start` instant — a real, non-rare occurrence in
            # the real corpus (confirmed on alexKim_03, see build_script's
            # own `verify_identity` docstring) — don't ALSO collide with
            # each other, which silently dropped one of their results
            # before this index was added. See run()'s and poc_entry.py's
            # report()'s own "at most one per alarm" grouping for how
            # cat2a/cat2b are recombined into a single silencedBy verdict
            # per alarm, matching the old single-UNIONed-query semantics —
            # unaffected by the extra tuple element, since it only reads
            # k[0]/k[1].
            for name in this_alarm_silenced:
                if name == "cat2a":
                    values = cat2a_values(alarm, now_literal, pending["incoming_prio"])
                    lines += CE.trace_block(f"check {len(checks)}",
                                            f"select distinct ?alarm ?witness where {{ "
                                            f"{values} {ONDEMAND_QUERY_BODIES['cat2a']} }}")
                    lines.append(cat2_silence_insert("cat2a", values, pending["tgraph"]))
                else:
                    values = cat2b_values(alarm, now_literal)
                    lines += CE.trace_block(f"check {len(checks)}",
                                            f"select distinct ?alarm ?witness where {{ "
                                            f"{values} {ONDEMAND_QUERY_BODIES['cat2b']} }}")
                    lines.append(cat2_silence_insert("cat2b", values, pending["tgraph"]))
                checks.append((patient, e.start, "silencedBy", name, ei))
            driver.complete_alarm(pending)
            lines.extend(driver.commands)
            driver.commands.clear()
            # This arrival may start or extend a clinical event of this patient
            # (or, through a cat1b withdrawal, let a heart-rate alarm count).
            lines.extend(CE.evaluate_commands(event_kinds | withdraw_kinds, event_rules, patient, e.start))
            lines.append(f"echo ALARM_DONE:{patient}")
        if progress:
            print()  # finalize this patient's in-place progress line
        driver.flush_all()
        lines.extend(driver.commands)

    if progress:
        print(f"  [{time.monotonic() - t0:7.1f}s] minting done for {num_patients} patient(s)")
    lines.append("quit")
    return "\n".join(lines), checks


def run_batched(kb, patients: dict, scratch_root: Path, batch_size: "int | None" = None,
                 enabled_rules=None, progress: bool = True, trace: list | None = None) -> tuple:
    """Process `patients` in bounded-size batches instead of one script for
    every patient in the run, returning the merged
    ({check_key: answer_count}, {check_key: seconds}) across all batches.

    `batch_size`: None (or >= len(patients)) processes everyone in a single
    batch — today's behaviour, unchanged. A smaller number bounds how much
    is held in memory/disk at once (every batch's trig files, its script
    text) and how many patients' worth of data live in the shared dstore
    simultaneously, which matters at the project's 3500-patient target —
    holding the entire run as one script/dstore doesn't scale the way it
    does for a handful of patients.

    Deliberately NOT the long-lived-streaming-subprocess design (a
    persistent RDFox process fed incrementally) — that needs its own
    spike first (unverified I/O territory: every RDFox invocation tested
    so far is one-shot blocking, write-then-close-stdin-then-read-all-of-
    stdout; a persistent open-stdin process risks output buffering and
    writer/reader deadlock that hasn't been exercised at all). This
    batches across separate, already-proven `subprocess.run` calls
    instead — zero new subprocess-I/O risk, and each batch still gets its
    own shared dstore (item 2), just scoped to that batch's patients
    rather than the whole run.
    """
    items = list(patients.items())
    size = batch_size if batch_size else len(items)
    counts_by_check: dict = {}
    timings_by_check: dict = {}
    num_batches = -(-len(items) // size) if items else 0  # ceil div
    for bi in range(0, len(items), size):
        batch = dict(items[bi:bi + size])
        batch_num = bi // size + 1
        if progress:
            print(f"[batch {batch_num}/{num_batches}] {len(batch)} patient(s)")
        batch_scratch = scratch_root / f"batch_{batch_num:04d}"
        if batch_scratch.exists():
            shutil.rmtree(batch_scratch)
        batch_scratch.mkdir(parents=True)
        script_text, checks = build_script(kb, batch, batch_scratch,
                                            enabled_rules=enabled_rules, progress=progress)
        # Scaled by TOTAL ALARM COUNT in the batch, not patient count —
        # real-corpus patients have wildly uneven alarm density (confirmed
        # directly: one real patient carried 25,793 alarms against ~10-16
        # for the small fabricated/excerpt datasets), so a handful of
        # patients can still mean a huge script. A patient-count-scaled
        # timeout (30s/patient) genuinely timed out RDFox mid-execution on
        # a real 5-patient/44,375-alarm batch at its 150s cap. ~10ms/alarm
        # gives real headroom over that observed case without being
        # wastefully large for small batches.
        #
        # Floor raised 120s -> 600s -> 1500s (this session): even single-patient
        # batches (batch_size=1) were still timing out at 120s on patients
        # with a large concurrently-active cluster on one device (e.g.
        # patient 2826's ABP-verkleinen storm, 59 concurrent alarms) —
        # cat1a/cat1b's cross-alarm consolidation (see cat1a_signal_
        # quality.dlog's own header) genuinely needs independent per-hop
        # graph variables for correctness, so unlike cat2a/cat2b this cost
        # has no query-shape fix yet. 600s is a stopgap to let those
        # patients actually finish instead of silently truncating results —
        # not a fix for the underlying cost.
        total_alarms = sum(len(events) for events in batch.values())
        timeout = max(1500, total_alarms // 100)
        batch_counts, batch_timings = execute_script(script_text, checks, batch_scratch,
                                                      timeout=timeout, patients=batch,
                                                      trace=trace)
        counts_by_check.update(batch_counts)
        timings_by_check.update(batch_timings)
    return counts_by_check, timings_by_check
