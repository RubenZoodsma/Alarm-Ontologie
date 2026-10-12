"""
execution.py — runs a generated script in Oxigraph and reads the results back.

Each run has one in-memory Oxigraph store (oxigraph_session.OxigraphSession),
fed each stream element's commands in lockstep. Each element's output is
read as it arrives: its trace blocks (read_blocks, for event_log.LiveLog)
and its timing groups (RuleStats); nothing of it is kept. Each rule
invocation is wrapped in a timing group (timed): the engine time of all its
statements, and whether any of its selects answered, give the performance
table.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import event_log as EL
from oxigraph_session import OxigraphSession
from rules import PREFIX_COMMANDS, RULES
from paths import DATA_DIR

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

# Top of every script: the rule files' prefixes, declared once (rules.py).
SCRIPT_PREAMBLE = list(PREFIX_COMMANDS)


TIME_BEGIN = "TIME_BEGIN"
TIME_END = "TIME_END"


def timed(rule: str, invocation: int, lines: list) -> list:
    """`lines` as one timing group of `rule`. Groups with the same rule and
    invocation number add up to one invocation."""
    return [f"echo {TIME_BEGIN} {rule} {invocation}", *lines, f"echo {TIME_END}"]


@dataclass
class Invocation:
    rule: str
    seconds: float   # engine statement time, summed
    hit: bool        # some select in it answered


def parse_invocations(out_lines: list) -> list:
    """Every invocation in the engine's output, from its timing groups."""
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


def make_session(scratch: Path, timeout: float, audit_log: bool = False) -> OxigraphSession:
    """One in-memory Oxigraph store for a run."""
    return OxigraphSession(scratch, timeout, audit_log)


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
    number of milliseconds (rounded to 1 ms, so percentiles are exact over
    the counts) — kept as counts, not per invocation."""

    def __init__(self):
        self.rules: dict = {}   # rule -> {"hits": int, "ms": Counter(ms -> invocations)}

    def merge(self, other: "RuleStats") -> None:
        """Add another run's counts (a parallel worker's)."""
        for rule, st in other.rules.items():
            mine = self.rules.setdefault(rule, {"hits": 0, "ms": Counter()})
            mine["hits"] += st["hits"]
            mine["ms"].update(st["ms"])

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
    """Print per rule: invocations, hits, hit rate, and engine time per
    invocation (total, mean, p95, max)."""
    print("\nRule performance (Oxigraph statement time, rounded to 1 ms; an invocation "
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
