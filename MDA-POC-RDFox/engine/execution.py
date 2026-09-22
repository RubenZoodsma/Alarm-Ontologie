"""
execution.py — runs a generated script in RDFox and reads the results back.

RDFox runs as one sandbox process per run (RDFoxSession), fed each stream
element's commands in lockstep; results come back as trace blocks
(event_log.trace_block). Each rule invocation is wrapped in
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


class RDFoxSession:
    """One RDFox process for a whole run, fed in lockstep: send() a stream
    element's commands, sync() until RDFox has executed them, then the next.

    Everything sent is logged to `log` (replay.rdfox), each imported TriG
    file's content under it as `#` comments; the file itself is deleted once
    RDFox has imported it. A sync that exceeds `timeout` seconds kills RDFox;
    `stopped` then says why, and the output so far can still be read."""

    def __init__(self, scratch: Path, timeout: float):
        self.scratch = scratch
        self.timeout = timeout
        self.out_lines: list = []
        self.stopped: str | None = None
        self._log = (scratch / "replay.rdfox").open("w", encoding="utf-8")
        self._imported: list = []
        self._syncs = 0
        self._process = subprocess.Popen(
            [str(RDFOX_BIN), "sandbox", str(scratch)],
            env={"RDFOX_LICENSE_FILE": str(LICENSE)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        """Forward RDFox's output lines to the queue, then None on EOF."""
        for line in self._process.stdout:
            self._q.put(line.rstrip("\n"))
        self._q.put(None)

    def send(self, lines: list) -> None:
        for line in lines:
            self._process.stdin.write(line + "\n")
            self._log.write(line + "\n")
            if line.startswith("import "):
                path = Path(line[len("import "):])
                if path.parent == self.scratch:
                    self._log.writelines(f"# {t}\n" for t in path.read_text().splitlines())
                    self._imported.append(path)
        self._process.stdin.flush()

    def sync(self) -> list:
        """Wait until RDFox has executed everything sent; return its output
        since the last sync. On timeout, kill RDFox and set `stopped`."""
        self._syncs += 1
        marker = f"SYNC {self._syncs}"
        self.send([f"echo {marker}"])
        received, deadline = [], time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stopped = f"timed out after {self.timeout:.0f} s"
                self._process.kill()
                break
            try:
                line = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                self.stopped = "RDFox exited"
                break
            if line == marker:
                break
            received.append(line)
        self.out_lines += received
        if self.stopped is None:
            for path in self._imported:
                path.unlink()
            self._imported = []
        return received

    def close(self) -> list:
        """Quit RDFox; return all its output."""
        if self.stopped is None:
            self.send(["quit"])
            self._process.stdin.close()
            while (line := self._q.get()) is not None:
                self.out_lines.append(line)
        self._process.wait()
        self._log.close()
        return self.out_lines


def read_results(out_lines: list, checks: list, trace: list | None = None,
                 complete: bool = True) -> list:
    """Parse a run's output: extend `trace` with every trace block as (tag,
    check_key or None, rows) — input for event_log — and return the
    invocations (parse_invocations)."""
    collected, n_checks = [], 0
    for tag, rows, _seconds in EL.parse_trace_blocks(out_lines):
        if tag.startswith("check "):
            collected.append(("check", checks[int(tag.split(" ", 1)[1])], rows))
            n_checks += 1
        else:
            collected.append((tag, None, rows))

    if n_checks != len(checks) and complete:
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
