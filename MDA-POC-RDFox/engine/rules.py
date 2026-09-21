"""
rules.py — the alarm-management rules (RSP-QL: R2R, what holds).

The registry of every rule a run can enable, the query bodies of the
on-demand CAT1/CAT2 checks, and the cheap Python-side gates that skip a
check that cannot match.
"""

from __future__ import annotations

import re
from pathlib import Path

import mint as M
from paths import ENGINE_DIR, RULES_DIR

CLINICAL_EVENTS_MODULE = ENGINE_DIR / "clinical_events.py"

# One file per rule (representation/rules/) so a caller (poc_entry.py) can
# enable/disable each independently — split out of the original combined
# clinical_rules.dlog/cat_rules.dlog for exactly that reason.
RULE_FILES = {
    # Clinical-event rules (clinical_events.EVENT_RULES): never imported —
    # they run as guarded SPARQL updates at arrival and drop times. Point at
    # the module that holds them, for traceability.
    "cardiac_arrest": CLINICAL_EVENTS_MODULE,
    "respiratory_arrest": CLINICAL_EVENTS_MODULE,
    "reduced_pulmonary_function": CLINICAL_EVENTS_MODULE,
    "cat1a": RULES_DIR / "cat1a_signal_quality.dlog",
    "cat1b": RULES_DIR / "cat1b_asystole_ibp.dlog",
    "cat2a": RULES_DIR / "cat2a_process_priority.dlog",
    "cat2b": RULES_DIR / "cat2b_metric_sensor.dlog",
    # CAT3a/CAT3b: the combined clinical events (cardiorespiratory arrest,
    # ventilation failure) — same mechanism as the three rules above; each
    # requires its constituent rules (clinical_events.EventRule.requires).
    "cat3a": CLINICAL_EVENTS_MODULE,
    "cat3b": CLINICAL_EVENTS_MODULE,
}


# cat2a/cat2b are NOT imported as standing Datalog rules (unlike every other
# name in RULE_FILES) — build_script instead runs ONDEMAND_QUERY_BODIES below
# as a one-shot `select ... limit 1` at each alarm's own check point. Why:
# both rules' only new predicate (mdapoc:silencedBy) has exactly one consumer —
# that same per-alarm check — so there's no reason to pay for RDFox
# continuously, incrementally re-maintaining their 14-atom self-join against
# every relevant transaction in the WHOLE store for the rule's entire
# lifetime, when the answer is only ever read once, at one specific instant.
# Root cause confirmed by direct RDFox instrumentation (not inferred): the
# expense is RDFox's incremental "delete, then re-derive" maintenance for a
# STANDING rule, triggered by every relevant import/DELETE WHERE anywhere in
# the store — proportional to how many standing rules could be affected by
# what changed, NOT to how much actually matches right now (a fresh,
# stateless query shaped identically to a standing rule's own join,
# re-issued over the exact same accumulated state, answered in 0.000s; the
# structural fan-out at that exact point was trivial — every hop count 1).
# cat1a/cat1b/cardiac_arrest/respiratory_arrest/reduced_pulmonary_function
# join within a SINGLE alarm's own chain (lower combinatorial risk than
# cat2a/cat2b's cross-alarm self-join), but all reference at least one
# `kb.last_wins_str`-tagged predicate (hasQualityState, hasValueState, hasRhythm) — the same
# argument applies: each predicate they read has exactly one consumer (this
# same per-alarm/per-check point), so there's no reason to pay standing
# incremental maintenance for a value nothing else ever reads back.
#
# IMPORTANT: this does NOT touch Driver's physical graph-drop mechanism
# (_drop_transient_cmd/_drop_persistent_cmd) — it stays exactly as the project's own design doc
# (~/.claude/plans/we-re-going-for-the-magical-candy.md) specifies: a
# landmark/sliding-window RDF-stream-processing model where physically
# dropping a graph the instant it's invalid IS the discard mechanism, and
# "currently in the store" and "currently valid" are the same condition by
# construction — the plan's own Phase 1 finding is exactly why NONE of
# these on-demand query bodies below need any validFrom/validUntil interval
# filtering: physical dropping already guarantees it. An earlier version of
# this fix considered replacing physical dropping with an append-only store
# filtered by explicit validity intervals at query time — reconsidered
# because that would make the store grow unboundedly for the life of a
# batch, directly working against the landmark-window discard model that's
# this project's actual RDF-stream-processing design, and turned out to be
# unnecessary once the real root cause (above) was understood: it's
# standing-rule maintenance cost, not physical deletion itself, that's
# expensive.
# HISTORY (2026-09): cardiac_arrest/respiratory_arrest/
# reduced_pulmonary_function were standing .dlog rules tagging a shared
# process concept; they are now patient-scoped clinical-event updates
# (clinical_events.py), gated per alarm by relevance, which removes both
# the cross-patient leak and the unscoped cost described next.
# They were tried
# on-demand too and REVERTED — real-corpus timing (patient 2826) got WORSE,
# not better: multiple new stalls (several seconds to 20s each) plus a
# fresh dead stop near the end, timing out again. Root cause understood,
# not just observed: unlike cat1a/cat1b/cat2a/cat2b, these three checks are
# NOT alarm-scoped (impliesClinicalEvent's subject is a
# PhysiologicalProcess, not an alarm — see the check-emission site's own
# comment) and run UNCONDITIONALLY on every single alarm regardless of
# relevance, with no VALUES-bound candidate set to narrow the search. Under
# a standing rule, that same check is a cheap read against an answer
# RDFox maintains incrementally; on demand, it's a full, unscoped query
# recomputed from scratch on every alarm — the wrong trade for a check
# that's both unconditional and unbounded, even though it was the right
# trade for cat2a/cat2b (checked once each, alarm-scoped, but with a
# 14-atom cross-alarm self-join RDFox had to re-justify on every unrelated
# mutation in the store). On-demand conversion isn't a universal win — it
# only pays off when what's being converted was itself the standing-rule
# maintenance cost, not merely "any rule with a last-wins predicate."
ONDEMAND_RULE_NAMES = {"cat1a", "cat1b", "cat2a", "cat2b"}

# Each flagging body also binds ?witness: the named graph holding the fact
# that made the rule fire (for cat1a, the reported quality state; for
# cat1b, the IBP pathway's link to the patient). Alarm graphs are named
# <alarm#transient>/<alarm#persistent>, so the witness identifies the
# causing alarm — which is all the firing log records (event_log.py).
#
# Hand-translated equivalents of cat2a_process_priority.dlog's/
# cat2b_metric_sensor.dlog's rule BODIES (not the head — only the join
# itself is needed here). NOT auto-generated from those files: confirmed
# directly against the real RDFox 7.6b binary that its bracket quad syntax
# (`[?s,?p,?o] ?g`), used throughout every .dlog file in this project, is
# RULE-file-only — it errors ("Line 1, column 24: Resource expected.")
# inside a plain `select` command, which only accepts standard SPARQL
# (`GRAPH ?g { ?s ?p ?o }`). Translating one syntax into the other is a real
# structural rewrite (bracket atoms -> GRAPH blocks, Datalog's comma
# conjunction -> SPARQL's `.`), not a text substitution, so it's done here
# by hand instead of by a fragile auto-translator.
#
# MUST BE KEPT IN SYNC BY HAND with the corresponding .dlog file if its join
# logic ever changes — there is no automated link between the two. Verified
# equivalent (not just assumed) by running engine/regression.py's
# regression — the same cat2a_pos/cat2a_neg/cat2b_pos/cat2b_neg
# fabricated fixtures that validate the .dlog files themselves — against
# this on-demand version and confirming identical PASS/FAIL.
#
# `?alarm` is bound via a `VALUES` clause at the call site (build_script),
# not string-substituted into this template — avoids any risk of a
# substring match inside a longer variable name (e.g. `?alarmStart`).
_ALARMCAT = "https://w3id.org/mda/vocab/alarm-category/"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_METRIC = "https://w3id.org/mda/vocab/metric/"
_METRIC_VALUE_STATE = "https://w3id.org/mda/vocab/metric-value-state/"
_METRIC_RHYTHM = "https://w3id.org/mda/vocab/metric-rhythm/"
_FUNCTIONAL_UNIT = "https://w3id.org/mda/vocab/functional-unit/"
_OPERATION_STATE = "https://w3id.org/mda/vocab/operation-state/"
_QUALITY_STATE = "https://w3id.org/mda/vocab/quality-state/"
_CLINICAL_EVENT = "https://w3id.org/mda/vocab/clinical-event/"
_DEVICE = "https://w3id.org/mda/vocab/device/"
_ALARMPRIO = "https://w3id.org/mda/vocab/alarm-priority/"
_SENSOR = "https://w3id.org/mda/vocab/sensor/"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_ONDEMAND_BODY_TEMPLATES = {
    # Alarm-scoped (bound via VALUES ?alarm at the call site), projects
    # ?signal — hand-translated from cat1a_signal_quality.dlog's body; see
    # that file for the clause-by-clause reading of the NL rule. Only a
    # PHYSIOLOGICAL alarm is flagged, and only for the signal on its own
    # sensing pathway: ?gT is its transient graph (category, message,
    # triggeredBy, producesMetric), ?gP a persistent copy of its
    # FunctionalUnit -> sensor -> signal -> analysis chain. The former
    # one-free-graph-variable-per-hop shape let hops come from another
    # alarm's chain: it flagged technical alarms, and flagged e.g. a
    # respiration-rate alarm (ECG_lead_Impedance) for an ECG_signal
    # quality problem whenever a heart-rate alarm supplied the missing hop.
    # Two ways the signal can be insufficient (the .dlog's two rules): a
    # quality state on the signal itself, or a fault state on the sensor
    # producing it (not being acquired at all — agreed extension of the NL
    # rule, 2026-09-21).
    "cat1a": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@triggeredBy> ?fu .
                    ?analysis <@MDA@producesMetric> ?metric . }
        GRAPH ?gP { ?fu <@MDA@hasSensor> ?sensor . ?sensor <@MDA@sensorProducesSignal> ?signal .
                    ?signal <@MDA@analyzedBy> ?analysis . }
        {
          GRAPH ?gQ { ?signal <@MDA@hasQualityState> ?quality }
          FILTER(?quality != <@QUALITYSTATE@Good>)
          BIND(?signal AS ?evidence)
        } UNION {
          GRAPH ?gQ { ?sensor <@MDA@hasSensorOperationState> ?sensorState }
          VALUES ?sensorState { <@OPSTATE@Disabled> <@OPSTATE@Disconnected> <@OPSTATE@Malfunction> }
          BIND(?sensor AS ?evidence)
        }
        BIND(?gQ AS ?witness)
    """,
    # Alarm-scoped, projects ?ibpFunctionalUnit — hand-translated from
    # cat1b_asystole_ibp.dlog; see that file for the clause-by-clause
    # reading of the NL rule and the agreed decisions (2026-09-21).
    #   - asystole: the arriving alarm's OWN metric (?gT, its transient
    #     graph) is a heart rate with an absent rhythm — not a metric
    #     borrowed from another alarm's graph.
    #   - IBP present: one IBP alarm's persistent graph (?gIbp, valid until
    #     15 min after that alarm ended) links this patient to an ARTERIAL
    #     transducer on a PATIENT MONITOR's IBP functional unit. ECMO
    #     circuit pressures and venous pressure never count.
    #   - without alarms on that pathway: no currently valid alarm is
    #     triggered by that functional unit — a literal NOT EXISTS, not a
    #     proxy over reported states. Its validity filter is written by
    #     hand: _append_validity_filters skips NOT EXISTS spans.
    # A flag is stored and withdrawn later if an alarm on the same pathway
    # arrives while the asystole is active — see CAT1B_FLAG_INSERT and
    # CAT1B_WITHDRAW below.
    "cat1b": """
        GRAPH ?gT { ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@RDFTYPE@> <@METRIC@HeartRate> .
                    ?metric <@MDA@hasRhythm> <@RHYTHM@Absent> . }
        GRAPH ?gIbp { ?patient <@MDA@isMonitoredBy> ?ibpDevice . ?ibpDevice <@RDFTYPE@> ?ibpDeviceType .
                      ?ibpDevice <@MDA@hasFunctionalUnit> ?ibpFunctionalUnit .
                      ?ibpFunctionalUnit <@RDFTYPE@> <@FUNCTIONALUNIT@FU_InvasiveBloodPressure> .
                      ?ibpFunctionalUnit <@MDA@hasSensor> ?ibpSensor .
                      ?ibpSensor <@RDFTYPE@> <@SENSOR@ABP_transducer> . }
        ?ibpDeviceType <@RDFS@subClassOf>* <@DEVICE@PhysiologicalMonitor> .
        FILTER NOT EXISTS {
          GRAPH ?gOther { ?other <@MDA@hasMessage> ?otherMsg . ?otherMsg <@MDA@triggeredBy> ?ibpFunctionalUnit . }
          ?gOther <@MDAPOC@validUntil> ?gOther_until . FILTER(?now <= ?gOther_until)
        }
        BIND(?ibpFunctionalUnit AS ?evidence)
        BIND(?gIbp AS ?witness)
    """,
    # Hand-translated from cat2a_process_priority.dlog; see that file for the
    # clause-by-clause reading and the agreed decisions (2026-09-21). ?alarm
    # (incoming), ?now and ?incomingPrio are bound via VALUES at the call
    # site: the incoming alarm's own hasPriority is only inserted after its
    # checks (Driver.insert_alarm's two phases), and an Unknown priority is
    # never checked at all. ?gT is the incoming alarm's transient graph, ?gA
    # an active alarm's — each pinned as one group, so no hop is borrowed
    # from another alarm. Returns one row per active alarm that justifies
    # the silence; all of them are stored (cat2_silence_insert) so the
    # silence can be lifted when the last one ends.
    "cat2a": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@MDA@approximates> ?property .
                    ?metric ?stateProp ?state . }
        VALUES ?stateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        ?property <@MDA@isPropertyOf> ?process .
        ?incomingPrio <@MDA@priorityRank> ?incomingRank .
        ?state <@MDAPOC@deviationDirection> ?direction . ?state <@MDAPOC@deviationSeverity> ?severity .

        GRAPH ?gA { ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?active <@MDA@hasMessage> ?activeMsg . ?activeMsg <@MDA@concernsPatient> ?patient .
                    ?active <@MDA@hasPriority> ?activePrio .
                    ?activeAnalysis <@MDA@producesMetric> ?activeMetric .
                    ?activeMetric <@MDA@approximates> ?activeProperty .
                    ?activeMetric ?activeStateProp ?activeState . }
        VALUES ?activeStateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        ?activeProperty <@MDA@isPropertyOf> ?process .
        ?activePrio <@MDA@priorityRank> ?activeRank .
        ?activeState <@MDAPOC@deviationDirection> ?direction . ?activeState <@MDAPOC@deviationSeverity> ?activeSeverity .

        FILTER(?active != ?alarm)
        FILTER(?activePrio != <@ALARMPRIO@Unknown>)
        FILTER(?activeRank >= ?incomingRank)
        FILTER(?activeSeverity >= ?severity)
        FILTER(?property = ?activeProperty || (
          NOT EXISTS { ?property <@MDA@isPropertyOf> ?otherProcess . FILTER(?otherProcess != ?process) } &&
          NOT EXISTS { ?activeProperty <@MDA@isPropertyOf> ?otherProcess2 . FILTER(?otherProcess2 != ?process) }))
        BIND(?gA AS ?witness)
    """,
    # Hand-translated from cat2b_metric_sensor.dlog; see that file for the
    # clause-by-clause reading and the agreed decisions (2026-09-21). ?alarm
    # (incoming) and ?now are bound via VALUES at the call site. ?gT/?gA are
    # the incoming/active alarm's transient graph, ?gP/?gAP a persistent copy
    # of its sensor chain, anchored on its own analysis. "Different sensor"
    # = a different functional unit or a different sensor type: refinements
    # (anatomical position) come only from technical alarms and split one
    # physical sensor into two identities depending on arrival order. One
    # row per active alarm justifying the silence; all are stored.
    "cat2b": """
        GRAPH ?gT { ?alarm <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?alarm <@MDA@hasMessage> ?msg . ?msg <@MDA@concernsPatient> ?patient .
                    ?msg <@MDA@triggeredBy> ?fu .
                    ?analysis <@MDA@producesMetric> ?metric . ?metric <@RDFTYPE@> ?metricType .
                    ?metric ?stateProp ?state . }
        VALUES ?stateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        GRAPH ?gP { ?fu <@MDA@hasSensor> ?sensor . ?sensor <@MDA@sensorProducesSignal> ?signal .
                    ?signal <@MDA@analyzedBy> ?analysis . ?sensor <@RDFTYPE@> ?sensorType . }
        ?state <@MDAPOC@deviationDirection> ?direction . ?state <@MDAPOC@deviationSeverity> ?severity .

        GRAPH ?gA { ?active <@MDA@hasCategory> <@ALARMCAT@Physiological> .
                    ?active <@MDA@hasMessage> ?activeMsg . ?activeMsg <@MDA@concernsPatient> ?patient .
                    ?activeMsg <@MDA@triggeredBy> ?activeFu .
                    ?activeAnalysis <@MDA@producesMetric> ?activeMetric . ?activeMetric <@RDFTYPE@> ?metricType .
                    ?activeMetric ?activeStateProp ?activeState . }
        VALUES ?activeStateProp { <@MDA@hasValueState> <@MDA@hasRhythm> }
        GRAPH ?gAP { ?activeFu <@MDA@hasSensor> ?activeSensor .
                     ?activeSensor <@MDA@sensorProducesSignal> ?activeSignal .
                     ?activeSignal <@MDA@analyzedBy> ?activeAnalysis .
                     ?activeSensor <@RDFTYPE@> ?activeSensorType . }
        ?activeState <@MDAPOC@deviationDirection> ?direction . ?activeState <@MDAPOC@deviationSeverity> ?activeSeverity .

        FILTER(?active != ?alarm)
        FILTER(?activeSeverity >= ?severity)
        FILTER(?activeFu != ?fu || ?activeSensorType != ?sensorType)
        BIND(?gA AS ?witness)
    """,
    # The clinical-event rules have no template here: they are updates,
    # not checks — see clinical_events.py.
}
# Plan's §3: replaces "still physically present" with an explicit
# validity check, so a batched/delayed physical eviction (plan's §4) can't
# silently make a stale-but-not-yet-dropped graph look active. Only graph
# variables used OUTSIDE any FILTER NOT EXISTS or OPTIONAL span get a
# filter appended here — a variable scoped entirely inside a negation
# isn't bound in the outer WHERE at all (cat1b's ?g12 is handled by hand,
# inside its own negation, in the template above), and a variable scoped
# inside an OPTIONAL must stay genuinely optional: cat1b's rewritten
# criteria (see its own .dlog header) rely on "unbound passes" — auto-
# injecting a MANDATORY validUntil check for an OPTIONAL graph var would
# force it to always resolve, silently turning "optional" back into
# "required" and reintroducing the exact unsatisfiability bug that
# rewrite exists to fix.
_NOT_EXISTS_RE = re.compile(r"FILTER NOT EXISTS\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_OPTIONAL_RE = re.compile(r"OPTIONAL\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_GRAPH_VAR_RE = re.compile(r"GRAPH\s+(\?g\w*)\s*\{")


def _append_validity_filters(body: str) -> str:
    outer = _OPTIONAL_RE.sub(" ", _NOT_EXISTS_RE.sub(" ", body))
    graph_vars = sorted(set(_GRAPH_VAR_RE.findall(outer)))
    tail = " ".join(
        f"{v} <{M.MDAPOC}validUntil> {v}_until . FILTER(?now <= {v}_until)"
        for v in graph_vars
    )
    return f"{body} {tail}" if tail else body


ONDEMAND_QUERY_BODIES = {
    name: _append_validity_filters(" ".join(
        tmpl.replace("@MDAPOC@", str(M.MDAPOC)).replace("@MDA@", str(M.MDA)).replace("@ALARMCAT@", _ALARMCAT)
            .replace("@RDFTYPE@", _RDF_TYPE).replace("@METRIC@", _METRIC)
            .replace("@VALUESTATE@", _METRIC_VALUE_STATE).replace("@RHYTHM@", _METRIC_RHYTHM)
            .replace("@FUNCTIONALUNIT@", _FUNCTIONAL_UNIT)
            .replace("@QUALITYSTATE@", _QUALITY_STATE).replace("@OPSTATE@", _OPERATION_STATE)
            .replace("@DEVICE@", _DEVICE).replace("@SENSOR@", _SENSOR).replace("@RDFS@", _RDFS)
            .replace("@ALARMPRIO@", _ALARMPRIO)
            .split()
    ))
    for name, tmpl in _ONDEMAND_BODY_TEMPLATES.items()
}


def _alarm_functional_unit(kb, event) -> str | None:
    """The functional-unit concept name of this alarm's archetype."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return None
    concept = M.archetype_structure(kb, type_iri).concept(M.MDA.FunctionalUnit)
    return str(concept).rsplit("/", 1)[-1] if concept is not None else None



# Metric types representation/rules/approximates_bridge.dlog derives
# mda:approximates for. A metric type outside it can never satisfy cat2a's
# ?metric -> approximates -> ?property hop, so cat2a is skipped for it in
# Python: RDFox's planner does not discover that dead end cheaply (patient
# 2826, an ArterialBloodPressure_Mean alarm: 100+ s to prove no match;
# reordering or sub-querying the body did not change its plan).
# Derived from the bridge file itself (every `rdf:type, metric:X` rule
# body), not a hand-kept list: the hand-kept list went stale when the
# bridge gained its _Mean/_Minute/_Tidal/_EndExpiratory rules, silently
# excluding 14 alarm types from CAT2a as the incoming alarm.
APPROXIMATES_COVERED_METRIC_TYPES = set(re.findall(
    r"rdf:type,\s*metric:(\w+)\]", (RULES_DIR / "approximates_bridge.dlog").read_text()))


def _alarm_metric_types(kb, event) -> set:
    """Every distinct Metric-kind concept name (e.g. {"ArterialBloodPressure_Mean"})
    M.ground_chain would mint for this alarm's own archetype — used only to
    decide whether cat2a's on-demand query can possibly match (see
    APPROXIMATES_COVERED_METRIC_TYPES above), not part of any minted
    output itself. Cheap: archetype_structure is memoized on `kb`
    (kb.archetype_cache), so this is a small, already-cached tree walk,
    safe to call once per alarm."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return set()
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
    return found
