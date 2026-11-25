"""
framework_stats_table.py

Every number in the manuscript's Section 3.3.1 "Elicitation outcome" paragraph
and its "Framework summary statistics" table, computed from the framework
files and written as one long table for Power Query:

    MANUSCRIPT/table_drafts/framework_stats.csv      Unit;Key;Value;Description

Unit is ADULT, PICU, NICU (per-department corpus figures) or ALL. The workbook
sheet "Table 3" looks every value up by Unit + Key, exactly like Table 4.

The knowledge base is rebuilt in memory with build_framework.py's own
functions (same exclusion filter, same inputs), so the numbers describe what
the pipeline produces now. If FRAMEWORK/KNOWLEDGE_BASE/kg_generated.ttl on
disk differs, `kb_on_disk_current` is 0: run build_framework.py first.

Not computable here, left blank for manual entry in the CSV/sheet:
  oops_critical / oops_important / oops_minor  (OOPS! is a web service)

Usage:  python3 CODE/reporting/framework_stats_table.py [--no-hermit]
"""

import csv
import sys
import tempfile
from pathlib import Path
from statistics import quantiles, median

import pandas as pd
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.append(str(SCRIPT_DIR.parent / "framework_build"))
import build_framework as R  # noqa: E402  (reuse the real builder)

ROOT = R.ROOT
OUT_PATH = ROOT / "MANUSCRIPT" / "table_drafts" / "framework_stats.csv"
TOTAL_CORPUS_PATH = ROOT / "DATA" / "CORPUS" / "Total_corpus.csv"
ANNOTATION_PATH = R.CSV_PATH      # the annotation file build_framework.py builds from
CUTOFF_PCT = int(ANNOTATION_PATH.stem.split("_")[0].lstrip("p"))
CUTOFF_CORPUS_PATH = ROOT / "DATA" / "CORPUS" / f"Annotation_corpus_{CUTOFF_PCT}.csv"

# What an alarm log provides (manuscript 3.3: "label, priority, originating
# device"). The "logs only" graph keeps these annotation columns and blanks
# every other one before running the same builder.
LOG_COLUMNS = {R.LABEL_COLUMN, R.PRIORITY_COLUMN, "Device", "Device.Type", "Device.Manufacturer"}

DEPARTMENTS = {"Adult intensive care": "ADULT",
               "Paediatric intensive care": "PICU",
               "Neonatal intensive care": "NICU"}

SOSA_SSN = ("http://www.w3.org/ns/sosa/", "http://www.w3.org/ns/ssn/")
# Clinical perspective: its concept schemes, and the relations into and within it.
VOCAB = "https://w3id.org/mda/vocab/"
CLINICAL_SCHEMES = tuple(VOCAB + s + "/" for s in
                         ("physiological-property", "physiological-process", "organ", "organ-system",
                          "clinical-event"))
CLINICAL_PROPS = {R.MDA.approximates, R.MDA.targetsProcess, R.MDA.evidencedBy, R.MDA.isPropertyOf,
                  R.MDA.presentIn, R.MDA.organPartOfSystem, R.MDA.processPartOf}


def gateway_models() -> dict:
    """Gateway device name -> device model (empty when unknown)."""
    m = pd.read_csv(R.DEVICE_MODELS_PATH, sep=";", dtype=str).fillna("")
    return dict(zip(m.gateway_name, m.model))


def norm(s) -> str:
    return " ".join(str(s).split()).casefold()


def iqr(values):
    q1, _, q3 = quantiles(values, n=4, method="inclusive")
    return median(values), q1, q3


# ── Corpus and elicitation (per department) ─────────────────────────────────

def corpus_rows():
    total = pd.read_csv(TOTAL_CORPUS_PATH, sep=";", encoding="utf-8-sig")
    cut = pd.read_csv(CUTOFF_CORPUS_PATH, sep=";", encoding="utf-8-sig")
    rows = []
    groups = [(DEPARTMENTS[d], total[total.Alarm_department == d], cut[cut.Alarm_department == d])
              for d in DEPARTMENTS]
    groups.append(("ALL", total, cut))
    # Distinct alarm = label x priority x device type. Within a unit each row of
    # the corpus files is one; the total counts an alarm raised in several
    # units once, so it is not the sum of the units.
    distinct = lambda df: len({(norm(l), norm(p), norm(d)) for l, p, d in
                               zip(df.Alarm_label, df.Alarm_priority, df.Alarm_device)})
    # Not annotatable: rows marked EXCL in the annotation file (label conveys
    # no interpretable alarm condition: a raw device code, or a metric without
    # direction). They get no blueprint, so their volume is not covered.
    ann = pd.read_csv(ANNOTATION_PATH, sep=";", dtype=str)
    ann.columns = ann.columns.str.strip()
    excl = {R.blueprint_key(l, p, m) for l, p, m, e in
            zip(ann[R.LABEL_COLUMN], ann[R.PRIORITY_COLUMN], ann[R.MODEL_COLUMN], ann[R.EXCLUSION_COLUMN])
            if str(e).strip() == R.EXCLUSION_VALUE}
    model_of = gateway_models()
    for unit, t, c in groups:
        n_t, n_c = distinct(t), distinct(c)
        is_excl = [R.blueprint_key(l, p, model_of[d]) in excl
                   for l, p, d in zip(c.Alarm_label, c.Alarm_priority, c.Alarm_device)]
        c_x, c_ok = c[is_excl], c[[not x for x in is_excl]]
        rows += [
            (unit, "not_annotatable_n", distinct(c_x), "Distinct alarms not annotatable (no interpretable alarm condition)"),
            (unit, "not_annotatable_volume_n", int(c_x.Quant_count.sum()), "Alarms of the not-annotatable distinct alarms"),
            (unit, "blueprint_volume_n", int(c_ok.Quant_count.sum()), "Alarms covered by a blueprint"),
            (unit, "blueprint_volume_pct", round(c_ok.Quant_count.sum() / t.Quant_count.sum() * 100, 1), "Covered by a blueprint, % of alarm volume (0-100)"),
        ]
        rows += [
            (unit, "corpus_alarms_n", int(t.Quant_count.sum()), "Alarms in corpus (volume)"),
            (unit, "corpus_distinct_n", n_t, "Distinct alarms in corpus (label x priority x device; total counts each once across units)"),
            (unit, "selected_distinct_n", n_c, f"Distinct alarms selected for annotation ({CUTOFF_PCT}% cutoff per stratum)"),
            (unit, "selected_distinct_pct", round(n_c / n_t * 100, 1), "Selected, % of distinct alarms (0-100)"),
            (unit, "selected_volume_n", int(c.Quant_count.sum()), "Alarms covered by the selected distinct alarms"),
            (unit, "selected_volume_pct", round(c.Quant_count.sum() / t.Quant_count.sum() * 100, 1), "Covered, % of alarm volume (0-100)"),
            (unit, "corpus_devices_n", t.Alarm_device.nunique(), "Device types in corpus"),
            (unit, "selected_devices_n", c.Alarm_device.nunique(), "Device types among selected alarms"),
        ]
    return rows


# ── Knowledge base, replayed with build_framework.py's own steps ────────────

def builder_context(onto, base, inference, df):
    # The TBox the builder consults, as build_framework.main() assembles it:
    # inference.ttl plus the curated kinds and subclass edges of hierarchy.ttl.
    tbox = inference + R.load_hierarchy(R.HIERARCHY_PATH)
    """build_framework.main() steps 2-6, without writing files."""
    tree = R.derive_class_tree(onto, R.ROOT_CLASS)
    columns = [c for c in df.columns if c in R.COLUMN_SCHEMES]
    bindings = R.derive_bindings(onto, columns)
    stateful = set(onto.subjects(R.MDA.nodeKind, R.MDA.Stateful))
    node_columns, node_kind, leaf_specs = {}, {}, []
    for col in columns:
        kind, prop, cls, scheme = bindings[col]
        if kind == "node":
            node_columns[cls] = col
            marker = next((o for o in onto.objects(cls, R.MDA.nodeKind) if o in R.KIND_OF), None)
            node_kind[cls] = R.KIND_OF.get(marker, "referential")
        else:
            if any(str(o).lower() == "true" for o in onto.objects(prop, R.MDA.refinesUniversalIdentity)):
                role = "identity"
            elif cls in stateful:
                role = "state"
            else:
                role = "plain"
            leaf_specs.append((cls, prop, col, role, scheme))
    target_types = {}
    for prop, extra in onto.subject_objects(R.MDA.targetType):
        target_types.setdefault(prop, []).append(extra)

    R.assign_unique_iris(df)          # as build_framework.main(): distinct labels, distinct IRIs
    index = R.build_notation_index(base)
    vocab_out = R.build_vocab_graph(R.collect_missing(df, columns, bindings, index), bindings, base)
    labels = {str(v).strip() for v in df[R.LABEL_COLUMN].dropna() if str(v).strip()}
    for t in R.build_label_vocab(labels, base):
        vocab_out.add(t)
    known = set(vocab_out.subjects(RDF.type, SKOS.Concept))
    for t in R.build_inference_concept_stubs(
            R.collect_inference_concepts(inference, base, onto, known), base, onto):
        vocab_out.add(t)

    args = dict(index=R.build_notation_index(base, vocab_out), tree=tree, node_kind=node_kind,
                node_columns=node_columns, leaf_specs=leaf_specs, target_types=target_types,
                metric_recipe=R.build_metric_recipe(tbox),
                sensor_of_signal=R.build_sensor_of_signal(inference),
                administers_map=R.build_administers_map(inference),
                inference_graph=tbox,
                # the same site refinement as the build (inference.ttl)
                site_table=R.SiteTable(inference, onto, tbox))
    return args, vocab_out


def triples_per_row(df, args):
    counts = []
    for _, row in df.iterrows():
        kg, ref = Graph(), Graph()
        R.build_alarmtype_triples(row, kg, ref, **args)
        counts.append(len(kg))
    return counts


def _terms(g: Graph, node, seen=None) -> set:
    """Every IRI mentioned in a node's class expression (through blank nodes)."""
    seen = seen if seen is not None else set()
    if not isinstance(node, BNode):
        return {node}
    if node in seen:
        return set()
    seen.add(node)
    out = set()
    for p, o in g.predicate_objects(node):
        out.add(p)
        out |= _terms(g, o, seen)
    return out


def axiom_counts(graphs) -> dict:
    """Logical axioms in the knowledge base's axiom files, split by perspective.

    An axiom is (a) a statement with a named subject (annotations, labels and
    rdf:type excluded), (b) a top-level class expression with a blank-node
    subject that is subsumed by or equivalent to another (GCI), or (c) an
    owl:AllDisjointClasses. It is CLINICAL when it mentions a concept of the
    clinical perspective or a relation into it; otherwise TECHNICAL.
    Taxonomy (named class rdfs:subClassOf named class) is counted as a subset.
    """
    counts = dict(technical=0, clinical=0, taxonomy=0)
    for g in graphs:
        objects = set(g.objects())
        axioms = []
        for s, p, o in g:
            if p in (RDFS.label, RDFS.comment) or str(p).startswith(str(SKOS)):
                continue
            if not isinstance(s, BNode) and p != RDF.type:
                axioms.append(((s, p, o), _terms(g, o) | {s, p}))
                if p == RDFS.subClassOf and not isinstance(o, BNode):
                    counts["taxonomy"] += 1
            elif isinstance(s, BNode) and s not in objects and p in (RDFS.subClassOf, OWL.equivalentClass):
                axioms.append(((s, p, o), _terms(g, s) | _terms(g, o)))
            elif p == RDF.type and o == OWL.AllDisjointClasses:
                axioms.append(((s, p, o), _terms(g, s)))
        for _, terms in axioms:
            clinical = any(t in CLINICAL_PROPS or str(t).startswith(CLINICAL_SCHEMES) for t in terms)
            counts["clinical" if clinical else "technical"] += 1
    return counts


def hermit(graphs):
    """HermiT (bundled with owlready2) over the union of the given graphs.
    Returns (consistent 1/0, n unsatisfiable named classes)."""
    from owlready2 import World, sync_reasoner, OwlReadyInconsistentOntologyError
    union = Graph()
    for g in graphs:
        for t in g:
            union.add(t)
    with tempfile.NamedTemporaryFile(suffix=".nt", delete=False) as fh:
        union.serialize(destination=fh.name, format="nt")
        path = fh.name
    world = World()
    onto = world.get_ontology("file://" + path).load(format="ntriples")
    try:
        with onto:
            sync_reasoner(world, infer_property_values=False, debug=0)
    except OwlReadyInconsistentOntologyError:
        return 0, ""
    unsat = [c for c in world.inconsistent_classes() if c is not OWL.Nothing]
    return 1, len(unsat)


def framework_rows(run_hermit: bool):
    onto = Graph().parse(R.ONTOLOGY_PATH, format="turtle")
    base = Graph().parse(R.VOCAB_BASE_PATH, format="turtle")
    base.parse(R.CLINICAL_EVENT_VOCAB_PATH, format="turtle")
    inference = Graph().parse(R.INFERENCE_PATH, format="turtle")

    df = pd.read_csv(ANNOTATION_PATH, sep=";", dtype=str)
    df.columns = df.columns.str.strip()
    n_annotated = len(df)
    df_all = df
    status = df[R.EXCLUSION_COLUMN].fillna("").str.strip()
    excluded = status == R.EXCLUSION_VALUE
    kept_review = list(status[~excluded] == R.REVIEW_VALUE)
    df = df.loc[~excluded].drop(columns=[R.EXCLUSION_COLUMN])

    args, vocab_out = builder_context(onto, base, inference, df)

    df_logs = df.copy()
    for col in df_logs.columns:
        if col not in LOG_COLUMNS:
            df_logs[col] = pd.NA
    before = triples_per_row(df_logs, args)
    after = triples_per_row(df, args)

    # Per unit: blueprints (model x label x priority) whose alarms occur in
    # that unit's selected corpus. A blueprint raised in several units counts
    # in each, and once in ALL.
    cut = pd.read_csv(CUTOFF_CORPUS_PATH, sep=";", encoding="utf-8-sig")
    model_of = gateway_models()
    units_of = {}
    for l, p, dev, d in zip(cut.Alarm_label, cut.Alarm_priority, cut.Alarm_device, cut.Alarm_department):
        units_of.setdefault(R.blueprint_key(l, p, model_of[dev]), set()).add(DEPARTMENTS[d])
    key = lambda row: R.blueprint_key(row[R.LABEL_COLUMN], row[R.PRIORITY_COLUMN], row.get(R.MODEL_COLUMN))
    excl_keys = [key(r) for _, r in df_all.loc[excluded].iterrows()]
    kept_keys = [key(r) for _, r in df.iterrows()]
    unit_rows = []
    for unit in list(DEPARTMENTS.values()) + ["ALL"]:
        inside = lambda k: unit == "ALL" or unit in units_of.get(k, ())
        idx = [i for i, k in enumerate(kept_keys) if inside(k)]
        n_excl = sum(1 for k in excl_keys if inside(k))
        b = iqr([before[i] for i in idx])
        a = iqr([after[i] for i in idx])
        unit_rows += [
            (unit, "annotated_types_n", len(idx) + n_excl, "Distinct alarms in the annotation file (model x label x priority)"),
            (unit, "excluded_types_n", n_excl, "Not annotatable (EXCL)"),
            (unit, "blueprints_n", len(idx), "Alarm blueprints in the knowledge base"),
            (unit, "review_pending_n", sum(1 for i in idx if kept_review[i]), "Blueprints built from a copied annotation, pending review (REVIEW)"),
            (unit, "triples_logs_median", b[0], "Triples per alarm type, logs only, median"),
            (unit, "triples_logs_q1", b[1], "Triples per alarm type, logs only, Q1"),
            (unit, "triples_logs_q3", b[2], "Triples per alarm type, logs only, Q3"),
            (unit, "triples_enriched_median", a[0], "Triples per alarm type, enriched, median"),
            (unit, "triples_enriched_q1", a[1], "Triples per alarm type, enriched, Q1"),
            (unit, "triples_enriched_q3", a[2], "Triples per alarm type, enriched, Q3"),
        ]
        if unit != "ALL":
            only = sum(1 for k in kept_keys if units_of.get(k) == {unit})
            unit_rows.append((unit, "blueprints_unit_only_n", only, "Blueprints whose alarms occur only in this unit"))
    assert len(kept_keys) + len(excl_keys) == n_annotated
    assert all(k in units_of for k in kept_keys + excl_keys), "annotated type missing from the selected corpus"

    kg, ref = R.build_graphs(df, **args)
    assert len(kg) == sum(after), "combined build disagrees with the per-row build"
    for t in ref:
        vocab_out.add(t)
    vocab_full = Graph()
    for g in (base, vocab_out):
        for t in g:
            vocab_full.add(t)

    n_blueprints = sum(1 for s in set(kg.subjects()) if str(s).startswith(str(R.ALARMTYPE)))
    kg_disk = Graph().parse(R.KG_OUT_PATH, format="turtle") if R.KG_OUT_PATH.exists() else Graph()
    on_disk_current = int(len(kg_disk) == len(kg))

    classes = set(onto.subjects(RDF.type, OWL.Class))
    obj_props = set(onto.subjects(RDF.type, OWL.ObjectProperty))
    dt_props = set(onto.subjects(RDF.type, OWL.DatatypeProperty))
    ann_props = set(onto.subjects(RDF.type, OWL.AnnotationProperty))
    sosa = sum(1 for s, p, o in onto
               if p in (RDFS.subClassOf, RDFS.subPropertyOf, OWL.equivalentClass, OWL.equivalentProperty)
               and isinstance(o, URIRef) and str(o).startswith(SOSA_SSN))
    concepts = set(vocab_full.subjects(RDF.type, SKOS.Concept))
    schemes = set(vocab_full.subjects(RDF.type, SKOS.ConceptScheme))
    defined = {s for s in vocab_full.subjects(OWL.equivalentClass, None)} | \
              {s for s in inference.subjects(OWL.equivalentClass, None)}
    clinical_events = Graph().parse(R.CLINICAL_EVENTS_PATH, format="turtle")
    ax_inf = axiom_counts([inference])
    ax_ce = axiom_counts([clinical_events])
    ax = {k: ax_inf[k] + ax_ce[k] for k in ax_inf}

    assert n_blueprints == len(kept_keys), "blueprints in the KB differ from the kept alarm types"
    rows = unit_rows + [
        ("ALL", "kb_triples_n", len(kg), "Knowledge base triples (all blueprints)"),
        ("ALL", "classes_n", len(classes), "Ontology classes (owl:Class)"),
        ("ALL", "object_props_n", len(obj_props), "Object properties"),
        ("ALL", "datatype_props_n", len(dt_props), "Datatype properties"),
        ("ALL", "annotation_props_n", len(ann_props), "Annotation properties"),
        ("ALL", "sosa_alignments_n", sosa, "Alignment axioms to SOSA/SSN"),
        ("ALL", "concepts_n", len(concepts), "Vocabulary concepts (skos:Concept)"),
        ("ALL", "schemes_n", len(schemes), "Concept schemes"),
        ("ALL", "defined_concepts_n", len(defined), "Concepts with an owl:equivalentClass definition"),
        ("ALL", "axioms_technical_n", ax["technical"], "Technical axioms (no clinical-perspective concept or relation)"),
        ("ALL", "axioms_clinical_n", ax["clinical"], "Clinical axioms (mention a clinical-perspective concept or relation)"),
        ("ALL", "axioms_n", ax["technical"] + ax["clinical"], "Axioms, total"),
        ("ALL", "axioms_taxonomy_n", ax["taxonomy"], "Of which taxonomy (named class rdfs:subClassOf named class)"),
        ("ALL", "axioms_clinical_events_n", ax_ce["technical"] + ax_ce["clinical"], "Of which clinical-event definitions (clinicalEvents.ttl)"),
        ("ALL", "kb_on_disk_current", on_disk_current, "1 if kg_generated.ttl on disk equals this rebuild"),
    ]

    if run_hermit:
        try:
            consistent, unsat = hermit([onto, vocab_full, inference, kg])
        except Exception as e:  # reasoner unavailable: leave for manual entry
            print(f"[WARN] HermiT not run: {e}")
            consistent, unsat = "", ""
    else:
        consistent, unsat = "", ""
    rows += [
        ("ALL", "hermit_consistent", consistent, "HermiT: 1 = jointly consistent (onto+vocab+axioms+KB)"),
        ("ALL", "hermit_unsat_n", unsat, "HermiT: unsatisfiable classes"),
        ("ALL", "oops_critical_n", "", "OOPS! critical pitfalls (manual entry)"),
        ("ALL", "oops_important_n", "", "OOPS! important pitfalls (manual entry)"),
        ("ALL", "oops_minor_n", "", "OOPS! minor pitfalls (manual entry)"),
    ]
    return rows


def check_identity(rows):
    """Distinct alarms annotated = not annotatable + blueprints, per unit.
    True by construction; stops the script if it is not."""
    v = {(u, k): val for u, k, val, _ in rows}
    for unit in list(DEPARTMENTS.values()) + ["ALL"]:
        sel, na, bp = v[(unit, "selected_distinct_n")], v[(unit, "not_annotatable_n")], v[(unit, "blueprints_n")]
        if sel != na + bp:
            raise SystemExit(f"{unit}: distinct alarms annotated {sel} != not annotatable {na} + blueprints {bp}")
        print(f"[check]  {unit:5} {sel} = {na} + {bp}")


def main():
    rows = corpus_rows() + framework_rows(run_hermit="--no-hermit" not in sys.argv)
    check_identity(rows)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Unit", "Key", "Value", "Description"])
        w.writerows(rows)
    for r in rows:
        print(f"{r[0]:6} {r[1]:26} {r[2]!s:>12}  {r[3]}")
    print(f"\nWrote {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
