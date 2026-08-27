"""
assess.py — CAT1/CAT2 alarm management rule detection.

Owns rule DETECTION only: given the situational graph at an alarm's arrival
instant, decide which management rules fire for that alarm. Every rule's
firing condition lives in a `.rq` file under rules/, run as-is against the
graph — no rule's logic is written as a Python `if`. The engine
(CODE/evaluation_poc/mda_poc_assessment.py, the entry point) owns the
per-patient, per-alarm loop and turns firings into a log; this module never
mutates the knowledge graph or decides what happens as a result of a firing.

Two small enrichments are added to the query graph here, neither of which
touches op_knowledge.py:

  - mda:priorityRank facts (FRAMEWORK/VOCABULARY/priority_rank.ttl) — needed
    by cat2a_process_priority.rq's priority comparison. Not part of
    op_knowledge.py's K.KB.reasoning_static because nothing CQ9-11 or the
    visualization does needs a priority ordering; loaded here instead.
  - mda:triggeredBy (AlarmMessage -> Device). The ontology documents this as
    "the sole path from AlarmMessage into the mda:Device subtree" and the
    static blueprint catalogue (kg_generated.ttl) does assert it per
    archetype — confirmed by EVALUATION/CQs/post_enrichment/
    CQ2_shared_sensing_pathway.rq's use of exactly this path against that
    file — but op_knowledge.py's operational grounding (alarm_message())
    never carries it over onto the per-occurrence AlarmMessage node. CQ9-11
    never needed it (they match on tick-coincidence, not a specific alarm's
    own pathway); every CAT1/CAT2 rule does, since each asks about the
    ARRIVING alarm's own pathway specifically. Rather than edit the
    validated op_knowledge.py, add_triggered_by() reconstructs the exact
    same triple using the same public device_iri/message_iri functions
    op_knowledge.py itself exports, for exactly the events revealed at a
    given instant.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph
from rdflib.plugins.sparql import prepareQuery

sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))
import op_knowledge as K

RULES_DIR = Path(__file__).resolve().parent / "rules"
PRIORITY_RANK = K.FRAMEWORK_DIR / "VOCABULARY" / "priority_rank.ttl"


def load_rules() -> dict:
    """
    rule id (filename stem) -> a PRE-COMPILED SPARQL query object, loaded and
    parsed once. Graph.query() accepts either a query object or raw text —
    passing raw text (the previous behaviour) makes rdflib call
    prepareQuery() internally on every single invocation, even though these
    four rule texts never change for the life of a run. assess() calls this
    once per rule per alarm evaluation — across a full corpus that is tens of
    millions of re-parses of four fixed strings. Compiling once here and
    handing assess() the object instead removes that entirely; the compiled
    query's evaluation semantics (including initBindings) are unaffected —
    prepareQuery() only changes when parsing happens, not what is parsed.
    """
    return {f.stem: prepareQuery(f.read_text(encoding="utf-8"))
            for f in sorted(RULES_DIR.glob("*.rq"))}


def load_priority_rank() -> Graph:
    g = Graph()
    g.parse(PRIORITY_RANK, format="turtle")
    return g


def resolve_identity(kb, events: list) -> dict:
    """
    The same {class: {(device_id, concept): refinements}} map
    op_knowledge.py's build_timeline computes internally (via
    K.resolve_particular_identities) — build_timeline's own 5-line loop, not
    exposed as a standalone public function there, so recomputed here instead
    of reached for on Timeline's private `_identity` field.

    Unlike build_timeline's own CQ9-11/visualization callers, which pass a
    patient's complete event list once, mda_poc_assessment.py calls this once
    PER ARRIVING ALARM with `events` already restricted to that alarm's
    events_so_far (start <= the arriving alarm's own start) — never the
    patient's full history. Passing the full history here would let a
    laterality/anatomical-position mention from a later alarm resolve an
    earlier alarm's particular identity, before that later alarm has
    occurred. See mda_poc_assessment.py's module docstring, "Why re-resolve
    identity per alarm."
    """
    individuated = [cls for cls, kind in kb.node_kind.items()
                    if kind == "individuated" and cls not in (K.MDA.Device, K.MDA.Patient)]
    identity = {}
    for cls in sorted(individuated, key=str):
        resolved, _ = K.resolve_particular_identities(kb, cls, events)
        identity[cls] = resolved
    return identity


def add_triggered_by(g: Graph, events: list, kb, identity: dict) -> None:
    """
    Add the missing AlarmMessage -triggeredBy-> target edge for each event's
    message node, in place — targeting the SPECIFIC FunctionalUnit particular
    that event's own archetype grounds (via the same particular_iri
    op_knowledge.py's ground_chain uses internally), falling back to the
    device itself when the archetype has no FunctionalUnit branch.

    Pointing straight at the device (an earlier version of this function did,
    unconditionally) is wrong whenever a device has more than one
    FunctionalUnit and more than one alarm is active on it at once — found
    while validating CAT2b against DATA/CAT_evaluation/cat2b_neg (an ECG
    alarm and an SpO2 alarm sharing one PhysiologicalMonitor): every alarm on
    that device reached EVERY FunctionalUnit the device had ever revealed,
    since mda:hasFunctionalUnit fans out from the one shared, stable device
    particular — nothing at the Device level says which FU a GIVEN alarm
    implicates. The static blueprint catalogue (kg_generated.ttl) never hits
    this: each archetype's mda:triggeredBy points to its OWN PRIVATE device
    blank node with only that one archetype's FU attached, never shared
    across archetypes the way the operational device_iri is. Targeting the
    FU directly sidesteps the fan-out instead of asking every rule query to
    disambiguate it. Rule files' property paths go straight from
    mda:triggeredBy to mda:hasSensor accordingly (no mda:hasFunctionalUnit
    hop) — see e.g. cat2b_metric_sensor.rq.

    `events` should be exactly the events revealed at the instant `g` was
    built for (Timeline.revealing_at), matching how background_at/
    situation_at already scoped `g`.
    """
    for e in events:
        type_iri = kb.type_index.get(e.label)
        if type_iri is None:
            continue
        arch = K.archetype_structure(kb, type_iri)
        msg = K.message_iri(e.device_id, e.start)
        fu = K.particular_iri(kb, arch, e.device_id, K.MDA.FunctionalUnit, identity)
        target = fu if fu is not None else K.device_iri(e.device_id)
        g.add((msg, K.MDA.triggeredBy, target))


def assess(rules: dict, query_graph: Graph, alarm) -> dict:
    """
    Run every loaded rule against `query_graph` with `?alarm` pre-bound to
    `alarm` (a URIRef). Returns {rule_id: [row_dict, ...]} for every rule
    that returned at least one row — an empty dict means nothing fired.

    `rules` values are load_rules()'s pre-compiled query objects, not text —
    Graph.query() accepts either interchangeably, with initBindings behaving
    identically either way; passing the compiled object is what actually
    avoids re-parsing on every call, not something this function does.
    """
    fired = {}
    for rule_id, compiled in rules.items():
        result = query_graph.query(compiled, initBindings={"alarm": alarm})
        rows = [
            {str(v): row[v] for v in result.vars if row[v] is not None}
            for row in result
        ]
        if rows:
            fired[rule_id] = rows
    return fired
