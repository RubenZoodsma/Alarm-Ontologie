"""
mint.py — the real MDA-framework EXTRACTION + MINTING pipeline, forked.

MDA-POC-RDFox is self-contained: it does not import code from outside
itself. This module is therefore a DUPLICATED COPY (not a live import) of
the generic, already-validated grounding logic in
CODE/evaluation_poc/core/op_knowledge.py (its EXTRACTION and MINTING
sections) plus two small enrichments from
CODE/evaluation_poc/reasoner/assess.py (load_priority_rank,
add_triggered_by, resolve_identity) — logic-for-logic, not reinvented.

Why fork this instead of hand-authoring per-label knowledge (the mistake
data/archetypes.py made, now deleted): the framework's data files
(kg_generated.ttl, entities.ttl) hold ONE static blueprint per alarm
label, not per-occurrence grounded instances. Turning a blueprint into
concrete, owner-scoped IRIs for a specific device_id is exactly what
ground_chain/particular_iri/resolve_particular_identities already do,
generically, driven entirely by ontology.ttl's own mda:nodeKind /
mda:refinesParticularIdentity / mda:refinesUniversalIdentity annotations —
no per-label code anywhere. Re-deriving that behaviour by hand (as
archetypes.py did) is strictly worse and was caught with a real bug
(mda:triggeredBy pointed at the bare Device instead of the alarm's own
FunctionalUnit — exactly the fan-out bug add_triggered_by's own docstring
documents fixing) within minutes of being questioned.

WHAT WAS DROPPED, AND WHY — CORRECTED TWICE
--------------------------------------------------------------------
An earlier version of this module dropped op_knowledge.py's entire
INFERENCE section, reasoning that RDFox's own Datalog rules
(representation/rules/*.dlog) already replace it. That was wrong for one
specific, load-bearing piece: mda:approximates is NOT a directly-asserted
per-instance fact anywhere in kg_generated.ttl — it's an OWL-RL ENTAILMENT
from inference.ttl's class-level owl:hasValue restriction axioms
(metric:HeartRate's own class declaration entails "this instance
approximates physiologicalProperty:ElectricalHeartRate", never asserted
as a ground fact). A second version of this module then forked op_
knowledge.py's reason()/clinical_predicates()/clinical_context()/
_extract_clinical() to run a real owlrl.DeductiveClosure PER ALARM
ARRIVAL — this fixed CAT2a's failure to fire, but reintroduced the exact
per-call OWL-RL cost (re-deriving the WHOLE static graph from scratch
every call, ~2.8-4s measured) that the RDFox migration was undertaken to
eliminate in the first place. At the project's 14M-alarm target that's
over a decade of serial compute.

CORRECTED AGAIN: mda:approximates' restriction axioms are all simple,
enumerable, one-hop type-to-value mappings (14 of them, inference.ttl
lines 520-614) — not a general reasoning problem requiring OWL-RL at
all. They're ported instead as plain RDFox Datalog rules,
representation/rules/approximates_bridge.dlog, loaded once like any
other rule file — RDFox derives them natively and incrementally, no
Python-side reasoning needed. `reason()`, `clinical_predicates()`,
`clinical_context()`, `_extract_clinical()`, `owlrl`, and
`KB.reasoning_static_closed` are gone from this module entirely — `owlrl`
is not a dependency of this module at all. (mda:administers/
targetsProcess, the OTHER mda:situational-tagged predicates the deleted
clinical_context() also derived, are unreferenced by every current rule.
Since the 2026-09 data sync, data/entities.ttl DOES list the therapeutic
modality concepts, so ground_chain now mints administers/targetsProcess
from the catalogue like any other edge — nothing reads them yet.)

FRAMEWORK/KNOWLEDGE_BASE/clinicalEvents.ttl is NOT copied into data/ or
parsed here either: it only states each clinical event's evidence
criterion, nothing EXTRACTION/MINTING reads. Those criteria are implemented
by engine/clinical_events.py (hand translations, kept in sync by hand).

Paths below point at MDA-POC-RDFox/data/ — physical copies of the
FRAMEWORK/DATA files this pipeline needs (per the project's own
self-containment rule: duplicate data files rather than reach outside the
folder for them, and duplicate/fork logic rather than reinvent it).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from rdflib import Graph, URIRef, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SKOS, XSD, DCTERMS, OWL

sys.path.append(str(Path(__file__).resolve().parent))
from ontology_tree import derive_class_tree, walk_instances

# ── Paths — physical copies under MDA-POC-RDFox/data/, see module docstring ──

DATA_DIR  = Path(__file__).resolve().parent.parent / "data"
ONTOLOGY  = DATA_DIR / "ontology.ttl"
VOCAB     = DATA_DIR / "vocab_generated.ttl"
CLINICAL_EVENT_VOCAB = DATA_DIR / "clinicalEvent_vocab.ttl"
INFERENCE = DATA_DIR / "inference.ttl"
CATALOGUE = DATA_DIR / "kg_generated.ttl"
ENTITIES  = DATA_DIR / "entities.ttl"

# ── Namespaces ────────────────────────────────────────────────────────────

MDA      = Namespace("https://w3id.org/mda/ontology#")
# POC-only terms (data/mdapoc.ttl): the POC's decisions and bookkeeping
# (silencing, false-positive flags, graph validity) and orderings not yet
# adopted by the framework — deliberately outside the mda: ontology.
MDAPOC   = Namespace("https://w3id.org/mda/poc#")
ENTITY   = Namespace("https://w3id.org/mda/entity/")
SCAFFOLD = Namespace("https://w3id.org/mda/scaffold/")
INST     = Namespace("https://w3id.org/mda/instance/")


def _clean(s) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_")


def _local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


# ── EXTRACTION ────────────────────────────────────────────────────────────

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
    last_wins_str: set = field(default_factory=set)  # last_wins, as "<iri>" strings —
                                                       # populated by load_kb(), consulted by
                                                       # engine/replay_driver.py to match its
                                                       # own hand-formatted "s p o ." triple text
    archetype_cache: dict = field(default_factory=dict)  # type_iri -> Archetype, memoised
    refining_props_cache: dict = field(default_factory=dict)  # cls -> refining_properties(kb, cls)
    leaf_props_cache: dict = field(default_factory=dict)       # cls -> leaf_properties(kb, cls)


NODE_KIND_OF = {
    MDA.Individuated: "individuated",
    MDA.Referential:  "referential",
    MDA.Stateful:     "stateful",
}


def node_kinds(g: Graph) -> dict:
    return {cls: NODE_KIND_OF[kind] for cls, kind in g.subject_objects(MDA.nodeKind)
            if kind in NODE_KIND_OF}


def concept_classes(g: Graph) -> dict:
    mapping = {}
    for scheme, cls in g.subject_objects(MDA.instantiatesClass):
        for concept in g.subjects(SKOS.inScheme, scheme):
            if (concept, SKOS.topConceptOf, scheme) in g:
                continue
            mapping[concept] = cls
    return mapping


def nodes_of_class(kb: KB, g: Graph, cls: URIRef) -> set:
    nodes = {t for triple in g for t in (triple[0], triple[2])}
    return {n for n in nodes if kb.concept_class.get(n) == cls}


def load_kb() -> KB:
    static = Graph()
    for f in (ONTOLOGY, VOCAB, CLINICAL_EVENT_VOCAB, INFERENCE, ENTITIES):
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
    kb.last_wins_str = {f"<{p}>" for p in kb.last_wins}
    return kb


def parse_duration(s: str) -> timedelta:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(s))
    if not m:
        return timedelta(0)
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=se)


@dataclass
class Archetype:
    nodes: dict          # class IRI → node in the catalogue
    catalogue: Graph
    vocab: Graph

    def node(self, cls: URIRef):
        return self.nodes.get(cls)

    def concept(self, cls: URIRef):
        node = self.nodes.get(cls)
        if node is None:
            return None
        return next((t for t in self.catalogue.objects(node, RDF.type)
                     if (t, RDF.type, SKOS.Concept) in self.vocab), None)

    def value(self, cls: URIRef, prop: URIRef):
        node = self.nodes.get(cls)
        if node is None:
            return None
        return next(self.catalogue.objects(node, prop), None)


def archetype_structure(kb: KB, type_iri: URIRef) -> Archetype:
    if type_iri not in kb.archetype_cache:
        nodes = walk_instances(kb.catalogue, kb.tree, type_iri, MDA.Alarm)
        kb.archetype_cache[type_iri] = Archetype(nodes, kb.catalogue, kb.reasoning_static)
    return kb.archetype_cache[type_iri]


# ── MINTING ───────────────────────────────────────────────────────────────
#
# Every minted entity IRI is scoped by (patient_id, device_id), not
# device_id alone — confirmed directly (not assumed) that device_id
# values repeat across DIFFERENT patients in the real corpus (DATA/
# POC_EVENTS/events_data.csv: e.g. PhysiologicalMonitorPhilips_00 appears
# under both janeRoe_02 and johnDoe_01), consistent with op_knowledge.py's
# own background_graph() docstring ("the same physical device can serve
# different patients at different times"). Safe under the ORIGINAL
# per-patient-isolated rdflib Timeline / per-patient RDFox dstore designs
# only because each patient's graph was never shared with another's; a
# shared dstore (the point of this scoping) would otherwise silently
# merge two different patients' devices onto one IRI. The raw, unscoped
# device_id is still recorded as the DCTERMS.identifier literal
# (background_for_key) — that's the real-world identifier, not the
# minted IRI, and must stay unscoped.

def device_iri(patient_id: str, device_id: str) -> URIRef:
    return ENTITY[f"Device_{_clean(patient_id)}_{_clean(device_id)}"]


def patient_iri(patient_id: str) -> URIRef:
    return ENTITY[f"Patient_{_clean(patient_id)}"]


def refining_properties(kb: KB, cls: URIRef) -> list:
    if cls not in kb.refining_props_cache:
        kb.refining_props_cache[cls] = sorted(
            (p for p in kb.reasoning_static.subjects(MDA.refinesParticularIdentity, Literal(True))
             if (p, RDFS.domain, cls) in kb.reasoning_static),
            key=str,
        )
    return kb.refining_props_cache[cls]


def leaf_properties(kb: KB, cls: URIRef) -> list:
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
    domains = {d for d in kb.reasoning_static.objects(None, RDFS.domain) if isinstance(d, URIRef)}
    return {p for cls in domains for p in leaf_properties(kb, cls)}


def ground_leaf_properties(g: Graph, kb: KB, arch, cls: URIRef, node) -> None:
    if node is None:
        return
    for prop in leaf_properties(kb, cls):
        value = arch.value(cls, prop)
        if value is not None:
            g.add((node, prop, value))


def resolve_particular_identities(kb: KB, cls: URIRef, events: list) -> tuple:
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
        by_key[(ev.patient, ev.device_id, concept)].append(refinements)

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


def particular_iri(kb: KB, arch: "Archetype", patient_id: str, device_id: str, cls: URIRef,
                    identity: dict = None) -> URIRef:
    identity = identity or {}
    if cls == MDA.Device:
        return device_iri(patient_id, device_id)
    if arch.node(cls) is None:
        return None

    def own_refinements(c: URIRef) -> dict:
        concept = arch.concept(c)
        own = {p: v for p in refining_properties(kb, c) if (v := arch.value(c, p)) is not None}
        return identity.get(c, {}).get((patient_id, device_id, concept), own)

    suffixes, node_cls = [], cls
    while node_cls is not None and node_cls != MDA.Device:
        concept = arch.concept(node_cls)
        suffixes.append(_refined_suffix(concept, own_refinements(node_cls))
                         if concept is not None else _local(node_cls))
        link = kb.tree.get(node_cls)
        node_cls = link[0] if link else None
    return ENTITY[f"{_local(cls)}_{_clean(patient_id)}_{_clean(device_id)}_" + "_".join(reversed(suffixes))]


def alarm_key(ev) -> str:
    """One alarm occurrence: patient, device, start, end and ALARM_ID (the
    event's own unique identifier — for the corpus, its row number in the
    locked source data; see replay_driver.Event). Patient + device + start
    alone collided for 45.5% of the corpus: alarms raised in the same
    second on one monitor got one IRI, hence one pair of named graphs, and
    the first to end dropped the others' content. Everything an alarm
    reports lives in graphs named after this key, so no other alarm's
    arrival or end can touch it."""
    return "_".join([_clean(ev.patient), _clean(ev.device_id),
                     ev.start.strftime("%Y%m%dT%H%M%S"), ev.end.strftime("%Y%m%dT%H%M%S"),
                     _clean(ev.alarm_id)])


def alarm_iri(ev) -> URIRef:
    return INST[f"Alarm_{alarm_key(ev)}"]


def message_iri(ev) -> URIRef:
    return INST[f"Msg_{alarm_key(ev)}"]


def ground_chain(kb: KB, arch: "Archetype", patient_id: str, device_id: str,
                  identity: dict = None) -> tuple:
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
            if kind == "individuated" and concept is not None and concept not in kb.scaffold_concepts:
                continue
            particular = particular_iri(kb, arch, patient_id, device_id, child_cls, identity)
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

    walk(MDA.Device, device_iri(patient_id, device_id))
    return background, condition, leaves


def background_for_key(kb: KB, patient_id: str, label: str, device_id: str, identity: dict) -> Graph:
    type_iri = kb.type_index.get(label)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    g = Graph()
    dev = device_iri(patient_id, device_id)
    device_type = arch.concept(MDA.Device)
    if device_type:
        g.add((dev, RDF.type, device_type))
        g.add((dev, DCTERMS.identifier, Literal(device_id)))
    bg, _, _ = ground_chain(kb, arch, patient_id, device_id, identity)
    g += bg
    # hasDeviceOperationState is a Device leaf property, but unlike every
    # other leaf property it's tagged mda:persistsPostAlarm true
    # (ontology.ttl) — a device fault must still be visible for the
    # 15-minute post-alarm window, not just while its own alarm is active.
    # Grounded here (persistent bucket) so it follows Device's own
    # background-graph lifecycle. condition_for_event grounds it AGAIN in
    # the transient graph, for consumers that need the fault while its
    # alarm is active (see that function).
    ground_leaf_properties(g, kb, arch, MDA.Device, dev)
    return g


def alarm_message(kb: KB, ev, identity: dict = None) -> Graph:
    g = Graph()
    type_iri = kb.type_index.get(ev.label)
    if type_iri is None:
        return g
    arch = archetype_structure(kb, type_iri)
    a = alarm_iri(ev)
    msg = message_iri(ev)
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
    _, _, leaves = ground_chain(kb, arch, ev.patient, ev.device_id, identity)
    for _cls, particular in leaves:
        g.add((msg, MDA.triggeredByStructure, particular))
    return g


def condition_for_event(kb: KB, patient_id: str, label: str, device_id: str,
                         identity: dict = None) -> Graph:
    type_iri = kb.type_index.get(label)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    _, condition, _ = ground_chain(kb, arch, patient_id, device_id, identity)
    # Device's own leaf property (hasDeviceOperationState) is grounded in
    # background_for_key for its post-alarm persistence — and ALSO here,
    # so the transient graph holds everything this alarm reports while it
    # is active. CAT3b (clinical_events.py, VentilationFailure) needs a
    # ventilator malfunction only while its alarm is active: read from the
    # persistent graph alone, a fault carried over 15 minutes joined the
    # next ventilator's alarms after a ventilator swap. Every OTHER leaf
    # property (Metric/Signal/Sensor/Component's, via ground_chain's walk)
    # lands here only.
    ground_leaf_properties(condition, kb, arch, MDA.Device, device_iri(patient_id, device_id))
    return condition


# ── ENRICHMENTS forked from CODE/evaluation_poc/reasoner/assess.py ────────
#
# assess.py's own header explains why these two facts are added separately
# from op_knowledge.py's core minting rather than folded into it — ported
# here verbatim, same reasoning:
#   - mda:priorityRank (FRAMEWORK/VOCABULARY/priority_rank.ttl) is a
#     STANDING, concept-level fact (alarmprio:Hoog priorityRank 3, not
#     per-alarm) — load ONCE into the default graph, like isPropertyOf.
#     data/archetypes.py's mistake (now deleted) was re-minting this INTO
#     every alarm's own transient graph, which is also why
#     representation/cat_rules.dlog's CAT2a rule needs its priorityRank
#     patterns reverted to ungraphed (see that file).
#   - mda:triggeredBy (AlarmMessage -> FunctionalUnit, falling back to
#     Device) is not part of op_knowledge.py's alarm_message() output at
#     all — op_knowledge.py only asserts triggeredByStructure. Every
#     CAT1/CAT2 rule needs the FU-targeting triggeredBy edge specifically.
#
# load_priority_rank() (assess.py's third small enrichment) is NOT ported
# here — dead code, confirmed unused: priority_rank.ttl is loaded directly
# by replay_driver.py's FRAMEWORK_FILES instead.

def resolve_identity(kb: KB, events: list) -> dict:
    """
    `events` must be exactly this patient's events-so-far (start <= the
    arriving alarm's own start) — never the full future history, or a
    later alarm's refinement could resolve an earlier alarm's identity
    before that later alarm has occurred (assess.py's own documented
    reason for calling this once per arriving alarm, not once per patient).
    """
    individuated = [cls for cls, kind in kb.node_kind.items()
                    if kind == "individuated" and cls not in (MDA.Device, MDA.Patient)]
    identity = {}
    for cls in sorted(individuated, key=str):
        resolved, _ = resolve_particular_identities(kb, cls, events)
        identity[cls] = resolved
    return identity


@dataclass
class IdentityTracker:
    """
    Incremental replacement for calling resolve_identity(kb, events_so_far)
    fresh on every new alarm arrival — that recomputation rescans a
    patient's ENTIRE alarm history so far on every single arrival, O(n^2)
    per patient in that patient's own alarm count. Ported from
    op_knowledge.py's Timeline.observe() (lines 905-1002 there) — one
    instance per patient, updated one event at a time via
    update_identity(), in arrival order.

    Verified equivalent to resolve_identity's own batch computation, per
    Timeline.observe()'s own docstring reasoning: both are the exact same
    union-of-refinements-then-check-for-conflict computation over the
    exact same (event, refinement) pairs; set union is associative and
    commutative, so there is no ordering-dependent step that could make
    the incremental and batch results differ. A key that starts
    conflicting stays conflicting for the rest of this tracker's life —
    the same deliberate asymmetry op_knowledge.py's own Timeline.observe()
    has vs a from-scratch batch recompute, and harmless here for the same
    reason it's harmless there: each alarm's minted triples are written
    to RDFox immediately in insert_alarm and never revisited once a later
    arrival's identity computation might disagree — true of this driver's
    per-alarm-recompute-from-scratch predecessor too, so this port
    introduces zero new behavior difference, only removes the redundant
    recomputation.
    """
    identity: dict          # class -> {(patient_id, device_id, concept): refinements} — read by particular_iri
    state: dict             # class -> {(patient_id, device_id, concept): {prop: {values}}}
    conflicted: dict        # class -> {(patient_id, device_id, concept)} sticky once conflicting
    refiners: dict          # class -> refining_properties(kb, class), cached once


def new_identity_tracker(kb: KB) -> IdentityTracker:
    individuated = [c for c, kind in kb.node_kind.items()
                    if kind == "individuated" and c not in (MDA.Device, MDA.Patient)]
    return IdentityTracker(
        identity={c: {} for c in individuated},
        state={c: {} for c in individuated},
        conflicted={c: set() for c in individuated},
        refiners={c: refining_properties(kb, c) for c in individuated},
    )


def update_identity(kb: KB, event, tracker: IdentityTracker) -> None:
    """Incorporate exactly one new event (MUST be called in arrival order)
    into `tracker`, mutating it in place. See IdentityTracker's own
    docstring for the equivalence argument against resolve_identity."""
    type_iri = kb.type_index.get(event.label)
    if type_iri is None:
        return
    arch = archetype_structure(kb, type_iri)
    for cls in tracker.state:
        concept = arch.concept(cls)
        if concept is None:
            continue
        ik = (event.patient, event.device_id, concept)
        if ik in tracker.conflicted[cls]:
            continue
        values_by_prop = tracker.state[cls].setdefault(ik, {})
        refinements = {p: v for p in tracker.refiners[cls]
                       if (v := arch.value(cls, p)) is not None}
        for p, v in refinements.items():
            values_by_prop.setdefault(p, set()).add(v)
        conflicting = any(len(vs) > 1 for vs in values_by_prop.values())
        if conflicting:
            tracker.conflicted[cls].add(ik)
            tracker.identity[cls].pop(ik, None)
        else:
            tracker.identity[cls][ik] = {p: next(iter(vs)) for p, vs in values_by_prop.items()}


def add_triggered_by(g: Graph, events: list, kb: KB, identity: dict) -> None:
    for e in events:
        type_iri = kb.type_index.get(e.label)
        if type_iri is None:
            continue
        arch = archetype_structure(kb, type_iri)
        msg = message_iri(e)
        fu = particular_iri(kb, arch, e.patient, e.device_id, MDA.FunctionalUnit, identity)
        target = fu if fu is not None else device_iri(e.patient, e.device_id)
        g.add((msg, MDA.triggeredBy, target))
