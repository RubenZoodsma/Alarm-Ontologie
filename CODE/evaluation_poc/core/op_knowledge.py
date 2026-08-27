"""
op_knowledge.py — knowledge helpers for the operational alarm simulation.

Four responsibilities, kept out of the simulation driver (op_inference.py):

  PARSING     read events, timestamps, durations; group per patient.
  EXTRACTION  load the knowledge base; resolve a label to its AlarmType
              archetype and read the structural knowledge off it; resolve
              type concepts to their scaffold entity IRIs.
  MINTING     build the instance ABox an alarm contributes while active,
              grounding the archetype onto the scaffold entity IRIs.
  INFERENCE   materialise OWL entailments with a real reasoner (owlrl), and
              expose the operation-state facts that persist post-alarm.

The time-advancing simulation itself lives in op_inference.py.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import owlrl
from rdflib import Graph, URIRef, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SKOS, XSD, DCTERMS, OWL

sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "shared"))
from ontology_tree import derive_class_tree, properties_below, walk_instances

# ── Paths ─────────────────────────────────────────────────────────────────────
#
# Reads the live FRAMEWORK/ pipeline output directly, plus DATA/POC_EVENTS
# for the operational inputs: FRAMEWORK/ONTOLOGY/ontology.ttl holds the TBox;
# FRAMEWORK/VOCABULARY/vocab_generated.ttl is the SKOS vocabulary,
# mechanically regenerated from FRAMEWORK/VOCABULARY/seed/vocab_base.ttl
# + the annotated CSV on every build_framework.py run — EXCEPT the Clinical
# Event scheme, which is not CSV-derived at all and so never appears there;
# it lives in its own hand-authored file, FRAMEWORK/VOCABULARY/seed/
# clinicalEvent_vocab.ttl (CLINICAL_EVENT_VOCAB below), parsed directly —
# load_kb() never parses vocab_base.ttl itself, so this is the only route
# by which clinicalEvent:CardiacArrest/RespiratoryArrest/... reach the
# runtime reasoning graph at all;
# FRAMEWORK/KNOWLEDGE_BASE/inference.ttl is the hand-authored bridge axioms
# (class-level owl:hasValue restrictions) that let an alarm's metric reach
# its physiology, and a device reach its therapeutic modality, by
# entailment. It must be loaded into reasoning_static alongside
# ontology.ttl/vocab.ttl for those entailments to fire — it carries no TBox
# declarations of its own, only axioms over concepts the other two already
# define.

OP_DIR        = Path(__file__).resolve().parent
ROOT          = OP_DIR.parent.parent.parent   # CODE/evaluation_poc/core -> evaluation_poc -> CODE -> repo root
FRAMEWORK_DIR = ROOT / "FRAMEWORK"
DATA_DIR      = ROOT / "DATA" / "POC_EVENTS"
ONTOLOGY  = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB     = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
CLINICAL_EVENT_VOCAB = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "clinicalEvent_vocab.ttl"
INFERENCE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"
CLINICAL_EVENTS = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "clinicalEvents.ttl"
CATALOGUE = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
ENTITIES  = DATA_DIR / "entities.ttl"
EVENTS    = DATA_DIR / "events_data.csv"

# ── Namespaces ────────────────────────────────────────────────────────────────

MDA      = Namespace("https://w3id.org/mda/ontology#")
ENTITY   = Namespace("https://w3id.org/mda/entity/")
SCAFFOLD = Namespace("https://w3id.org/mda/scaffold/")
INST     = Namespace("https://w3id.org/mda/instance/")
OPSTATE  = Namespace("https://w3id.org/mda/vocab/operation-state/")
SNAP     = Namespace("https://w3id.org/mda/snapshot/")


def _clean(s) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_")


def _local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


# ── PARSING ───────────────────────────────────────────────────────────────────

@dataclass
class Event:
    patient: str
    label: str
    device_id: str
    start: datetime
    end: datetime


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).strip())


def parse_duration(s: str) -> timedelta:
    """Minimal ISO-8601 duration parser for PT#H#M#S."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(s))
    if not m:
        return timedelta(0)
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=se)


def load_events(path: Path = EVENTS) -> list:
    df = pd.read_csv(path, sep=";", dtype=str).fillna("")
    df.columns = df.columns.str.strip()
    return [
        Event(r["patientID"].strip(), r["label"].strip(), r["device_id"].strip(),
              parse_ts(r["start"]), parse_ts(r["end"]))
        for _, r in df.iterrows()
    ]


def group_by_patient(events: list) -> dict:
    groups: dict = {}
    for e in events:
        groups.setdefault(e.patient, []).append(e)
    return groups


# ── EXTRACTION ────────────────────────────────────────────────────────────────

@dataclass
class KB:
    reasoning_static: Graph   # TBox + inference + vocabulary + scaffold (ABox context)
    catalogue: Graph          # kg_generated.ttl — the AlarmType archetypes
    type_index: dict          # alarm label  → AlarmType IRI
    scaffold_concepts: set    # concepts declared in the scaffold type catalogue
    window: timedelta         # post-alarm validity (PostAlarmValidScheme)
    tree: dict                # class nesting derived from the ontology
    concept_class: dict       # vocabulary concept → the class it instantiates
    node_kind: dict           # class → "individuated"|"referential"|"stateful", from mda:nodeKind
    last_wins: set            # every leaf/condition property, any class (see leaf_properties)
    reasoning_static_closed: Graph = None  # reasoning_static's own OWL-RL closure, computed
                                            # once here — see reason()'s docstring for why
    archetype_cache: dict = field(default_factory=dict)  # type_iri -> Archetype, memoised —
                                                           # see archetype_structure()
    refining_props_cache: dict = field(default_factory=dict)  # cls -> refining_properties(kb, cls)
    leaf_props_cache: dict = field(default_factory=dict)       # cls -> leaf_properties(kb, cls)


NODE_KIND_OF = {
    MDA.Individuated: "individuated",
    MDA.Referential:  "referential",
    MDA.Stateful:     "stateful",
}


def node_kinds(g: Graph) -> dict:
    """
    class → "individuated"|"referential"|"stateful", read from mda:nodeKind —
    the same annotation build_framework.py classifies its blueprint
    nodes by. Grounding uses this to decide, for ANY class the tree contains
    (not a fixed list), whether to mint a particular at all, and if so
    whether its identity is timeless background or per-alarm condition — see
    ground_chain. A class with no mda:nodeKind triple at all (e.g.
    mda:SignalAnalysis, deliberately: see ontology.ttl's comment on it) is
    absent from this dict; callers treat that as "structural" — connective
    plumbing with no leaves of its own, still minted, always background.
    """
    return {cls: NODE_KIND_OF[kind] for cls, kind in g.subject_objects(MDA.nodeKind)
            if kind in NODE_KIND_OF}


def concept_classes(g: Graph) -> dict:
    """
    Map every vocabulary concept to the ontology class it instantiates, via
    mda:instantiatesClass scheme bindings — the same declaration entity_seed.py
    and the readout use, so a node can be classified by what it *is* rather
    than by which property happened to reach it (see nodes_of_class).
    """
    mapping = {}
    for scheme, cls in g.subject_objects(MDA.instantiatesClass):
        for concept in g.subjects(SKOS.inScheme, scheme):
            if (concept, SKOS.topConceptOf, scheme) in g:
                continue          # the scheme's own top concept is scaffolding
            mapping[concept] = cls
    return mapping


def nodes_of_class(kb: KB, g: Graph, cls: URIRef) -> set:
    """
    Every node appearing in `g` (as subject or object) whose vocabulary concept
    instantiates `cls` — classification by concept membership, so a caller
    never needs to name the property that led to the node. A new or renamed
    edge into the same class (e.g. a future direct process→organ-system
    property, see clinical_axioms.ttl §3) is picked up with no code change.
    """
    nodes = {t for triple in g for t in (triple[0], triple[2])}
    return {n for n in nodes if kb.concept_class.get(n) == cls}


def load_kb() -> KB:
    static = Graph()
    for f in (ONTOLOGY, VOCAB, CLINICAL_EVENT_VOCAB, INFERENCE, CLINICAL_EVENTS, ENTITIES):
        static.parse(f, format="turtle")

    onto = Graph()
    onto.parse(ONTOLOGY, format="turtle")
    tree = derive_class_tree(onto, MDA.Alarm)

    catalogue = Graph()
    catalogue.parse(CATALOGUE, format="turtle")

    type_index = {
        str(lbl): s
        for s, lbl in catalogue.subject_objects(MDA.hasLabel)
        if (s, RDF.type, MDA.AlarmType) in catalogue
    }

    # The scaffold (entities.ttl) is a TYPE CATALOGUE, not a store of usable
    # particulars — its rows exist only to say "this concept is a legitimate
    # individuated type".  Grounding never reuses a scaffold IRI directly (see
    # sensor_iri/signal_iri): Sensor/Signal belong exclusively to the device/
    # sensor that owns them (mda:hasSensor and mda:producesSignal are declared
    # InverseFunctional in the ontology), so a flat, concept-keyed IRI cannot
    # be their identity without colliding across owners.
    scaffold = Graph()
    scaffold.parse(ENTITIES, format="turtle")
    scaffold_concepts = {
        t for _, t in scaffold.subject_objects(RDF.type)
        if "/vocab/" in str(t)
    }

    dur = next(static.objects(MDA.PostAlarmValidScheme, MDA.postAlarmValidityDuration),
               Literal("PT0S"))
    kb = KB(static, catalogue, type_index, scaffold_concepts,
            parse_duration(str(dur)), tree, concept_classes(static), node_kinds(static),
            last_wins=set())
    kb.last_wins = condition_properties(kb)

    # Computed once, reused by every reason() call for the rest of the
    # process's life instead of re-closing the same ~2000-triple static
    # graph from scratch on every one of potentially millions of calls (see
    # reason()'s docstring — profiled at 97% of a run's wall time before
    # this existed).
    kb.reasoning_static_closed = Graph()
    kb.reasoning_static_closed += static
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(kb.reasoning_static_closed)
    return kb


@dataclass
class Archetype:
    """
    One AlarmType archetype, read through the ontology's own nesting.

    The traversal is not written here: `walk_instances` follows the tree derived
    from rdfs:domain/rdfs:range, so a branch added to the ontology is followed
    without touching this class.  Callers ask for a class and a property by IRI
    rather than for a fixed key, which is what keeps this from drifting out of
    step with the blueprint the way a hand-coded path did.
    """
    nodes: dict          # class IRI → node in the catalogue
    catalogue: Graph
    vocab: Graph

    def node(self, cls: URIRef):
        """The archetype's node for a class, if the branch is populated."""
        return self.nodes.get(cls)

    def concept(self, cls: URIRef):
        """
        The vocabulary concept typing that node — its identity.  Recognised by
        an actual skos:Concept declaration, not by a substring of the IRI.
        """
        node = self.nodes.get(cls)
        if node is None:
            return None
        return next((t for t in self.catalogue.objects(node, RDF.type)
                     if (t, RDF.type, SKOS.Concept) in self.vocab), None)

    def value(self, cls: URIRef, prop: URIRef):
        """A leaf property's value on that class's node."""
        node = self.nodes.get(cls)
        if node is None:
            return None
        return next(self.catalogue.objects(node, prop), None)


def archetype_structure(kb: KB, type_iri: URIRef) -> Archetype:
    """
    Read one AlarmType archetype by walking the ontology-derived class tree.

    The archetype is typed mda:AlarmType, which is a subclass of mda:Alarm, so
    the walk starts at the tree's root.

    Memoised on kb.archetype_cache, keyed by type_iri alone: the result is a
    pure function of (kb.catalogue, kb.tree, type_iri), both fixed for the
    life of one `kb` (built once in load_kb()), and Archetype is read-only
    everywhere it's consulted — so every caller sharing a `kb` (every patient,
    every device) can safely share one Archetype per distinct archetype
    instead of re-walking kb.catalogue on every lookup. Was previously
    uncached, making this the dominant cost in mda_poc_assessment.py's
    per-alarm loop (every one of condition_for_event/ground_chain's callers/
    resolve_particular_identities/assess.py's add_triggered_by starts here).
    """
    if type_iri not in kb.archetype_cache:
        nodes = walk_instances(kb.catalogue, kb.tree, type_iri, MDA.Alarm)
        kb.archetype_cache[type_iri] = Archetype(nodes, kb.catalogue, kb.reasoning_static)
    return kb.archetype_cache[type_iri]


# ── MINTING ───────────────────────────────────────────────────────────────────

def device_iri(device_id: str) -> URIRef:
    return ENTITY[f"Device_{_clean(device_id)}"]


def patient_iri(patient_id: str) -> URIRef:
    return ENTITY[f"Patient_{_clean(patient_id)}"]


def refining_properties(kb: KB, cls: URIRef) -> list:
    """
    Leaf properties whose domain is `cls` and which are marked
    mda:refinesParticularIdentity: properties whose value distinguishes one
    minted particular of this class from another of the same type under the
    same owner (e.g. a device's pre-ductal vs post-ductal SpO2 sensor), rather
    than describing per-alarm/per-owner state.

    Deliberately NOT mda:refinesUniversalIdentity — that annotation marks
    properties that pre-coordinate into a shared vocabulary concept at the
    FRAMEWORK layer (e.g. Metric.hasPhase, Device.hasManufacturer), which is a
    different mechanism from folding a value into a grounded particular's IRI.
    The two are not interchangeable; see ontology.ttl's node-kind section.

    Read from the ontology so a new identity-refining column needs no code
    change here.

    Memoised on kb.refining_props_cache, keyed on cls alone: this is a pure
    function of the STATIC ontology (kb.reasoning_static never changes mid-
    run) — not of label, device_id, or identity — yet ground_chain's walk
    calls it once per tree class on EVERY invocation, and background_for_key
    only caches the walk's overall OUTPUT per (label, device_id). Two
    archetypes that both happen to populate e.g. Sensor were each re-running
    this rdflib query from scratch for the same cls, once per distinct
    (label, device_id) pair reached — bounded by pair count, not alarm count,
    but still real waste across a corpus with many distinct devices per
    label. Caching here benefits every caller, not just this one.
    """
    if cls not in kb.refining_props_cache:
        kb.refining_props_cache[cls] = sorted(
            (p for p in kb.reasoning_static.subjects(MDA.refinesParticularIdentity, Literal(True))
             if (p, RDFS.domain, cls) in kb.reasoning_static),
            key=str,
        )
    return kb.refining_props_cache[cls]


def leaf_properties(kb: KB, cls: URIRef) -> list:
    """
    Concept-valued properties on `cls` that describe a CONDITION rather than
    an identity: rdfs:domain cls, rdfs:range skos:Concept, excluding both
    mda:refinesUniversalIdentity (already implied for free by the node's
    type — see background_graph's docstring on hasManufacturer/hasDeviceType)
    and mda:refinesParticularIdentity (identity, handled by
    refining_properties/the minted IRI itself, not by this function).

    This is the generic counterpart to refining_properties: given any node
    the archetype minted, grounding its condition means reading every
    property this returns off the archetype and asserting it — see
    ground_leaf_properties. A new condition-carrying column (a future
    hasCableOperationState, say) needs an ontology declaration only, no
    change here or at any call site.

    Memoised on kb.leaf_props_cache, keyed on cls alone — same rationale as
    refining_properties' own cache: a pure function of the static ontology,
    called once per tree class on every ground_chain walk, previously
    re-querying kb.reasoning_static from scratch every time regardless of
    which label or device_id triggered the walk.
    """
    if cls not in kb.leaf_props_cache:
        kb.leaf_props_cache[cls] = sorted(
            (p for p in kb.reasoning_static.subjects(RDFS.domain, cls)
             if (p, RDF.type, OWL.ObjectProperty) in kb.reasoning_static
             and (p, RDFS.range, SKOS.Concept) in kb.reasoning_static
             and (p, MDA.refinesUniversalIdentity, Literal(True)) not in kb.reasoning_static
             and (p, MDA.refinesParticularIdentity, Literal(True)) not in kb.reasoning_static),
            key=str,
        )
    return kb.leaf_props_cache[cls]


def condition_properties(kb: KB) -> set:
    """
    Every property leaf_properties would return, for ANY class — the full
    set of single-valued condition properties a per-alarm condition graph can
    carry, regardless of which node kind they land on (Metric, Signal,
    Sensor, Component, Device, ...). Used to decide LAST_WINS on merge (see
    merge_conditions): each describes a node's current condition, not an
    accumulating fact, so a later alarm's value overwrites an earlier one on
    the same subject. Computed once from the ontology (see KB.last_wins) so a
    new condition property on any class is covered without touching this
    file.
    """
    domains = {d for d in kb.reasoning_static.objects(None, RDFS.domain) if isinstance(d, URIRef)}
    return {p for cls in domains for p in leaf_properties(kb, cls)}


def ground_leaf_properties(g: Graph, kb: KB, arch, cls: URIRef, node) -> None:
    """Assert every leaf_properties(kb, cls) value the archetype supplies onto `node`."""
    if node is None:
        return
    for prop in leaf_properties(kb, cls):
        value = arch.value(cls, prop)
        if value is not None:
            g.add((node, prop, value))


def resolve_particular_identities(kb: KB, cls: URIRef, events: list) -> tuple:
    """
    For every (device_id, concept) pair `cls`'s occurrences reference across
    `events`, decide whether every refinement dict witnessed for it may be
    safely merged into one grounded particular.

    Two mentions merge if they never assert DIFFERENT values for the same
    refining property (refining_properties, i.e. mda:refinesParticularIdentity)
    — one mention lacking a value never conflicts with another supplying it,
    so a bare 'PulseOximeter_sensor' mention and a 'PulseOximeter_sensor' +
    postDuctal mention merge, taking the UNION of every (property, value)
    pair witnessed as the resolved identity (not just the most-specific
    single mention — two mentions could each supply a different property
    and only jointly describe the one sensor fully).

    A genuine conflict — two DIFFERENT explicit values for the same
    property anywhere in the group, e.g. postDuctal vs preDuctal — means
    there really are (at least) two particulars. The WHOLE (device_id,
    concept) group then falls back to today's behaviour instead: identity
    stays keyed on each occurrence's own exact refinement tuple, no
    merging at all, including for any unrefined mention — it is never
    guessed onto one of the conflicting variants, since nothing in the
    data says which one it belongs to.

    `events` must be one device's (or one patient's) FULL event history,
    never a subset of a single tick — resolving from a partial view would
    make the merge decision depend on which alarms happen to be active at
    that instant, which must not affect a device's identity.

    Returns ({(device_id, concept): refinements}, [(device_id, concept,
    property, {conflicting values})]): the dict covers only safely-merged
    groups — a caller falls back to computing its own event's exact tuple
    for any (device_id, concept) absent from it; the list is for reporting
    (op_inference.py / op_visual.py print it as a [WARN]).
    """
    refiners = refining_properties(kb, cls)
    by_key = defaultdict(list)
    for ev in events:
        type_iri = kb.type_index.get(ev.label)
        if type_iri is None:
            continue
        arch = archetype_structure(kb, type_iri)
        concept = arch.concept(cls)
        if concept is None:
            continue
        refinements = {p: v for p in refiners if (v := arch.value(cls, p)) is not None}
        by_key[(ev.device_id, concept)].append(refinements)

    resolved, conflicts = {}, []
    for key, dicts in by_key.items():
        values_by_prop = defaultdict(set)
        for d in dicts:
            for p, v in d.items():
                values_by_prop[p].add(v)
        conflicting = {p: vs for p, vs in values_by_prop.items() if len(vs) > 1}
        if conflicting:
            for p, vs in sorted(conflicting.items(), key=lambda kv: str(kv[0])):
                conflicts.append((*key, p, vs))
        else:
            resolved[key] = {p: next(iter(vs)) for p, vs in values_by_prop.items()}
    return resolved, conflicts


def _refined_suffix(concept: URIRef, refinements: dict) -> str:
    suffix = _local(concept)
    for prop in sorted(refinements or {}, key=str):
        suffix += f"_{_local(refinements[prop])}"
    return suffix


def particular_iri(kb: KB, arch: "Archetype", device_id: str, cls: URIRef,
                    identity: dict = None) -> URIRef:
    """
    A stable entity IRI for `cls`'s particular in this archetype, grounded to
    `device_id` — generalises the old sensor_iri/signal_iri/component_iri
    (one hand-written function per class) into one function driven entirely
    by kb.tree, so a class inserted anywhere in the ontology's chain (as
    mda:FunctionalUnit was) needs no new minting function and no new call
    site here.

    Deterministic in (device_id, the full ancestor chain of concepts +
    refining-identity values from mda:Device down to cls) — this is what
    keeps an owl:InverseFunctionalProperty-declared link (mda:isMonitoredBy
    et al.) from colliding two different owners onto one IRI, the same
    guarantee sensor_iri's docstring described for exactly one hop,
    generalised to however many hops the tree actually has. A class with no
    specific concept in this archetype (the generic connective nodes
    build_framework.py inserts for e.g. mda:Sensor/mda:Signal/
    mda:SignalAnalysis on most Metric-bearing rows — see mda:FunctionalUnit's
    recipe axioms in inference.ttl) still mints, keyed by its bare class name
    instead of a concept — the chain stays deterministic and shared across
    every occurrence of the same (device, archetype) pair either way.

    `identity` is the {class: {(device_id, concept): refinements}} map
    resolve_particular_identities resolves once per patient (see
    build_timeline) — looked up per ancestor hop so two mentions of the same
    ancestor across different alarms land on the same particular. Falls back
    to this archetype's own refinements when the ancestor/concept pair is
    absent from it (unresolved — no cross-event history to merge from, e.g.
    a single-event caller).

    Returns None if `cls` has no node at all in this archetype (its branch
    is inactive).
    """
    identity = identity or {}
    if cls == MDA.Device:
        return device_iri(device_id)
    if arch.node(cls) is None:
        return None

    def own_refinements(c: URIRef) -> dict:
        concept = arch.concept(c)
        own = {p: v for p in refining_properties(kb, c) if (v := arch.value(c, p)) is not None}
        return identity.get(c, {}).get((device_id, concept), own)

    suffixes, node_cls = [], cls
    while node_cls is not None and node_cls != MDA.Device:
        concept = arch.concept(node_cls)
        suffixes.append(_refined_suffix(concept, own_refinements(node_cls))
                         if concept is not None else _local(node_cls))
        link = kb.tree.get(node_cls)
        node_cls = link[0] if link else None
    return ENTITY[f"{_local(cls)}_{_clean(device_id)}_" + "_".join(reversed(suffixes))]


def alarm_iri(device_id: str, start: datetime) -> URIRef:
    return INST[f"Alarm_{_clean(device_id)}_{start.strftime('%Y%m%dT%H%M%S')}"]


def message_iri(device_id: str, start: datetime) -> URIRef:
    return INST[f"Msg_{_clean(device_id)}_{start.strftime('%Y%m%dT%H%M%S')}"]


def ground_chain(kb: KB, arch: "Archetype", device_id: str, identity: dict = None) -> tuple:
    """
    Walk every class the archetype populates below mda:Device, minting a
    stable particular for each (particular_iri) and asserting the link via
    whatever property kb.tree records for that hop — read from the ontology,
    never hardcoded, so a renamed or newly-inserted property/class (as
    mda:FunctionalUnit was) needs no change here. Generalises what used to be
    background_graph's and condition_for_event's separate, hand-written
    per-class logic (Device→Sensor→Signal, Device→Component, Sensor→Metric)
    into one recursive walk that follows however deep the tree actually goes
    — mirrors build_framework.py's resolve_node, minting stable
    per-device IRIs instead of fresh blank nodes.

    Returns (background, condition, leaves):

      - An Individuated node's own identity (type + refining-identity leaves,
        e.g. Sensor.hasAnatomicalPosition) is BACKGROUND — timeless
        composition of the device, unaffected by any alarm.
      - Its condition-carrying leaves (leaf_properties — e.g.
        hasSensorOperationState, hasQualityState) are CONDITION — situational,
        true only while the alarm reporting them is active.
      - A Stateful node (mda:Metric) exists only because an alarm currently
        reports it, so BOTH its identity and its leaves are CONDITION.
      - A node with no mda:nodeKind marker at all (structural connective
        plumbing with no leaves of its own, e.g. mda:SignalAnalysis, or the
        generic untyped mda:Sensor/mda:Signal nodes most Metric-bearing rows
        now carry — see inference.ttl's FunctionalUnit recipes) is minted the
        same way and goes to BACKGROUND: it never varies per alarm.
      - A Referential-kind class (e.g. mda:AnatomicalPosition itself, as
        opposed to the hasAnatomicalPosition VALUE asserted on its owning
        node) is never descended into — those concepts are reached by
        inference.ttl's axioms (clinical_predicates/entailed_situation),
        never asserted explicitly here.
      - `leaves` is every (class, particular) pair the walk minted that has
        no further populated child of its own — the terminus/termini of
        THIS archetype's own technical trace, in CQ2's sense of "wherever
        the trace terminates" (Sensor for a bare connectivity fault, Signal
        for a quality fault, Metric for a full measurement — see CQ2's own
        header). Used by alarm_message() to assert mda:triggeredByStructure,
        so a query can join an alarm to its own reading instead of to
        anything merely co-present in the same tick.

    `identity` is threaded straight through to particular_iri — see its
    docstring and build_timeline, which resolves it once per patient across
    every Individuated class, not just Sensor/Component.
    """
    background, condition, leaves = Graph(), Graph(), []

    def walk(parent_cls: URIRef, parent_particular) -> None:
        had_child = False
        for child_cls, link in kb.tree.items():
            if link is None or link[0] != parent_cls:
                continue
            prop = link[1]
            kind = kb.node_kind.get(child_cls)
            if kind == "referential" or arch.node(child_cls) is None:
                continue
            concept = arch.concept(child_cls)
            # entities.ttl (kb.scaffold_concepts) only catalogues Individuated
            # classes (see entity_seed.py) — mda:Stateful/structural classes
            # (Metric, SignalAnalysis) were never meant to appear there, so the
            # legitimacy check applies only where it was actually populated for.
            if kind == "individuated" and concept is not None and concept not in kb.scaffold_concepts:
                continue
            particular = particular_iri(kb, arch, device_id, child_cls, identity)
            if particular is None:
                continue
            had_child = True
            target = condition if kind == "stateful" else background
            target.add((parent_particular, prop, particular))
            target.add((particular, RDF.type, concept if concept is not None else child_cls))
            for rprop in refining_properties(kb, child_cls):
                v = arch.value(child_cls, rprop)
                if v is not None:
                    target.add((particular, rprop, v))
            for lprop in leaf_properties(kb, child_cls):
                v = arch.value(child_cls, lprop)
                if v is not None:
                    condition.add((particular, lprop, v))
            walk(child_cls, particular)
        if not had_child and parent_cls != MDA.Device:
            leaves.append((parent_cls, parent_particular))

    walk(MDA.Device, device_iri(device_id))
    return background, condition, leaves


def background_for_key(kb: KB, label: str, device_id: str, identity: dict) -> Graph:
    """
    One (label, device_id) pair's contribution to background_graph: the
    device's type/identifier triples (if the archetype has a device concept)
    plus ground_chain's background half. Pure function of its arguments for a
    given `kb` — this is what Timeline._background_by_key memoises per key,
    so an archetype repeated across many events (the same alarm recurring, or
    different alarms sharing a device) costs one ground_chain walk, not one
    per occurrence. Returns an empty Graph if `label` doesn't resolve to a
    known AlarmType, matching background_graph's previous per-event skip.
    """
    type_iri = kb.type_index.get(label)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    g = Graph()
    dev = device_iri(device_id)
    device_type = arch.concept(MDA.Device)
    if device_type:
        g.add((dev, RDF.type, device_type))
        g.add((dev, DCTERMS.identifier, Literal(device_id)))
    bg, _, _ = ground_chain(kb, arch, device_id, identity)
    g += bg
    return g


def background_graph(kb: KB, events: list, identity: dict = None) -> Graph:
    """
    The persistent merge anchors — the shared blocks alarms reason and merge
    over, stated once: the patient (identified), the device's type, and its
    full technical composition (ground_chain) — everything below mda:Device
    that is not mda:Stateful.

    Deliberately NOT mda:isMonitoredBy: a device's own identity and
    composition (its type, its sensors) is timeless background knowledge, but
    which patient it is currently monitoring is not — the same physical
    device can serve different patients at different times (see
    events_seed.py's device pool). mda:isMonitoredBy is now declared
    owl:InverseFunctionalProperty (a device monitors at most one patient at a
    time), so asserting it here — once, into the persistent background that
    op_inference.py writes to the shared default graph — would put two
    patients who ever shared a device permanently in the same graph, which
    the reasoner would then correctly (and unhelpfully) read as evidence
    they are the same individual. It is asserted per-tick instead, scoped to
    only the alarms actually active at that instant — see
    Timeline.situation_at.

    The device's manufacturer/model are NOT asserted here as separate triples.
    hasManufacturer and hasDeviceType are mda:refinesUniversalIdentity, so the
    archetype's Device node — and therefore `device_type` below — is already
    typed by a pre-coordinated concept (e.g. device:PhysiologicalMonitor_
    Philips, see build_framework.py's precoordinate()) whose OWL definition
    entails both facts on any instance of that type. Asserting them again
    here would duplicate what the type already gives for free.

    `identity` is the {class: {(device_id, concept): refinements}} map from
    build_timeline (resolve_particular_identities, run once over the
    device's/patient's full event history — never re-derived here from
    `events`, which via Timeline.background_at may be only the subset
    revealing at one tick; the merge decision must not depend on that).
    Defaults to empty so a caller with no cross-event history to resolve
    (e.g. a single-event test) can omit it.

    Delegates the actual per-archetype work to background_for_key, once per
    DISTINCT (label, device_id) pair rather than once per raw event — the
    same graph either way (RDF Graph is a set; a repeated pair's triples are
    idempotent), just without re-walking ground_chain for a pair already
    seen in this same `events` list.
    """
    identity = identity or {}
    valid = [ev for ev in events if kb.type_index.get(ev.label) is not None]
    g = Graph()
    for patient in {ev.patient for ev in valid}:
        p = patient_iri(patient)
        g.add((p, RDF.type, MDA.Patient))
        g.add((p, DCTERMS.identifier, Literal(patient)))
    for label, device_id in sorted({(ev.label, ev.device_id) for ev in valid}):
        g += background_for_key(kb, label, device_id, identity)
    return g


def alarm_message(kb: KB, ev: Event, identity: dict = None) -> Graph:
    """
    The alarm and its per-alarm message node.  hasMessage points to a distinct
    mda:AlarmMessage (not the patient); the message concernsPatient the shared,
    persistent patient — the merge anchor.

    The message also gets one mda:triggeredByStructure edge per leaf
    ground_chain finds for this archetype — the terminus/termini of THIS
    alarm's own technical trace (see ground_chain's own docstring), not
    just any node that happens to be in the same background/condition
    graph. This is what lets a query join an alarm to its own reading
    instead of to anything merely co-present at the same tick (see
    EVALUATION/CQs/temporal/CQ9_technical_invalidity.rq).
    """
    g = Graph()
    type_iri = kb.type_index.get(ev.label)
    if type_iri is None:
        return g
    arch = archetype_structure(kb, type_iri)
    a = alarm_iri(ev.device_id, ev.start)
    msg = message_iri(ev.device_id, ev.start)
    patient = patient_iri(ev.patient)

    g.add((a, RDF.type, MDA.Alarm))
    g.add((a, MDA.isOfType, type_iri))
    g.add((a, MDA.hasLabel, Literal(ev.label, lang="en")))
    g.add((a, MDA.hasStart, Literal(ev.start.isoformat(), datatype=XSD.dateTime)))
    g.add((a, MDA.hasEnd, Literal(ev.end.isoformat(), datatype=XSD.dateTime)))
    ground_leaf_properties(g, kb, arch, MDA.Alarm, a)
    g.add((a, MDA.hasMessage, msg))
    g.add((msg, RDF.type, MDA.AlarmMessage))
    g.add((msg, MDA.concernsPatient, patient))
    _, _, leaves = ground_chain(kb, arch, ev.device_id, identity)
    for _cls, particular in leaves:
        g.add((msg, MDA.triggeredByStructure, particular))
    return g


def alarm_condition(kb: KB, ev: Event, identity: dict = None) -> Graph:
    """
    The alarm's situational content, asserted onto the shared merge anchors so
    that concurrent alarms accumulate into one coherent picture: the clinical
    condition (metric identity + rate/rhythm), the operation state of
    whichever node the archetype reaches (sensor, functional unit, component,
    device), and the signal's quality state.  Conflicting values are resolved
    last-wins at merge time (see merge_conditions).
    """
    return condition_for_event(kb, ev.label, ev.device_id, identity)


def condition_for_event(kb: KB, label: str, device_id: str, identity: dict = None) -> Graph:
    """
    The condition an alarm of this kind, on this device, contributes.

    It depends only on the archetype and the device it is grounded to, never on
    the occurrence: the nodes it attaches to are that device's own particulars
    (ground_chain/particular_iri — never a shared, type-level anchor), and the
    values come from the blueprint. That is what lets a timeline compute one
    condition per distinct (label, device) pair instead of one per alarm,
    while keeping two devices of the same type — or the same device across
    two patients — from colliding onto one node.

    Delegates entirely to ground_chain, the same walk background_graph uses —
    only the CONDITION half of its return matters here (a Stateful node's
    identity, e.g. mda:Metric typed to metric:SpO2, plus every node's
    condition-carrying leaf_properties, e.g. hasSensorOperationState,
    hasQualityState). Whatever class the ontology declares those on — Metric,
    Signal, Sensor, FunctionalUnit, Component, Device, or a class not yet
    invented — needs no change here.

    `identity` is the same resolved {class: {(device_id, concept):
    refinements}} map background_graph takes (see resolve_particular_identities)
    — a (label, device) pair must resolve to the very same particular IRIs
    background_graph minted for it, or the two would talk past each other.
    """
    type_iri = kb.type_index.get(label)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    _, condition, _ = ground_chain(kb, arch, device_id, identity)
    ground_leaf_properties(condition, kb, arch, MDA.Device, device_iri(device_id))
    return condition


def merge_conditions(graphs: list, last_wins: set) -> Graph:
    """
    Merge condition graphs given in ascending start order.  Non-functional
    facts union; for `last_wins` properties (see KB.last_wins/
    condition_properties) the latest alarm's value replaces any earlier one
    on the same subject — each describes a single current condition (e.g.
    Impaired → CriticallyImpaired), not an accumulating fact.
    """
    g = Graph()
    for src in graphs:
        for s, p, o in src:
            if p in last_wins:
                g.remove((s, p, None))
            g.add((s, p, o))
    return g


# ── INFERENCE ─────────────────────────────────────────────────────────────────

@dataclass
class Timeline:
    """
    One patient's monitoring window, with the situational graph computable at
    any instant.

    The per-event graphs are built once and reused across ticks, so stepping
    through time is cheap enough to drive a UI.  Both the TriG writer
    (op_inference.py) and the visual (op_visual.py) go through this, so the two
    cannot disagree about what the situation at a given instant is.
    """
    patient: str
    events: list
    background: Graph
    ticks: list
    window: timedelta
    _kb: KB
    _messages: dict          # id(event) → alarm + message node (unique per event)
    _conditions: dict        # (label, device_id) → condition graph
    _persisted_cache: dict   # (label, device_id) → post-alarm operation state, LAZILY
                             # memoised on first actual read — see _persisted_for
    _cache: dict             # active (label, device) sequence → merged + entailed
    _bg_cache: dict          # revealing (label, device) set → revealed anchors. Deliberately
                             # PRIVATE to this Timeline, never reasoning_cache-shared: the key
                             # carries no patient identity but the cached value (via
                             # _background_from_keys) embeds this Timeline's own patient triples,
                             # so sharing risks leaking one patient's identity into another's
                             # result if their revealing sets ever coincide.
    _background_by_key: dict # (label, device_id) → background_for_key's contribution, memoised
    _identity: dict          # class → {(device_id, concept): resolved refinements} — every
                             # Individuated class, not just Sensor/Component (resolve_particular_identities)

    # The four fields below exist ONLY for observe()'s incremental path — see
    # empty()/observe()'s own docstrings. build_timeline() never sets them
    # (defaults cover it); nothing in the batch path (CQ9-11, op_visual.py,
    # op_inference.py) reads them.
    _identity_refiners: dict = field(default_factory=dict)   # class → refining_properties(kb, class), cached once
    _identity_state: dict = field(default_factory=dict)      # class → {(device_id, concept): {prop: {values}}}
    _identity_conflicted: dict = field(default_factory=dict) # class → {(device_id, concept) keys ever conflicting}
    _reasoning_cache: dict = field(default_factory=dict)     # the shared cache empty()/observe() were given

    @classmethod
    def empty(cls, kb: "KB", patient: str, reasoning_cache: dict = None) -> "Timeline":
        """
        A Timeline with zero events observed — the starting state for
        observe()'s incremental path, used by mda_poc_assessment.py instead
        of calling build_timeline(events_so_far) fresh at every arrival.

        build_timeline itself is UNCHANGED and still the right call for
        CQ9-11/op_visual.py/op_inference.py, which reason over an
        already-complete corpus and have no arrival-order claim to keep (see
        mda_poc_assessment.py's own module docstring, "Why re-resolve
        identity per alarm"). empty()+observe() exists ONLY to make that
        same arrival-ordered, causally-restricted result cheaper to compute:
        one Timeline per patient, extended by one alarm at a time, instead of
        rebuilt from scratch — over the growing prefix — at every single
        arrival. See observe()'s own docstring for why this produces the
        IDENTICAL result to calling build_timeline(events_so_far) fresh each
        time, not merely a similar one.
        """
        cache = reasoning_cache if reasoning_cache is not None else {}
        individuated = [c for c, kind in kb.node_kind.items()
                        if kind == "individuated" and c not in (MDA.Device, MDA.Patient)]
        return cls(
            patient=patient,
            events=[],
            background=Graph(),
            ticks=[],
            window=kb.window,
            _kb=kb,
            _messages={},
            _conditions={},
            _persisted_cache=cache.setdefault("persisted", {}),
            _cache=cache.setdefault("situation", {}),
            _bg_cache={},
            _background_by_key={},
            _identity={c: {} for c in individuated},
            _identity_refiners={c: refining_properties(kb, c) for c in individuated},
            _identity_state={c: {} for c in individuated},
            _identity_conflicted={c: set() for c in individuated},
            _reasoning_cache=cache,
        )

    def observe(self, event) -> None:
        """
        Incorporate exactly one new event into this patient's running state.
        MUST be called in arrival order (event.start non-decreasing across
        calls) — this is what makes the result causal at all; nothing here
        checks or enforces that ordering itself, the same as build_timeline
        never checking that `events` arrived in order either.

        Produces the IDENTICAL state build_timeline(events_so_far) would for
        the same prefix, not merely an equivalent one — verified by diffing
        mda_poc_assessment.py's outcomes CSV against the pre-observe()
        per-tick-rebuild version on every dataset available at the time this
        was written (DATA/CAT_evaluation, DATA/POC_EVENTS, DATA/LOCKED_CORPUS
        1- and 10-patient exports). Re-run that diff after touching this
        method — the equivalence is what makes it safe, not the reasoning
        below on its own.

        Why identity resolution is safe to make incremental (an event at a
        time) rather than batch (the whole prefix, every time): both are the
        exact same union/conflict computation over the exact same set of
        (event, refinement) pairs — resolve_particular_identities forms
        values_by_prop = union of every refinement dict witnessed for a key,
        then checks len(values) > 1 per property. A union built by adding one
        event's refinements at a time equals the union built from the whole
        list at once — set union is associative and commutative, so there is
        no ordering-dependent step to get wrong. The only asymmetry against
        the batch path: once a (device_id, concept) key conflicts, it stays
        conflicting for the rest of this Timeline's life, and an EARLIER
        event already processed while that key still looked resolved keeps
        whatever particular IRI it was already given — never retroactively
        re-minted. This is not a compromise unique to going incremental: the
        existing per-tick full-rebuild (events_so_far recomputed fresh every
        arrival) has the identical property, since a tick's own outcome is
        already computed and logged before the NEXT tick's larger prefix
        could reveal a conflict. Confirmed dormant either way: the only two
        properties resolve_particular_identities' conflict logic ever
        touches are mda:hasAnatomicalPosition/mda:hasLaterality (see
        refining_properties()), and none of the CAT1/CAT2 rules query
        either — see cat_rules_report.md.

        Why (label, device_id) grounding is frozen at first sight, for BOTH
        background_by_key AND conditions (not just background_by_key, which
        is what the pre-observe() code already froze via reasoning_cache):
        the pre-observe() code recomputed `conditions` fresh every tick from
        whatever self._identity happened to be at that tick, which COULD in
        principle diverge from the frozen background_by_key entry for the
        same key if identity for an underlying concept changed between the
        tick that first cached background_by_key[key] and a later tick's
        conditions computation — a latent inconsistency in the pre-observe()
        code, not introduced here. observe() freezes both the same way
        (compute once, on first sight, together) specifically to remove that
        inconsistency rather than reproduce it. This is verified inert on
        every dataset available (same reason as above: no refinement ever
        actually varies for the same key in this data), and is simpler and
        more clearly correct than what it replaces regardless.
        """
        kb = self._kb
        key = (event.label, event.device_id)
        type_iri = kb.type_index.get(event.label)

        if type_iri is not None:
            arch = archetype_structure(kb, type_iri)
            for cls in self._identity_state:
                concept = arch.concept(cls)
                if concept is None:
                    continue
                ik = (event.device_id, concept)
                if ik in self._identity_conflicted[cls]:
                    continue  # already conflicting — stays conflicting, never re-examined
                values_by_prop = self._identity_state[cls].setdefault(ik, {})
                refinements = {p: v for p in self._identity_refiners[cls]
                               if (v := arch.value(cls, p)) is not None}
                for p, v in refinements.items():
                    values_by_prop.setdefault(p, set()).add(v)
                conflicting = any(len(vs) > 1 for vs in values_by_prop.values())
                if conflicting:
                    self._identity_conflicted[cls].add(ik)
                    self._identity[cls].pop(ik, None)
                else:
                    self._identity[cls][ik] = {p: next(iter(vs)) for p, vs in values_by_prop.items()}

        if key not in self._conditions:
            self._conditions[key] = condition_for_event(kb, event.label, event.device_id, self._identity)
            bg_cache = self._reasoning_cache.setdefault("background_by_key", {})
            if key not in bg_cache:
                bg_cache[key] = background_for_key(kb, event.label, event.device_id, self._identity)
            self._background_by_key[key] = bg_cache[key]
            self.background += bg_cache[key]
            if type_iri is not None:
                # Idempotent (Graph is a set) — safe to add on every new key
                # rather than checking first, matching background_graph's
                # own unconditional add for the same two triples.
                p = patient_iri(self.patient)
                self.background.add((p, RDF.type, MDA.Patient))
                self.background.add((p, DCTERMS.identifier, Literal(self.patient)))

        self._messages[id(event)] = alarm_message(kb, event, self._identity)
        self.events.append(event)

    def active_at(self, t: datetime) -> list:
        """Alarms valid at `t`, in ascending start order (so last wins)."""
        return sorted((e for e in self.events if e.start <= t <= e.end),
                      key=lambda e: e.start)

    def _persisted_for(self, key: tuple) -> Graph:
        """
        This (label, device_id) key's persisting-state facts (inferred_states),
        computed and memoised on first actual read rather than eagerly for
        every key build_timeline sees. inferred_states is a full OWL-RL
        reasoning pass (owlrl.DeductiveClosure) — the dominant cost in this
        whole pipeline (~2.5s+/call, see reason()'s docstring) — and both
        callers (revealing_at's `valid`, situation_at's persistence loop)
        only ever consult a key when `e.end < t < e.end + self.window`,
        i.e. an event actually inside another's post-alarm window at the
        instant being queried. Empirically (POC_EVENTS, 100 alarms) only 14
        of 91 distinct keys are EVER read that way — eagerly reasoning about
        all 91 in build_timeline (the previous behaviour) paid full closure
        cost for the ~85% that were never consulted. Backed by the same
        reasoning_cache-shared `_persisted_cache` dict as before, so cross-
        patient reuse for the keys that ARE read is unchanged.
        """
        if key not in self._persisted_cache:
            self._persisted_cache[key] = inferred_states(self._kb, self.background + self._conditions[key])
        return self._persisted_cache[key]

    def revealing_at(self, t: datetime) -> list:
        """
        Alarms whose knowledge is valid at `t`, and so reveals structural
        context: the active ones, plus any in their post-alarm window that
        actually leave *persisting operation state* behind.

        The second clause is deliberately narrower than "any alarm still in its
        window".  Per the temporal model only operation state survives past an
        alarm's end — a plain physiological alarm's situational facts expire at
        `end` and leave nothing — so once ended it should stop revealing its
        sensor.  A technical alarm whose inferred `Enabled` lingers keeps its
        device/sensor/signal on screen for exactly as long as that state holds,
        which is what stops the persisting state from floating unattached.
        """
        def valid(e):
            if e.start <= t <= e.end:
                return True
            return (e.end < t < e.end + self.window
                    and len(self._persisted_for((e.label, e.device_id))) > 0)
        return sorted((e for e in self.events if valid(e)),
                      key=lambda e: e.start)

    def background_at(self, t: datetime) -> Graph:
        """
        The merge anchors revealed by the alarms valid at `t` — the patient,
        the devices those alarms came from, and only the sensors and signals
        those alarms actually implicate.  Includes alarms in their post-alarm
        window, so the operation state persisting after an alarm ends stays
        attached to the patient and device it belongs to rather than floating.

        `self.background` is the accumulation over the patient's whole stream,
        which is what the TriG default graph holds.  That is knowledge about the
        deployment, not about this instant: at a tick where only an SpO2 alarm is
        firing it would still show the ECG and arterial-line sensors, which no
        active alarm has told us anything about.  A view of the situation asks
        for this instead.

        self._bg_cache (the memoisation of this method's own result) is
        deliberately NOT reasoning_cache-shared across patients: the key
        carries no patient identity while the cached value (via
        _background_from_keys) embeds this Timeline's own patient triples,
        so sharing would risk leaking one patient's identity into another's
        result if their revealing sets ever coincide.
        """
        revealing = self.revealing_at(t)
        key = tuple(sorted((e.label, e.device_id) for e in revealing))
        if key not in self._bg_cache:
            self._bg_cache[key] = self._background_from_keys(key)
        return self._bg_cache[key]

    def _background_from_keys(self, keys: tuple) -> Graph:
        """
        Union self._background_by_key's memoised contribution for each
        (label, device_id) key, plus this Timeline's own patient triples if
        at least one key resolves to a known archetype — mirrors
        background_graph's per-event skip of an unresolvable label, applied
        per distinct key here instead of per raw event.
        """
        g = Graph()
        if any(self._kb.type_index.get(label) is not None for label, _ in keys):
            p = patient_iri(self.patient)
            g.add((p, RDF.type, MDA.Patient))
            g.add((p, DCTERMS.identifier, Literal(self.patient)))
        for k in keys:
            g += self._background_by_key[k]
        return g

    def situation_at(self, t: datetime, mode: str = "situational") -> Graph:
        """
        The situational awareness at `t` — the tick's named-graph content.

        `mode` selects how much of the reasoner's output is folded in:
        "situational" (default, unchanged behaviour) keeps only
        mda:situational-tagged predicates — exactly what op_inference.py
        writes to kg_inference.trig. "all" keeps everything the reasoner
        actually entailed reachable from this tick's own nodes, including
        background/taxonomic facts (e.g. an entailed rdf:type superclass
        membership) that mda:situational deliberately excludes from the
        export. op_inference.py never passes mode — its output is
        unaffected by this parameter's existence. op_visual.py uses both, so
        a viewer can toggle between them without kg_inference.trig changing
        at all.

        Reasons over background_at(t) — the devices/sensors/signals THIS
        tick's alarms actually reveal — not self.background, which is the
        accumulation over the patient's whole stream. mda:administers and
        mda:approximates are entailed purely from a node's TYPE, so a device
        or metric needs no active alarm to produce them; reasoning over the
        full background would extract them for every device the patient has
        ever had an alarm on, not just the one implicated right now — a
        ventilator alarm's snapshot would carry an unrelated ECMO circuit's
        therapeutic modality. That is what background_at(t) exists to
        prevent for the drawn anchors; it needs to bound the reasoning input
        too, not just what op_visual.py draws on top of it.

        Everything below the per-alarm message depends only on *which* archetypes
        are active and in what order, not on which particular alarms they are, so
        the reasoned part is cached against that sequence.  Two bursts of the same
        alarms therefore cost one closure, not two — for both modes at once,
        since entailed_situation computes them from a single reasoning pass.
        Caching by `active` alone (not the broader `revealing`, which
        background_at(t) is keyed on) stays correct here: which OTHER,
        lingering devices happen to also be revealing at a given instant
        never changes what a type-derived fact entails for the actively
        alarming device itself.
        """
        g = Graph()
        active = self.active_at(t)
        patient = patient_iri(self.patient)
        for e in active:
            g += self._messages[id(e)]              # unique per alarm occurrence
        # isMonitoredBy is asserted here, not in background: it is true only
        # while this tick reveals the device (active OR still within its
        # post-alarm persistence window), never a standing fact — see
        # background_graph's docstring for why. Keyed on revealing_at, not
        # active_at, so it always matches the anchors background_at(t) draws
        # for this same tick (both derive from the same set) — otherwise a
        # persisting post-alarm device/patient pair is drawn with no edge
        # connecting them.
        for e in self.revealing_at(t):
            g.add((patient, MDA.isMonitoredBy, device_iri(e.device_id)))

        key = tuple((e.label, e.device_id) for e in active)
        if key not in self._cache:
            merged = merge_conditions([self._conditions[(e.label, e.device_id)]
                                        for e in active], self._kb.last_wins)
            situational, everything = Graph(), Graph()
            situational += merged
            everything += merged
            if active:
                states, clinical, all_entailed = entailed_situation(
                    self._kb, self.background_at(t) + merged)
                situational += states
                situational += clinical
                everything += states
                everything += all_entailed
            self._cache[key] = (situational, everything)
        g += self._cache[key][0 if mode == "situational" else 1]

        for e in self.events:                       # post-alarm persistence
            if e.end < t < e.end + self.window:
                g += self._persisted_for((e.label, e.device_id))
        return g

    def state_label(self, t: datetime, situation: Graph) -> str:
        """Short human label for a tick: the active alarms, or why it is empty."""
        active = self.active_at(t)
        if active:
            return ", ".join(e.label.split(" - ")[-1] for e in active)
        return "post-alarm" if len(situation) else "idle"


def ticks_for(events: list, window: timedelta) -> list:
    """State-change instants: each start, each end, the mid-window point
    (to witness post-alarm persistence), and the window expiry."""
    times = set()
    for e in events:
        times.add(e.start)
        times.add(e.end)
        times.add(e.end + window / 2)
        times.add(e.end + window)
    return sorted(times)


def _report_identity_conflicts(patient: str, cls: URIRef, conflicts: list) -> None:
    if not conflicts:
        return
    print(f"[WARN]  {patient}: {len(conflicts)} {_local(cls).lower()} identity conflict(s) — "
          f"kept as separate particulars, never merged:")
    for device_id, concept, prop, values in conflicts:
        vals = ", ".join(sorted(_local(v) for v in values))
        print(f"        {device_id} / {_local(concept)}: {_local(prop)} disagrees ({vals})")


def build_timeline(kb: KB, patient: str, events: list, reasoning_cache: dict = None) -> Timeline:
    """
    Precompute everything a patient's timeline needs to answer any instant.

    `reasoning_cache`, if given, is a dict this call reads from and writes
    into ("persisted", "situation", "background_by_key" — created on first
    use) that the CALLER may share across MULTIPLE build_timeline calls for
    DIFFERENT patients, so a (label, device_id) pair — or an active-alarm
    signature — reasoned about for one patient is reused for the next
    patient that happens to reference the same device_id (real when
    device_ids are pooled/reused across patients over time, as
    events_seed.py's own device pool already models) instead of re-invoking
    owlrl. Bounded by archetype x device cardinality (or by concurrent-alarm
    combinations for "situation"), not by alarm count, so this stays small
    even across a very large run. Deliberately NOT shared this way:
    Timeline's own _bg_cache (background_at) — its key carries no patient
    identity while its cached value embeds this Timeline's own patient
    triples, so cross-patient sharing risks leaking one patient's identity
    into another's result if their revealing sets ever coincide; each
    Timeline gets its own private dict for that instead. Safe for the keys that ARE
    shared here: every one of these cache keys already only ever depended on
    (label, device_id[, identity]) or an active-signature tuple, never on
    anything else in a specific patient's OWN background (see
    inferred_states' per-key memoisation below) — check_device_exclusivity
    already guarantees no cross-patient IFP violation exists in valid input
    before this ever runs, so a cross-patient hit can't smuggle in a
    different answer. Omit (default None) for the exact previous behaviour —
    a fresh, unshared cache local to this one Timeline.

    Conditions and their entailed operation state are keyed by (label,
    device_id), not by event: two occurrences of the same alarm on the same
    device produce the same condition graph, so the reasoner runs once per
    distinct (archetype, device) pair rather than once per alarm — and two
    devices of the same type (or the same alarm on a different device) never
    share a condition, because they never share a sensor/signal/component
    particular.

    Every Individuated class's identity is resolved ONCE here, from this
    patient's full event history (see resolve_particular_identities), before
    anything is minted — not just Sensor/Component: whichever classes the
    ontology currently marks mda:Individuated (Device and Patient excepted —
    they are keyed by an external id, never by concept+refinement matching),
    so a class added later (as mda:FunctionalUnit was) is covered with no
    change here. background_graph and condition_for_event both consult the
    same resolved maps, so a (label, device) pair's condition always lands on
    the same particular IRIs background_graph minted for it, regardless of
    which subset of events a given call happens to see (Timeline.background_at
    draws only on the subset of (label, device) pairs it reveals — see
    _background_by_key below).
    """
    individuated = [cls for cls, kind in kb.node_kind.items()
                    if kind == "individuated" and cls not in (MDA.Device, MDA.Patient)]
    identity = {}
    for cls in sorted(individuated, key=str):
        resolved, conflicts = resolve_particular_identities(kb, cls, events)
        identity[cls] = resolved
        _report_identity_conflicts(patient, cls, conflicts)

    background = background_graph(kb, events, identity)
    keys = sorted({(e.label, e.device_id) for e in events})
    conditions = {k: condition_for_event(kb, *k, identity) for k in keys}

    cache = reasoning_cache if reasoning_cache is not None else {}
    # NOT computed eagerly here (previously was, once per key) — inferred_states
    # is a full OWL-RL reasoning pass, the dominant cost in this pipeline, and
    # most (label, device_id) keys are never actually read (see Timeline.
    # _persisted_for's docstring). Timeline computes each key on first demand
    # instead, still memoised in this same reasoning_cache-shared dict.
    persisted_cache = cache.setdefault("persisted", {})

    # One background_for_key walk per distinct (label, device_id) pair this
    # patient ever reveals, memoised — background_at then only ever does dict
    # lookups + Graph unions over this, never re-walking ground_chain per
    # call. Same reasoning_cache-sharing pattern as persisted_cache above
    # (see this function's own docstring for why that's safe cross-patient).
    background_cache = cache.setdefault("background_by_key", {})
    background_by_key = {}
    for k in keys:
        if k not in background_cache:
            background_cache[k] = background_for_key(kb, *k, identity)
        background_by_key[k] = background_cache[k]

    return Timeline(
        patient=patient,
        events=events,
        background=background,
        ticks=ticks_for(events, kb.window),
        window=kb.window,
        _kb=kb,
        _messages={id(e): alarm_message(kb, e, identity) for e in events},
        _conditions=conditions,
        _persisted_cache=persisted_cache,
        _cache=cache.setdefault("situation", {}),
        _bg_cache={},  # NOT reasoning_cache-shared — see the field's own comment
        _background_by_key=background_by_key,
        _identity=identity,
    )


def reason(base: Graph, static: Graph) -> Graph:
    """
    Materialise the OWL-RL closure of `static + base` (real reasoner).

    Callers within this module pass `kb.reasoning_static_closed` (its own
    closure, computed once in load_kb()), not the raw `kb.reasoning_static` —
    profiling a 100-alarm run found 97% of wall time inside
    owlrl.DeductiveClosure.expand(), ~2.8s/call, essentially independent of
    how small the `base` delta was: the cost is dominated by re-deriving the
    ~2000-triple static graph's OWN entailments (the ontology declares
    several owl:InverseFunctionalProperty/owl:FunctionalProperty relations —
    deliberately, so the double-booking check in op_inference.py's
    check_device_exclusivity can catch real violations — and OWL-RL's
    equality-rule handling for those scales with overall graph size, not
    with what actually changed) from scratch on every single call. Starting
    from an already-closed static graph does not change what gets entailed
    (closure is idempotent — re-closing an already-closed graph plus a small
    ABox reaches the same fixpoint) but lets the reasoner reach it in fewer
    iterations. `reason()` itself stays generic/kb-agnostic; the choice of
    which static graph to pass is the caller's.
    """
    g = Graph()
    g += static
    g += base
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    return g


def clinical_predicates(kb: KB) -> set:
    """
    The relations whose entailed assertions belong in a per-alarm situational
    export — read directly off the ontology's mda:situational true tag
    (see ontology.ttl's "Situational-reasoning classification" section),
    not derived from graph shape.

    Earlier versions of this function tried to derive the set structurally —
    first from kb.tree (a single spanning tree, one parent per class, built
    for CSV/instance attachment) via properties_below, then from a
    multi-parent reachability walk (properties_reachable) rooted at
    mda:Metric. Both worked only by accident of which classes happen to be
    reachable from Metric; neither had a principled way to also include
    mda:administers/targetsProcess (rooted at mda:Device, a sibling branch,
    not a descendant of Metric) without conflating "reachable from Metric"
    with "belongs in the export" — two different questions that happened to
    have overlapping answers only for the property chain, not the
    therapeutic one. Tagging predicates directly answers the actual question
    instead of a proxy for it.
    """
    return {p for p in kb.reasoning_static.subjects(MDA.situational, Literal(True))}


def clinical_context(kb: KB, abox: Graph) -> Graph:
    """
    The situational context entailed by an ABox: metric → physiological
    property → process → organ → organ system, AND device → therapeutic
    modality → process (mda:situational predicates — see clinical_predicates),
    walked out from the nodes the situation actually mentions so the snapshot
    carries the reachable chain and not the whole knowledge base. Named for
    its original, narrower scope (the physiological chain); it now also
    carries therapeutic content since mda:administers/targetsProcess were
    tagged mda:situational too.

    Unlike operation state these facts do NOT persist past alarm-end.  They hang
    off the alarm's metric/device node, which is itself only valid while the
    alarm is active; the anatomy and device capability are timeless but this
    alarm's implication of them is not.
    """
    return _extract_clinical(kb, reason(abox, kb.reasoning_static_closed), abox)


def _extract_clinical(kb: KB, reasoned: Graph, abox: Graph) -> Graph:
    """The mda:situational-tagged part of an already-computed closure."""
    predicates = clinical_predicates(kb)
    g = Graph()
    frontier = {t for triple in abox for t in (triple[0], triple[2])
                if isinstance(t, URIRef)}
    seen = set()
    while frontier:
        nxt = set()
        for node in frontier - seen:
            seen.add(node)
            for p, o in reasoned.predicate_objects(node):
                if p not in predicates:
                    continue
                g.add((node, p, o))
                if isinstance(o, URIRef):
                    nxt.add(o)
        frontier = nxt
    return g


def _extract_all(kb: KB, reasoned: Graph, abox: Graph) -> Graph:
    """
    Everything the reasoner entailed via the domain's own object properties,
    reachable from this abox's own nodes — the same frontier walk as
    _extract_clinical, but the predicate whitelist is "every owl:ObjectProperty
    the ontology declares" (clinical_predicates(kb) is a strict subset of
    this) instead of just the mda:situational-tagged ones, plus rdf:type so an
    entailed superclass membership is visible. Not exported anywhere
    (kg_inference.trig only ever carries the situational/state subsets); this
    exists for op_visual.py's "all entailed" view.

    Deliberately narrower than "every triple in the closure": SKOS/RDFS/OWL
    bookkeeping (skos:broader, rdfs:subClassOf, skos:inScheme, owl:sameAs,
    dcterms:identifier, ...) is excluded, and rdf:type's object is never
    added back to the frontier. Without both, touching a single vocabulary
    concept pulls in that concept's entire scheme (broader chains, siblings,
    scheme metadata) — background/taxonomic noise even by "show me
    everything" standards, not situational content about this alarm. An
    earlier version of this function filtered nothing and pulled in ~50x the
    triples for a single tick, almost none of them meaningful.
    """
    object_properties = set(kb.reasoning_static.subjects(RDF.type, OWL.ObjectProperty))
    g = Graph()
    frontier = {t for triple in abox for t in (triple[0], triple[2])
                if isinstance(t, URIRef)}
    seen = set()
    while frontier:
        nxt = set()
        for node in frontier - seen:
            seen.add(node)
            for p, o in reasoned.predicate_objects(node):
                if p == RDF.type:
                    if isinstance(o, URIRef):
                        g.add((node, p, o))          # shown, but not a doorway
                    continue
                if p not in object_properties:
                    continue
                g.add((node, p, o))
                if isinstance(o, URIRef):
                    nxt.add(o)
        frontier = nxt
    return g


def persisting_predicates(kb: KB) -> set:
    """
    The relations whose entailed assertions about a technical node outlive
    the alarm that entailed them, for the PostAlarmValidScheme window — read
    directly off the ontology's mda:persistsPostAlarm true tag (see
    ontology.ttl's "Post-alarm persistence classification" section), not
    hardcoded to mda:hasOperationState. mda:hasQualityState carries the same
    tag, so a signal quality state derived from a metric's rate (see
    inference.ttl's Signal quality axioms) persists exactly like a device's
    Disconnected does — no code change needed here for that to be true, only
    the ontology tag.
    """
    return {p for p in kb.reasoning_static.subjects(MDA.persistsPostAlarm, Literal(True))}


def inferred_states(kb: KB, abox: Graph) -> Graph:
    """
    The persisting-state facts entailed by an ABox (background + situation):
    signal quality ⇒ signal/sensor/device Enabled, via the property chains,
    plus a signal's own entailed hasQualityState. Only the entailed triples
    for persisting_predicates(kb) are returned (not the closure). These
    persist past alarm-end for the PostAlarmValidScheme window.
    """
    return _extract_states(kb, reason(abox, kb.reasoning_static_closed))


def _extract_states(kb: KB, reasoned: Graph) -> Graph:
    """The persisting-state part of an already-computed closure."""
    g = Graph()
    for p in persisting_predicates(kb):
        for s, o in reasoned.subject_objects(p):
            if str(s).startswith(str(ENTITY)) or str(s).startswith(str(INST)):
                g.add((s, p, o))
    return g


def entailed_situation(kb: KB, abox: Graph):
    """
    Persisting state, situational (mda:situational-tagged) context, and the
    full entailed closure, from a SINGLE reasoning pass.

    `inferred_states` and `clinical_context` each reason independently, which is
    fine for a one-off but doubles the cost per tick.  Stepping a timeline calls
    this instead: OWL-RL runs once and all three views are read off the same
    result. The third (full) view exists for op_visual.py; nothing writes it
    to kg_inference.trig.
    """
    reasoned = reason(abox, kb.reasoning_static_closed)
    return (_extract_states(kb, reasoned), _extract_clinical(kb, reasoned, abox),
            _extract_all(kb, reasoned, abox))
