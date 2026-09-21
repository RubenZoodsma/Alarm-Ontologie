"""
clinical_events.py — clinical events in the graph of belief.

A clinical event is a node of its own, one per episode, for one patient:

    event  a mda:ClinicalEvent, clinicalEvent:<Kind>
    event  mda:concernsPatient  patient
    event  mda:evidencedBy      evidence      (one or more)
    event  mda:hasStart         time
    event  mda:hasEnd           time          (only in the moment it ends)

Only the event points outward; nothing points from evidence back to it.
Each kind's criterion is stated in the framework
(FRAMEWORK/KNOWLEDGE_BASE/clinicalEvents.ttl, as an evidencedBy axiom on
the event class). The EVIDENCE_BODIES below are hand translations of those
axioms into SPARQL — keep them in sync by hand.

WHY SPARQL UPDATES, NOT DATALOG OR PYTHON
--------------------------------------------------------------------
An event outlives the individual facts that support it (a second heart
rate alarm keeps a cardiac arrest going after the first one ends), so it
cannot be a derived Datalog fact: RDFox would retract it the moment the
first alarm's graph is dropped, and a derived fact cannot carry an end
time. And replay_driver.build_script writes the whole replay as ONE RDFox
script before anything runs, so Python cannot react to a query result
mid-run. The per-(patient, kind) state machine therefore runs inside
RDFox, as guarded `INSERT ... WHERE` updates:

    inactive + evidence   -> START:  create the event (hasStart = now)
    active   + evidence   -> EXTEND: add evidencedBy for new evidence
    active   + none left  -> END:    set hasEnd = now, log it, remove it

"Active" is simply "an event of this kind for this patient exists in the
store": an ended event is removed in the same step it ends, so the store
holds only current belief. "Inactive" is its absence.

WHEN THE STATE MACHINE RUNS, AND WHY now IS THE RIGHT TIMESTAMP
--------------------------------------------------------------------
Evidence can only appear or disappear when a relevant alarm is inserted
(arrival) or one of its graphs is dropped (end, window expiry) — no alarm
ever edits another alarm's graphs (replay_driver's module docstring).
evaluate_commands() is emitted at exactly those moments, with `now` set to
that moment's logical time. So the first moment a criterion holds, and the
moment it stops holding, are always evaluation times — hasStart = now and
hasEnd = now are exact, never "when the driver happened to notice".
For a combined event this gives the later of its constituents' starts and
the earliest of their ends, as the framework requires.

Order inside one evaluation: END for every kind (single-alarm kinds before
combined ones, so a combined event sees its constituent already ended),
then log and remove the ended events, then EXTEND/START (again single
before combined, so a combined event can start on a constituent that
started in this same step).

FLAGGED ALARMS ARE NO EVIDENCE (agreed 2026-09-21)
--------------------------------------------------------------------
An alarm CAT1 flagged as likely false positive (mdapoc:
flaggedLikelyFalsePositive, stored in its own transient graph by
replay_driver's cat1_flag_insert) supports no clinical event: a condition
must not rest on an alarm the POC itself believes to be false. This is the
rule order CAT1 -> clinical events: build_script runs an arrival's CAT1
checks before its clinical-event evaluation. A CAT1b flag can be withdrawn
later; the asystole then becomes evidence at the withdrawal, so hasStart is
the withdrawal time (build_script evaluates the heart-rate kinds then).
CAT2 silences do not matter here: a silenced alarm is redundant, not false.
POC policy, not framework: the framework's criteria (clinicalEvents.ttl)
know nothing of this POC's decisions.

SCOPE AND GATING
--------------------------------------------------------------------
Every statement is bound to one patient, so an event can never be built
from another patient's evidence, whatever else shares the data store.
relevant_kinds() lets the driver skip evaluation for alarms that cannot be
evidence for any enabled kind — a pure cost saving; it may over-include,
never under-include.

LOGGING
--------------------------------------------------------------------
The log is not part of the graph of belief. Two things are printed, as
tab-separated rows inside trace blocks (trace_block below), and turned into
records by engine/event_log.py after the run; nothing is written back into
the store:
  - "support": after every START/EXTEND, each active event with what
    currently supports it — an alarm's named graph (<alarm#transient> or
    <alarm#persistent>, i.e. the alarm itself) or, for a combined event,
    a constituent event. The log unions these per event and resolves
    constituent events down to their alarms.
  - "ended": the events that end in this step, with start and end, just
    before they are removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import mint as M

MDA = str(M.MDA)
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"
METRIC = "https://w3id.org/mda/vocab/metric/"
RHYTHM = "https://w3id.org/mda/vocab/metric-rhythm/"
VALUE_STATE = "https://w3id.org/mda/vocab/metric-value-state/"
OPSTATE = "https://w3id.org/mda/vocab/operation-state/"
DEVICE = "https://w3id.org/mda/vocab/device/"
COMPONENT = "https://w3id.org/mda/vocab/component/"
MODALITY = "https://w3id.org/mda/vocab/therapeutic-modality/"
CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"
EVENT_BASE = "https://w3id.org/mda/instance/ClinicalEvent_"

TRACE_BEGIN = "TRACE_BEGIN"
TRACE_END = "TRACE_END"

# Set once at the top of every script: every select prints its answers, as
# TSV (one header line plus one line per row). Each select is wrapped in a
# trace block, so its rows can be told apart from RDFox's own statistics
# lines and matched to what asked for them.
SCRIPT_PREAMBLE = ["set query.answer-format text/tab-separated-values", "set output out"]


def trace_block(tag: str, query: str) -> list:
    """`query` wrapped in TRACE_BEGIN <tag> / TRACE_END marker lines."""
    return [f"echo {TRACE_BEGIN} {tag}", query, f"echo {TRACE_END}"]


def _valid(graph_var: str) -> str:
    """The graph is still valid at @NOW@ (plan §3 validity filter)."""
    return f"{graph_var} <{M.MDAPOC}validUntil> {graph_var}_until . FILTER(@NOW@ <= {graph_var}_until)"


def _metric_evidence(metric_type: str, prop: str, value: str) -> str:
    """A single-alarm criterion: a metric of `metric_type` whose `prop` is
    `value`, reported by an alarm of this patient that is still valid and
    not CAT1-flagged (module docstring). Everything is matched in ONE
    alarm's transient graph: the alarm's message (hence its patient), its
    condition content (the metric's type and state) and any CAT1 flag on it
    are inserted there together, so a metric found in that graph is that
    alarm's metric, reporting that state."""
    return f"""
        GRAPH ?gEv {{
          ?evAlarm <{MDA}hasMessage> ?evMsg .
          ?evMsg <{MDA}concernsPatient> @PATIENT@ .
          ?evidence <{RDF_TYPE}> <{metric_type}> .
          ?evidence <{prop}> <{value}> .
        }}
        FILTER NOT EXISTS {{ GRAPH ?gEv {{ ?evAlarm <{M.MDAPOC}flaggedLikelyFalsePositive> ?why }} }}
        {_valid("?gEv")}
        BIND(?gEv AS ?support)
    """


def _active_event(var: str, kind: str, graph_var: str) -> str:
    """An event of `kind` for this patient that has not ended."""
    return f"""
        GRAPH {graph_var} {{
          {var} <{RDF_TYPE}> <{CLINICAL_EVENT}{kind}> .
          {var} <{MDA}concernsPatient> @PATIENT@ .
          FILTER NOT EXISTS {{ {var} <{MDA}hasEnd> {var}_end }}
        }}
    """


# Hand translations of clinicalEvents.ttl. Each body binds ?evidence (one
# row per supporting node) for @PATIENT@ at @NOW@.
EVIDENCE_BODIES = {
    # evidencedBy some (metric:HeartRate and hasRhythm value Absent)
    "CardiacArrest": _metric_evidence(f"{METRIC}HeartRate", f"{MDA}hasRhythm", f"{RHYTHM}Absent"),
    # evidencedBy some (metric:RespirationRate and hasRhythm value Absent)
    "RespiratoryArrest": _metric_evidence(f"{METRIC}RespirationRate", f"{MDA}hasRhythm", f"{RHYTHM}Absent"),
    # evidencedBy some (metric:RespirationVolume_Minute and hasValueState value Decreased)
    "ReducedPulmonaryFunction": _metric_evidence(
        f"{METRIC}RespirationVolume_Minute", f"{MDA}hasValueState", f"{VALUE_STATE}Decreased"),
    # evidencedBy some CardiacArrest, and evidencedBy some RespiratoryArrest
    # — both events of this patient, both current (hence overlapping). See
    # EVENT_RULES' cat3a entry for how this departs from the NL rule.
    # VALUES ?which yields one row per piece of evidence. ?support is the
    # constituent event itself; the log resolves it to that event's alarms.
    "CardioRespiratoryArrest": f"""
        {_active_event("?ca", "CardiacArrest", "?gCa")}
        {_active_event("?ra", "RespiratoryArrest", "?gRa")}
        VALUES ?which {{ 1 2 }}
        BIND(IF(?which = 1, ?ca, ?ra) AS ?evidence)
        BIND(?evidence AS ?support)
    """,
    # evidencedBy some ReducedPulmonaryFunction, and evidencedBy some
    # (therapeuticModality:VentilationTherapy and hasTherapyDeliveryQuality
    # value Impaired) — this patient's ventilation therapy compromised by
    # an alarm that is ACTIVE now (its transient graph ?gV), so the two
    # hold at the same time. hasTherapyDeliveryQuality is not materialised
    # in RDFox: the three inference.ttl axioms that entail it are inlined
    # as the UNION's branches (a circuit leak, a malfunctioning ventilator,
    # a disconnected patient circuit). Each branch anchors on a device-
    # scoped node reported in ?gV (the leak's analysis, the device, the
    # component), so the therapy found is that alarm's device's therapy.
    # Structure (the analysis chain, hasComponent, administers) comes from
    # a persistent graph; any valid copy carries the same IRIs.
    # A mode of ventilation therapy (therapeuticModality:CPAP) counts as
    # ventilation therapy: RDFox does not reason over rdfs:subClassOf, so
    # the therapy's type is walked up the hierarchy (inference.ttl, default
    # graph) explicitly.
    "VentilationFailure": f"""
        {_active_event("?rpf", "ReducedPulmonaryFunction", "?gRpf")}
        GRAPH ?gV {{
          ?vAlarm <{MDA}hasMessage> ?vMsg .
          ?vMsg <{MDA}concernsPatient> @PATIENT@ .
        }}
        {{
          GRAPH ?gV {{
            ?leakAnalysis <{MDA}producesMetric> ?leak .
            ?leak <{RDF_TYPE}> <{METRIC}VentilationCircuitLeak> .
            ?leak <{MDA}hasValueState> <{VALUE_STATE}Increased> .
          }}
          GRAPH ?gVS {{
            ?vDev <{MDA}hasFunctionalUnit> ?vFu .
            ?vFu <{MDA}hasSensor> ?vSensor .
            ?vSensor <{MDA}sensorProducesSignal> ?vSignal .
            ?vSignal <{MDA}analyzedBy> ?leakAnalysis .
          }}
          {_valid("?gVS")}
        }} UNION {{
          GRAPH ?gV {{ ?vDev <{MDA}hasDeviceOperationState> <{OPSTATE}Malfunction> . }}
        }} UNION {{
          GRAPH ?gV {{ ?vComp <{MDA}hasComponentOperationState> <{OPSTATE}Disconnected> . }}
          GRAPH ?gVS {{
            ?vDev <{MDA}hasComponent> ?vComp .
            ?vComp <{RDF_TYPE}> <{COMPONENT}AirwayCircuit> .
          }}
          {_valid("?gVS")}
        }}
        GRAPH ?gVT {{
          ?vDev <{MDA}administers> ?therapy .
          ?therapy <{RDF_TYPE}> ?therapyType .
        }}
        ?therapyType <{RDFS_SUBCLASS}>* <{MODALITY}VentilationTherapy> .
        {_valid("?gV")}
        {_valid("?gVT")}
        VALUES ?which {{ 1 2 }}
        BIND(IF(?which = 1, ?rpf, ?therapy) AS ?evidence)
        BIND(IF(?which = 1, ?rpf, ?gV) AS ?support)
    """,
}


@dataclass(frozen=True)
class EventRule:
    """One switchable rule (a poc_entry.py SETTINGS['enabled_rules'] name)
    and the event kind it maintains."""
    name: str
    kind: str
    combined: bool
    requires: frozenset = frozenset()


# Single-alarm kinds first, then combined kinds — the evaluation order.
#
# CAT3a — a DELIBERATE DEPARTURE from the NL rule table (decided
# 2026-09-21). The table reads: "If alarms conveying [reduced heart rate,
# reduced respiratory rate] occur within a 60s timeframe, they collectively
# convey a respiratory arrest." Implemented instead, and the wording the
# table should carry: "If a cardiac arrest (asystole) and a respiratory
# arrest (apnoea) are present at the same time, they collectively convey a
# cardiorespiratory arrest." Clause by clause:
#   "reduced heart rate"       -> a CardiacArrest condition: a heart-rate
#                                 metric with an ABSENT rhythm (asystole).
#                                 Strict on purpose: faithful to clinical
#                                 practice rather than to the table.
#   "reduced respiratory rate" -> a RespiratoryArrest condition: a
#                                 respiration-rate metric with an ABSENT
#                                 rhythm (apnoea).
#   "within a 60s timeframe"   -> both conditions active at the same moment;
#                                 no tolerance window. Sub-analysis on
#                                 DATA_LOCKED: overlap alone gives 929
#                                 episodes in 199 patients; a 60 s window
#                                 would add 44 patients — not needed to
#                                 show the rule triggers.
#   "respiratory arrest"       -> CardioRespiratoryArrest. RespiratoryArrest
#                                 is a condition in its own right, created
#                                 by one alarm (today only "PHILIPSMONITOR -
#                                 Apneu"), and a constituent here.
# Conditions are uniform labels: evidence from any source that meets the
# criterion (e.g. a ventilator apnoea alarm, once annotated) creates the
# same condition, so CAT3a combines conditions, never specific alarms.
#
# CAT3b — also a departure from the NL table (decided 2026-09-21); the
# clause-by-clause account is on clinicalEvent:VentilationFailure in
# FRAMEWORK/KNOWLEDGE_BASE/clinicalEvents.ttl. In short: "mechanical
# ventilation alarm" -> the ventilation therapy is compromised (leak,
# malfunctioning ventilator, disconnected patient circuit); "reduced
# function of pulmonary processes" -> ReducedPulmonaryFunction (low minute
# volume); "within 180 s" -> at the same time; any ventilator of the
# patient.
EVENT_RULES = (
    EventRule("cardiac_arrest", "CardiacArrest", combined=False),
    EventRule("respiratory_arrest", "RespiratoryArrest", combined=False),
    EventRule("reduced_pulmonary_function", "ReducedPulmonaryFunction", combined=False),
    EventRule("cat3a", "CardioRespiratoryArrest", combined=True,
              requires=frozenset({"cardiac_arrest", "respiratory_arrest"})),
    EventRule("cat3b", "VentilationFailure", combined=True,
              requires=frozenset({"reduced_pulmonary_function"})),
)
EVENT_RULE_NAMES = {r.name for r in EVENT_RULES}
KIND_BY_RULE = {r.name: r.kind for r in EVENT_RULES}


def enabled_event_rules(rule_names) -> list:
    """The enabled event rules, in evaluation order. Raises if a combined
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


# What an alarm must carry to possibly be evidence, per kind (see
# relevant_kinds). Combined kinds are relevant whenever one of their
# constituents is, plus — for ventilation failure — any ventilator alarm.
_METRIC_FOR_KIND = {
    "CardiacArrest": "HeartRate",
    "RespiratoryArrest": "RespirationRate",
    "ReducedPulmonaryFunction": "RespirationVolume_Minute",
}


def kinds_supported_by_metric(metric_type: str, rules: list) -> frozenset:
    """The enabled kinds an alarm with `metric_type` can be evidence for,
    directly or through a constituent — used when a CAT1b withdrawal lets a
    heart-rate alarm count from that moment on."""
    kinds = {r.kind for r in rules}
    direct = {k for k, m in _METRIC_FOR_KIND.items() if k in kinds and m == metric_type}
    combined = {r.kind for r in rules if r.combined and {KIND_BY_RULE[n] for n in r.requires} & direct}
    return frozenset(direct | combined)


def _is_ventilator(kb, concept) -> bool:
    target = M.URIRef(f"{DEVICE}MechanicalVentilator")
    if concept is None:
        return False
    return concept == target or target in set(
        kb.reasoning_static.transitive_objects(concept, M.RDFS.subClassOf))


def relevant_kinds(kb, label: str, metric_types: set, rules: list) -> set:
    """The enabled event kinds an alarm with this label could be evidence
    for (directly, or through a constituent). Over-inclusion only costs a
    few cheap queries; under-inclusion would miss an event, so when in
    doubt a kind is included."""
    kinds = {r.kind for r in rules}
    direct = {k for k, m in _METRIC_FOR_KIND.items() if k in kinds and m in metric_types}
    if "VentilationFailure" in kinds:
        type_iri = kb.type_index.get(label)
        if type_iri is not None:
            arch = M.archetype_structure(kb, type_iri)
            if _is_ventilator(kb, arch.concept(M.MDA.Device)):
                direct.add("VentilationFailure")
    if "CardioRespiratoryArrest" in kinds and direct & {"CardiacArrest", "RespiratoryArrest"}:
        direct.add("CardioRespiratoryArrest")
    if "VentilationFailure" in kinds and "ReducedPulmonaryFunction" in direct:
        direct.add("VentilationFailure")
    return direct


def _literal(when: datetime) -> str:
    return f'"{when.isoformat()}"^^<{XSD_DATETIME}>'


def _bind(text: str, patient_iri: str, now: datetime) -> str:
    body = text.replace("@PATIENT@", f"<{patient_iri}>").replace("@NOW@", _literal(now))
    return " ".join(body.split())


def event_iri(kind: str, patient_id: str, start: datetime) -> str:
    return f"{EVENT_BASE}{kind}_{M._clean(patient_id)}_{start.strftime('%Y%m%dT%H%M%S')}"


def _end_update(kind: str) -> str:
    return f"""
        INSERT {{ GRAPH ?eg {{ ?ev <{MDA}hasEnd> @NOW@ }} }}
        WHERE {{
          {_active_event("?ev", kind, "?eg")}
          FILTER NOT EXISTS {{ {EVIDENCE_BODIES[kind]} }}
        }}
    """


def _extend_update(kind: str) -> str:
    return f"""
        INSERT {{ GRAPH ?eg {{ ?ev <{MDA}evidencedBy> ?evidence }} }}
        WHERE {{
          {_active_event("?ev", kind, "?eg")}
          {EVIDENCE_BODIES[kind]}
        }}
    """


def _start_update(kind: str, iri: str) -> str:
    # The event's own named graph (<iri#event>) is its unit of removal.
    return f"""
        INSERT {{ GRAPH <{iri}#event> {{
          <{iri}> <{RDF_TYPE}> <{MDA}ClinicalEvent> .
          <{iri}> <{RDF_TYPE}> <{CLINICAL_EVENT}{kind}> .
          <{iri}> <{MDA}concernsPatient> @PATIENT@ .
          <{iri}> <{MDA}hasStart> @NOW@ .
          <{iri}> <{MDA}evidencedBy> ?evidence .
        }} }}
        WHERE {{
          FILTER NOT EXISTS {{ GRAPH ?anyGraph {{
            ?open <{RDF_TYPE}> <{CLINICAL_EVENT}{kind}> .
            ?open <{MDA}concernsPatient> @PATIENT@ .
          }} }}
          {EVIDENCE_BODIES[kind]}
        }}
    """


def _support_select(kind: str) -> str:
    return f"""
        select distinct ?ev ?support where {{
          {_active_event("?ev", kind, "?eg")}
          {EVIDENCE_BODIES[kind]}
        }}
    """


# The ended events' rows, printed for the log. `?kind` excludes the generic
# mda:ClinicalEvent type so each row carries the concrete kind.
_LOG_SELECT = f"""
    select ?ev ?kind ?start ?end where {{
      GRAPH ?eg {{
        ?ev <{MDA}concernsPatient> @PATIENT@ .
        ?ev <{MDA}hasEnd> ?end .
        ?ev <{MDA}hasStart> ?start .
        ?ev <{RDF_TYPE}> ?kind .
        FILTER(?kind != <{MDA}ClinicalEvent>)
      }}
    }}
"""

_REMOVE_ENDED = f"""
    DELETE {{ GRAPH ?eg {{ ?s ?p ?o }} }}
    WHERE {{
      GRAPH ?eg {{
        ?ev <{MDA}concernsPatient> @PATIENT@ .
        ?ev <{MDA}hasEnd> ?end .
        ?s ?p ?o .
      }}
    }}
"""


def evaluate_commands(kinds: set, rules: list, patient_id: str, now: datetime) -> list:
    """RDFox script lines that bring this patient's events of `kinds` up to
    date at logical time `now`. Empty when no kind is relevant."""
    ordered = [r.kind for r in rules if r.kind in kinds]
    if not ordered:
        return []
    patient_iri = str(M.patient_iri(patient_id))
    lines = [f"# clinical events: {', '.join(ordered)} @ {now.isoformat()}"]
    for kind in ordered:
        lines.append(_bind(_end_update(kind), patient_iri, now))
    lines += trace_block("ended", _bind(_LOG_SELECT, patient_iri, now))
    lines.append(_bind(_REMOVE_ENDED, patient_iri, now))
    for kind in ordered:
        lines.append(_bind(_extend_update(kind), patient_iri, now))
        lines.append(_bind(_start_update(kind, event_iri(kind, patient_id, now)), patient_iri, now))
        lines += trace_block("support", _bind(_support_select(kind), patient_iri, now))
    return lines
