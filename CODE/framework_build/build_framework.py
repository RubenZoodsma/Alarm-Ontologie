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
from canonical_turtle import serialize_canonical

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR      = Path(__file__).resolve().parent
ROOT            = SCRIPT_DIR.parent.parent   # CODE/framework_build -> CODE -> Restructured
FRAMEWORK_DIR   = ROOT / "FRAMEWORK"
ONTOLOGY_PATH   = FRAMEWORK_DIR / "ONTOLOGY" / "ontology.ttl"
VOCAB_BASE_PATH = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "vocab_base.ttl"
HIERARCHY_PATH  = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "hierarchy.ttl"
CLINICAL_EVENT_VOCAB_PATH = FRAMEWORK_DIR / "VOCABULARY" / "seed" / "clinicalEvent_vocab.ttl"
CSV_PATH        = ROOT / "DATA" / "ANNOTATION" / "consensus" / "p99_annotated.csv"
VOCAB_OUT_PATH  = FRAMEWORK_DIR / "VOCABULARY" / "vocab_generated.ttl"
KG_OUT_PATH     = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "kg_generated.ttl"
INFERENCE_PATH  = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "inference.ttl"
CLINICAL_EVENTS_PATH = FRAMEWORK_DIR / "KNOWLEDGE_BASE" / "clinicalEvents.ttl"

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

# Human bookkeeping marker for the annotation review, not ontology data:
#   x       reviewed
#   EXCL    not annotatable (label conveys no interpretable alarm condition);
#           never read out into the ontology
#   REVIEW  copied from another device's annotation when a label + priority
#           was split per device model; built, pending team annotation
EXCLUSION_COLUMN = "reviewStatus"
EXCLUSION_VALUE  = "EXCL"
REVIEW_VALUE     = "REVIEW"
MODEL_COLUMN     = "Device.Type"

# A blueprint is identified by device model + label + priority
# (FRAMEWORK/PLAN_blueprint_identity.md). The selected corpus names devices by
# their gateway name; device_models.csv maps that name to the model.
CORPUS_PATH       = ROOT / "DATA" / "CORPUS" / "Annotation_corpus_99.csv"
DEVICE_MODELS_PATH = ROOT / "DATA" / "ANNOTATION" / "device_models.csv"

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
    # The four metric characteristics — see ontology.ttl's "FOUR
    # CHARACTERISTICS OF A METRIC" comment for why they are four.
    # Metric.Phase and Metric.Aggregation are both identity-refining
    # (pre-coordinated into the metric concept); Metric.ValueState and
    # Metric.Rhythm are per-alarm state.
    # All four share the metric- prefix on purpose: they are characteristics
    # OF a metric, not free-standing axes, and three of them used to read
    # aggregation:/valuestate:/rhythm: against metricPhase:.
    "Metric.Phase":              ("metricPhase",           VOCAB + "metric-phase/"),
    "Metric.Aggregation":        ("metricAggregation",     VOCAB + "metric-aggregation/"),
    "Metric.ValueState":         ("metricValueState",      VOCAB + "metric-value-state/"),
    "Metric.Rhythm":             ("metricRhythm",          VOCAB + "metric-rhythm/"),
    "PhysiologicalProperty":     ("physiologicalProperty", VOCAB + "physiological-property/"),
    "PhysiologicalProcess":      ("physiologicalProcess",  VOCAB + "physiological-process/"),
    "Organ":                     ("organ",                 VOCAB + "organ/"),
    "OrganSystem":               ("organSystem",           VOCAB + "organ-system/"),
    "Patient":                   ("patient",               VOCAB + "patient/"),
    "TherapeuticModality":       ("therapeuticModality",   VOCAB + "therapeutic-modality/"),
}


# ── Scheme binding derivation ─────────────────────────────────────────────────

# Some columns share a vocabulary scheme with sibling columns, or with a
# property that is never CSV-driven at all, and must name the property they
# attach through (Device.OperationalState, Sensor.OperationalState and
# Component.OperationalState all draw values from opstate:Scheme, each via
# its own single-domain sub-property of mda:hasOperationState;
# Signal.QualityState shares qualitystate:Scheme with the entailment-only
# mda:hasTherapyDeliveryQuality — see ontology.ttl). A scheme
# bound to more than one property is genuinely ambiguous from the ontology
# alone, so those columns are listed here explicitly; every other column has
# exactly one property per scheme and is resolved automatically below.
# Vocabulary namespaces that no CSV column drives, and therefore do not
# appear in COLUMN_SCHEMES above. They still need a prefix bound on every
# graph this script serialises, or their concepts come out as bare absolute
# IRIs — which is exactly what happened to signal-analysis: 29 concepts, 15
# of them typing nodes in kg_generated.ttl, every one written out in full
# while every other node kind in the same sensing pathway rendered as a
# prefixed name. Registering the scheme in vocab_base.ttl fixed the
# declarations; this fixes the serialisation.
EXTRA_SCHEME_PREFIXES = {
    "signalAnalysis": VOCAB + "signal-analysis/",
}


def bind_vocab_prefixes(g: Graph) -> None:
    """Bind every vocabulary prefix this framework mints into, column-driven
    or not. Use in place of iterating COLUMN_SCHEMES directly."""
    for prefix, base_uri in COLUMN_SCHEMES.values():
        g.bind(prefix, Namespace(base_uri))
    for prefix, base_uri in EXTRA_SCHEME_PREFIXES.items():
        g.bind(prefix, Namespace(base_uri))


COLUMN_PROPERTY_OVERRIDE = {
    "Device.OperationalState":    MDA.hasDeviceOperationState,
    "Sensor.OperationalState":    MDA.hasSensorOperationState,
    "Component.OperationalState": MDA.hasComponentOperationState,
    # qualitystate:Scheme became shared the moment
    # mda:hasTherapyDeliveryQuality was bound to it — a quality state is a
    # quality state, and the scheme was never specific to a Signal. Only
    # this column is CSV-driven; hasTherapyDeliveryQuality is entailed by an
    # axiom in inference.ttl and never read from a column, but the scheme is
    # ambiguous from the ontology alone either way, so the column says which
    # property it means. The guard below caught this on the first build
    # after the binding was added.
    "Signal.QualityState":        MDA.hasQualityState,
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
    bind_vocab_prefixes(g)

    for notation, column in sorted(missing, key=lambda m: (m[1], m[0])):
        kind, _, cls, scheme = bindings[column]
        ns    = Namespace(COLUMN_SCHEMES[column][1])
        c_iri = ns[notation]
        label = _human_label(notation)
        top   = next(base.subjects(SKOS.topConceptOf, scheme), None)

        g.add((c_iri, RDF.type, SKOS.Concept))
        g.add((c_iri, SKOS.inScheme, scheme))
        # The seed may place a generated concept in its scheme's hierarchy
        # (skos:broader) or link it to another scheme (skos:broadMatch)
        # without defining it — see vocab_base.ttl's Metric Value State
        # section. Without a seed broader, the concept sits under the
        # scheme's top concept.
        seed_broader = set(base.objects(c_iri, SKOS.broader))
        for parent in seed_broader or ({top} if top is not None else set()):
            g.add((c_iri, SKOS.broader, parent))
        for match in base.objects(c_iri, SKOS.broadMatch):
            g.add((c_iri, SKOS.broadMatch, match))
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
    than hardcoded — so a namespace with no base registration is never a
    target for auto-stubbing.

    That skip used to apply to signal-analysis:, on the reasoning that
    mda:SignalAnalysis had no vocabulary scheme of its own. It does now:
    the namespace held 29 concepts that typed nodes in the published graph
    while being declared nowhere, which is the one thing a vocabulary must
    not do. Registering the scheme in vocab_base.ttl is all it took to make
    them stub like everything else — no change was needed here.

    node_class is the class the scheme instantiates
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
    {(functional_unit_concept, metric_concept):
        {(sensor_concept, signal_concept, analysis_concept), ...}},
    derived by walking inference.ttl's FunctionalUnit-recipe reference
    triples forward from each sensor, through any number of derivation
    layers, to every metric that sensor can reach.

    A SET per key, not a single trace. Most pathways yield exactly one and
    behave as before. A COMPOSITE instrument yields several: the 12-lead
    ECG derives metric:HeartRate from nine different electrodes, because
    every augmented and precordial lead references the limb electrodes
    through Wilson's central terminal. recipe_override() asserts a trace
    only where the set is a singleton — see there for why.

    KEYED ON THE PAIR, NOT ON METRIC ALONE. A metric name is not unique
    across functional units: metric:RespirationRate is produced both by
    FU_PneumoTachograph (flow-derived) and by impedance pneumography under
    FU_ElectroCardiography, and metric:VentilationCircuitLeak is annotated
    under both FU_PneumoTachograph and FU_AirwayPressure. The previous
    metric-only key was a dict comprehension over producesMetric, so a
    metric with two traces silently kept whichever parsed last and handed
    it to every alarm naming that metric regardless of the functional unit
    the alarm's own row names. The pair is a complete key: all 17
    (FunctionalUnit, Metric) combinations in the p75 corpus are distinct.

    These triples are deliberately plain data, not OWL restrictions (see
    inference.ttl's "FunctionalUnit recipes" header) — a functional unit's
    full technical menu (which sensors it COULD have) is not the same claim
    as which single trace a given alarm actually evidences.
    build_alarmtype_triples()'s recipe_override() is the consumer: for a
    row naming a FunctionalUnit and a Metric with no Sensor/Signal of its
    own, it looks up the one trace that pair identifies and asserts only
    that — never the functional unit's other, unevidenced sensor(s)/
    metric(s).

    A row naming a Metric with no FunctionalUnit gets no recipe rather than
    a guessed one, and keeps its generic mda:Sensor/mda:Signal nodes: a
    visible gap is preferable to a trace picked by a metric name that two
    different pathways both use.
    """
    recipe: dict = {}
    for fu, sensor in inference.subject_objects(MDA.hasSensor):
        # Walk forward from this electrode/transducer through however many
        # derivation layers the pathway has. A SignalAnalysis may produce a
        # derived signal (mda:producesSignal) instead of, or as well as, a
        # metric — the 12-lead ECG does exactly this, deriving lead traces
        # from raw electrode traces before any metric is computed — so the
        # walk cannot assume the metric sits one analysis away.
        frontier = list(inference.objects(sensor, MDA.sensorProducesSignal))
        seen = set()
        while frontier:
            signal = frontier.pop()
            if signal in seen:
                continue
            seen.add(signal)
            # A step that takes a KIND of signal (signal:ECG_lead) consumes
            # every signal of that kind: follow rdfs:subClassOf up from the
            # signal (hierarchy.ttl section B — so `inference` must be the
            # merged TBox, as main() passes it).
            for analysis in _consumers(inference, signal):
                for metric in inference.objects(analysis, MDA.producesMetric):
                    recipe.setdefault((fu, metric), set()).add((sensor, signal, analysis))
                    _walk_derived(inference, recipe, fu, sensor, signal, metric)
                frontier.extend(inference.objects(analysis, MDA.producesSignal))
    return recipe


def _consumers(inference: Graph, signal):
    """The steps that consume `signal`: those it is analyzedBy, and those
    any of its superclasses (a kind such as signal:ECG_lead) is analyzedBy."""
    seen, frontier, steps = {signal}, [signal], []
    while frontier:
        c = frontier.pop()
        steps.extend(inference.objects(c, MDA.analyzedBy))
        for parent in inference.objects(c, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in seen:
                seen.add(parent); frontier.append(parent)
    return steps


def _walk_derived(inference: Graph, recipe: dict, fu, sensor, signal, metric):
    """
    Continue build_metric_recipe()'s walk from a metric into the analysis
    steps that take it as INPUT (metric mda:analyzedBy step — inference.ttl's
    "Derived metrics" section), recording each derived metric under the same
    functional unit with the trace (sensor, start signal, final step).

    That triple only RESOLVES THE SLOTS (which sensor, which start signal,
    which final step). It is not what the archetype asserts between them:
    build_alarmtype_triples() replaces the direct "start signal analyzedBy
    final step" edge, which would be false (the cardiac-index step consumes
    cardiac output, not the temperature curve), by the full local
    derivation graph from derivation_graph() — every intermediate step and
    metric, including many-to-one steps (end-diastolic volume from stroke
    volume AND ejection fraction). Only inputs reachable from this
    functional unit's own sensors appear: heart rate from the bedside
    monitor and body surface area from entered data do not, so the graph is
    incomplete but every edge in it is true.

    Side effect, harmless: walks from other units reach the same steps
    (FU_ElectroCardiography's heart rate feeds StrokeVolumeAnalysis), giving
    keys such as (FU_ElectroCardiography, StrokeVolume). A key is only used
    when an alarm row names that exact (FunctionalUnit, Metric) pair.
    """
    frontier, seen = [metric], set()
    while frontier:
        m = frontier.pop()
        if m in seen:
            continue
        seen.add(m)
        for step in inference.objects(m, MDA.analyzedBy):
            for derived in inference.objects(step, MDA.producesMetric):
                recipe.setdefault((fu, derived), set()).add((sensor, signal, step))
                frontier.append(derived)


def derivation_graph(tbox: Graph, fu, metric, named_signal=None):
    """
    The local derivation graph of `metric` on functional unit `fu`: every
    sensing and computation step between the unit's own sensors and the
    metric, as (edges, types, root). An edge is (subject_key, property,
    object_key); "FU" is the key of the functional unit itself, types[key]
    is the concept a key's node is typed by, and root is the key of
    `metric` itself. None when the metric cannot be traced from this unit's
    sensors, or not unambiguously.

    `metric` may also be a SIGNAL: a row with no metric that names one (a
    signal-quality alarm) gets the pathway up to that signal the same way —
    an "ECG artefact" annotated on signal:ECG_lead becomes "some ECG
    electrode -> some raw signal -> some lead derivation -> some ECG lead",
    the very lead node a heart-rate alarm on the same monitor analyses, so
    the two share a particular when grounded.

    Built BACKWARDS from the metric, so that only what the alarm's metric
    depends on is written:

      - an input produced by a computation step: recurse into the step;
      - an input produced by a sensor: stop, if the unit has that sensor;
      - every mda:analyzedBy edge into a step is a REQUIRED input, so a
        many-to-one step keeps all its inputs (end-diastolic volume from
        stroke volume and ejection fraction; ECG lead II from the RA and LL
        raw signals), and an input reached through two paths is one node
        (the LL raw signal feeding leads II and III);
      - an input branch that does not reach this unit's sensors is pruned
        (heart rate and pressures from the bedside monitor, body surface
        area from entered data, on the HemoSphere);
      - an input that is a KIND, produced by nothing itself but by each of
        its subclasses (signal:ECG_lead, the input of the heart-rate and ST
        steps), is collapsed per layer: one node per layer of its members'
        chains, typed by what all members share there ("some ECG electrode
        -> some ECG raw signal -> some lead derivation -> some ECG lead").
        The kind is how a step that works on any one of several signals is
        written without modelling the device's choice between them. If the
        row names a concrete member (signal:ECG_lead_II on an ST alarm),
        that member replaces the kind and its own chain is written in full;
      - a metric with several producing steps, one a superclass of the
        others (cardiac output: CardiacOutputAnalysis and its subclass
        ThermodilutionAnalysis), is produced by the GENERAL step, unless
        the row names a signal on a specific step's route. The specific
        steps are known methods; which one the device used is not known;
      - a step with no stated input (CardiacOutputAnalysis: the method,
        and so the input, is unknown) is fed by a blank signal from a blank
        sensor of the unit, typed only mda:Signal and mda:Sensor. The unit
        keeps a sensor, but no particular sensor is claimed.

    A linear pathway (pulse oximetry, a pressure line) comes out as the same
    single chain the class tree gives, so its archetype does not change.
    """
    sensors = set(tbox.objects(fu, MDA.hasSensor))

    def ancestors(c):
        out, frontier = set(), [c]
        while frontier:
            for parent in tbox.objects(frontier.pop(), RDFS.subClassOf):
                if isinstance(parent, URIRef) and parent not in out:
                    out.add(parent); frontier.append(parent)
        return out

    def descendants(c):
        out, frontier = set(), [c]
        while frontier:
            for child in tbox.subjects(RDFS.subClassOf, frontier.pop()):
                if isinstance(child, URIRef) and child not in out:
                    out.add(child); frontier.append(child)
        return out

    def producers(x):
        return ([(MDA.sensorProducesSignal, s) for s in tbox.subjects(MDA.sensorProducesSignal, x)]
                + [(MDA.producesSignal, s) for s in tbox.subjects(MDA.producesSignal, x)]
                + [(MDA.producesMetric, s) for s in tbox.subjects(MDA.producesMetric, x)])

    def shared(concepts):
        concepts = set(concepts)
        if len(concepts) == 1:
            return next(iter(concepts))
        return _shared_supertype(concepts, tbox)

    def up(x, stack):
        """(key, edges, types) for x and everything upstream of it, or None."""
        if x in stack:
            return None
        if named_signal is not None and named_signal != x and x in ancestors(named_signal):
            x = named_signal
        found = []                               # (producer, (key, edges, types))
        for prop, p in producers(x):
            if prop == MDA.sensorProducesSignal:
                if p in sensors:
                    found.append((p, (x, {("FU", MDA.hasSensor, p), (p, prop, x)}, {x: x, p: p})))
                continue
            sub = step_up(p, stack + (x,))
            if sub is not None:
                edges, types = sub
                found.append((p, (x, edges | {(p, prop, x)}, {**types, x: x})))
        if not found and not producers(x):
            members = [m for m in descendants(x) if producers(m)]
            if members:
                return collapse(x, members, stack)
        if len(found) > 1:
            found = choose(found)
        return found[0][1] if len(found) == 1 else None

    def choose(found):
        """Several producers of one concept: the general step, unless the
        row's signal is on a specific step's route. Unrelated producers
        stay ambiguous."""
        if named_signal is not None:
            on_route = [f for f in found if named_signal in f[1][2]]
            if len(on_route) == 1:
                return on_route
        general = [f for f in found
                   if all(g is f or f[0] in ancestors(g[0]) for g in found)]
        return general if len(general) == 1 else found

    def step_up(step, stack):
        edges, types = set(), {step: step}
        if next(tbox.subjects(MDA.analyzedBy, step), None) is None:
            sensor, signal = ("open", step, "sensor"), ("open", step, "signal")
            return ({("FU", MDA.hasSensor, sensor),
                     (sensor, MDA.sensorProducesSignal, signal),
                     (signal, MDA.analyzedBy, step)},
                    {step: step, sensor: MDA.Sensor, signal: MDA.Signal})
        for inp in tbox.subjects(MDA.analyzedBy, step):
            r = up(inp, stack + (step,))
            if r is not None:
                key, e, t = r
                edges |= e | {(key, MDA.analyzedBy, step)}
                types.update(t)
        return (edges, types) if edges else None

    def collapse(kind, members, stack):
        subs = [r for r in (up(m, stack + (kind,)) for m in members) if r is not None]
        if len(subs) <= 1:
            return subs[0] if subs else None
        layer_concepts, layer_props, depths = {}, {}, set()
        for key, edges, types in subs:
            depth, frontier = {key: 0}, [key]
            while frontier:                      # breadth-first, upstream
                nxt = []
                for b in frontier:
                    for a, p, o in edges:
                        if o != b or a == "FU":
                            continue
                        if a not in depth:
                            depth[a] = depth[b] + 1; nxt.append(a)
                        elif depth[a] != depth[b] + 1:
                            return None          # one node at two depths
                        layer_props.setdefault(depth[b], set()).add(p)
                frontier = nxt
            for k, d in depth.items():
                layer_concepts.setdefault(d, set()).add(types[k])
            depths.add(max(depth.values()))
        if len(depths) != 1 or any(len(ps) != 1 for ps in layer_props.values()):
            return None                          # members' chains not parallel
        top = depths.pop()
        edges, types = {("FU", MDA.hasSensor, ("kind", kind, top))}, {}
        for d in range(top + 1):
            t = shared(layer_concepts[d])
            if t is None:
                return None
            types[("kind", kind, d)] = t
            if d < top:
                edges.add((("kind", kind, d + 1), next(iter(layer_props[d])), ("kind", kind, d)))
        return ("kind", kind, 0), edges, types

    r = up(metric, ())
    if r is None:
        return None
    root, edges, types = r
    if not edges:
        return None
    return edges, types, root


def build_sensor_of_signal(inference: Graph) -> dict:
    """
    {(functional_unit_concept, signal_concept): sensor_concept} — the same
    inference.ttl recipe data, keyed one hop shorter.

    recipe_override()'s fallback for a TECHNICAL row that names a Signal but
    no Metric, where build_metric_recipe()'s key cannot apply: e.g.
    "PHILIPSMONITOR - SpO2 zwak signaal" (signal:PPG_waveform, no metric).
    ("Monitor - ECG artefact" was the first such case, then annotated
    signal:ECG_signal; since 2026-11-25 it names signal:ECG_lead and is
    built by derivation_graph() instead.)
    Which sensor produced a given signal on a given functional unit is
    already fully determined by the recipe, so leaving those rows with a
    generic mda:Sensor node was a gap rather than a limitation — this is
    exactly what Nena flagged on "Monitor - ECG artefact" ("sensor: electro
    cardio graphy_sensor?").

    Fills the SENSOR only. The signal is what the row already names, and the
    analysis is deliberately NOT derivable this way: one signal can feed
    several analyses (signal:PPG_waveform is analyzed by SpO2Analysis,
    PulseAnalysis and PulseFlowIndexAnalysis at once), so a row that names no
    metric gives nothing to choose between them, and guessing would assert a
    processing step the alarm does not evidence.
    """
    mapping = {}
    for fu, sensor in inference.subject_objects(MDA.hasSensor):
        for signal in inference.objects(sensor, MDA.sensorProducesSignal):
            mapping[(fu, signal)] = sensor
    return mapping


def _shared_supertype(concepts: set, inference: Graph):
    """
    The most specific class every concept in `concepts` is a subclass of,
    following inference.ttl's own rdfs:subClassOf edges, or None if they
    share nothing above the ontology's top class.

    Used when a metric's trace is not determined down to one sensor or one
    signal. Asserting the ontology class (mda:Sensor) in that position is
    too weak to be worth saying — a pulse oximeter satisfies it — whereas
    the shared supertype (sensor:ElectroCardioGraphy_sensor) is exactly the
    claim the alarm supports.
    """
    def ancestors(c):
        out, frontier = set(), [c]
        while frontier:
            x = frontier.pop()
            for parent in inference.objects(x, RDFS.subClassOf):
                if isinstance(parent, URIRef) and parent not in out:
                    out.add(parent); frontier.append(parent)
        return out
    if not concepts:
        return None
    common = set.intersection(*(ancestors(c) for c in concepts))
    common = {c for c in common if str(c).startswith(VOCAB)}
    if not common:
        return None
    # most specific = the one with the most ancestors of its own
    return max(common, key=lambda c: len(ancestors(c)))


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


# ── Curated hierarchy (seed/hierarchy.ttl) ────────────────────────────────────
#
# Every curated is-a placement between vocabulary concepts lives in one seed
# file, in three sections (see its header): value refinements
# (mda:refinesConcept, entailed through the property chains in
# ontology.ttl), kind subclassing (rdfs:subClassOf) and cross-scheme links
# (skos:broadMatch). This script is the only reader: it merges the edges into
# vocab_generated.ttl, so every consumer gets them by loading that file.

HIERARCHY_PREDICATES = {MDA.refinesConcept, RDFS.subClassOf, SKOS.broadMatch}


def load_hierarchy(path: Path) -> Graph:
    """The seed hierarchy, validated: only the three section predicates, both
    ends /vocab/ concepts, and refinement/subclassing within one scheme
    (crossing schemes is what skos:broadMatch is for)."""
    g = Graph()
    if not path.exists():
        return g
    g.parse(path, format="turtle")
    errors = []
    for s, p, o in g:
        if p not in HIERARCHY_PREDICATES:
            errors.append(f"unexpected predicate {p} on {s}")
            continue
        if not (isinstance(s, URIRef) and isinstance(o, URIRef)
                and str(s).startswith(VOCAB) and str(o).startswith(VOCAB)):
            errors.append(f"not a vocabulary-to-vocabulary edge: {s} {_local(p)} {o}")
            continue
        same_scheme = str(s).rsplit("/", 1)[0] == str(o).rsplit("/", 1)[0]
        if p != SKOS.broadMatch and not same_scheme:
            errors.append(f"{_local(p)} crosses schemes (use skos:broadMatch): {s} -> {o}")
        if p == SKOS.broadMatch and same_scheme:
            errors.append(f"skos:broadMatch within one scheme (use a refinement): {s} -> {o}")
    if errors:
        raise ValueError(f"{path.name} is malformed:\n  " + "\n  ".join(errors))
    return g


def apply_hierarchy(vocab: Graph, hierarchy: Graph, base: Graph) -> int:
    """Merge the seed hierarchy into the generated vocabulary. Per refinement
    or subclass edge also writes skos:broader to the parent and drops the
    child's default skos:broader to its scheme's top concept, so the SKOS tree
    matches. Returns the number of edges merged."""
    tops = set(base.subjects(SKOS.topConceptOf, None))
    n = 0
    for s, p, o in hierarchy:
        vocab.add((s, p, o))
        n += 1
        if p in (MDA.refinesConcept, RDFS.subClassOf):
            for t in list(vocab.objects(s, SKOS.broader)):
                if t in tops:
                    vocab.remove((s, SKOS.broader, t))
            vocab.add((s, SKOS.broader, o))
    return n


def report_unplaced_values(vocab: Graph, hierarchy: Graph, onto: Graph) -> None:
    """List, per value scheme whose property carries a refinesConcept
    chain, the concepts the hierarchy does not place (neither child nor
    parent). Informational: many are legitimately top-level (Enabled,
    Increased); the point is that a NEW annotator term is seen once."""
    chained = set()
    for prop, chain in onto.subject_objects(OWL.propertyChainAxiom):
        if MDA.refinesConcept in set(Collection(onto, chain)):
            chained |= set(onto.objects(prop, MDA.valuesFromScheme))
    placed = set(hierarchy.subjects(MDA.refinesConcept, None)) | \
             set(hierarchy.objects(None, MDA.refinesConcept))
    for scheme in sorted(chained, key=str):
        tops = set(vocab.subjects(SKOS.topConceptOf, scheme))
        loose = sorted(_local(c) for c in vocab.subjects(SKOS.inScheme, scheme)
                       if c not in placed and c not in tops)
        if loose:
            print(f"[hier]   not placed in {_local(str(scheme).rsplit('/', 1)[0])}: {', '.join(loose)}")


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


def check_approximates_axioms(kg: Graph, inference: Graph, vocab: Graph) -> None:
    """
    Two sources state which physiological property a metric approximates,
    deliberately kept apart: the annotated CSV, per alarm (the archetype's
    "[ a metric:X ; mda:approximates P ]" in kg_generated.ttl, built here),
    and inference.ttl's hand-authored clinical axioms, per metric concept
    ("metric:X rdfs:subClassOf [owl:onProperty mda:approximates ;
    owl:hasValue P]", inherited by subclasses). Nothing else keeps them in
    step, so this checks every annotated edge against the axiom for its
    metric's type:

      - an axiom giving a DIFFERENT property: the build stops (one of the
        two sources is wrong; which one is a clinical decision);
      - no axiom for the metric at all: warning (the annotation is kept,
        but a consumer that reads the axioms, such as the POC's CAT2a, does
        not see it);
      - an annotated property on a metric without a type: warning.
    """
    tbox = inference + vocab

    def axiom_properties(concept, seen=()):
        found = set()
        for sup in tbox.objects(concept, RDFS.subClassOf):
            if (sup, OWL.onProperty, MDA.approximates) in tbox:
                found |= set(tbox.objects(sup, OWL.hasValue))
            elif isinstance(sup, URIRef) and sup not in seen:
                found |= axiom_properties(sup, seen + (concept,))
        return found

    conflicts, unbridged, untyped = set(), set(), 0
    for metric, prop in kg.subject_objects(MDA.approximates):
        types = [t for t in kg.objects(metric, RDF.type) if isinstance(t, URIRef)]
        if not types:
            untyped += 1
            continue
        for t in types:
            axioms = axiom_properties(t)
            if not axioms:
                unbridged.add((_local(t), _local(prop)))
            elif axioms != {prop}:
                conflicts.add((_local(t), _local(prop), ", ".join(sorted(_local(a) for a in axioms))))

    for metric, prop in sorted(unbridged):
        print(f"[WARN]   approximates: metric:{metric} is annotated as approximating "
              f"{prop}, but inference.ttl has no approximates axiom for it")
    if untyped:
        print(f"[WARN]   approximates: {untyped} archetype(s) annotate a physiological "
              f"property on a metric without a metric type")
    if conflicts:
        raise ValueError(
            "The annotated CSV and inference.ttl disagree on what a metric approximates:\n"
            + "\n".join(f"  metric:{m}: annotated {p}, inference.ttl {a}" for m, p, a in sorted(conflicts))
            + "\nFix the annotation or the axiom (FRAMEWORK/KNOWLEDGE_BASE/inference.ttl).")
    print(f"[check]  approximates: annotated edges agree with inference.ttl "
          f"({len(unbridged)} metric(s) without an axiom)")


def alarmtype_iri(label: str, priority: str, model: str = "") -> URIRef:
    """
    Stable alarm type IRI from device model + label + priority; the model is
    left out when unknown (gateway names such as Medibus, Philipsmonitor).
    'ServoU' + 'SERVOU - PEEP laag' + 'Medium' → alarmtype:ServoU_ServouPeepLaag_Medium
    """
    return ALARMTYPE[_alarmtype_local(label, priority, model)
                     + _IRI_SUFFIX.get(("type", model.strip(), label.strip(), priority.strip()), "")]


def _alarmtype_local(label: str, priority: str, model: str = "") -> str:
    clean_label    = re.sub(r"[^A-Za-z0-9]+", "", label.title().replace(" ", ""))
    clean_priority = re.sub(r"[^A-Za-z0-9]+", "_", priority).strip("_")
    clean_model    = re.sub(r"[^A-Za-z0-9]+", "", model)
    return f"{clean_model + '_' if clean_model else ''}{clean_label}_{clean_priority}"


def _label_local(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", label.title().replace(" ", ""))


# IRI local names keep only A-Z, a-z and 0-9, so distinct labels can collide
# ("PHILIPSMONITOR - ??? Desaturatie" / "PHILIPSMONITOR - Desaturatie";
# "SERVOU - Patiëntcircuit los" / "SERVOU - Patintcircuit los"). Distinct
# labels are documented distinctly: within a colliding group the most
# complete spelling keeps the plain IRI and the others get _1, _2, ... in a
# fixed order: most letters/digits first (Unicode, so 'ë' counts and '???'
# does not), then the shorter label, then the label itself. Filled once per
# build from the whole CSV by assign_unique_iris(); read by alarmtype_iri()
# and label_concept_iri().
_IRI_SUFFIX: dict = {}


def _completeness(label: str) -> tuple:
    return (-sum(ch.isalnum() for ch in label), len(label), label)


def assign_unique_iris(df: pd.DataFrame) -> list:
    """Suffix map for colliding alarm-type and label-concept IRIs; returns
    the collisions resolved, for the build log."""
    _IRI_SUFFIX.clear()
    resolved = []

    def resolve(kind, keys, local_of, label_of):
        groups = {}
        for k in keys:
            groups.setdefault(local_of(k), set()).add(k)
        for local_name, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            ranked = sorted(members, key=lambda k: _completeness(label_of(k)))
            for n, k in enumerate(ranked):
                if n:
                    _IRI_SUFFIX[(kind,) + k] = f"_{n}"
            resolved.append((kind, local_name, [label_of(k) for k in ranked]))

    def val(row, col):
        v = row.get(col)
        return "" if v is None or pd.isna(v) else str(v).strip()

    types = {(val(r, MODEL_COLUMN), val(r, LABEL_COLUMN), val(r, PRIORITY_COLUMN))
             for _, r in df.iterrows() if val(r, LABEL_COLUMN)}
    resolve("type", types, lambda k: _alarmtype_local(k[1], k[2], k[0]), lambda k: k[1])
    labels = {(k[1],) for k in types}
    resolve("label", labels, lambda k: _label_local(k[0]), lambda k: k[0])
    for kind, local_name, ranked in resolved:
        print(f"[iri]    {kind} IRI {local_name} shared by distinct labels -> "
              + ", ".join(f"{lab!r}{'' if i == 0 else f' (_{i})'}" for i, lab in enumerate(ranked)))
    return resolved


def _norm(s) -> str:
    return " ".join(str(s).split()).casefold() if s is not None and not pd.isna(s) else ""


def blueprint_key(label, priority, model) -> tuple:
    """Normalised identity of a blueprint: (model, label, priority)."""
    return (_norm(model), _norm(label), _norm(priority))


def check_blueprint_identity(df_all: pd.DataFrame) -> None:
    """Every row of the annotation file (EXCL included) is one distinct alarm
    of the selected corpus, and vice versa: no duplicate identities, none
    missing, none extra. Stops the build otherwise."""
    import csv
    with DEVICE_MODELS_PATH.open(encoding="utf-8", newline="") as fh:
        model_of = {r["gateway_name"]: r["model"] for r in csv.DictReader(fh, delimiter=";")}
    with CORPUS_PATH.open(encoding="utf-8-sig", newline="") as fh:
        corpus = list(csv.DictReader(fh, delimiter=";"))
    unknown = sorted({r["Alarm_device"] for r in corpus} - set(model_of))
    if unknown:
        raise ValueError(f"Gateway name(s) missing from {DEVICE_MODELS_PATH.name}: {unknown}")
    expected = {blueprint_key(r["Alarm_label"], r["Alarm_priority"], model_of[r["Alarm_device"]])
                for r in corpus}
    keys = [blueprint_key(r[LABEL_COLUMN], r[PRIORITY_COLUMN], r.get(MODEL_COLUMN))
            for _, r in df_all.iterrows()]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    missing, extra = sorted(expected - set(keys)), sorted(set(keys) - expected)
    if dupes or missing or extra:
        raise ValueError(
            "Blueprint identity (model, label, priority) does not match the selected corpus:\n"
            + "".join(f"  duplicate: {k}\n" for k in dupes)
            + "".join(f"  missing:   {k}\n" for k in missing)
            + "".join(f"  extra:     {k}\n" for k in extra))
    print(f"[check]  blueprint identity: {len(keys)} rows = {len(expected)} distinct alarms "
          f"(model, label, priority) in {CORPUS_PATH.name}")


def check_model_axioms(df: pd.DataFrame, index: dict, inference: Graph) -> list:
    """Rows whose annotated Device category disagrees with inference.ttl's
    device-model axioms ([hasDeviceType hasValue M] subClassOf D). Reported,
    not fatal: with the disjoint device categories such a row makes the
    knowledge base inconsistent until the annotation is revised."""
    category_of = {}
    for r in inference.subjects(OWL.onProperty, MDA.hasDeviceType):
        model = inference.value(r, OWL.hasValue)
        for d in inference.objects(r, RDFS.subClassOf):
            category_of[_local(model)] = _local(d)
    conflicts = []
    for _, row in df.iterrows():
        model, device = str(row.get(MODEL_COLUMN) or "").strip(), str(row.get("Device") or "").strip()
        if model in category_of and device and device != "nan" and device != category_of[model]:
            conflicts.append((model, row[LABEL_COLUMN], device, category_of[model]))
    for model, label, device, expected in conflicts:
        print(f"[WARN]   model axiom: {model} is {expected}, annotated {device}: {label}")
    print(f"[check]  device-model axioms: {len(category_of)} model(s), {len(conflicts)} conflicting row(s)")
    return conflicts


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
    return ALARM_LABEL[_label_local(label) + _IRI_SUFFIX.get(("label", label.strip()), "")]


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
    single stable IRI:  metric:ArterialBloodPressure + phase Mean →
    metric:ArterialBloodPressure_Mean.
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
    Identity leaves, e.g. Metric.hasPhase/hasAggregation or Device.hasManufacturer) into one
    shared, deduplicated universal concept — the mechanism behind e.g.
    metric:ArterialBloodPressure + phase Mean →
    metric:ArterialBloodPressure_Mean, generalised to any node-column
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


def link_precoordinated(vocab: Graph) -> list:
    """
    Place every pre-coordinated concept under the pre-coordinated concepts it
    refines (decided 2026-11-25): a concept pre-coordinated with refinements
    R is a narrower case of the one with any proper subset of R, on the same
    base — metric:RespirationVolume_Minute_Expiratory (aggregation Minute,
    phase Expiratory) under metric:RespirationVolume_Minute (aggregation
    Minute). A refinement by definition: precoordinate() defines each concept
    as owl:equivalentClass (base ⊓ restriction per refinement), so the edge
    is already entailed; this writes it out for consumers that do not reason
    (the MDA-POC follows rdfs:subClassOf* without a reasoner).

    Only DIRECT edges, and only between concepts that exist (no concept is
    minted for an unused subset). Adds rdfs:subClassOf, and replaces the
    concept's skos:broader edge to its base with skos:broader to its
    pre-coordinated parent(s), so SKOS tools show the same tree. The
    rdfs:subClassOf to the base stays (entailed, harmless).

    Returns [(child, parent)] for the build log.
    """
    defs = {}
    for sc, d in vocab.subject_objects(OWL.equivalentClass):
        members_node = vocab.value(d, OWL.intersectionOf)
        if not isinstance(sc, URIRef) or members_node is None:
            continue
        members = list(Collection(vocab, members_node))
        if not members or not isinstance(members[0], URIRef):
            continue
        refs = set()
        for m in members[1:]:
            p, v = vocab.value(m, OWL.onProperty), vocab.value(m, OWL.hasValue)
            if p is None or v is None:
                refs = None
                break
            refs.add((p, v))
        if refs:
            defs[sc] = (members[0], frozenset(refs))

    by_base = {}
    for sc, (base, refs) in defs.items():
        by_base.setdefault(base, []).append((sc, refs))

    edges = []
    for base, group in by_base.items():
        for child, crefs in group:
            above = [(p, prefs) for p, prefs in group if prefs < crefs]
            direct = [p for p, prefs in above
                      if not any(prefs < qrefs for _q, qrefs in above)]
            for parent in sorted(direct, key=str):
                vocab.add((child, RDFS.subClassOf, parent))
                vocab.add((child, SKOS.broader, parent))
                edges.append((child, parent))
            if direct:
                vocab.remove((child, SKOS.broader, base))
    return edges


def build_alarmtype_triples(row: pd.Series, kg: Graph, ref: Graph, index: dict,
                            tree: dict, node_kind: dict, node_columns: dict,
                            leaf_specs: list, target_types: dict,
                            metric_recipe: dict, sensor_of_signal: dict,
                            administers_map: dict, inference_graph: Graph) -> None:
    """
    Emit one CSV row as an mda:AlarmType, routing triples to two graphs:
    per-alarm particulars to `kg`, deduplicable universals to `ref`.
    """
    label  = str(row[LABEL_COLUMN]).strip()
    model  = row.get(MODEL_COLUMN)
    model  = "" if model is None or pd.isna(model) else str(model).strip()
    at_iri = alarmtype_iri(label, str(row.get(PRIORITY_COLUMN, "")).strip(), model)
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
        inference.ttl's recipe says actually produces this row's Metric on
        this row's FunctionalUnit — never a FunctionalUnit's full menu of
        possible sensors/metrics, only the one this alarm evidences (see
        inference.ttl's "FunctionalUnit recipes" header and
        build_metric_recipe()).

        Both columns are required. A row naming a Metric with no
        FunctionalUnit is not resolvable — a metric name alone does not
        identify a pathway (RespirationRate is produced by two of them) —
        so it keeps its generic nodes rather than being handed a guess.

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
        fu_concept = column_concept(MDA.FunctionalUnit)
        if fu_concept is None:
            return None
        metric_concept = column_concept(MDA.Metric)
        if metric_concept is None:
            # No Metric: a technical row may still name a Signal, which
            # together with the FunctionalUnit determines the Sensor (see
            # build_sensor_of_signal). Sensor only — the analysis step stays
            # generic, since one signal can feed several.
            if cls != MDA.Sensor:      # NB: `is not` is wrong here — URIRef
                                       # equality is by value, not identity
                return None
            signal_concept = column_concept(MDA.Signal)
            if signal_concept is not None:
                sensor_c = sensor_of_signal.get((fu_concept, signal_concept))
                if sensor_c is not None:
                    return sensor_c
            # Last resort: the row names no Metric, and no specific sensor is
            # determined — but if every sensor this FunctionalUnit reads from
            # shares a supertype, that much IS known. A row naming the group
            # type signal:ECG_signal (as "Monitor - ECG artefact" did until
            # 2026-11-25) determines no single electrode; "an ECG electrode"
            # still follows. Returns None where the
            # unit's sensors share nothing (FU_InvasiveBloodPressure's arterial
            # and venous transducers), leaving the node generic as before.
            return _shared_supertype(
                set(inference_graph.objects(fu_concept, MDA.hasSensor)),
                inference_graph)
        traces = metric_recipe.get((fu_concept, metric_concept))
        if not traces:
            return None
        # EACH SLOT RESOLVES INDEPENDENTLY. A composite instrument may pin
        # down one level of the pathway while leaving another open: on a
        # 12-lead ECG, heart rate comes from a known analysis over known
        # rhythm leads, but not from a known electrode — every lead is a
        # difference between several. So rather than accept or reject the
        # trace as a whole, take each position on its own:
        #
        #   all traces agree      -> assert that concept
        #   they differ          -> assert their most specific shared
        #                            supertype (sensor:ElectroCardioGraphy_
        #                            sensor rather than the bare mda:Sensor,
        #                            which a pulse oximeter also satisfies)
        #   they share nothing   -> assert nothing, node stays generic
        slot = {MDA.Sensor: 0, MDA.Signal: 1, MDA.SignalAnalysis: 2}[cls]
        candidates = {trace[slot] for trace in traces}
        if len(candidates) == 1:
            return next(iter(candidates))
        return _shared_supertype(candidates, inference_graph)

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
            refs = identity_refinements(cls)
            if refs:
                # Without a concept (e.g. a model but no device category),
                # the node is typed by its class and keeps its refining leaves;
                # inference.ttl's device-model axioms infer the category. Before
                # 2026-11-19 these leaves were dropped silently.
                if concept is not None:
                    precoordinate(ref, concept, refs)
                kg.add((b, RDF.type, concept if concept is not None else cls))
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

        # A Stateful parent (mda:Metric) never gets an outgoing link written
        # to `ref`, even when concept<->concept dedup would otherwise apply
        # (a plain, unrefined metric like HeartRate has no identity leaves,
        # so out[Metric] collapses to the bare concept IRI — see the
        # "stateful" branch above — and PhysiologicalProperty is
        # "referential" by this function's own default for an untagged
        # class, so both sides register as _is_shared() and used to pass).
        # Stateful is documented (ontology.ttl's node-kind section) as "a
        # hinge: its identity is a pre-coordinated universal concept, but
        # each alarm attaches its OWN state" — a per-row leaf like
        # mda:approximates is exactly that per-alarm state, not a universal
        # fact about the concept, and inference.ttl's own class-restriction
        # axiom (metric:HeartRate rdfs:subClassOf [hasValue(approximates,
        # ElectricalHeartRate)]) already derives it, per-instance, at
        # reasoning time — writing it again here statically onto the bare
        # concept is not just redundant (visibly duplicated in op_visual.py's
        # diagrams: the per-alarm particular's own approximates edge AND a
        # second one via rdf:type -> concept -> approximates -> same target)
        # but actively wrong: it makes metric:HeartRate itself satisfy
        # mda:approximates's rdfs:domain mda:Metric (prp-dom), so OWL-RL
        # treats the SHARED CONCEPT as a Metric individual in its own right —
        # confirmed to let the former CardiacArrestCondition/
        # RespiratoryArrestCondition axioms (FRAMEWORK/KNOWLEDGE_BASE/
        # clinicalEvents.ttl, before the 2026-09 rework) misclassify
        # physiologicalProcess:CardiacContraction/PulmonaryVentilation as
        # carrying the since-removed mda:impliesClinicalEvent, not just the
        # triggering alarm's own Metric particular. The general hazard —
        # a shared concept satisfying a per-instance axiom — still applies. Same failure mode build_administers_map()'s docstring
        # above already documents and fixes for mda:administers/
        # hasOperationState/hasTherapyDeliveryQuality — never generalised to
        # mda:approximates/isPropertyOf until now.
        #
        # Routing the edge to kg was not enough: a stateful node's outgoing
        # anchor (out[cls]) is its CONCEPT, so the edge still had the shared
        # concept as its subject ("metric:SpO2 mda:approximates ..." in
        # kg_generated.ttl). It now leaves from the archetype's own node
        # (nodes[cls], the per-alarm blank node): "[ a metric:SpO2 ;
        # mda:approximates physiologicalProperty:SaO2 ]", like every other
        # edge in an archetype. check_approximates_axioms() then checks it
        # against inference.ttl.
        parent_stateful = node_kind.get(parent_cls) == "stateful"
        if parent_stateful:
            kg.add((nodes[parent_cls], prop, child_in))
        else:
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

    # The sensing pathway as a graph, not a chain. The class tree gives one
    # node per class (one Sensor -> Signal -> SignalAnalysis -> Metric), so
    # wherever the pathway is not a single chain it can only state something
    # false: "temperature curve analyzedBy cardiac-index step" (that step
    # consumes cardiac output), or "one ECG electrode produces the very lead
    # heart rate is computed from". derivation_graph() gives the pathway as
    # it is, and replaces the tree's Sensor/Signal/SignalAnalysis nodes
    # below the functional unit. The tree's Metric node stays, so the row's
    # state lands on the alarm's own metric: in an archetype that is the
    # metric node with no outgoing mda:analyzedBy (intermediate metrics,
    # such as cardiac output under a cardiac-index alarm, carry none of the
    # row's state). A linear pathway comes out unchanged.
    #
    # Kept as the tree builds it (with an [INFO] line) when the row names a
    # Sensor or Signal the graph does not contain, or carries Sensor/Signal
    # states and the graph has no single node of that class to put them on.
    #
    # A row with no metric but a Signal of its own (a signal-quality alarm)
    # gets the pathway up to that signal the same way (see derivation_graph).
    def own(cls):
        col = node_columns.get(cls)
        v = cell(col)
        return resolve(_scheme_of_column(col), v, index, col) if v is not None else None
    fu_c, met_c = column_concept(MDA.FunctionalUnit), column_concept(MDA.Metric)
    named = {MDA.Sensor: own(MDA.Sensor), MDA.Signal: own(MDA.Signal)}
    # The signal anchor only where the row has no Metric node at all: a
    # Metric node without a metric type (a row annotating only the
    # physiological property) hangs below the signal and must not be cut off.
    anchor = MDA.Metric if met_c is not None else (None if MDA.Metric in nodes else MDA.Signal)
    goal = met_c if met_c is not None else (named[MDA.Signal] if anchor else None)
    if fu_c is not None and goal is not None and anchor in nodes \
            and MDA.FunctionalUnit in nodes:
        graph = derivation_graph(inference_graph, fu_c, goal, named[MDA.Signal])
        problem, target = None, {}
        if graph is not None:
            edges, types, root = graph
            role = {MDA.Sensor: {o for _, p, o in edges if p == MDA.hasSensor},
                    MDA.Signal: {o for _, p, o in edges
                                 if p in (MDA.sensorProducesSignal, MDA.producesSignal)}}
            def is_a(c, sup):
                seen, frontier = {c}, [c]
                while frontier:
                    for parent in inference_graph.objects(frontier.pop(), RDFS.subClassOf):
                        if parent not in seen:
                            seen.add(parent); frontier.append(parent)
                return sup in seen
            for cls in (MDA.Sensor, MDA.Signal):
                keys = role[cls]
                if named[cls] is not None:
                    keys = {k for k in keys if is_a(types[k], named[cls])}
                    if not keys:
                        problem = f"names {_local(named[cls])}, not on the traced pathway"
                has_state = any(dcls == cls and r != "identity" and cell(col) is not None
                                for dcls, _, col, r, _ in leaf_specs)
                if has_state and not problem:
                    if len(keys) != 1:
                        problem = f"{_local(cls)} state, but {len(keys)} {_local(cls)} nodes"
                    else:
                        target[cls] = next(iter(keys))
            if anchor == MDA.Signal and not problem:
                target[MDA.Signal] = root          # the row's own signal
            tree_nodes = [nodes[c] for c in (MDA.Sensor, MDA.Signal, MDA.SignalAnalysis)
                          if c in nodes]
            structural = {RDF.type, MDA.sensorProducesSignal, MDA.analyzedBy, MDA.producesMetric}
            if not problem and any(p not in structural for n in tree_nodes
                                   for p, _ in kg.predicate_objects(n)):
                problem = "identity refinements on the tree's own Sensor/Signal nodes"
            if problem:
                print(f"[INFO]   pathway kept as a chain ({problem}): {label}")
            else:
                for n in tree_nodes:
                    kg.remove((n, None, None)); kg.remove((None, None, n))
                node_of = {"FU": nodes[MDA.FunctionalUnit]}
                if anchor == MDA.Metric:
                    node_of[root] = nodes[MDA.Metric]
                def node(k):
                    if k not in node_of:
                        node_of[k] = BNode()
                        kg.add((node_of[k], RDF.type, types[k]))
                    return node_of[k]
                for a, p, o in edges:
                    kg.add((node(a), p, node(o)))
                for cls, k in target.items():
                    nodes[cls] = node(k)

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
                 metric_recipe: dict, sensor_of_signal: dict, administers_map: dict,
                 inference_graph: Graph):
    """Return (kg, ref): per-alarm particulars, and deduplicated universals."""
    kg, ref = Graph(), Graph()
    for g in (kg, ref):
        g.bind("alarmtype", ALARMTYPE)
        g.bind("mda", MDA)
        g.bind("skos", SKOS)
        bind_vocab_prefixes(g)

    failed = []
    for i, row in df.iterrows():
        try:
            build_alarmtype_triples(row, kg, ref, index, tree,
                                    node_kind, node_columns, leaf_specs, target_types,
                                    metric_recipe, sensor_of_signal, administers_map,
                                    inference_graph)
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
    # Clinical Event scheme lives in its own hand-authored file (see that
    # file's own header) — parsed into the SAME `base` graph so the
    # notation-duplicate check below sees it as "already registered" too,
    # even though no CSV column ever contests these notations in practice.
    base.parse(CLINICAL_EVENT_VOCAB_PATH, format="turtle")
    inference = Graph()
    inference.parse(INFERENCE_PATH, format="turtle")
    hierarchy = load_hierarchy(HIERARCHY_PATH)
    # The hand-authored TBox the KG expansion consults: inference.ttl's
    # recipes plus the curated subclass edges (the ECG group types that
    # _shared_supertype() resolves an undetermined trace to live in
    # hierarchy.ttl since 2026-11-24).
    tbox = inference + hierarchy

    # 2. Derive the nesting tree from the ontology and show it
    tree = derive_class_tree(onto, ROOT_CLASS)
    for line in format_tree(tree, ROOT_CLASS):
        print(f"[tree]   {line}")

    # 3. Read the alarm type definitions
    df = pd.read_csv(CSV_PATH, sep=";", dtype=str)
    df.columns = df.columns.str.strip()
    if EXCLUSION_COLUMN not in df.columns:
        raise ValueError(f"CSV is missing the review marker column '{EXCLUSION_COLUMN}'.")
    check_blueprint_identity(df)
    status = df[EXCLUSION_COLUMN].fillna("").str.strip()
    excluded = status == EXCLUSION_VALUE
    print(f"[csv]    {excluded.sum()} row(s) marked '{EXCLUSION_VALUE}' (not annotatable) — excluded from readout; "
          f"{(status == REVIEW_VALUE).sum()} row(s) marked '{REVIEW_VALUE}' — built, pending annotation")
    df = df.loc[~excluded].drop(columns=[EXCLUSION_COLUMN])
    assign_unique_iris(df)
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
    # hierarchy.ttl may name a concept nothing else does yet (a parent such as
    # opstate:Unavailable, or a value no row uses yet): stub it the same way,
    # but list it — a typo in a placement must not pass silently.
    hierarchy_only = collect_inference_concepts(hierarchy, base, onto,
                                                already_known | inferred_concepts)
    inferred_stubs = build_inference_concept_stubs(inferred_concepts | hierarchy_only, base, onto)
    for t in inferred_stubs:
        vocab_out.add(t)
    print(f"[vocab]  {len(inferred_concepts)} concept stub(s) from inference.ttl")
    if hierarchy_only:
        print(f"[vocab]  {len(hierarchy_only)} concept stub(s) named only in {HIERARCHY_PATH.name}: "
              + ", ".join(sorted(_local(c) for c in hierarchy_only)))

    # 6. KG expansion — particulars → kg_generated; universals → vocab_generated
    index            = build_notation_index(base, vocab_out)
    metric_recipe    = build_metric_recipe(tbox)   # tbox: kinds (hierarchy.ttl) are followed
    sensor_of_signal = build_sensor_of_signal(inference)
    administers_map  = build_administers_map(inference)
    kg_out, ref_out = build_graphs(df, index, tree, node_kind, node_columns,
                                   leaf_specs, target_types, metric_recipe,
                                   sensor_of_signal, administers_map, tbox)
    for t in ref_out:                     # merge the deduplicated universal graph
        vocab_out.add(t)
    pc_edges = link_precoordinated(vocab_out)
    print(f"[pc]     {len(pc_edges)} pre-coordinated concept(s) placed under a less refined one"
          + "".join(f"\n[pc]       {_local(c)} ⊑ {_local(p)}" for c, p in sorted(pc_edges, key=str)))
    n_hier = apply_hierarchy(vocab_out, hierarchy, base)
    print(f"[hier]   {n_hier} curated edge(s) merged from {HIERARCHY_PATH.name}")
    report_unplaced_values(vocab_out, hierarchy, onto)
    check_approximates_axioms(kg_out, inference, vocab_out)
    check_model_axioms(df, index, inference)

    VOCAB_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    KG_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Canonical blank-node labels (RDFC-1.0): an unchanged framework rebuilds
    # byte-identically, so git diffs and the POC's copy check stay meaningful.
    serialize_canonical(vocab_out, VOCAB_OUT_PATH)
    serialize_canonical(kg_out, KG_OUT_PATH)

    n_types = sum(1 for s in set(kg_out.subjects()) if str(s).startswith(str(ALARMTYPE)))
    n_bnodes = len({x for t in kg_out for x in t if isinstance(x, BNode)})
    print(f"[vocab]  {len(vocab_out)} triples ({len(ref_out)} universal) → "
          f"{VOCAB_OUT_PATH.relative_to(ROOT)}")
    print(f"[kg]     {len(kg_out)} triples, {n_types} alarm type(s), "
          f"{n_bnodes} blank node(s) → {KG_OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
