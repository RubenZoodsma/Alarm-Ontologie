"""
cases.py — verification cases: one YAML file per case in
MDA-POC/validation/cases/<rule>/ (one folder per rule), each holding its input stream, its expected
conclusions and their rationale. Read by regression.py (to run them) and
MANUSCRIPT/scripts/cases_appendix.py (to report them).

A case, in the paper's vocabulary rather than the engine's:

  id: cat1a_pos                     # also the patient id of the replay
  title: ...                        # one line
  rule: 1a                          # 1a 1b 2a 2b 3a 3b, or events, engine, scenario
  purpose: confirm                  # confirm | falsify | edge | scenario
  premise: [P5a]                    # optional: premise id(s) of the rule's sheet in validation/case_design.xlsx
  pairs_with: cat1a_c1              # optional: the confirm case a falsify case differs from
  status: active                    # active | expected-fail | draft
  review: pending                   # pending | done: the team's review of the case
  rationale: {clinical: ..., technical: ...}
  author: ...
  start_at: "08:00:00"              # clock time of offset 0 (default 08:00:00)
  strict: true                      # default true: any conclusion not listed fails
  stream:                           # the alarms, as the devices report them
    - {id: a1, device: Monitor_00, label: "PHILIPSMONITOR - SpO2 laag", start: "0s", end: "2m"}
  expect:                           # see ASSERTIONS
    - {silenced: a1, reason: likely-invalid, rule: 1a, by: [a2]}

Times are offsets from start_at ("0s", "30s", "1m30s", "1h") or clock
times ("08:00:30"). Always quote them: YAML reads 08:00:30 as a number.
The priority of an alarm is not part of a case: it follows from its label
(the alarm type in the knowledge base).

ASSERTIONS (one key names the kind; the other keys are optional filters):

  forwarded: a3                      no silence was ever set on a3
  silenced: a2                       a silence was set on a2 at its arrival
      reason: likely-invalid | redundant
      rule:   1a | 1b | 2a | 2b
      by:     [a1, ...]              exactly these alarms justified it
  lifted: a2                         a silence on a2 was lifted before it ended
      rule:   1b | 2                 1b: the flag withdrawn; 2: a redundancy lifted
      by:     [a3]                   the alarm that ended (2) or arrived (1b)
      at:     "40s"
  interpretation: VentilationFailure a clinical event of this kind was added
      from: "30s"   until: "1m40s"
      evidence: [a5, a6]             exactly these alarms supported it
  no_interpretation: CardiacArrest   no clinical event of this kind
  not_evidence: a2                   a2 supports no clinical event

With strict: true, every silence, lift and clinical event the system
produces must match an assertion of the case; anything else fails the
case's "no other conclusions" check. Alarms that are not mentioned must
then simply be forwarded.

Start a new case from validation/cases/CASE_Template.yaml, saved as
validation/cases/<rule>/<id>.yaml: the folder must equal the case's rule
(files named CASE_* are skipped by the runner). The cases before 2026-11-26 are retired
to old/MDA-POC/validation/cases/.

Status: active cases count towards the result; expected-fail cases state
behavior the rules do not (yet) have, and are reported as XFAIL (or XPASS
when they unexpectedly pass); draft cases are validated but not run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

import event_log as EL
import mint as M
import rules as RU
from paths import POC_DIR
from stream import Event

CASES_DIR = POC_DIR / "validation" / "cases"
CASE_DESIGN = POC_DIR / "validation" / "case_design.xlsx"   # premises per rule (one sheet each)
RESULTS_DIR = POC_DIR / "validation" / "results"
BASE_DATE = "2026-01-01"
DEFAULT_START = "08:00:00"

RULES = {"1a", "1b", "2a", "2b", "3a", "3b", "events", "engine", "scenario"}
PURPOSES = {"confirm", "falsify", "edge", "scenario"}
STATUSES = {"active", "expected-fail", "draft"}
REVIEWS = {"pending", "done"}

# Paper vocabulary -> engine vocabulary.
REASON_KIND = {"likely-invalid": "flaggedLikelyFalsePositive", "redundant": "silencedBy"}
KIND_REASON = {v: k for k, v in REASON_KIND.items()}
SILENCE_RULE = {"1a": "cat1a", "1b": "cat1b", "2a": "cat2a", "2b": "cat2b"}
LIFT_RULE = {"1b": EL.WITHDRAWN_RULE, "2": EL.LIFTED_RULE}
ENGINE_SILENCE_RULE = {v: k for k, v in SILENCE_RULE.items()}
ENGINE_LIFT_RULE = {v: k for k, v in LIFT_RULE.items()}
EVENT_KINDS = set(RU.RULE_BY_KIND)

ASSERTION_KEYS = {
    "forwarded": set(),
    "silenced": {"reason", "rule", "by"},
    "lifted": {"rule", "by", "at"},
    "interpretation": {"from", "until", "evidence"},
    "no_interpretation": set(),
    "not_evidence": set(),
}


class CaseError(ValueError):
    """A case file that cannot be run as written."""


# --------------------------------------------------------------------
# Premises
# --------------------------------------------------------------------

_PREMISE_ID = re.compile(r"^P\d+[a-z]?$")


def load_premises(rule: str) -> dict:
    """{premise id: (premise, meaning)} from the sheet named `rule` in
    validation/case_design.xlsx: the premise table, i.e. the unbroken run of
    rows whose column A holds a premise id (P1, P2, ...) directly below the
    first row whose column A reads "ID". Anything after that run (open
    questions, the case and coverage tables) is not read, whatever its
    headings. {} when the rule has no sheet."""
    if not CASE_DESIGN.exists():
        return {}
    from openpyxl import load_workbook
    wb = load_workbook(CASE_DESIGN, read_only=True)
    if rule not in wb.sheetnames:
        wb.close()
        return {}
    out, in_table = {}, False
    for row in wb[rule].iter_rows(min_col=1, max_col=3, values_only=True):
        first = str(row[0] or "").strip()
        if not in_table:
            in_table = first == "ID"
            continue
        if not _PREMISE_ID.match(first):
            break
        out[first] = (str(row[1] or ""), str(row[2] or ""))
    wb.close()
    return out


# --------------------------------------------------------------------
# Time
# --------------------------------------------------------------------

_OFFSET = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_CLOCK = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def parse_time(value, start_at: datetime, where: str) -> datetime:
    """An offset ("1m30s") from start_at, or a clock time ("08:01:30")."""
    if not isinstance(value, str):
        raise CaseError(f"{where}: time {value!r} must be a quoted string, e.g. \"30s\" or \"08:00:30\"")
    if _CLOCK.match(value):
        return datetime.fromisoformat(f"{BASE_DATE}T{value}")
    m = _OFFSET.match(value)
    if not m or not value:
        raise CaseError(f"{where}: cannot read time {value!r}")
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return start_at + timedelta(hours=h, minutes=mi, seconds=s)


def format_offset(t: datetime, start_at: datetime) -> str:
    """t as an offset from start_at ("1m30s"); negative offsets as clock time."""
    seconds = int((t - start_at).total_seconds())
    if seconds < 0:
        return t.strftime("%H:%M:%S")
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    out = (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{s}s" if s else "")
    return out or "0s"


# --------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------

@dataclass
class Case:
    path: Path
    id: str
    title: str
    rule: str
    purpose: str
    status: str
    review: str
    rationale: dict
    author: str
    start_at: datetime
    strict: bool
    stream: list            # raw alarm dicts, with parsed times
    expect: list            # raw assertion dicts
    premise: list = field(default_factory=list)  # premise ids (validation/case_design.xlsx)
    pairs_with: str = ""                         # the confirm case a falsify case differs from
    events: list = field(default_factory=list)   # stream.Event per alarm
    clock: bool = False     # the case writes clock times: report in clock times too

    def show(self, t: datetime) -> str:
        return t.strftime("%H:%M:%S") if self.clock else format_offset(t, self.start_at)

    def alarm_ids(self) -> list:
        return [a["id"] for a in self.stream]


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def load_case(path: Path, kb=None) -> Case:
    """One case file, validated. With kb, every label must be an alarm type."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseError(f"{path.name}: not valid YAML: {exc}") from exc
    where = path.name
    for key in ("id", "title", "rule", "purpose", "status", "stream", "expect"):
        if key not in raw:
            raise CaseError(f"{where}: missing '{key}'")
    if raw["id"] != path.stem:
        raise CaseError(f"{where}: id '{raw['id']}' must equal the file name")
    if path.parent != CASES_DIR and path.parent.name != str(raw.get("rule")):
        raise CaseError(f"{where}: rule '{raw.get('rule')}' must equal its folder '{path.parent.name}'")
    for key, allowed in (("rule", RULES), ("purpose", PURPOSES), ("status", STATUSES)):
        if str(raw[key]) not in allowed:
            raise CaseError(f"{where}: {key} '{raw[key]}' is not one of {sorted(allowed)}")
    review = raw.get("review", "pending")
    if review not in REVIEWS:
        raise CaseError(f"{where}: review '{review}' is not one of {sorted(REVIEWS)}")

    premise = [str(x) for x in _as_list(raw.get("premise") or [])]
    for pid in premise:
        if not _PREMISE_ID.match(pid):
            raise CaseError(f"{where}: premise '{pid}' is not a premise id such as P3 or P5a")
    if premise:
        known = load_premises(str(raw["rule"]))
        if not known:
            raise CaseError(f"{where}: cites premises but {CASE_DESIGN.name} has no sheet "
                            f"'{raw['rule']}' with premises")
        unknown = [pid for pid in premise if pid not in known]
        if unknown:
            raise CaseError(f"{where}: premise {unknown} not in sheet '{raw['rule']}' of {CASE_DESIGN.name}")
    pairs_with = str(raw.get("pairs_with") or "")

    start_at = datetime.fromisoformat(f"{BASE_DATE}T{raw.get('start_at', DEFAULT_START)}")
    stream, ids = [], set()
    for i, alarm in enumerate(raw["stream"] or [], start=1):
        w = f"{where}, alarm {i}"
        for key in ("id", "device", "label", "start", "end"):
            if key not in alarm:
                raise CaseError(f"{w}: missing '{key}'")
        if alarm["id"] in ids:
            raise CaseError(f"{w}: duplicate id '{alarm['id']}'")
        ids.add(alarm["id"])
        if kb is not None and M.type_of(kb, SimpleNamespace(label=alarm["label"],
                                                            device_name=alarm.get("device_name", ""),
                                                            priority=alarm.get("priority", ""))) is None:
            raise CaseError(f"{w}: no blueprint for device_name '{alarm.get('device_name', '')}', "
                            f"label '{alarm['label']}', priority '{alarm.get('priority', '')}'")
        start = parse_time(alarm["start"], start_at, w)
        end = parse_time(alarm["end"], start_at, w)
        if end < start:
            raise CaseError(f"{w}: ends before it starts")
        stream.append({**alarm, "start": start, "end": end})

    expect = raw["expect"] or []
    for i, a in enumerate(expect, start=1):
        w = f"{where}, expectation {i}"
        kinds = [k for k in a if k in ASSERTION_KEYS]
        if len(kinds) != 1:
            raise CaseError(f"{w}: name exactly one of {sorted(ASSERTION_KEYS)}")
        kind = kinds[0]
        extra = set(a) - {kind} - ASSERTION_KEYS[kind]
        if extra:
            raise CaseError(f"{w}: '{kind}' takes no {sorted(extra)}")
        if kind in ("forwarded", "silenced", "lifted", "not_evidence") and a[kind] not in ids:
            raise CaseError(f"{w}: unknown alarm '{a[kind]}'")
        if kind in ("interpretation", "no_interpretation") and a[kind] not in EVENT_KINDS:
            raise CaseError(f"{w}: '{a[kind]}' is not one of {sorted(EVENT_KINDS)}")
        for ref in _as_list(a.get("by", [])) + _as_list(a.get("evidence", [])):
            if ref not in ids:
                raise CaseError(f"{w}: unknown alarm '{ref}'")
        if kind == "silenced":
            if "reason" in a and a["reason"] not in REASON_KIND:
                raise CaseError(f"{w}: reason must be one of {sorted(REASON_KIND)}")
            if "rule" in a and str(a["rule"]) not in SILENCE_RULE:
                raise CaseError(f"{w}: rule must be one of {sorted(SILENCE_RULE)}")
        if kind == "lifted" and "rule" in a and str(a["rule"]) not in LIFT_RULE:
            raise CaseError(f"{w}: rule must be one of {sorted(LIFT_RULE)}")
        for key in ("at", "from", "until"):
            if key in a:
                parse_time(a[key], start_at, w)

    case = Case(path, raw["id"], raw["title"], str(raw["rule"]), raw["purpose"], raw["status"], review,
                raw.get("rationale") or {}, raw.get("author", ""), start_at, raw.get("strict", True),
                stream, expect, premise, pairs_with)
    case.clock = any(isinstance(a[k], str) and _CLOCK.match(a[k]) for a in raw["stream"] or [] for k in ("start", "end"))
    case.events = [Event(case.id, a["label"], a["device"], a["start"], a["end"], a["id"],
                         a.get("device_name", ""), a.get("priority", "")) for a in stream]
    return case


def case_files(folder: Path = CASES_DIR) -> list:
    """The case files in `folder` and its rule folders, sorted by id. Files
    named CASE_* (the template CASE_Template.yaml) are not cases."""
    return sorted((p for p in folder.rglob("*.yaml") if not p.name.startswith("CASE_")),
                  key=lambda p: p.stem)


def load_cases(folder: Path = CASES_DIR, kb=None, only=None) -> list:
    """Every case in `folder` (or those whose id is in `only`), sorted by id.
    All errors are collected and raised together."""
    cases, errors = [], []
    seen = {}
    for path in case_files(folder):
        if path.stem in seen:
            errors.append(f"{path.name}: id also used by {seen[path.stem]}")
            continue
        seen[path.stem] = path.relative_to(folder)
        if only and path.stem not in only:
            continue
        try:
            cases.append(load_case(path, kb))
        except CaseError as exc:
            errors.append(str(exc))
    ids = {c.id for c in cases}
    for c in cases:
        if c.pairs_with and c.pairs_with not in ids and not only:
            errors.append(f"{c.path.name}: pairs_with '{c.pairs_with}' is not a case")
    if errors:
        raise CaseError("cases that cannot be run:\n  " + "\n  ".join(errors))
    return cases


# --------------------------------------------------------------------
# What the system concluded, per case
# --------------------------------------------------------------------

@dataclass
class Observed:
    """The conclusions about one case, in case-local alarm ids."""
    silences: list      # (alarm, reason, rule, frozenset(by), time)
    lifts: list         # (alarm, rule, frozenset(by), time)
    events: list        # (kind, start, end, frozenset(evidence))


def observe(case: Case, firings: list, records: list) -> Observed:
    local = {str(M.alarm_iri(e)): e.alarm_id for e in case.events}

    def ids(iris) -> frozenset:
        return frozenset(local[i] for i in iris if i in local)

    silences, lifts = [], []
    for f in firings:
        if f.patient != case.id:
            continue
        alarm = local.get(f.alarm, f.alarm)
        if f.rule in ENGINE_LIFT_RULE:
            lifts.append((alarm, ENGINE_LIFT_RULE[f.rule], ids(f.causes), f.time))
        else:
            silences.append((alarm, KIND_REASON[f.kind], ENGINE_SILENCE_RULE.get(f.rule, f.rule),
                             ids(f.causes - {f.alarm}), f.time))
    events = [(r.kind, r.start, r.end, ids(r.alarms)) for r in records if r.patient == case.id]
    return Observed(silences, lifts, events)


# --------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------

def _t(case: Case, a: dict, key: str):
    return parse_time(a[key], case.start_at, case.id) if key in a else None


def _matches_silence(case: Case, a: dict, s) -> bool:
    alarm, reason, rule, by, _time = s
    return (alarm == a["silenced"]
            and ("reason" not in a or a["reason"] == reason)
            and ("rule" not in a or str(a["rule"]) == rule)
            and ("by" not in a or frozenset(_as_list(a["by"])) == by))


def _matches_lift(case: Case, a: dict, lift) -> bool:
    alarm, rule, by, time = lift
    return (alarm == a["lifted"]
            and ("rule" not in a or str(a["rule"]) == rule)
            and ("by" not in a or frozenset(_as_list(a["by"])) == by)
            and ("at" not in a or _t(case, a, "at") == time))


def _matches_event(case: Case, a: dict, ev) -> bool:
    kind, start, end, evidence = ev
    return (kind == a["interpretation"]
            and ("from" not in a or _t(case, a, "from") == start)
            and ("until" not in a or _t(case, a, "until") == end)
            and ("evidence" not in a or frozenset(_as_list(a["evidence"])) == evidence))


def describe(case: Case, a: dict) -> str:
    """An assertion as one readable line."""
    kind = next(k for k in a if k in ASSERTION_KEYS)
    words = {"forwarded": "{} forwarded", "silenced": "{} silenced", "lifted": "silence on {} lifted",
             "interpretation": "interpretation {}", "no_interpretation": "no interpretation {}",
             "not_evidence": "{} is evidence for no interpretation"}[kind].format(a[kind])
    details = [f"{k} {', '.join(map(str, _as_list(v)))}" for k, v in a.items() if k != kind]
    return words + (f" ({'; '.join(details)})" if details else "")


def _show_silence(case, s) -> str:
    alarm, reason, rule, by, t = s
    return f"{alarm} silenced {reason} by rule {rule} ({', '.join(sorted(by))}) at {case.show(t)}"


def _show_lift(case, lift) -> str:
    alarm, rule, by, t = lift
    return f"{alarm} lifted by rule {rule} ({', '.join(sorted(by))}) at {case.show(t)}"


def _show_event(case, ev) -> str:
    kind, start, end, evidence = ev
    return (f"{kind} {case.show(start)}–{case.show(end)} "
            f"({', '.join(sorted(evidence))})")


def evaluate(case: Case, obs: Observed) -> list:
    """[(assertion text, passed, observed text)], plus the strict check."""
    out = []
    for a in case.expect:
        kind = next(k for k in a if k in ASSERTION_KEYS)
        if kind == "forwarded":
            hits = [s for s in obs.silences if s[0] == a[kind]]
            ok, seen = not hits, "; ".join(_show_silence(case, s) for s in hits) or "forwarded"
        elif kind == "silenced":
            hits = [s for s in obs.silences if _matches_silence(case, a, s)]
            mine = [s for s in obs.silences if s[0] == a[kind]]
            ok, seen = bool(hits), "; ".join(_show_silence(case, s) for s in mine) or "not silenced"
        elif kind == "lifted":
            hits = [x for x in obs.lifts if _matches_lift(case, a, x)]
            mine = [x for x in obs.lifts if x[0] == a[kind]]
            ok, seen = bool(hits), "; ".join(_show_lift(case, x) for x in mine) or "not lifted"
        elif kind == "interpretation":
            hits = [e for e in obs.events if _matches_event(case, a, e)]
            mine = [e for e in obs.events if e[0] == a[kind]]
            ok, seen = bool(hits), "; ".join(_show_event(case, e) for e in mine) or "none"
        elif kind == "no_interpretation":
            hits = [e for e in obs.events if e[0] == a[kind]]
            ok, seen = not hits, "; ".join(_show_event(case, e) for e in hits) or "none"
        else:  # not_evidence
            hits = [e for e in obs.events if a[kind] in e[3]]
            ok, seen = not hits, "; ".join(_show_event(case, e) for e in hits) or "no evidence"
        out.append((describe(case, a), ok, seen))

    if case.strict:
        unexpected = (
            [_show_silence(case, s) for s in obs.silences
             if not any("silenced" in a and _matches_silence(case, a, s) for a in case.expect)]
            + [_show_lift(case, x) for x in obs.lifts
               if not any("lifted" in a and _matches_lift(case, a, x) for a in case.expect)]
            + [_show_event(case, e) for e in obs.events
               if not any("interpretation" in a and _matches_event(case, a, e) for a in case.expect)])
        out.append(("no other conclusions (strict)", not unexpected, "; ".join(unexpected) or "none"))
    return out
