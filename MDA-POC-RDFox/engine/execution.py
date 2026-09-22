"""
execution.py — runs a generated script in RDFox and reads the results back.

RDFox runs as a sandbox process fed the script on stdin; results come back
as trace blocks (event_log.trace_block).
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

# Top of every script: answers as TSV, and the rule files' prefixes
# declared once (rules.py).
SCRIPT_PREAMBLE = (["set query.answer-format text/tab-separated-values", "set output out"]
                   + PREFIX_COMMANDS)


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    filled = width if total <= 0 else min(width, int(width * done / total))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _reader_thread(pipe, q: queue.Queue) -> None:
    """Forward RDFox's output lines to `q`, then None on EOF. A thread, so
    the timeout also fires when RDFox goes silent."""
    for line in pipe:
        q.put(line.rstrip("\n"))
    q.put(None)


def execute_script(script_text: str, checks: list, scratch: Path, timeout: int = 600,
                    progress: bool = True, patients: dict | None = None,
                    trace: list | None = None) -> tuple:
    """Run `script_text` in RDFox. Returns ({check_key: answer_count},
    {check_key: seconds}) for `checks`, the time as RDFox reports it.

    `progress`: a live per-patient bar, driven by the PATIENT_START /
    ALARM_DONE echo lines build_script emits (RDFox flushes per line).
    `patients`: {patient: events}, only to size that bar.
    `trace`: if given, extended with every trace block as
    (tag, check_key or None, rows) — input for event_log.

    On timeout RDFox is killed and the output so far is still parsed.
    """
    script_path = scratch / "replay.rdfox"
    script_path.write_text(script_text)

    total_per_patient = {p: len(evs) for p, evs in (patients or {}).items()}
    num_patients = len(total_per_patient)

    # RDFox takes no script-file argument: the script goes in on stdin,
    # whose EOF also ends the shell.
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

    # A check's count is its trace block's number of rows.
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
    """Print per CAT1/CAT2 rule: invocations, hits, total time, and average
    time per call and per hit."""
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
