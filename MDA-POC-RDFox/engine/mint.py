"""
mint.py — turns one alarm into RDF, using the MDA framework.

The framework's catalogue (kg_generated.ttl) holds one blueprint per alarm
label: its device, functional unit, sensor, signal, metric and states.
Minting grounds that blueprint for one occurrence: concrete IRIs scoped to
(patient, device), driven only by the ontology's annotations
(mda:nodeKind, mda:refinesParticularIdentity, mda:refinesUniversalIdentity).
No per-label code.

Each alarm yields three graphs (windows.py decides where they go):
  alarm_message       the alarm, its message, patient, triggeredBy
  condition_for_event what it reports while active (states, conditions)
  background_for_key  the structural chain, persisting after the alarm

A fork of CODE/evaluation_poc/core/op_knowledge.py (EXTRACTION, MINTING)
and reasoner/assess.py (add_triggered_by): the POC imports nothing from
outside its folder. No reasoning here — rules read inference.ttl's axioms
directly (rules.py: NO MATERIALISATION).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from rdflib import Graph, URIRef, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SKOS, XSD, DCTERMS, OWL

sys.path.append(str(Path(__file__).resolve().parent))
from ontology_tree import derive_class_tree, walk_instances

# ── Paths: copies of the framework files, under MDA-POC-RDFox/data/ ──────

DATA_DIR  = Path(__file__).resolve().parent.parent / "data"
ONTOLOGY  = DATA_DIR / "ontology.ttl"
VOCAB     = DATA_DIR / "vocab_generated.ttl"
CLINICAL_EVENT_VOCAB = DATA_DIR / "clinicalEvent_vocab.ttl"
INFERENCE = DATA_DIR / "inference.ttl"
CATALOGUE = DATA_DIR / "kg_generated.ttl"
ENTITIES  = DATA_DIR / "entities.ttl"

# ── Namespaces ────────────────────────────────────────────────────────────

MDA      = Namespace("https://w3id.org/mda/ontology#")
ENTITY   = Namespace("https://w3id.org/mda/entity/")
INST     = Namespace("https://w3id.org/mda/instance/")


def _clean(s) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_")


def _local(iri) -> str:
    return re.split(r"[#/]", str(iri))[-1]


# ── EXTRACTION ────────────────────────────────────────────────────────────

@dataclass
class KB:
    """The framework, loaded once per run, plus memoised lookups."""
    reasoning_static: Graph   # TBox + inference + vocabulary + scaffold (ABox context)
    catalogue: Graph          # kg_generated.ttl — the AlarmType archetypes
    type_index: dict          # alarm label  → AlarmType IRI
    scaffold_concepts: set    # concepts declared in the scaffold type catalogue
    window: timedelta         # post-alarm validity (PostAlarmValidScheme)
    tree: dict                # class nesting derived from the ontology
    node_kind: dict           # class → "individuated"|"referential"|"stateful", from mda:nodeKind
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


def load_kb() -> KB:
    """Parse the framework files; the post-alarm window comes from the
    ontology (mda:postAlarmValidityDuration)."""
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

    dur = static.value(MDA.PostAlarmValidScheme, MDA.postAlarmValidityDuration)
    if dur is None:
        raise ValueError("ontology states no mda:postAlarmValidityDuration on mda:PostAlarmValidScheme")
    return KB(static, catalogue, type_index, scaffold_concepts,
              parse_duration(str(dur)), tree, node_kinds(static))


def parse_duration(s: str) -> timedelta:
    """An xsd:duration of the form PTnHnMnS."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(s))
    if not m or not any(m.groups()):
        raise ValueError(f"unsupported xsd:duration {s!r} (expected PTnHnMnS)")
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=se)


@dataclass
class Archetype:
    """One alarm type's blueprint: its node per class in the catalogue."""
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
    """The blueprint of an alarm type (memoised)."""
    if type_iri not in kb.archetype_cache:
        nodes = walk_instances(kb.catalogue, kb.tree, type_iri, MDA.Alarm)
        kb.archetype_cache[type_iri] = Archetype(nodes, kb.catalogue, kb.reasoning_static)
    return kb.archetype_cache[type_iri]


# ── MINTING ───────────────────────────────────────────────────────────────
#
# Entity IRIs are scoped by (patient, device): device ids repeat across
# patients in the corpus, and patients share one store. The raw device id
# is kept as its dcterms:identifier.

def device_iri(patient_id: str, device_id: str) -> URIRef:
    return ENTITY[f"Device_{_clean(patient_id)}_{_clean(device_id)}"]


def patient_iri(patient_id: str) -> URIRef:
    return ENTITY[f"Patient_{_clean(patient_id)}"]


def refining_properties(kb: KB, cls: URIRef) -> list:
    """Properties of `cls` that refine a particular's identity (they go
    into its IRI)."""
    if cls not in kb.refining_props_cache:
        kb.refining_props_cache[cls] = sorted(
            (p for p in kb.reasoning_static.subjects(MDA.refinesParticularIdentity, Literal(True))
             if (p, RDFS.domain, cls) in kb.reasoning_static),
            key=str,
        )
    return kb.refining_props_cache[cls]


def leaf_properties(kb: KB, cls: URIRef) -> list:
    """Concept-valued properties of `cls` that describe its state, not its
    identity (e.g. hasValueState, hasRhythm)."""
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


def ground_leaf_properties(g: Graph, kb: KB, arch, cls: URIRef, node) -> None:
    """Add `node`'s leaf-property values from the blueprint to `g`."""
    if node is None:
        return
    for prop in leaf_properties(kb, cls):
        value = arch.value(cls, prop)
        if value is not None:
            g.add((node, prop, value))


def _refined_suffix(concept: URIRef, refinements: dict) -> str:
    suffix = _local(concept)
    for prop in sorted(refinements or {}, key=str):
        suffix += f"_{_local(refinements[prop])}"
    return suffix


def particular_iri(kb: KB, arch: "Archetype", patient_id: str, device_id: str, cls: URIRef,
                    identity: dict = None) -> URIRef:
    """The IRI of the `cls` particular of this alarm: patient, device and
    the refined concept of every class from `cls` up to the device. None
    when the blueprint has no such class."""
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
    """One alarm occurrence: patient, device, start and ALARM_ID. The
    ALARM_ID is needed: patient + device + start collide for 45.5% of the
    corpus. The end is not part of it — unknown at arrival."""
    return "_".join([_clean(ev.patient), _clean(ev.device_id),
                     ev.start.strftime("%Y%m%dT%H%M%S"), _clean(ev.alarm_id)])


def alarm_iri(ev) -> URIRef:
    return INST[f"Alarm_{alarm_key(ev)}"]


def message_iri(ev) -> URIRef:
    return INST[f"Msg_{alarm_key(ev)}"]


# A clinical event (actions.py's episodes): its kind, patient and start.
EVENT_BASE = str(INST) + "ClinicalEvent_"


def event_iri(kind: str, patient_id: str, start) -> str:
    return f"{EVENT_BASE}{kind}_{_clean(patient_id)}_{start.strftime('%Y%m%dT%H%M%S')}"


def ground_chain(kb: KB, arch: "Archetype", patient_id: str, device_id: str,
                  identity: dict = None) -> tuple:
    """Walk the class tree from the device down the blueprint: (background
    graph — structure, condition graph — stateful nodes and leaf
    properties, leaves — the deepest particulars reached)."""
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
    """What persists after the alarm: the device and its structural chain."""
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
    # The device's own state (hasDeviceOperationState) is mda:persistsPostAlarm:
    # it stays visible for the post-alarm window. condition_for_event also
    # grounds it, for rules that need it only while the alarm is active.
    ground_leaf_properties(g, kb, arch, MDA.Device, dev)
    return g


def alarm_message(kb: KB, ev, identity: dict = None) -> Graph:
    """The alarm, its message, patient and triggeredByStructure."""
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
    """What the alarm reports while active: stateful nodes and every leaf
    property, including the device's state (CAT3b needs a ventilator fault
    only while its alarm is active)."""
    type_iri = kb.type_index.get(label)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    _, condition, _ = ground_chain(kb, arch, patient_id, device_id, identity)
    ground_leaf_properties(condition, kb, arch, MDA.Device, device_iri(patient_id, device_id))
    return condition


# ── IDENTITY ──────────────────────────────────────────────────────────────

@dataclass
class IdentityTracker:
    """One patient's resolved particular identities, updated per arrival.

    A particular (e.g. a sensor) is refined by what alarms so far have said
    about it (e.g. its anatomical position). Refinements of one
    (patient, device, concept) are unioned; if two alarms disagree, the key
    is conflicted — for good — and its particulars fall back to each
    blueprint's own refinements. Uses only alarms that have arrived.
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
    """Fold one arriving alarm into `tracker` (in arrival order)."""
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
    """Each message mda:triggeredBy its functional unit (else its device):
    the anchor every CAT1/CAT2 rule starts from."""
    for e in events:
        type_iri = kb.type_index.get(e.label)
        if type_iri is None:
            continue
        arch = archetype_structure(kb, type_iri)
        msg = message_iri(e)
        fu = particular_iri(kb, arch, e.patient, e.device_id, MDA.FunctionalUnit, identity)
        target = fu if fu is not None else device_iri(e.patient, e.device_id)
        g.add((msg, MDA.triggeredBy, target))
