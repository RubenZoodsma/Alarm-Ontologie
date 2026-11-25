"""
mint.py — turns one alarm into RDF, using the MDA framework.

The framework's catalogue (kg_generated.ttl) holds one blueprint per alarm
label: its device, functional unit, sensor, signal, metric and states.
Minting grounds that blueprint for one occurrence: concrete IRIs scoped to
(patient, device), driven only by the ontology's annotations
(mda:nodeKind, mda:refinesParticularIdentity, mda:refinesUniversalIdentity).
No per-label code.

Minting follows the blueprint's own nodes (ontology_tree.walk_blueprint),
not the class tree: a chain may repeat a class (sensor -> raw signal -> lead
derivation -> lead -> analysis -> metric; metric -> derivation -> metric)
and a node may have several parents (two electrodes feeding one lead
derivation). See particular_iri for how such nodes are named.

Each alarm yields three graphs (windows.py decides where they go):
  alarm_message       the alarm, its message, patient, triggeredBy
  condition_for_event what it reports while active (states, conditions)
  background_for_key  the structural chain, persisting after the alarm

A fork of the retired old/CODE/evaluation_poc/core/op_knowledge.py
(EXTRACTION, MINTING) and reasoner/assess.py (add_triggered_by): the POC imports nothing from
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
from ontology_tree import BlueprintNode, class_edges, derive_class_tree, walk_blueprint

# ── Paths: copies of the framework files, under MDA-POC/data/ ──────

DATA_DIR  = Path(__file__).resolve().parent.parent / "data"
ONTOLOGY  = DATA_DIR / "ontology.ttl"
VOCAB     = DATA_DIR / "vocab_generated.ttl"
CLINICAL_EVENT_VOCAB = DATA_DIR / "clinicalEvent_vocab.ttl"
INFERENCE = DATA_DIR / "inference.ttl"
CATALOGUE = DATA_DIR / "kg_generated.ttl"
ENTITIES  = DATA_DIR / "entities.ttl"
# Gateway device name -> device model (FRAMEWORK/PLAN_blueprint_identity.md).
DEVICE_MODELS = DATA_DIR.parent.parent / "DATA" / "ANNOTATION" / "device_models.csv"

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
    type_index: dict          # (model, label, priority), normalised → AlarmType IRI
    model_of: dict            # gateway device name → device model ("" when unknown)
    scaffold_concepts: set    # concepts declared in the scaffold type catalogue
    window: timedelta         # post-alarm validity (PostAlarmValidScheme)
    tree: dict                # class nesting derived from the ontology (no longer used for minting)
    edges: dict               # class -> [(property, range class)], the edges a blueprint walk may follow
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


def load_kb(window: str | None = None) -> KB:
    """Parse the framework files; the post-alarm window comes from the
    ontology (mda:postAlarmValidityDuration), unless `window` (an
    xsd:duration such as "PT5M") overrides it, for sensitivity analyses."""
    static = Graph()
    for f in (ONTOLOGY, VOCAB, CLINICAL_EVENT_VOCAB, INFERENCE, ENTITIES):
        static.parse(f, format="turtle")

    onto = Graph()
    onto.parse(ONTOLOGY, format="turtle")
    tree = derive_class_tree(onto, MDA.Alarm)
    edges = class_edges(onto)

    catalogue = Graph()
    catalogue.parse(CATALOGUE, format="turtle")

    type_index = blueprint_index(catalogue)
    model_of = load_device_models()

    scaffold = Graph()
    scaffold.parse(ENTITIES, format="turtle")
    scaffold_concepts = {
        t for _, t in scaffold.subject_objects(RDF.type)
        if "/vocab/" in str(t)
    }

    dur = static.value(MDA.PostAlarmValidScheme, MDA.postAlarmValidityDuration)
    if dur is None:
        raise ValueError("ontology states no mda:postAlarmValidityDuration on mda:PostAlarmValidScheme")
    return KB(static, catalogue, type_index, model_of, scaffold_concepts,
              parse_duration(window or str(dur)), tree, edges, node_kinds(static))


def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def blueprint_key(model, label, priority) -> tuple:
    """A blueprint's identity, normalised: (device model, label, priority)."""
    return (_norm(model), _norm(label), _norm(priority))


def blueprint_index(catalogue: Graph) -> dict:
    """(model, label, priority) -> AlarmType, read from each blueprint: its
    mda:hasLabel, mda:hasPriority, and the model (mda:hasDeviceType) of the
    device its message is triggered by; model "" when the blueprint has none."""
    index = {}
    for at in catalogue.subjects(RDF.type, MDA.AlarmType):
        label = catalogue.value(at, MDA.hasLabel)
        prio = catalogue.value(at, MDA.hasPriority)
        models = {m for msg in catalogue.objects(at, MDA.hasMessage)
                  for dev in catalogue.objects(msg, MDA.triggeredBy)
                  for m in catalogue.objects(dev, MDA.hasDeviceType)}
        if len(models) > 1:
            raise ValueError(f"{at} has more than one device model: {sorted(models)}")
        model = str(next(iter(models))).rsplit("/", 1)[-1] if models else ""
        key = blueprint_key(model, label, str(prio).rsplit("/", 1)[-1] if prio else "")
        if key in index:
            raise ValueError(f"two blueprints share the identity {key}: {index[key]}, {at}")
        index[key] = at
    return index


def load_device_models() -> dict:
    import csv
    with DEVICE_MODELS.open(encoding="utf-8", newline="") as fh:
        return {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}


def type_of(kb: KB, ev):
    """The blueprint of an alarm (an Event or AlarmArrival), by device model
    + label + priority; None when the gateway name is unknown or no
    blueprint has that identity (a coverage gap)."""
    name = getattr(ev, "device_name", "")
    if name not in kb.model_of:
        return None
    return kb.type_index.get(blueprint_key(kb.model_of[name], ev.label, getattr(ev, "priority", "")))


def parse_duration(s: str) -> timedelta:
    """An xsd:duration of the form PTnHnMnS."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(s))
    if not m or not any(m.groups()):
        raise ValueError(f"unsupported xsd:duration {s!r} (expected PTnHnMnS)")
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=se)


@dataclass
class Archetype:
    """One alarm type's blueprint: its nodes, parents before children.

    concept(cls)/value(cls, prop) answer for a class that occurs ONCE in the
    blueprint (Alarm, Device, FunctionalUnit) and raise for one that occurs
    more than once (Signal, SignalAnalysis, Metric in a derived chain):
    a caller must then look at the nodes themselves (node_concept,
    node_value)."""
    nodes: list          # [BlueprintNode], topological
    catalogue: Graph
    vocab: Graph
    by_class: dict = field(default_factory=dict, repr=False)   # class IRI -> [BlueprintNode]
    # concept() and value() read only the blueprint, which is fixed for the
    # run (the catalogue and vocabulary are never written after loading), so
    # their answers are kept. Nothing from the stream enters these caches:
    # per-patient identity is resolved in particular_iri, per arrival.
    _concepts: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for bn in self.nodes:
            self.by_class.setdefault(bn.cls, []).append(bn)

    def node(self, cls: URIRef):
        """The single node of class `cls`, or None. Raises when there are several."""
        found = self.by_class.get(cls, [])
        if len(found) > 1:
            raise ValueError(f"blueprint has {len(found)} {_local(cls)} nodes: ask for a node, not a class")
        return found[0] if found else None

    def node_concept(self, bn: BlueprintNode):
        if id(bn) not in self._concepts:
            self._concepts[id(bn)] = next(
                (t for t in self.catalogue.objects(bn.node, RDF.type)
                 if (t, RDF.type, SKOS.Concept) in self.vocab), None)
        return self._concepts[id(bn)]

    def node_value(self, bn: BlueprintNode, prop: URIRef):
        return next(self.catalogue.objects(bn.node, prop), None)

    def concept(self, cls: URIRef):
        bn = self.node(cls)
        return None if bn is None else self.node_concept(bn)

    def value(self, cls: URIRef, prop: URIRef):
        bn = self.node(cls)
        return None if bn is None else self.node_value(bn, prop)

    def under(self, cls: URIRef) -> list:
        """The nodes strictly below the single `cls` node, topological."""
        top = self.node(cls)
        if top is None:
            return []
        below, todo = set(), [top]
        while todo:
            for _prop, child in todo.pop().children:
                if id(child) not in below:
                    below.add(id(child))
                    todo.append(child)
        return [bn for bn in self.nodes if id(bn) in below]


def archetype_structure(kb: KB, type_iri: URIRef) -> Archetype:
    """The blueprint of an alarm type (memoised)."""
    if type_iri not in kb.archetype_cache:
        nodes = walk_blueprint(kb.catalogue, kb.edges, type_iri, MDA.Alarm,
                               stop=lambda cls: kb.node_kind.get(cls) == "referential")
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


def node_refinements(kb: KB, arch: "Archetype", patient_id: str, device_id: str,
                     bn: BlueprintNode, identity: dict = None) -> dict:
    """The identity refinements of one blueprint node for this (patient,
    device): what the identity tracker has resolved for its concept, else
    the blueprint's own values."""
    concept = arch.node_concept(bn)
    own = {p: v for p in refining_properties(kb, bn.cls) if (v := arch.node_value(bn, p)) is not None}
    return (identity or {}).get(bn.cls, {}).get((patient_id, device_id, concept), own)


def particular_iri(kb: KB, arch: "Archetype", patient_id: str, device_id: str,
                   bn: BlueprintNode, identity: dict = None, _memo: dict = None) -> URIRef:
    """The IRI of the particular for blueprint node `bn` of this alarm:
    {Class}_{patient}_{device}_{suffix}. The suffix names the node by the
    nodes above it, up to the device:

      one parent       the parent's suffix, then the node's own refined
                       concept — for a single chain exactly the IRIs of the
                       former class-tree minting
      several parents  the parents' suffixes, sorted and joined by '+',
                       then the node's own refined concept — a derivation
                       with two inputs is ONE particular, however many
                       paths reach it

    Readable on purpose: the logs name these IRIs."""
    if bn.cls == MDA.Device:
        return device_iri(patient_id, device_id)
    memo = {} if _memo is None else _memo

    def suffix(node: BlueprintNode) -> str:
        if id(node) in memo:
            return memo[id(node)]
        concept = arch.node_concept(node)
        own = (_refined_suffix(concept, node_refinements(kb, arch, patient_id, device_id, node, identity))
               if concept is not None else _local(node.cls))
        above = sorted({suffix(p) for p, _prop in node.parents if p.cls != MDA.Device})
        memo[id(node)] = f"{'+'.join(above)}_{own}" if above else own
        return memo[id(node)]

    return ENTITY[f"{_local(bn.cls)}_{_clean(patient_id)}_{_clean(device_id)}_{suffix(bn)}"]


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


# A clinical event (actions.py's episodes): its kind, patient, start, and
# the number of the evaluation that started it — two events of one kind can
# start in the same second (one ending, the next starting), and must differ.
EVENT_BASE = str(INST) + "ClinicalEvent_"


def event_iri(kind: str, patient_id: str, start, number: int) -> str:
    return f"{EVENT_BASE}{kind}_{_clean(patient_id)}_{start.strftime('%Y%m%dT%H%M%S')}_{number}"


def ground_chain(kb: KB, arch: "Archetype", patient_id: str, device_id: str,
                  identity: dict = None) -> tuple:
    """Ground every blueprint node below the device: (background graph —
    every node, its type, its identity refinements and every edge between
    nodes; condition graph — the leaf properties, i.e. the states the alarm
    reports).

    CONTRACT (agreed 2026-11-25, TODO_derivation_graphs.md): structure
    persists, states are transient. A metric is a node like a signal or a
    sensor: it and its edges are in <alarm#persistent>; its hasValueState /
    hasRhythm is in <alarm#transient>, as a signal's hasQualityState is.
    mda:Stateful (a universal identity carrying a per-alarm state) is
    honoured by where the STATE goes, not the node. So the whole pathway,
    sensor to the alarm's own metric, intermediate metrics included, is one
    graph, and a rule finds the alarm's own metric in it: the metric no
    step consumes (no mda:analyzedBy out of it). mda:triggeredByStructure,
    a pointer from the message to those termini, is retired (2026-11-25):
    the persistent graph already scopes the pathway to the alarm. No rule
    may conclude anything from a metric lacking a state: after the alarm
    ends, its metric node stays for the post-alarm window without one.
    Referential nodes are not grounded. An individuated
    node whose concept the scaffold (entities.ttl) does not declare is an
    error, not a skip: a stale scaffold used to cut the chain silently."""
    background, condition = Graph(), Graph()
    dev = device_iri(patient_id, device_id)
    device_bn = arch.node(MDA.Device)
    if device_bn is None:
        return background, condition
    minted = {id(device_bn): dev}
    memo = {}
    for bn in arch.under(MDA.Device):
        kind = kb.node_kind.get(bn.cls)
        if kind == "referential":
            continue
        grounded_parents = [(p, prop) for p, prop in bn.parents if id(p) in minted]
        if not grounded_parents:
            continue
        concept = arch.node_concept(bn)
        if kind == "individuated" and concept is not None and concept not in kb.scaffold_concepts:
            raise ValueError(f"{_local(concept)} ({_local(bn.cls)}) is not in the scaffold "
                             f"{ENTITIES.name}: regenerate it (entity_seed.py) and copy it to data/")
        particular = particular_iri(kb, arch, patient_id, device_id, bn, identity, memo)
        minted[id(bn)] = particular
        for p, prop in grounded_parents:
            background.add((minted[id(p)], prop, particular))
        background.add((particular, RDF.type, concept if concept is not None else bn.cls))
        for rprop in refining_properties(kb, bn.cls):
            v = arch.node_value(bn, rprop)
            if v is not None:
                background.add((particular, rprop, v))
        for lprop in leaf_properties(kb, bn.cls):
            v = arch.node_value(bn, lprop)
            if v is not None:
                condition.add((particular, lprop, v))
    return background, condition


def background_for_key(kb: KB, patient_id: str, ev, device_id: str, identity: dict) -> Graph:
    """What persists after the alarm: the device and its structural chain."""
    type_iri = type_of(kb, ev)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    g = Graph()
    dev = device_iri(patient_id, device_id)
    device_type = arch.concept(MDA.Device)
    if device_type:
        g.add((dev, RDF.type, device_type))
        g.add((dev, DCTERMS.identifier, Literal(device_id)))
    bg, _ = ground_chain(kb, arch, patient_id, device_id, identity)
    g += bg
    # The device's own state (hasDeviceOperationState) is mda:persistsPostAlarm:
    # it stays visible for the post-alarm window. condition_for_event also
    # grounds it, for rules that need it only while the alarm is active.
    ground_leaf_properties(g, kb, arch, MDA.Device, dev)
    return g


def alarm_message(kb: KB, ev, identity: dict = None) -> Graph:
    """The alarm, its message and patient."""
    g = Graph()
    type_iri = type_of(kb, ev)
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
    return g


def condition_for_event(kb: KB, patient_id: str, ev, device_id: str,
                         identity: dict = None) -> Graph:
    """What the alarm reports while active: every leaf property (the states;
    the nodes they sit on are in background_for_key), including the device's state (CAT3b needs a ventilator fault
    only while its alarm is active)."""
    type_iri = type_of(kb, ev)
    if type_iri is None:
        return Graph()
    arch = archetype_structure(kb, type_iri)
    _, condition = ground_chain(kb, arch, patient_id, device_id, identity)
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
    type_iri = type_of(kb, event)
    if type_iri is None:
        return
    arch = archetype_structure(kb, type_iri)
    for bn in arch.nodes:
        cls = bn.cls
        if cls not in tracker.state:
            continue
        concept = arch.node_concept(bn)
        if concept is None:
            continue
        ik = (event.patient, event.device_id, concept)
        if ik in tracker.conflicted[cls]:
            continue
        values_by_prop = tracker.state[cls].setdefault(ik, {})
        refinements = {p: v for p in tracker.refiners[cls]
                       if (v := arch.node_value(bn, p)) is not None}
        for p, v in refinements.items():
            values_by_prop.setdefault(p, set()).add(v)
        conflicting = any(len(vs) > 1 for vs in values_by_prop.values())
        if conflicting:
            tracker.conflicted[cls].add(ik)
            tracker.identity[cls].pop(ik, None)
        else:
            tracker.identity[cls][ik] = {p: next(iter(vs)) for p, vs in values_by_prop.items()}


def add_triggered_by(g: Graph, events: list, kb: KB, identity: dict) -> None:
    """Each message mda:triggeredBy its device, as in the ontology and the
    blueprints. Rules reach the alarm's own functional unit through the
    device inside the alarm's own persistent graph (<alarm#persistent>),
    which holds only that alarm's chain: the device is shared across the
    alarms of one device, so walking device -> functional unit outside that
    graph would reach every functional unit the device has reported."""
    for e in events:
        type_iri = type_of(kb, e)
        if type_iri is None:
            continue
        arch = archetype_structure(kb, type_iri)
        msg = message_iri(e)
        g.add((msg, MDA.triggeredBy, device_iri(e.patient, e.device_id)))
