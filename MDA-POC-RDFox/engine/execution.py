"""
execution.py — runs a generated script in RDFox and reads the results back.

RDFox runs as one sandbox process per run (RDFoxSession), fed each stream
element's commands in lockstep. Each element's output is read as it
arrives: its trace blocks (read_blocks, for event_log.LiveLog) and its
timing groups (RuleStats); nothing of it is kept. Each rule invocation is wrapped in
a timing group (timed): the RDFox time of all its statements, and whether
any of its selects answered, give the performance table.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections import Counter
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

    With `audit_log`, everything sent is logged to replay.rdfox, each
    imported TriG file's content under it as `#` comments (large: ~17 KB per
    alarm). Imported TriG files are deleted once RDFox has imported them. A sync that exceeds `timeout` seconds kills RDFox;
    `stopped` then says why (also when RDFox exits), and nothing more is
    sent."""

    def __init__(self, scratch: Path, timeout: float, audit_log: bool = False):
        self.scratch = scratch
        self.timeout = timeout
        self.stopped: str | None = None
        self._log = (scratch / "replay.rdfox").open("w", encoding="utf-8") if audit_log else None
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
        if self.stopped is not None:
            return
        try:
            for line in lines:
                self._process.stdin.write(line + "\n")
                if self._log is not None:
                    self._log.write(line + "\n")
                if line.startswith("import "):
                    path = Path(line[len("import "):])
                    if path.parent == self.scratch:
                        if self._log is not None:
                            self._log.writelines(f"# {t}\n" for t in path.read_text().splitlines())
                        self._imported.append(path)
            self._process.stdin.flush()
        except BrokenPipeError:
            self.stopped = "RDFox exited"

    def sync(self) -> list:
        """Wait until RDFox has executed everything sent; return its output
        since the last sync. On timeout, kill RDFox and set `stopped`."""
        self._syncs += 1
        marker = f"SYNC {self._syncs}"
        self.send([f"echo {marker}"])
        if self.stopped is not None:
            return []
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
        if self.stopped is None:
            for path in self._imported:
                path.unlink()
            self._imported = []
        return received

    def close(self) -> list:
        """Quit RDFox; return its output since the last sync."""
        received = []
        if self.stopped is None:
            self.send(["quit"])
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                pass
            while (line := self._q.get()) is not None:
                received.append(line)
        self._process.wait()
        if self._log is not None:
            self._log.close()
        return received


def read_blocks(lines: list, checks: dict) -> list:
    """The trace blocks in `lines`, as (tag, check_key or None, rows) — input
    for event_log.LiveLog. A check block takes (and removes) its key from
    `checks`."""
    blocks = []
    for tag, rows, _seconds in EL.parse_trace_blocks(lines):
        if tag.startswith("check "):
            blocks.append(("check", checks.pop(int(tag.split(" ", 1)[1])), rows))
        else:
            blocks.append((tag, None, rows))
    return blocks


class RuleStats:
    """Per rule, over a whole run: invocations, hits, and how many took each
    number of milliseconds (RDFox's 1 ms resolution, so percentiles are
    exact) — kept as counts, not per invocation."""

    def __init__(self):
        self.rules: dict = {}   # rule -> {"hits": int, "ms": Counter(ms -> invocations)}

    def add(self, lines: list) -> None:
        """The invocations in one element's output (parse_invocations)."""
        for inv in parse_invocations(lines):
            stats = self.rules.setdefault(inv.rule, {"hits": 0, "ms": Counter()})
            stats["hits"] += inv.hit
            stats["ms"][round(1000 * inv.seconds)] += 1


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


def _percentile(ms: Counter, q: float) -> int:
    """Nearest-rank percentile of the counted millisecond values."""
    rank, seen = max(1, -(-q * sum(ms.values()) // 1)), 0
    for value in sorted(ms):
        seen += ms[value]
        if seen >= rank:
            return value
    return 0


def summarize_invocations(stats: RuleStats) -> None:
    """Print per rule: invocations, hits, hit rate, and RDFox time per
    invocation (total, mean, p95, max)."""
    print("\nRule performance (RDFox statement time, 1 ms resolution; an invocation "
          "is every statement of one rule at one moment):")
    print(f"  {'rule':<28} {'invocations':>11} {'hits':>7} {'hit rate':>9} {'total s':>9} "
          f"{'mean ms':>8} {'p95 ms':>7} {'max ms':>7}")
    order = _table_order()
    for rule in [r for r in order if r in stats.rules] + sorted(set(stats.rules) - set(order)):
        ms, hits = stats.rules[rule]["ms"], stats.rules[rule]["hits"]
        n, total_ms = sum(ms.values()), sum(value * k for value, k in ms.items())
        print(f"  {rule:<28} {n:>11} {hits:>7} {100 * hits / n:>8.1f}% {total_ms / 1000:>9.3f} "
              f"{total_ms / n:>8.2f} {_percentile(ms, 0.95):>7} {max(ms):>7}")
    print("  hit —")
    for who, what in HIT_MEANING:
        print(f"    {who + ':':<28} {what}")
