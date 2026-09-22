"""
execution.py — runs a generated script in RDFox and reads the results back.

RDFox runs as a sandbox process fed the script on stdin; results come back
as trace blocks (event_log.trace_block). Each rule invocation is wrapped in
a timing group (timed): the RDFox time of all its statements, and whether
any of its selects answered, give the performance table.
"""

from __future__ import annotations

import math
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import event_log as EL
from rules import PREFIX_COMMANDS, RULES
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


TIME_BEGIN = "TIME_BEGIN"
TIME_END = "TIME_END"


def timed(rule: str, invocation: int, lines: list) -> list:
    """`lines` as one timing group of `rule`. Groups with the same rule and
    invocation number add up to one invocation."""
    return [f"echo {TIME_BEGIN} {rule} {invocation}", *lines, f"echo {TIME_END}"]


@dataclass
class Invocation:
    rule: str
    seconds: float   # RDFox statement time, summed; 1 ms resolution
    hit: bool        # some select in it answered


def parse_invocations(out_lines: list) -> list:
    """Every invocation in RDFox's output, from its timing groups."""
    found: dict = {}
    current = None
    for line in out_lines:
        if line.startswith(TIME_BEGIN + " "):
            _, rule, number = line.split(" ")
            current = found.setdefault((rule, number), Invocation(rule, 0.0, False))
        elif line == TIME_END:
            current = None
        elif current is not None:
            if line.startswith("Total statement evaluation time"):
                current.seconds += float(line.split(":", 1)[1].strip().split()[0])
            elif line.startswith("<") or line.startswith('"'):
                current.hit = True
    return list(found.values())


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
    """Run `script_text` in RDFox. Returns its invocations (parse_invocations).

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
    collected, n_checks = [], 0
    for tag, rows, _seconds in EL.parse_trace_blocks(out_lines):
        if tag.startswith("check "):
            collected.append(("check", checks[int(tag.split(" ", 1)[1])], rows))
            n_checks += 1
        else:
            collected.append((tag, None, rows))

    error_lines = [l for i, l in enumerate(out_lines)
                   if l.startswith("An error occurred")
                   or (i and out_lines[i - 1].startswith("An error occurred"))]
    if error_lines:
        print("--- errors seen in RDFox output ---")
        for l in error_lines:
            print(" ", l)

    if n_checks != len(checks) and not timed_out:
        print(f"  WARNING: {n_checks} check result(s) for {len(checks)} check(s) — "
              f"some check blocks are missing; results below are incomplete.")
    if trace is not None:
        trace.extend(collected)
    return parse_invocations(out_lines)


# Table order and what a hit means, per action.
_EXTRA_RULES = {"cat1b": "cat1b_withdraw", "cat2b": "cat2_lift"}
HIT_MEANING = [
    ("CAT1, CAT2", "the check matched: a flag or silence is stored"),
    ("cat1b_withdraw, cat2_lift", "a flag was withdrawn, a silence lifted"),
    ("clinical events", "the criterion held: the event started or went on"),
]


def _table_order() -> list:
    order = []
    for action in ("flag", "silence", "episode"):
        for r in RULES.values():
            if r.action == action:
                order.append(r.name)
                if r.name in _EXTRA_RULES:
                    order.append(_EXTRA_RULES[r.name])
    return order


def _percentile(sorted_values: list, q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return sorted_values[max(0, math.ceil(q * len(sorted_values)) - 1)]


def summarize_invocations(invocations: list) -> None:
    """Print per rule: invocations, hits, hit rate, and RDFox time per
    invocation (total, mean, p95, max)."""
    by_rule: dict = {}
    for inv in invocations:
        by_rule.setdefault(inv.rule, []).append(inv)
    print("\nRule performance (RDFox statement time, 1 ms resolution; an invocation "
          "is every statement of one rule at one moment):")
    print(f"  {'rule':<28} {'invocations':>11} {'hits':>7} {'hit rate':>9} {'total s':>9} "
          f"{'mean ms':>8} {'p95 ms':>7} {'max ms':>7}")
    for rule in [r for r in _table_order() if r in by_rule] + sorted(set(by_rule) - set(_table_order())):
        invs = by_rule[rule]
        ms = sorted(1000 * i.seconds for i in invs)
        hits = sum(i.hit for i in invs)
        print(f"  {rule:<28} {len(invs):>11} {hits:>7} {100 * hits / len(invs):>8.1f}% "
              f"{sum(ms) / 1000:>9.3f} {sum(ms) / len(ms):>8.2f} {_percentile(ms, 0.95):>7.0f} "
              f"{ms[-1]:>7.0f}")
    print("  hit —")
    for who, what in HIT_MEANING:
        print(f"    {who + ':':<28} {what}")
