"""
build_framework.py

Ontology-driven readout script for the annotated alarm CSV.

Analyzes the annotated CSV (DATA/ANNOTATION/consensus/p50_annotated.csv)
against the hand-maintained base files in FRAMEWORK/ONTOLOGY/ and
FRAMEWORK/VOCABULARY/seed/, and fully regenerates two output files;
everything the generated files hold, generated or hand-authored, is what
the readout produced from those inputs:

  FRAMEWORK/VOCABULARY/vocab_generated.ttl   every SKOS concept required by
                        the CSV that is not yet present in vocab_base.ttl
                        (auto-generated scheme expansion, for expert review)
  FRAMEWORK/KNOWLEDGE_BASE/kg_generated.ttl  one mda:AlarmType archetype per
                        CSV row, with blank-node existential structure
                        ("some device of this type, carrying some sensor of
                        this type, ...")

The nesting of the generated KG is NOT hard-coded.  It is derived at
runtime from FRAMEWORK/ONTOLOGY/ontology.ttl:

  1. Object properties with a named rdfs:domain and rdfs:range span a
     class tree, discovered by breadth-first search from mda:Alarm
     (inverse/cyclic edges are pruned; genuine ambiguity aborts the run).
  2. mda:instantiatesClass binds a concept scheme to the class its
     concepts specialise — a "node column": the concept becomes the
     rdf:type of a blank node of that class (device, sensor, signal, ...).
  3. mda:valuesFromScheme binds an object property to the scheme its
     values are drawn from — a "leaf column": the concept becomes the
     property's object on the node of the property's domain class
     (priority, category, ...).

Intermediate classes on a path (e.g. AlarmMessage between Alarm and its
category) are materialized automatically, so ontology rules like
"category always sits on an AlarmMessage node" are enforced by the
ontology itself, not by this script.

Adding a new (nested) CSV column therefore requires NO change to the
triple builder: declare the class and property in ontology.ttl, add the
scheme binding there, add the scheme to vocab_base.ttl, and map the CSV
column name to its scheme namespace in COLUMN_SCHEMES below.

Usage
-----
  python3 build_framework.py

Both outputs are pure functions of (ontology.ttl, vocab_base.ttl,
vocab_data.csv): they are rewritten from scratch on every run, never
appended to.

Two distinct promotion paths exist for a generated stub, and they are not
interchangeable. Adding a notation directly to vocab_base.ttl (checked by
build_notation_index/collect_missing below) marks it as truly universal,
timeless scaffold — rare, and this script stops re-generating a stub for it
on the next run. The much more common path is 3_CURATION: a stub gets
reviewed, enriched, and possibly redefined there instead, as a *catalogue-
scoped* curated concept. This script deliberately does NOT check 3_CURATION
before generating — that would make stage 2 depend on stage 3, breaking the
pipeline's one-way ordering — so a promoted concept's stub keeps being
regenerated here too. That is harmless surplus, not drift: 4_OPERATIONAL
reads 3_CURATION exclusively, never this file's output directly, and
3_CURATION/check_delta.py is the tool for seeing where curation has
knowingly diverged from what this script still (mechanically, correctly)
produces.
"""

import re
import sys
from pathlib import Path

import pandas as pd
from rdflib import Graph, URIRef, Literal, BNode, Namespace
from rdflib.collection import Collection
from rdflib.namespace import RDF, RDFS, OWL, SKOS

sys.path.append(str(Path(__file__).resolve().parent.parent / "shared"))
from ontology_tree import derive_class_tree, format_tree, local as _local

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR      = Path(__file__).resolve().parent
ROOT            = SCRIPT_DIR.parent.parent   # CODE/framework_build -> CODE -> Restructured
FRAMEWORK_DIR   = ROOT / "FRAMEWORK"
ONTOLOGY_PATH   = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB_BASE_PATH = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "vocab_base.ttl"
CSV_PATH        = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p75_annotated.csv"
VOCAB_OUT_PATH  = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
KG_OUT_PATH     = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
INFERENCE_PATH  = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"

# ── Namespaces ────────────────────────────────────────────────────────────────

MDA       = Namespace("https://w3id.org/mda/ontology#")
ALARMTYPE = Namespace("https://w3id.org/mda/alarmtype/")
VOCAB     = "https://w3id.org/mda/vocab/"
ALARM_LABEL = Namespace(VOCAB + "alarm-label/")

# Root of the class tree used for path-finding.  Domains in the ontology are
# asserted on mda:Alarm; the emitted root individuals are typed mda:AlarmType.
ROOT_CLASS = MDA.Alarm

# ── Column configuration ─────────────────────────────────────────────────────
# LABEL_COLUMN holds the alarm label (mda:hasLabel + IRI minting, together
# with PRIORITY_COLUMN).  COLUMN_SCHEMES is the ONLY per-column vocabulary
# configuration: which concept scheme a CSV column draws from.  Everything
# else (attachment property or class, nesting position, broader concept for
# stubs) is derived from ontology.ttl and vocab_base.ttl.  The dotted column
# names ("Sensor.AnatomicalPosition") are documentation for the reader; the
# actual attachment always comes from the ontology bindings.  Columns listed
# here but absent from the CSV are ignored; CSV columns not listed here are
# reported as a warning.

LABEL_COLUMN    = "alarmLabel"
PRIORITY_COLUMN = "alarmPriority"

# The CSV's second column has no header (it sits between "alarmLabel;" and
# "alarmPriority;" as a bare ";"), so pandas names it "Unnamed: 1". It is a
# human bookkeeping marker for the annotation review, not ontology data: a
# row marked 'EXCL' has been excluded from that review and must not be
# read out into the ontology at all.
EXCLUSION_COLUMN = "Unnamed: 1"
EXCLUSION_VALUE  = "EXCL"

COLUMN_SCHEMES = {
    "alarmPriority":             ("alarmprio",             VOCAB + "alarm-priority/"),
    "alarmCategory":             ("alarmcat",              VOCAB + "alarm-category/"),
    "Device":                    ("device",                VOCAB + "device/"),
    "Device.Type":               ("deviceType",            VOCAB + "device-type/"),
    "Device.Manufacturer":       ("manufacturer",          VOCAB + "manufacturer/"),
    "Device.OperationalState":   ("opstate",               VOCAB + "operation-state/"),
    "Component":                 ("component",             VOCAB + "component/"),
    "Component.OperationalState":("opstate",               VOCAB + "operation-state/"),
    "FunctionalUnit":            ("functionalUnit",        VOCAB + "functional-unit/"),
    "Sensor":                    ("sensor",                VOCAB + "sensor/"),
    "Sensor.OperationalState":   ("opstate",               VOCAB + "operation-state/"),
    "Sensor.AnatomicalPosition": ("anatomicalPosition",    VOCAB + "anatomical-position/"),
    "Sensor.Laterality":         ("laterality",            VOCAB + "laterality/"),
    "Signal":                    ("signal",                VOCAB + "signal/"),
    "Signal.QualityState":       ("qualitystate",          VOCAB + "quality-state/"),
    "Metric":                    ("metric",                VOCAB + "metric/"),
    "Metric.Phase":              ("metricPhase",           VOCAB + "metric-phase/"),
    "Metric.Rate":               ("metricRate",            VOCAB + "metric-rate/"),
    "Metric.Rhythm":             ("rhythm",                VOCAB + "rhythm/"),
    "PhysiologicalProperty":     ("physiologicalProperty", VOCAB + "physiological-property/"),
    "PhysiologicalProcess":      ("physiologicalProcess",  VOCAB + "physiological-process/"),
    "Organ":                     ("organ",                 VOCAB + "organ/"),
    "OrganSystem":               ("organSystem",           VOCAB + "organ-system/"),
    "Patient":                   ("patient",               VOCAB + "patient/"),
    "TherapeuticModality":       ("therapeuticModality",   VOCAB + "therapeutic-modality/"),
}


# ── Scheme binding derivation ─────────────────────────────────────────────────

# Some columns share a vocabulary scheme with sibling columns but must attach
# through a DIFFERENT property each (Device.OperationalState,
# Sensor.OperationalState, and Component.OperationalState all draw values
# from opstate:Scheme, but each attaches via its own single-domain
# sub-property of mda:hasOperationState — see ontology.ttl). A scheme
# bound to more than one property is genuinely ambiguous from the ontology
# alone, so those columns are listed here explicitly; every other column has
# exactly one property per scheme and is resolved automatically below.
COLUMN_PROPERTY_OVERRIDE = {
    "Device.OperationalState":    MDA.hasDeviceOperationState,
    "Sensor.OperationalState":    MDA.hasSensorOperationState,
    "Component.OperationalState": MDA.hasComponentOperationState,
}


def derive_bindings(onto: Graph, columns: list) -> dict:
    """
    For each CSV column, derive how its concepts attach to the KG:

      ("node", None, class, scheme)   concept types a blank node of `class`
      ("leaf", prop, domain, scheme)  concept is the object of `prop` on
                                      the node of `domain`

    from mda:instantiatesClass / mda:valuesFromScheme in the ontology, or
    COLUMN_PROPERTY_OVERRIDE above where a scheme is shared by more than one
    property.
    """
    bindings = {}
    for col in columns:
        prefix, base_uri = COLUMN_SCHEMES[col]
        scheme = Namespace(base_uri)["Scheme"]

        override = COLUMN_PROPERTY_OVERRIDE.get(col)
        if override is not None:
            domains = [d for d in onto.objects(override, RDFS.domain) if isinstance(d, URIRef)]
            if len(domains) != 1:
                raise ValueError(
                    f"Column '{col}' overrides to {_local(override)}, which "
                    f"needs exactly one named rdfs:domain."
                )
            bindings[col] = ("leaf", override, domains[0], scheme)
            continue

        props   = sorted(onto.subjects(MDA.valuesFromScheme, scheme))
        classes = sorted(onto.objects(scheme, MDA.instantiatesClass))

        if props and classes:
            raise ValueError(
                f"Scheme {prefix}:Scheme is bound both via mda:valuesFromScheme "
                f"and mda:instantiatesClass — it must be one or the other."
            )
        if len(props) > 1 or len(classes) > 1:
            raise ValueError(f"Scheme {prefix}:Scheme has multiple bindings in ontology.ttl.")

        if props:
            prop = props[0]
            domains = [d for d in onto.objects(prop, RDFS.domain) if isinstance(d, URIRef)]
            if len(domains) != 1:
                raise ValueError(
                    f"Cannot determine a unique attachment class for {_local(prop)} "
                    f"(column '{col}'): it needs exactly one named rdfs:domain."
                )
            bindings[col] = ("leaf", prop, domains[0], scheme)
        elif classes:
            bindings[col] = ("node", None, classes[0], scheme)
        else:
            raise ValueError(
                f"No binding for column '{col}': assert either "
                f"'?property mda:valuesFromScheme {prefix}:Scheme' or "
                f"'{prefix}:Scheme mda:instantiatesClass ?class' in ontology.ttl."
            )
    return bindings


# ── Notation → IRI lookup (scheme-scoped) ─────────────────────────────────────

def build_notation_index(*graphs: Graph) -> dict:
    """Return {(scheme, notation): concept} over all given graphs."""
    index = {}
    for g in graphs:
        for concept, notation in g.subject_objects(SKOS.notation):
            for scheme in g.objects(concept, SKOS.inScheme):
                index[(scheme, str(notation))] = concept
    return index


def resolve(scheme: URIRef, notation: str, index: dict, column: str) -> URIRef:
    """Resolve a notation within its scheme, raising clearly on miss."""
    iri = index.get((scheme, notation))
    if iri is None:
        raise KeyError(
            f"No concept with skos:notation '{notation}' in scheme "
            f"<{scheme}> (column: {column})."
        )
    return iri


# ── Vocabulary expansion ──────────────────────────────────────────────────────

def _human_label(notation: str) -> str:
    """
    Convert a CamelCase notation to a human-readable label.
    'BPSensor'             → 'BP Sensor'
    'PhysiologicalMonitor' → 'Physiological Monitor'
    """
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", notation)
    return spaced.strip()


# Characters that cannot appear in an IRI local name (would break the
# generated concept IRI and its Turtle serialization).
_INVALID_NOTATION = re.compile(r"[\s<>\"{}|\^`\\]")


def validate_notations(df: pd.DataFrame, columns: list) -> None:
    """
    Abort with a clear, aggregated error if any vocabulary cell holds a
    value that cannot become a valid IRI local name (e.g. contains a
    space).  Fails loudly here instead of crashing later in rdflib
    serialization.
    """
    bad = []
    for col in columns:
        for i, val in df[col].dropna().items():
            v = str(val).strip()
            if v and _INVALID_NOTATION.search(v):
                bad.append(f"  row {i + 2}, column '{col}': '{v}'")
    if bad:
        raise ValueError(
            "Invalid notation value(s) — a vocabulary cell contains "
            "whitespace or an IRI-illegal character:\n" + "\n".join(bad) +
            "\nFix these in the source CSV (e.g. 'Critical decrease' → "
            "'CriticalDecrease')."
        )


def collect_missing(df: pd.DataFrame, columns: list, bindings: dict, index: dict) -> list:
    """Return (notation, column) pairs present in df but absent from index."""
    missing = []
    for col in columns:
        scheme = bindings[col][3]
        for val in df[col].dropna().str.strip().unique():
            if val and (scheme, val) not in index:
                missing.append((val, col))
    return missing


def build_vocab_graph(missing: list, bindings: dict, base: Graph) -> Graph:
    """Build the auto-generated SKOS expansion graph for all missing notations."""
    g = Graph()
    g.bind("mda", MDA)
    g.bind("skos", SKOS)
    g.bind("rdfs", RDFS)
    for prefix, base_uri in COLUMN_SCHEMES.values():
        g.bind(prefix, Namespace(base_uri))

    for notation, column in sorted(missing, key=lambda m: (m[1], m[0])):
        kind, _, cls, scheme = bindings[column]
        ns    = Namespace(COLUMN_SCHEMES[column][1])
        c_iri = ns[notation]
        label = _human_label(notation)
        top   = next(base.subjects(SKOS.topConceptOf, scheme), None)

        g.add((c_iri, RDF.type, SKOS.Concept))
        g.add((c_iri, SKOS.inScheme, scheme))
        if top is not None:
            g.add((c_iri, SKOS.broader, top))
        if kind == "node":
            g.add((c_iri, RDFS.subClassOf, cls))
        g.add((c_iri, SKOS.prefLabel, Literal(label, lang="en")))
        # Dutch placeholder — edit manually for a real translation
        g.add((c_iri, SKOS.prefLabel, Literal(label, lang="nl")))
        g.add((c_iri, SKOS.definition, Literal(label, lang="en")))
        g.add((c_iri, SKOS.definition, Literal(label, lang="nl")))
        g.add((c_iri, SKOS.notation, Literal(notation)))

    return g


# ── Inference-referenced concepts ───────────────────────────────────────────
#
# inference.ttl is hand-authored, not generated (see its own
# header) — but every /vocab/ concept it references (e.g. a new
# physiologicalProcess: concept introduced only for a targetsProcess tail
# axiom, never mentioned anywhere in the CSV) still needs to actually BE a
# registered skos:Concept somewhere, or it exists as a bare, undocumented
# IRI. The alternative — hand-maintaining a second registration list in
# vocab_base.ttl alongside the axioms that use it — is exactly the kind of
# duplication this pipeline's "generated stub, not hand-curated" discipline
# elsewhere in this file exists to avoid. So inference.ttl is scanned the
# same way the CSV is: whatever it references becomes the source, a stub is
# generated from that alone, and re-running this script keeps the vocabulary
# in sync with whatever the axioms currently say — no second list to forget
# to update.

def vocab_namespaces(base: Graph, onto: Graph) -> dict:
    """
    {namespace_uri: (scheme_iri, node_class_or_None)} for every SKOS scheme
    registered in vocab_base.ttl, derived from the scheme's own IRI rather
    than hardcoded — so a namespace with no base registration (e.g.
    signal-analysis:, deliberately: see mda:SignalAnalysis's comment on why
    it has no vocabulary scheme of its own) is correctly never a target for
    auto-stubbing. node_class is the class the scheme instantiates
    (mda:instantiatesClass) when it is a "node" scheme, so a stub generated
    here gets the same rdfs:subClassOf a CSV-driven one would; None for a
    "leaf" scheme.
    """
    mapping = {}
    for scheme in base.subjects(RDF.type, SKOS.ConceptScheme):
        ns = str(scheme).rsplit("/", 1)[0] + "/"
        cls = next(onto.objects(scheme, MDA.instantiatesClass), None)
        mapping[ns] = (scheme, cls)
    return mapping


def collect_inference_concepts(inference: Graph, base: Graph, onto: Graph,
                                already_known: set) -> set:
    """
    Every /vocab/ concept inference.ttl references (as subject or object of
    any triple) that isn't already registered — in vocab_base.ttl, or
    already produced as a CSV-driven stub (`already_known`, so the two
    sources dedupe against each other rather than double-stubbing a concept
    the CSV also happens to mention).
    """
    namespaces = vocab_namespaces(base, onto)
    known = {s for s in base.subjects(RDF.type, SKOS.Concept)} | already_known
    referenced = set()
    for s, p, o in inference:
        for term in (s, o):
            if not isinstance(term, URIRef):
                continue
            s_str = str(term)
            if not s_str.startswith(VOCAB) or s_str.endswith("/Scheme"):
                continue
            ns = s_str.rsplit("/", 1)[0] + "/"
            if ns in namespaces and term not in known:
                referenced.add(term)
    return referenced


def build_metric_recipe(inference: Graph) -> dict:
    """
    {metric_concept: (sensor_concept, signal_concept, analysis_concept)},
    derived by walking inference.ttl's FunctionalUnit-recipe reference
    triples backward: Metric <-producesMetric- SignalAnalysis
    <-analyzedBy- Signal <-sensorProducesSignal- Sensor.

    These triples are deliberately plain data now, not OWL restrictions
    (see inference.ttl's "FunctionalUnit recipes" header) — a functional
    unit's full technical menu (which sensors it COULD have) is not the
    same claim as which single trace a given alarm actually evidences.
    build_alarmtype_triples()'s recipe_override() is the consumer: for a
    row naming a Metric with no Sensor/Signal of its own, it looks up the
    one trace that specific Metric came from and asserts only that —
    never the functional unit's other, unevidenced sensor(s)/metric(s).
    """
    analysis_of_metric = {m: a for a, m in inference.subject_objects(MDA.producesMetric)}
    signal_of_analysis = {a: s for s, a in inference.subject_objects(MDA.analyzedBy)}
    sensor_of_signal   = {s: sn for sn, s in inference.subject_objects(MDA.sensorProducesSignal)}
    recipe = {}
    for metric, analysis in analysis_of_metric.items():
        signal = signal_of_analysis.get(analysis)
        sensor = sensor_of_signal.get(signal) if signal is not None else None
        if signal is not None and sensor is not None:
            recipe[metric] = (sensor, signal, analysis)
    return recipe


def build_administers_map(inference: Graph) -> dict:
    """
    {device_broad_class: modality_concept}, read directly off inference.ttl's
    plain device:X mda:administers therapeuticModality:Y triples — the same
    plain-triples-not-restrictions pattern and the same reason as the
    FunctionalUnit recipes above (see that section's header): a plain triple
    on a punned concept doesn't propagate to instances under OWL-RL, so this
    sits inertly as pure reference data for this function to read, never as
    something a reasoner would fire onto a per-alarm individual.

    build_alarmtype_triples()'s recipe_override() is the consumer: every alarm
    whose device is one of these categories gets its own individuated
    TherapeuticModality blank node and a real per-row mda:administers edge,
    using the CSV's own TherapeuticModality value when given or this mapping
    otherwise — never a class-level restriction pointing every alarm at the
    same shared concept (that was the pre-individuation design; it made
    mda:hasOperationState/mda:hasTherapyDeliveryQuality's propertyChainAxiom
    leak per-alarm state onto the shared modality concept globally, since a
    device would end up administering both the shared concept AND its own
    per-alarm modality node at once — see inference.ttl's "Technical axioms"
    header for why the class-level restrictions were replaced with this).
    """
    return {device: modality
            for device, modality in inference.subject_objects(MDA.administers)}


def build_inference_concept_stubs(concepts: set, base: Graph, onto: Graph) -> Graph:
    """Same generated-stub shape as build_vocab_graph, keyed on the concept
    IRIs inference.ttl itself references rather than a CSV notation."""
    g = Graph()
    namespaces = vocab_namespaces(base, onto)
    for c_iri in sorted(concepts, key=str):
        ns = str(c_iri).rsplit("/", 1)[0] + "/"
        scheme, cls = namespaces[ns]
        top = next(base.subjects(SKOS.topConceptOf, scheme), None)
        notation = _local(c_iri)
        label = _human_label(notation)

        g.add((c_iri, RDF.type, SKOS.Concept))
        g.add((c_iri, SKOS.inScheme, scheme))
        if top is not None:
            g.add((c_iri, SKOS.broader, top))
        if cls is not None:
            g.add((c_iri, RDFS.subClassOf, cls))
        g.add((c_iri, SKOS.prefLabel, Literal(label, lang="en")))
        g.add((c_iri, SKOS.prefLabel, Literal(label, lang="nl")))
        g.add((c_iri, SKOS.definition, Literal(label, lang="en")))
        g.add((c_iri, SKOS.definition, Literal(label, lang="nl")))
        g.add((c_iri, SKOS.notation, Literal(notation)))
    return g


# ── KG expansion ──────────────────────────────────────────────────────────────
#
# Every node in an alarm archetype is classified (from mda:nodeKind in the
# ontology) as one of:
#
#   individuated  physical particular → a fresh per-alarm node (blank now,
#                 entity IRI after grounding).  Stays in the KG graph.
#   referential   conceptual universal → the concept IRI is used directly;
#                 its onward relations are concept-to-concept and therefore
#                 deduplicate across alarms.  These go to the VOCAB graph.
#   stateful      a hinge (Metric): identity is a pre-coordinated universal
#                 concept (base + mda:refinesUniversalIdentity leaves such as
#                 phase), but each alarm gets a thin per-alarm instance
#                 carrying its state (rate/rhythm).  Concept + tail → VOCAB;
#                 instance → KG.
#   structural    a class with no vocabulary scheme (e.g. AlarmMessage) →
#                 a per-alarm blank node typed by the class itself.  KG.

KIND_OF = {
    MDA.Individuated: "individuated",
    MDA.Referential:  "referential",
    MDA.Stateful:     "stateful",
}


def alarmtype_iri(label: str, priority: str) -> URIRef:
    """
    Generate a stable alarm type IRI from label + priority.
    'HR High' + 'High' → alarmtype:HrHigh_High
    """
    clean_label    = re.sub(r"[^A-Za-z0-9]+", "", label.title().replace(" ", ""))
    clean_priority = re.sub(r"[^A-Za-z0-9]+", "_", priority).strip("_")
    return ALARMTYPE[f"{clean_label}_{clean_priority}"]


def label_concept_iri(label: str) -> URIRef:
    """
    The controlled-vocabulary concept for an alarm label — deliberately NOT
    alarmtype_iri: the same label can recur across archetypes that differ
    only in priority (e.g. two CSV rows for "SERVOU - Ademfrequentie hoog",
    one Unknown one Medium), and mda:hasLabel/mda:hasLabelConcept are two
    readings of the same label (the free-text string as encountered, and the
    controlled term for it as a distinct concept), not two different data —
    see mda:hasLabel's comment in ontology.ttl. Keyed on the label alone, so
    every archetype sharing a label points at the same one concept.
    """
    clean = re.sub(r"[^A-Za-z0-9]+", "", label.title().replace(" ", ""))
    return ALARM_LABEL[clean]


def build_label_vocab(labels: set, base: Graph) -> Graph:
    """
    One skos:Concept per distinct alarm label encountered in the CSV, in
    alarmLabel:Scheme — the vocabulary mda:hasLabelConcept points into.
    Same generated-stub shape as build_vocab_graph, but keyed on the raw
    label string rather than a CSV notation: labels contain spaces/
    punctuation that validate_notations would reject outright, so this
    never goes through the COLUMN_SCHEMES/resolve() pipeline at all.
    """
    g = Graph()
    scheme = ALARM_LABEL["Scheme"]
    top = next(base.subjects(SKOS.topConceptOf, scheme), None)
    for label in sorted(labels):
        c_iri = label_concept_iri(label)
        g.add((c_iri, RDF.type, SKOS.Concept))
        g.add((c_iri, SKOS.inScheme, scheme))
        if top is not None:
            g.add((c_iri, SKOS.broader, top))
        g.add((c_iri, SKOS.prefLabel, Literal(label, lang="en")))
        g.add((c_iri, SKOS.notation, Literal(label)))
    return g


def _is_shared(node) -> bool:
    """True for a concept IRI (deduplicable); False for a per-alarm node."""
    return isinstance(node, URIRef) and not str(node).startswith(str(ALARMTYPE))


def _scheme_of(concept: URIRef) -> URIRef:
    """The <ns>/Scheme IRI for a concept in namespace <ns>/."""
    return URIRef(str(concept).rsplit("/", 1)[0] + "/Scheme")


def _specialised_iri(base: URIRef, refinements: list) -> URIRef:
    """
    Pre-coordinate a base concept with its identity refinements into a
    single stable IRI:  metric:ABP + phase Mean → metric:ABP_Mean.
    The phase lives in a triple, not only in the name.
    """
    ns, local = str(base).rsplit("/", 1)
    suffix = "_".join(
        re.sub(r"[^A-Za-z0-9]+", "", _local(c))
        for _, c in sorted(refinements, key=lambda r: str(r[0]))
    )
    return URIRef(f"{ns}/{local}_{suffix}")


def _restriction(ref: Graph, prop: URIRef, value: URIRef) -> BNode:
    r = BNode()
    ref.add((r, RDF.type, OWL.Restriction))
    ref.add((r, OWL.onProperty, prop))
    ref.add((r, OWL.hasValue, value))
    return r


def precoordinate(ref: Graph, concept: URIRef, refs: list) -> URIRef:
    """
    Pre-coordinate `concept` with its identity refinements (mda:refinesUniversal
    Identity leaves, e.g. Metric.hasPhase or Device.hasManufacturer) into one
    shared, deduplicated universal concept — the mechanism behind e.g.
    metric:ABP + phase Mean → metric:ABP_Mean, generalised to any node-column
    class, not only Stateful ones.

    Gives the pre-coordinated concept a full OWL definition rather than a bare
    IRI, so a reasoner recognises it both ways:

      sc rdfs:subClassOf concept                       necessary: every
                                                         PhilipsMonitor is a
                                                         PhysiologicalMonitor
      sc owl:equivalentClass [intersectionOf (concept   necessary AND
             restriction...)]                           sufficient: anything
                                                          that IS a
                                                          PhysiologicalMonitor
                                                          made by Philips is
                                                          thereby entailed a
                                                          PhilipsMonitor too
      sc <refining-prop> <refining-value>               plain triple, for
                                                         direct readability/
                                                         querying without a
                                                         reasoner

    The equivalentClass direction is unlike clinical_axioms.ttl's bridge
    axioms deliberately: those encode external medical knowledge no CSV row
    contains, so they can only assert necessary conditions by hand. This
    concept's necessary AND sufficient conditions are both already fully
    known — they are exactly the CSV cells that built `refs` — so defining it
    costs nothing extra and is the more complete, more correct OWL modelling
    choice.

    This is a GENERATED stub, same as everything else in vocab_generated.ttl:
    subject to expert review, and superseded the moment it is hand-promoted
    into a curated file with its own richer definition — regenerating never
    overwrites a promoted concept, it just stops re-emitting the stub (see
    module docstring).

    Deduplicates by construction: `sc`'s IRI is a pure function of
    (concept, refs), so two rows needing the same pre-coordinated concept
    produce the same IRI — but the OWL definition below is built from fresh
    blank nodes on every call, which plain triple-set dedup cannot collapse,
    so a second call for an already-registered `sc` is a deliberate no-op
    rather than a second (redundant, blank-node-distinct) definition.
    """
    sc = _specialised_iri(concept, refs)
    if (sc, OWL.equivalentClass, None) in ref:
        return sc

    ref.add((sc, RDF.type, SKOS.Concept))
    ref.add((sc, SKOS.inScheme, _scheme_of(concept)))
    ref.add((sc, SKOS.broader, concept))
    ref.add((sc, SKOS.notation, Literal(_local(sc))))
    ref.add((sc, SKOS.prefLabel, Literal(_human_label(_local(sc)), lang="en")))
    ref.add((sc, RDFS.subClassOf, concept))

    for p_r, cval in refs:
        ref.add((sc, p_r, cval))

    members = BNode()
    Collection(ref, members,
               [concept] + [_restriction(ref, p_r, cval) for p_r, cval in refs])
    definition = BNode()
    ref.add((definition, RDF.type, OWL.Class))
    ref.add((definition, OWL.intersectionOf, members))
    ref.add((sc, OWL.equivalentClass, definition))

    return sc


def build_alarmtype_triples(row: pd.Series, kg: Graph, ref: Graph, index: dict,
                            tree: dict, node_kind: dict, node_columns: dict,
                            leaf_specs: list, target_types: dict,
                            metric_recipe: dict, administers_map: dict) -> None:
    """
    Emit one CSV row as an mda:AlarmType, routing triples to two graphs:
    per-alarm particulars to `kg`, deduplicable universals to `ref`.
    """
    label  = str(row[LABEL_COLUMN]).strip()
    at_iri = alarmtype_iri(label, str(row.get(PRIORITY_COLUMN, "")).strip())
    kg.add((at_iri, RDF.type, MDA.AlarmType))
    kg.add((at_iri, MDA.hasLabel, Literal(label, lang="en")))
    kg.add((at_iri, MDA.hasLabelConcept, label_concept_iri(label)))

    def cell(col):
        if not col:
            return None
        v = row.get(col)
        if v is None or pd.isna(v) or not str(v).strip():
            return None
        return str(v).strip()

    def column_concept(cls):
        """Vocabulary concept named by cls's node-column in this row, or
        the recipe-derived trace for Sensor/Signal/SignalAnalysis when this
        row has no column value of its own (see recipe_override)."""
        col = node_columns.get(cls)
        v = cell(col)
        if v is not None:
            return resolve(_scheme_of_column(col), v, index, col)
        return recipe_override(cls)

    def recipe_override(cls):
        """For mda:Sensor/mda:Signal/mda:SignalAnalysis with no CSV value of
        their own: the single Sensor->Signal->SignalAnalysis trace
        inference.ttl's recipe says actually produces this row's Metric —
        never a FunctionalUnit's full menu of possible sensors/metrics, only
        the one this alarm evidences (see inference.ttl's "FunctionalUnit
        recipes" header and build_metric_recipe()).

        For mda:TherapeuticModality with no CSV value of its own: the
        modality build_administers_map()'s device-category mapping says this
        row's Device administers, so every alarm on an administering device
        type still gets its own individuated modality node (and a real
        per-row mda:administers edge) even when the CSV column is blank —
        never a class-level restriction pointing every alarm at one shared
        concept (see build_administers_map()'s docstring for why)."""
        if cls == MDA.TherapeuticModality:
            device_concept = column_concept(MDA.Device)
            if device_concept is None:
                return None
            return administers_map.get(device_concept)
        if cls not in (MDA.Sensor, MDA.Signal, MDA.SignalAnalysis):
            return None
        metric_concept = column_concept(MDA.Metric)
        if metric_concept is None:
            return None
        chain = metric_recipe.get(metric_concept)
        if chain is None:
            return None
        sensor_c, signal_c, analysis_c = chain
        return {MDA.Sensor: sensor_c, MDA.Signal: signal_c, MDA.SignalAnalysis: analysis_c}[cls]

    def _scheme_of_column(col):
        return Namespace(COLUMN_SCHEMES[col][1])["Scheme"]

    def identity_refinements(cls):
        """(property, concept) pairs from mda:refinesUniversalIdentity leaves of cls."""
        refs = []
        for dcls, prop, col, role, scheme in leaf_specs:
            if dcls == cls and role == "identity":
                v = cell(col)
                if v is not None:
                    refs.append((prop, resolve(scheme, v, index, col)))
        return refs

    nodes = {ROOT_CLASS: at_iri}   # in-node: object of the incoming link
    out   = {ROOT_CLASS: at_iri}   # out-node: subject of outgoing links

    def resolve_node(cls):
        """Returns True if `cls` was resolved, False if its branch is inactive
        (see the mda:administers short-circuit below) — callers must check
        before relying on nodes[cls]/out[cls]."""
        if cls in nodes:
            return True
        link = tree.get(cls)
        if link is None:
            raise KeyError(
                f"No path from {_local(ROOT_CLASS)} to {_local(cls)} in the "
                f"ontology — assert the connecting property's domain/range."
            )
        parent_cls, prop = link
        if not resolve_node(parent_cls):
            return False

        kind    = node_kind.get(cls)          # None → structural
        concept = column_concept(cls)

        # mda:administers stays inactive when the CSV names no modality —
        # nothing honest left to assert: forcing "administers some unnamed
        # modality" as an existential is wrong for a device that doesn't
        # administer anything (e.g. PhysiologicalMonitor), and whatever a
        # CSV's PhysiologicalProcess column would hang off that scaffold
        # (e.g. SpO2 monitor -> PulmonaryOxygenation) is already entailed
        # from the alarm's own Metric via approximates -> isPropertyOf —
        # verified against the reasoner for every metric currently used
        # this way (SpO2, CO2_EndTidal, HeartRate). For the device-CATEGORY
        # fact itself ("every ExtracorporealMembraneOxygenator administers
        # ECMO therapy"), inference.ttl's class-level bridge restriction
        # already covers every alarm on that device type regardless of
        # whether this branch is active at all — this check is only about
        # whether THIS row gets its own per-alarm TherapeuticModality node
        # (see the "individuated" branch below and the edge-write comment
        # further down for why that per-row node still matters).
        if prop == MDA.administers and concept is None:
            return False

        if kind == "individuated":
            b = BNode(); nodes[cls] = b; out[cls] = b
            # Identity-refining leaves (e.g. Device.hasManufacturer) still
            # register a pre-coordinated shorthand concept in the vocab graph
            # (precoordinate()'s side effect below) — a reasoner can derive
            # its membership from the raw leaves via the owl:equivalentClass
            # definition either way. But the KG blueprint itself asserts only
            # what the CSV row literally says: the base concept plus each
            # refining leaf as its own plain triple on the per-alarm particular
            # (device:PhysiologicalMonitor + hasManufacturer + hasDeviceType),
            # never the folded-together device:PhysiologicalMonitor_Philips.
            # The blueprint stays untransformed; only the vocab layer
            # pre-coordinates.
            refs = identity_refinements(cls) if concept is not None else []
            if refs:
                precoordinate(ref, concept, refs)
                kg.add((b, RDF.type, concept))
                for ref_prop, ref_val in refs:
                    kg.add((b, ref_prop, ref_val))
            else:
                # a data-valued node is typed by its concept; a structural
                # existential (e.g. the patient message-root, no CSV column)
                # is typed by its own class.
                kg.add((b, RDF.type, concept if concept is not None else cls))

        elif kind == "stateful":
            b = BNode(); nodes[cls] = b
            refs = identity_refinements(cls) if concept is not None else []
            if concept is None:
                out[cls] = b
            elif refs:
                sc = precoordinate(ref, concept, refs)
                out[cls] = sc
                kg.add((b, RDF.type, sc))
            else:
                out[cls] = concept
                kg.add((b, RDF.type, concept))

        elif kind == "referential":
            if concept is not None:
                nodes[cls] = concept; out[cls] = concept
            else:
                # genuinely-unspecified universal on a populated branch:
                # an honest existential ("some organ of …"), typed by its class
                b = BNode(); nodes[cls] = b; out[cls] = b
                kg.add((b, RDF.type, cls))

        else:  # structural — per-alarm blank typed by the class itself, or
               # by a more specific concept when one can be derived without
               # a CSV column of its own (mda:SignalAnalysis via the Metric
               # recipe — see recipe_override)
            b = BNode(); nodes[cls] = b; out[cls] = b
            kg.add((b, RDF.type, concept if concept is not None else cls))

        # Incoming link: concept→concept goes to vocab (dedup), else per-alarm KG.
        #
        # mda:administers is written here like any other property: since
        # TherapeuticModality is now Individuated (own per-alarm blank node,
        # not a shared concept — see its nodeKind comment in ontology.ttl),
        # parent_out (Device) is always a blank node too, so _is_shared is
        # always False and this always lands in kg, never deduplicated into
        # ref. That per-alarm edge is what makes the node reachable from the
        # alarm at all — without it, the individuated TherapeuticModality
        # blank node created above would be an orphan, and
        # mda:hasTherapyDeliveryQuality's propertyChainAxiom (ontology.ttl)
        # would have nothing to propagate across. inference.ttl's
        # device-category administers bridge is unaffected by this and
        # remains the fallback for rows where the CSV names no modality at
        # all (this branch never reaches here in that case — see above).
        parent_out, child_in = out[parent_cls], nodes[cls]
        target = ref if (_is_shared(parent_out) and _is_shared(child_in)) else kg
        target.add((parent_out, prop, child_in))

        # Marker co-types stamped by the connecting property (mda:targetType),
        # e.g. the message root (a Patient) is also typed mda:AlarmMessage.
        for extra in target_types.get(prop, ()):
            kg.add((child_in, RDF.type, extra))

        return True

    # Active classes: those with a node-column value or a present leaf's domain.
    # Waypoint ancestors are pulled in transitively by resolve_node — EXCEPT
    # mda:Patient and mda:TherapeuticModality, forced in unconditionally below.
    #
    # mda:Patient has no CSV column of its own, and used to be pulled in for
    # free as mda:Device's waypoint ancestor (Patient -isMonitoredBy-> Device).
    # Now that mda:triggeredBy (AlarmMessage -> Device directly) replaced that
    # routing, Patient is no longer an ancestor of anything CSV-active and
    # would never be resolved at all — silently dropping mda:concernsPatient
    # from every archetype, not just decoupling it from Device as intended.
    #
    # mda:TherapeuticModality is a CHILD of Device (Device -administers->
    # TherapeuticModality), not an ancestor of anything else active on a row
    # that leaves both TherapeuticModality and PhysiologicalProcess blank
    # (most rows) — so unlike Sensor/Signal/SignalAnalysis (pulled in as
    # Metric's ancestors whenever Metric is active), nothing transitively
    # reaches it, and recipe_override()'s build_administers_map() fallback
    # would never even get a chance to run without this. Forcing it active
    # unconditionally is safe: resolve_node's own mda:administers/concept-None
    # check still correctly leaves the branch inactive for device categories
    # that administer nothing (e.g. PhysiologicalMonitor).
    active = {cls for cls, col in node_columns.items() if cell(col) is not None}
    active |= {dcls for dcls, _, col, _, _ in leaf_specs if cell(col) is not None}
    active.add(MDA.Patient)
    active.add(MDA.TherapeuticModality)
    for cls in active:
        resolve_node(cls)

    # Attach leaf properties (identity leaves already folded into the concept)
    for dcls, prop, col, role, scheme in leaf_specs:
        if role == "identity":
            continue
        v = cell(col)
        if v is None:
            continue
        if not resolve_node(dcls):
            continue  # dcls's own branch is inactive (see resolve_node)
        kg.add((nodes[dcls], prop, resolve(scheme, v, index, col)))


def build_graphs(df: pd.DataFrame, index: dict, tree: dict, node_kind: dict,
                 node_columns: dict, leaf_specs: list, target_types: dict,
                 metric_recipe: dict, administers_map: dict):
    """Return (kg, ref): per-alarm particulars, and deduplicated universals."""
    kg, ref = Graph(), Graph()
    for g in (kg, ref):
        g.bind("alarmtype", ALARMTYPE)
        g.bind("mda", MDA)
        g.bind("skos", SKOS)
        for prefix, base_uri in COLUMN_SCHEMES.values():
            g.bind(prefix, Namespace(base_uri))

    failed = []
    for i, row in df.iterrows():
        try:
            build_alarmtype_triples(row, kg, ref, index, tree,
                                    node_kind, node_columns, leaf_specs, target_types,
                                    metric_recipe, administers_map)
        except KeyError as e:
            failed.append(f"  Row {i + 2}: {e}")
    if failed:
        print("\n[WARN]  Rows skipped:")
        for line in failed:
            print(line)
    return kg, ref


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Load the hand-maintained base files
    onto = Graph()
    onto.parse(ONTOLOGY_PATH, format="turtle")
    base = Graph()
    base.parse(VOCAB_BASE_PATH, format="turtle")
    inference = Graph()
    inference.parse(INFERENCE_PATH, format="turtle")

    # 2. Derive the nesting tree from the ontology and show it
    tree = derive_class_tree(onto, ROOT_CLASS)
    for line in format_tree(tree, ROOT_CLASS):
        print(f"[tree]   {line}")

    # 3. Read the alarm type definitions
    df = pd.read_csv(CSV_PATH, sep=";", dtype=str)
    df.columns = df.columns.str.strip()
    if EXCLUSION_COLUMN in df.columns:
        excluded = df[EXCLUSION_COLUMN].str.strip() == EXCLUSION_VALUE
        if excluded.any():
            print(f"[csv]    {excluded.sum()} row(s) marked '{EXCLUSION_VALUE}' — excluded from readout")
        df = df.loc[~excluded].drop(columns=[EXCLUSION_COLUMN])
    columns = [c for c in df.columns if c in COLUMN_SCHEMES]
    print(f"[csv]    Read {len(df)} row(s) from {CSV_PATH.name}; "
          f"vocabulary columns: {', '.join(columns)}")
    if LABEL_COLUMN not in df.columns:
        raise ValueError(f"CSV is missing the required label column '{LABEL_COLUMN}'.")
    unmapped = [c for c in df.columns if c not in COLUMN_SCHEMES and c != LABEL_COLUMN]
    if unmapped:
        print(f"[WARN]   Column(s) IGNORED — no COLUMN_SCHEMES mapping: {', '.join(unmapped)}")
    validate_notations(df, columns)

    # 4. Derive per-column attachment bindings, plus node-kind / leaf-role
    bindings = derive_bindings(onto, columns)
    stateful = set(onto.subjects(MDA.nodeKind, MDA.Stateful))
    node_columns, node_kind, leaf_specs = {}, {}, []
    for col in columns:
        kind, prop, cls, scheme = bindings[col]
        if kind == "node":
            node_columns[cls] = col
            marker = next((o for o in onto.objects(cls, MDA.nodeKind) if o in KIND_OF), None)
            node_kind[cls] = KIND_OF.get(marker, "referential")
        else:
            if any(str(o).lower() == "true" for o in onto.objects(prop, MDA.refinesUniversalIdentity)):
                role = "identity"
            elif cls in stateful:
                role = "state"
            else:
                role = "plain"
            leaf_specs.append((cls, prop, col, role, scheme))
    for col in columns:
        kind, prop, cls, _ = bindings[col]
        if kind == "node":
            how = f"{node_kind[cls]:12s} node on {_local(cls)}"
        else:
            role = next(r for c, p, cc, r, s in leaf_specs if cc == col)
            how = f"leaf {_local(prop)} ({role}) on {_local(cls)}"
        print(f"[bind]   {col:<24} → {how}")

    # Marker co-types stamped on nodes reached by a property (mda:targetType)
    target_types = {}
    for prop, extra in onto.subject_objects(MDA.targetType):
        target_types.setdefault(prop, []).append(extra)

    # 5. Vocabulary expansion — SKOS stubs for missing notations
    index     = build_notation_index(base)
    missing   = collect_missing(df, columns, bindings, index)
    vocab_out = build_vocab_graph(missing, bindings, base)
    print(f"[vocab]  {len(missing)} generated concept stub(s)")

    # 5b. Alarm-label vocabulary — one concept per distinct label, regardless
    # of how many (label, priority) archetypes share it (see label_concept_iri).
    labels = {str(v).strip() for v in df[LABEL_COLUMN].dropna() if str(v).strip()}
    label_vocab = build_label_vocab(labels, base)
    for t in label_vocab:
        vocab_out.add(t)
    print(f"[vocab]  {len(labels)} alarm-label concept(s)")

    # 5c. Concepts inference.ttl references but nothing has registered yet —
    # see the "Inference-referenced concepts" section above for why this
    # exists instead of a hand-kept registration list.
    already_known = {s for s in vocab_out.subjects(RDF.type, SKOS.Concept)}
    inferred_concepts = collect_inference_concepts(inference, base, onto, already_known)
    inferred_stubs = build_inference_concept_stubs(inferred_concepts, base, onto)
    for t in inferred_stubs:
        vocab_out.add(t)
    print(f"[vocab]  {len(inferred_concepts)} concept stub(s) from inference.ttl")

    # 6. KG expansion — particulars → kg_generated; universals → vocab_generated
    index            = build_notation_index(base, vocab_out)
    metric_recipe    = build_metric_recipe(inference)
    administers_map  = build_administers_map(inference)
    kg_out, ref_out = build_graphs(df, index, tree, node_kind, node_columns,
                                   leaf_specs, target_types, metric_recipe,
                                   administers_map)
    for t in ref_out:                     # merge the deduplicated universal graph
        vocab_out.add(t)

    VOCAB_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    KG_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    vocab_out.serialize(destination=str(VOCAB_OUT_PATH), format="turtle")
    kg_out.serialize(destination=str(KG_OUT_PATH), format="turtle")

    n_types = sum(1 for s in set(kg_out.subjects()) if str(s).startswith(str(ALARMTYPE)))
    n_bnodes = len({x for t in kg_out for x in t if isinstance(x, BNode)})
    print(f"[vocab]  {len(vocab_out)} triples ({len(ref_out)} universal) → "
          f"{VOCAB_OUT_PATH.relative_to(ROOT)}")
    print(f"[kg]     {len(kg_out)} triples, {n_types} alarm type(s), "
          f"{n_bnodes} blank node(s) → {KG_OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
