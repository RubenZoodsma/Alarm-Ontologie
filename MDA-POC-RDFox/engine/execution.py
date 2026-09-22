"""
execution.py — running a generated script and reading its results.

The engine behind it is RDFox (a sandbox process fed the script on stdin);
results come back as trace blocks (event_log.trace_block) parsed by
event_log.parse_trace_blocks.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

import event_log as EL
from rules import PREFIX_COMMANDS
from paths import DATA_DIR, LICENSE, RDFOX_BIN

FRAMEWORK_FILES = [
    DATA_DIR / "ontology.ttl",
    DATA_DIR / "vocab_generated.ttl",
    DATA_DIR / "clinicalEvent_vocab.ttl",
    DATA_DIR / "inference.ttl",
    DATA_DIR / "priority_rank.ttl",
    # mdapoc: — the POC's own terms (silencing, false-positive flags),
    # outside the mda: ontology.
    DATA_DIR / "mdapoc.ttl",
]

# Set once at the top of every script: every select prints its answers as
# TSV (event_log.trace_block wraps each one), and the prefixes the rule and
# action files use are declared once (rules.py: the RDFox shell rejects a
# PREFIX clause inside a one-line command).
SCRIPT_PREAMBLE = (["set query.answer-format text/tab-separated-values", "set output out"]
                   + PREFIX_COMMANDS)


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = width if total <= 0 else min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _reader_thread(pipe, q: queue.Queue) -> None:
    """Runs in a background thread: forward every line RDFox prints to
    `q`, then a final None sentinel on EOF. Needed (rather than just
    iterating `pipe` in the main thread) so execute_script can still
    enforce a wall-clock timeout even if RDFox goes completely silent —
    an in-loop time check only runs between lines received, which never
    fires at all if no more lines ever arrive."""
    for line in pipe:
        q.put(line.rstrip("\n"))
    q.put(None)


def execute_script(script_text: str, checks: list, scratch: Path, timeout: int = 600,
                    progress: bool = True, patients: dict | None = None,
                    trace: list | None = None) -> tuple:
    """Run `script_text` against a real RDFox instance and return
    ({check_key: answer_count}, {check_key: seconds}) for every entry in
    `checks`, in order — the second dict is per-statement wall-clock time
    as RDFox itself reports it ("Total statement evaluation time"), used
    by summarize_rule_timings for per-rule trigger-count/duration
    analysis.

    Shared by regression.py, poc_entry.py and the validation scripts so the RDFox invocation/output-parsing
    logic — both non-obvious, see the comments inline — lives in exactly
    one place.

    `progress`: render an in-place, per-patient progress bar as RDFox
    actually executes the script — the counterpart to build_script's own
    per-alarm minting bar, for the step that follows it. Confirmed
    directly (not assumed) that this is possible at all: RDFox flushes
    its stdout per-line even when piped, not just when attached to a
    TTY — verified with an `echo`-then-`sleep 3000`-then-`echo` script,
    where the first echo arrived immediately and the second only after
    the full 3s, ruling out RDFox block-buffering its own output until
    exit (the common failure mode that would have made "live" progress
    silently do nothing until the process ends anyway). Driven by the
    `echo PATIENT_START:<patient>` / `echo ALARM_DONE:<patient>` marker
    lines build_script emits into the script for exactly this purpose —
    `echo`'s own RDFox semantics (`help echo`: "Prints the tokens
    specified... separated by a single space") make these unambiguous,
    exact lines to match on, unlike inferring progress from counting
    select/DELETE-WHERE result lines (which don't carry a patient
    identity at all).

    `patients`: the same {patient: events} dict build_script was called
    with, used only to size the progress bar (alarm count per patient).

    `trace`: if given, extended with every trace block printed during the
    run, as (tag, check_key or None, rows) — the input of event_log's
    event_records/firing_records. Optional so existing callers are
    unaffected.
    """
    script_path = scratch / "replay.rdfox"
    script_path.write_text(script_text)

    total_per_patient = {p: len(evs) for p, evs in (patients or {}).items()}
    num_patients = len(total_per_patient)

    # RDFox's CLI has no "run this script file" positional argument — any
    # argument after <root> in sandbox/shell mode is itself a SHELL COMMAND
    # (confirmed via `RDFox -help`: "all supplied commands are executed").
    # The working pattern is piping the script's own text via stdin, which
    # also closes stdin on EOF (no interactive prompt to hang on) without
    # needing an explicit `< /dev/null`.
    t0 = time.monotonic()
    script_file = script_path.open()
    process = subprocess.Popen(
        [str(RDFOX_BIN), "sandbox", str(scratch)],
        env={"RDFOX_LICENSE_FILE": str(LICENSE)},
        stdin=script_file, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(process.stdout, q), daemon=True)
    reader.start()

    out_lines: list = []
    current_patient = None
    patient_index = 0
    alarms_done = 0
    timed_out = False
    deadline = t0 + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Used to raise TimeoutExpired here, which meant a stalling
            # run produced ZERO diagnostic data — exactly the case where
            # per-rule timing (summarize_rule_timings) matters most.
            # Instead: kill the process, and fall through to the same
            # parsing logic below on whatever output was captured before
            # the kill, so every check/alarm that DID complete still gets
            # counted and timed. The caller can tell a partial result from
            # a complete one via the printed warning below (there's no
            # separate return signal — the point is graceful degradation,
            # not a new error-handling contract every caller must adopt).
            timed_out = True
            process.kill()
            break
        try:
            line = q.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        out_lines.append(line)

        if not progress:
            continue
        if line.startswith("PATIENT_START:"):
            current_patient = line.split(":", 1)[1]
            patient_index += 1
            alarms_done = 0
            total = total_per_patient.get(current_patient, 0)
            print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                  f"{_progress_bar(0, total)} 0/{total} alarms "
                  f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)
        elif line.startswith("ALARM_DONE:"):
            total = total_per_patient.get(current_patient, 0)
            report_every = max(1, total // 100)
            alarms_done += 1
            if alarms_done == total or alarms_done % report_every == 0:
                print(f"\r  RDFox: patient {patient_index}/{num_patients} ({current_patient}) "
                      f"{_progress_bar(alarms_done, total)} {alarms_done}/{total} alarms "
                      f"[{time.monotonic() - t0:6.1f}s]", end="", flush=True)

    process.wait()
    script_file.close()
    if progress and num_patients:
        print()  # finalize the in-place line
    if timed_out:
        print(f"  [{time.monotonic() - t0:7.1f}s] *** TIMED OUT after {timeout}s — "
              f"RDFox killed mid-execution. Parsing partial output below: every check "
              f"that DID complete before the kill is still counted/timed; anything "
              f"after the stall is simply absent from the result. ***")
    else:
        print(f"  [{time.monotonic() - t0:7.1f}s] RDFox execution finished "
              f"({len(checks)} check(s) run)")
    output = "\n".join(out_lines)

    # Every select runs inside a trace block (event_log.trace_block),
    # with output switched on for the whole script, so results are read
    # from the rows each block printed — not from RDFox's "Number of query
    # answers" statistics, which updates and deletes print too. A check's
    # count is its number of rows; its timing is the block's own "Total
    # statement evaluation time".
    blocks = EL.parse_trace_blocks(out_lines)
    counts_by_check, timings_by_check = {}, {}
    collected = []
    for tag, rows, seconds in blocks:
        if tag.startswith("check "):
            key = checks[int(tag.split(" ", 1)[1])]
            counts_by_check[key] = len(rows)
            timings_by_check[key] = seconds
            collected.append(("check", key, rows))
        else:
            collected.append((tag, None, rows))

    error_lines = [l for i, l in enumerate(out_lines)
                   if l.startswith("An error occurred")
                   or (i and out_lines[i - 1].startswith("An error occurred"))]
    if error_lines:
        print("--- errors seen in RDFox output ---")
        for l in error_lines:
            print(" ", l)

    if len(counts_by_check) != len(checks) and not timed_out:
        print(f"  WARNING: {len(counts_by_check)} check result(s) for {len(checks)} check(s) — "
              f"some check blocks are missing; counts below are incomplete.")
    if trace is not None:
        trace.extend(collected)
    return counts_by_check, timings_by_check


def summarize_rule_timings(counts_by_check: dict, timings_by_check: dict) -> None:
    """Per-rule breakdown across a whole run: how many times each
    on-demand check (cat1a/cat1b/cat2a/cat2b) was evaluated,
    how many of those evaluations actually matched ("hits"), total time
    RDFox itself reports spending on that check's queries, and average
    time per invocation vs. average time per hit — the latter is what
    actually answers "does this rule get slower when it fires, or is
    cost independent of outcome." Keyed by (kind, name) — e.g.
    ("silencedBy", "cat2a") — since every checks tuple now carries a rule
    name as its 4th element (see build_script's own comment on why:
    keeping every on-demand check as its own separate query, one rule
    name per query, both to avoid the confirmed cat2a/cat2b UNION
    regression and to make exactly this kind of per-rule instrumentation
    possible without extra bookkeeping)."""
    groups: dict = {}
    for key, t in timings_by_check.items():
        kind, name = key[2], key[3]
        g = groups.setdefault((kind, name), {"invocations": 0, "hits": 0, "total_s": 0.0, "hit_s": 0.0})
        g["invocations"] += 1
        if t is not None:
            g["total_s"] += t
        n = counts_by_check.get(key, 0)
        if n and n > 0:
            g["hits"] += 1
            if t is not None:
                g["hit_s"] += t

    print("\nPer-rule timing summary:")
    print(f"  {'rule':<28} {'invocations':>12} {'hits':>8} {'total_s':>10} "
          f"{'avg_s/call':>12} {'avg_s/hit':>10}")
    for (kind, name), g in sorted(groups.items()):
        avg_call = g["total_s"] / g["invocations"] if g["invocations"] else 0.0
        avg_hit = g["hit_s"] / g["hits"] if g["hits"] else 0.0
        print(f"  {kind + '/' + name:<28} {g['invocations']:>12} {g['hits']:>8} "
              f"{g['total_s']:>10.3f} {avg_call:>12.5f} {avg_hit:>10.5f}")
