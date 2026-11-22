"""
rules.py — the rule registry, the .rq loader, and cheap gates (RSP-QL: R2R).

A rule is a CONDITION, representation/rules/<name>.rq: a SPARQL graph
pattern whose header gives the natural-language rule, its clause-by-clause
reading, its bound and bound-by variables, and its regression fixtures.
An ACTION, representation/actions/<name>.rq, is a SPARQL command with a
{{BINDINGS}} slot (a VALUES clause) and a {{CONDITION}} slot (a rule).
No rule logic lives in Python.

The loader strips the PREFIX lines, checks all files agree, and the
script declares them once (the session prepends them to each statement).
Each command is collapsed to one line.

ON DEMAND, NOT STANDING DATALOG. A rule is a query run when its result can
change (an arrival or an end). Standing rules cost an engine incremental
maintenance on every import and drop — CAT2 stalled for tens of seconds
per alarm that way — and absence, withdrawal, lifting and events that
outlive their evidence are not monotone Datalog anyway.

NO MATERIALISATION. Rules read the framework's axioms where they are
stated: CAT2a follows inference.ttl's mda:approximates restrictions
through rdfs:subClassOf*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import mint as M
from paths import ACTIONS_DIR, RULES_DIR

# --------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One switchable rule (a SETTINGS['enabled_rules'] name).

    condition: its file in representation/rules/.
    action:    "flag" (CAT1), "silence" (CAT2) or "episode" (clinical
               events, CAT3).
    kind:      for an episode rule, the clinical-event class it maintains.
    requires:  the rules a combined episode rule is built on.
    """
    name: str
    condition: str
    action: str
    kind: str | None = None
    requires: frozenset = frozenset()

    @property
    def combined(self) -> bool:
        return bool(self.requires)

    @property
    def path(self) -> Path:
        return RULES_DIR / self.condition


# Single-alarm episode kinds before combined ones: the evaluation order.
RULES = {r.name: r for r in (
    Rule("cardiac_arrest", "cardiac_arrest.rq", "episode", "CardiacArrest"),
    Rule("respiratory_arrest", "respiratory_arrest.rq", "episode", "RespiratoryArrest"),
    Rule("reduced_pulmonary_function", "reduced_pulmonary_function.rq", "episode",
         "ReducedPulmonaryFunction"),
    Rule("cat1a", "cat1a_signal_quality.rq", "flag"),
    Rule("cat1b", "cat1b_asystole_ibp.rq", "flag"),
    Rule("cat2a", "cat2a_process_priority.rq", "silence"),
    Rule("cat2b", "cat2b_metric_sensor.rq", "silence"),
    Rule("cat3a", "cat3a_cardiorespiratory_arrest.rq", "episode", "CardioRespiratoryArrest",
         frozenset({"cardiac_arrest", "respiratory_arrest"})),
    Rule("cat3b", "cat3b_ventilation_failure.rq", "episode", "VentilationFailure",
         frozenset({"reduced_pulmonary_function"})),
)}

# Conditions that belong to a rule but are no rule of their own.
CAT1B_WITHDRAW = "cat1b_withdraw.rq"
CAT2_LIFT = "cat2_lift.rq"

EVENT_RULES = tuple(r for r in RULES.values() if r.action == "episode")
EVENT_RULE_NAMES = {r.name for r in EVENT_RULES}
KIND_BY_RULE = {r.name: r.kind for r in EVENT_RULES}
RULE_BY_KIND = {r.kind: r for r in EVENT_RULES}


def enabled_event_rules(rule_names) -> list:
    """The enabled episode rules, in evaluation order. Raises if a combined
    rule is enabled without the rules its evidence comes from — it would
    silently never fire."""
    names = set(rule_names)
    rules = [r for r in EVENT_RULES if r.name in names]
    for r in rules:
        missing = r.requires - names
        if missing:
            raise ValueError(f"{r.name} requires {sorted(r.requires)} also enabled "
                             f"(missing: {sorted(missing)})")
    return rules


# --------------------------------------------------------------------
# Loading and composing .rq files
# --------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^PREFIX\s+([\w-]*):\s*<([^>]*)>\s*$", re.IGNORECASE)


def _read(path: Path) -> tuple:
    """(prefixes, body) of one .rq file, the body on one line."""
    prefixes, body = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _PREFIX_RE.match(s)
        if m:
            prefixes[m.group(1)] = m.group(2)
        else:
            body.append(s)
    return prefixes, " ".join(" ".join(body).split())


def _load_all() -> tuple:
    """Every rule and action body, and their agreed prefixes."""
    bodies, prefixes = {}, {}
    for folder in (RULES_DIR, ACTIONS_DIR):
        for path in sorted(folder.glob("*.rq")):
            file_prefixes, body = _read(path)
            for p, iri in file_prefixes.items():
                if prefixes.setdefault(p, iri) != iri:
                    raise ValueError(f"{path.name}: prefix {p}: is <{iri}>, elsewhere <{prefixes[p]}>")
            bodies[path] = body
    return bodies, prefixes


_BODIES, PREFIXES = _load_all()
PREFIX_COMMANDS = [f"prefix {p}: <{iri}>" for p, iri in sorted(PREFIXES.items())]

for _rule in RULES.values():
    if _rule.path not in _BODIES:
        raise FileNotFoundError(f"rule {_rule.name}: no condition file {_rule.path}")


def condition(name: str) -> str:
    """The body of a condition file in representation/rules/."""
    return _BODIES[RULES_DIR / name]


def compose(action: str, bindings: str, condition_file: str | None = None) -> str:
    """One engine command: action template `action` with its slots filled."""
    text = _BODIES[ACTIONS_DIR / f"{action}.rq"].replace("{{BINDINGS}}", bindings)
    if condition_file is not None:
        text = text.replace("{{CONDITION}}", condition(condition_file))
    if "{{" in text:
        raise ValueError(f"action {action}: unfilled slot in {text[:120]}...")
    return text


# --------------------------------------------------------------------
# Gates: cheap Python-side checks that skip a query that cannot match.
# They may over-include, never under-include; the conditions decide.
# --------------------------------------------------------------------

ALARMPRIO = "https://w3id.org/mda/vocab/alarm-priority/"
DEVICE = "https://w3id.org/mda/vocab/device/"


def _alarm_functional_unit(kb, event) -> str | None:
    """The functional-unit concept name of this alarm's archetype."""
    type_iri = M.type_of(kb, event)
    if type_iri is None:
        return None
    concept = M.archetype_structure(kb, type_iri).concept(M.MDA.FunctionalUnit)
    return str(concept).rsplit("/", 1)[-1] if concept is not None else None


_metric_types_cache: dict = {}


def _alarm_metric_types(kb, event) -> frozenset:
    """The metric concept names in this alarm's blueprint (e.g.
    {"ArterialBloodPressure_Mean"}). Read from the blueprint only, so kept
    per blueprint."""
    type_iri = M.type_of(kb, event)
    if type_iri is None:
        return frozenset()
    key = (id(kb), type_iri)
    if key not in _metric_types_cache:
        _metric_types_cache[key] = _blueprint_metric_types(kb, type_iri)
    return _metric_types_cache[key]


def _blueprint_metric_types(kb, type_iri) -> frozenset:
    arch = M.archetype_structure(kb, type_iri)
    found = set()

    def walk(cls):
        concept = arch.concept(cls)
        if concept is not None and "vocab/metric/" in str(concept):
            found.add(str(concept).rsplit("/", 1)[-1])
        for child_cls, link in kb.tree.items():
            if link and link[0] == cls:
                walk(child_cls)

    walk(M.MDA.Device)
    return frozenset(found)


# What an alarm must carry to possibly be evidence, per episode kind (see
# relevant_kinds). Combined kinds are relevant whenever one of their
# constituents is, plus — for ventilation failure — any ventilator alarm.
_METRIC_FOR_KIND = {
    "CardiacArrest": frozenset({"HeartRate"}),
    "RespiratoryArrest": frozenset({"RespirationRate"}),
    "ReducedPulmonaryFunction": frozenset({"RespirationVolume_Minute"}),
}

# ReducedPulmonaryFunction also admits any metric approximating a property
# of pulmonary oxygenation (rules/reduced_pulmonary_function.rq). The gate
# reads those metrics from inference.ttl rather than naming them, so a new
# oxygenation metric is never gated out.
_OXYGENATION = M.URIRef("https://w3id.org/mda/vocab/physiological-process/PulmonaryOxygenation")
_OWL = M.Namespace("http://www.w3.org/2002/07/owl#")
_oxygenation_cache: dict = {}


def _oxygenation_metrics(kb) -> frozenset:
    """Metric concept names whose type (or a superclass) approximates a
    property of pulmonary oxygenation, read from inference.ttl."""
    key = id(kb)
    if key not in _oxygenation_cache:
        g = kb.reasoning_static
        properties = set(g.subjects(M.MDA.isPropertyOf, _OXYGENATION))
        names = set()
        for r in g.subjects(_OWL.onProperty, M.MDA.approximates):
            if g.value(r, _OWL.hasValue) in properties:
                for metric in g.subjects(M.RDFS.subClassOf, r):
                    for m in {metric} | set(g.transitive_subjects(M.RDFS.subClassOf, metric)):
                        names.add(str(m).rsplit("/", 1)[-1])
        _oxygenation_cache[key] = frozenset(names)
    return _oxygenation_cache[key]


def _metrics_for_kind(kb, kind: str) -> frozenset:
    metrics = _METRIC_FOR_KIND[kind]
    if kind == "ReducedPulmonaryFunction" and kb is not None:
        metrics = metrics | _oxygenation_metrics(kb)
    return metrics


def kinds_supported_by_metric(metric_type: str, rules: list, kb=None) -> frozenset:
    """The enabled kinds an alarm with `metric_type` can be evidence for,
    directly or through a constituent — used when a CAT1b withdrawal lets a
    heart-rate alarm count from that moment on."""
    kinds = {r.kind for r in rules}
    direct = {k for k in _METRIC_FOR_KIND if k in kinds and metric_type in _metrics_for_kind(kb, k)}
    combined = {r.kind for r in rules if r.combined and {KIND_BY_RULE[n] for n in r.requires} & direct}
    return frozenset(direct | combined)


def _is_ventilator(kb, concept) -> bool:
    """Whether a device concept is a mechanical ventilator (or subclass)."""
    target = M.URIRef(f"{DEVICE}MechanicalVentilator")
    if concept is None:
        return False
    return concept == target or target in set(
        kb.reasoning_static.transitive_objects(concept, M.RDFS.subClassOf))


def relevant_kinds(kb, event, metric_types: set, rules: list) -> set:
    """The enabled episode kinds an alarm with this label could be evidence
    for (directly, or through a constituent). Over-inclusion only costs a
    few cheap queries; under-inclusion would miss an event, so when in
    doubt a kind is included."""
    kinds = {r.kind for r in rules}
    direct = {k for k in _METRIC_FOR_KIND if k in kinds and _metrics_for_kind(kb, k) & metric_types}
    if "VentilationFailure" in kinds:
        type_iri = M.type_of(kb, event)
        if type_iri is not None:
            arch = M.archetype_structure(kb, type_iri)
            if _is_ventilator(kb, arch.concept(M.MDA.Device)):
                direct.add("VentilationFailure")
    if "CardioRespiratoryArrest" in kinds and direct & {"CardiacArrest", "RespiratoryArrest"}:
        direct.add("CardioRespiratoryArrest")
    if "VentilationFailure" in kinds and "ReducedPulmonaryFunction" in direct:
        direct.add("VentilationFailure")
    return direct
