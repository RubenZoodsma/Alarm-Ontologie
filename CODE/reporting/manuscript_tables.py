"""
manuscript_tables.py

Every number of the manuscript's Table 3 ("Framework summary statistics",
Section 3.3.1 and its "Elicitation outcome" paragraph) and Table 4
("Expressibility and occurrence per rule", Section 4.3.3), as ONE long table:

    MANUSCRIPT/table_drafts/manuscript_tables.csv
        Table;Unit;Key;Value;Description

Table is "Table 3" or "Table 4". Unit is ADULT, PICU, NICU or ALL. The
workbook MANUSCRIPT/table_drafts/Table4_occurrence.xlsx loads this file with
Power Query (CODE/reporting/manuscript_tables.pq): one query per table,
filtered on Table, into the sheets Data_T3 and Data, where the formatted
sheets "Table 3" and "Table 4" look every value up by Unit + Key. Refresh in
Excel (Data > Refresh All) after running this script; nothing writes into
the workbook.

Table 3 is computed from the framework files: the knowledge base is rebuilt
in memory with build_framework.py's own functions (same exclusion filter,
same site refinement), so the numbers describe what the pipeline produces
now; kb_on_disk_current is 0 when kg_generated.ttl on disk differs (run
build_framework.py first).

Table 4 is computed from one MDA-POC run: the manifest MDA-POC/_scratch/
run.json (written by poc_main.py) and MDA-POC/validation/results/
rule_coverage_summary.csv. Definitions are in the "Table 4" section below.

Values that cannot be computed (OOPS! pitfalls: a web service) come from
MANUSCRIPT/table_drafts/manuscript_tables_manual.csv
(Table;Unit;Key;Value) when it exists; enter them there, not in the
workbook, where a refresh would overwrite them.

Usage:
  python3 CODE/reporting/manuscript_tables.py             both tables
  python3 CODE/reporting/manuscript_tables.py --table 3   Table 3 only (Table 4 rows kept from the existing CSV)
  python3 CODE/reporting/manuscript_tables.py --table 4   Table 4 only (Table 3 rows kept)
  --no-hermit   skip the HermiT consistency check (Table 3; needs Java)

Replaces framework_stats_table.py, table4_stats.py, build_table3_sheet.py and
build_table4_data.py (2026-11-25; in old/CODE/reporting/).
"""

import argparse
import csv
import importlib
import json
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import quantiles, median

import pandas as pd
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

SCRIPT_DIR = Path(__file__).resolve().parent
_REPO = SCRIPT_DIR.parents[1]


def _isolated_import(names: list, first_dir: Path) -> list:
    """Import `names` with `first_dir` first on sys.path and no module named
    ontology_tree cached. build_framework.py (CODE/shared/ontology_tree.py)
    and the MDA-POC engine (MDA-POC/engine/ontology_tree.py) each import a
    module of that name, with different functions; Python caches one per
    name. Each side is imported against its own copy, and whatever was
    cached before is put back (both sides have bound their names by then),
    so this works whatever the caller imported first."""
    before = sys.modules.pop("ontology_tree", None)
    sys.path.insert(0, str(first_dir))
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        sys.path.remove(str(first_dir))
        if before is not None:
            sys.modules["ontology_tree"] = before
        else:
            sys.modules.pop("ontology_tree", None)


sys.path.append(str(_REPO / "CODE" / "framework_build"))
R, = _isolated_import(["build_framework"], _REPO / "CODE" / "shared")   # the real builder
M, S = _isolated_import(["mint", "stream"], _REPO / "MDA-POC" / "engine")

OUT_PATH = R.ROOT / "MANUSCRIPT" / "table_drafts" / "manuscript_tables.csv"
MANUAL_PATH = R.ROOT / "MANUSCRIPT" / "table_drafts" / "manuscript_tables_manual.csv"
POC = R.ROOT / "MDA-POC"


# ═════════════════════════════════════════════════════════════════════════
# Table 3 — framework summary statistics (was framework_stats_table.py)
# ═════════════════════════════════════════════════════════════════════════

ROOT = R.ROOT
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


def norm3(s) -> str:
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
    distinct = lambda df: len({(norm3(l), norm3(p), norm3(d)) for l, p, d in
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


# ═════════════════════════════════════════════════════════════════════════
# Table 4 — expressibility and occurrence per rule (was table4_stats.py)
# ═════════════════════════════════════════════════════════════════════════
# Every number of Table 4 ("Expressibility and occurrence per rule", manuscript
# 4.3.3) from one MDA-POC run. Unit is NICU, PICU (from the events file's
# `department` column) or ALL; the keys are those the workbook's "Table 4"
# sheet looks up in its Data sheet by Unit + Key.
#
# Inputs: the run manifest MDA-POC/_scratch/run.json (written by poc_main.py:
# dataset, patients, logs) and validation/results/rule_coverage_summary.csv
# (label combinations).
#
# Definitions
# -----------
# Replayed alarm   an alarm with a blueprint (device model + label + priority).
#                  Alarms without one are ignored throughout, as in the POC;
#                  they appear only in "without a blueprint".
# Unit             each alarm counts in its own department (the events file's
#                  `department` column), as in Table 3 and Total_corpus.csv. A
#                  patient with alarms in both units is a case in both (ALL
#                  counts it once); a clinical event goes to the unit of its
#                  first supporting alarm.
# Non-empty state  at arrival, another replayed alarm of the patient arrived
#                  earlier and ended less than the post-alarm window ago (or is
#                  still active): exactly what the engine's patient state holds.
#                  The state is per patient, across units; the alarm counts in
#                  its own unit.
# Silenced by rule the alarm is the subject of a firing of that rule (1a, 1b,
#                  2a, 2b) at any time; lifted = its silence lifted (2a, 2b) or
#                  its flag withdrawn (1b) before the alarm ended.
# Removed          silenced, and still silenced when it ended: a 1a flag, a 1b
#                  flag not withdrawn, or a 2a/2b silence not lifted before its end.
# Alarm rate       per patient: replayed alarms / clock hours with at least one
#                  active replayed alarm (denominator from the complete stream,
#                  before silencing); "with silences" leaves out removed alarms.
#                  Per patient-unit stay (a patient's alarms in one unit, hours
#                  from those alarms); median [IQR] over the unit's stays (ALL:
#                  per patient, all its alarms).
# Filter           a 5-second delay: low or medium priority alarms shorter than
#                  5 s are suppressed; high and unknown priority never.
# Processing time  per arriving alarm (engine/execution.py, wall clock),
#                  median [IQR]. The timing log is per patient, not per alarm:
#                  a patient's timings go to the unit of most of its alarms.
#
# Memory: computed one patient at a time (chunks of CHUNK_SIZE patients, split
# from the dataset in one streaming pass); the full run fits in a few GB.
# """

MANIFEST = POC / "_scratch" / "run.json"
COVERAGE = POC / "validation" / "results" / "rule_coverage_summary.csv"

FILTER_SECONDS = 5
FILTER_PRIORITIES = {"laag", "medium"}
SILENCE_RULES = {"cat1a": "1a", "cat1b": "1b", "cat2a": "2a", "cat2b": "2b"}
EVENT_RULES = {"CardioRespiratoryArrest": "3a", "VentilationFailure": "3b"}


def norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def pct(n, d):
    return round(100 * n / d, 1) if d else ""


def quartiles(values: list):
    """Median, Q1, Q3 (linear interpolation); empty when no values."""
    if not values:
        return "", "", ""
    s = pd.Series(values, dtype=float)
    return round(s.median(), 2), round(s.quantile(0.25), 2), round(s.quantile(0.75), 2)


def weighted_quartiles(hist: Counter):
    """Median, Q1, Q3 of a histogram {value: count} (nearest rank)."""
    total = sum(hist.values())
    if not total:
        return "", "", ""
    out = []
    for q in (0.5, 0.25, 0.75):
        rank, seen = max(1, -(-q * total // 1)), 0
        for v in sorted(hist):
            seen += hist[v]
            if seen >= rank:
                out.append(v)
                break
    return tuple(out)


def parse_window(s: str) -> timedelta:
    h, m, sec = (float(x) for x in str(s).split(":"))
    return timedelta(hours=h, minutes=m, seconds=sec)


CHUNK_SIZE = 100            # patients per pass; memory holds one chunk
WORK_DIR = ROOT / "CODE" / "reporting" / "_scratch_table4"


def alarm_id(e) -> str:
    return str(M.alarm_iri(e)).rsplit("/", 1)[-1]


def run_device_models(run: dict, current: dict) -> dict:
    """The gateway -> device model mapping the run used: device_models.csv at
    the run's commit (run.json), so blueprints are matched as the engine
    matched them, even if the mapping changed after the run."""
    import subprocess
    commit = (run.get("git") or {}).get("commit")
    rel = M.DEVICE_MODELS.resolve().relative_to(ROOT).as_posix()
    if not commit:
        print("WARNING: run.json has no commit; using the current device_models.csv.")
        return current
    text = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{rel}"],
                          capture_output=True, text=True, check=True).stdout
    used = {r["gateway_name"]: r["model"] for r in csv.DictReader(text.splitlines(), delimiter=";")}
    if used != current:
        changed = sorted(k for k in used.keys() | current.keys() if used.get(k) != current.get(k))
        print(f"NOTE: device_models.csv changed since the run ({commit[:7]}): {changed}; "
              f"using the run's mapping.")
    return used


def read_chunk(path: Path) -> dict:
    """{patient: [(Event, department), ...]} in input order (the stream's
    order at one instant)."""
    out = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        col = {name: i for i, name in enumerate(next(reader))}
        get = lambda row, name: row[col[name]] if name in col else ""  # noqa: E731
        for row in reader:
            if not row:
                continue
            e = S.Event(get(row, "patientID"), get(row, "label"), get(row, "device_id"),
                        datetime.fromisoformat(get(row, "start")), datetime.fromisoformat(get(row, "end")),
                        get(row, "alarm_id"), get(row, "device_name"), get(row, "priority"))
            out[e.patient].append((e, get(row, "department")))
    return out


def table4_rows(manifest: Path = None, coverage: Path = None) -> list:
    """Every Table 4 number, computed one patient at a time: the dataset is
    split into chunks of CHUNK_SIZE patients in one streaming pass, and each
    patient's counts are added to its unit's totals. Memory holds one chunk
    and the firing log (indexed by alarm), never the whole run."""
    run = json.loads((manifest or MANIFEST).read_text())
    if not run.get("complete", False):
        print("WARNING: the run manifest says the run was incomplete; numbers are partial.")
    for key in ("firing_log", "event_log", "arrival_times"):
        p = Path(run[key])
        if not p.exists() or p.stat().st_size == 0:
            raise SystemExit(f"{p.name} is empty or missing: the run has not written its logs yet "
                             f"(complete: {run.get('complete')}). Wait for poc_main.py to finish.")
    kb = M.load_kb()
    kb.model_of = run_device_models(run, kb.model_of)
    window = parse_window(run["post_alarm_validity"])
    dataset = Path(run["dataset"])
    patients = list(run["patients"])
    if "department" not in pd.read_csv(dataset, sep=";", dtype=str, nrows=0).columns:
        raise SystemExit(f"{dataset.name} has no 'department' column: re-export with export_rdata.R")

    # firings: subject alarm -> rules; lifts and withdrawals with their time
    rules_of = defaultdict(set)
    lifted_at, withdrawn_at = {}, {}
    with Path(run["firing_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            subject = r["alarm_ids"].split()[0] if r["alarm_ids"] else None
            if subject is None:
                continue
            t = pd.Timestamp(r["time"]).to_pydatetime()
            if r["rule"] in SILENCE_RULES:
                rules_of[subject].add(SILENCE_RULES[r["rule"]])
            elif r["rule"] == "cat2_lifted":
                lifted_at[subject] = min(t, lifted_at.get(subject, t))
            elif r["rule"] == "cat1b_withdrawn":
                withdrawn_at[subject] = min(t, withdrawn_at.get(subject, t))

    # clinical events: the supporting alarm whose department places the event
    event_alarm = {}                      # first supporting alarm id -> [kind, ...]
    with Path(run["event_log"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["kind"] in EVENT_RULES and r["alarm_ids"]:
                event_alarm.setdefault(r["alarm_ids"].split()[0], []).append((r["patient"], r["kind"]))
    ev_n, ev_cases = defaultdict(Counter), defaultdict(lambda: defaultdict(set))

    # per unit: counts, per-stay rates, the patients with each kind of hit
    tot = defaultdict(Counter)            # unit -> key -> n
    cases = defaultdict(lambda: defaultdict(set))   # unit -> key -> {patient}
    rates = defaultdict(lambda: ([], []))           # unit -> (without, with)
    unit_of = {}                          # patient -> unit of most alarms (timing log only)

    def rate(alarms, n_removed):
        hours = set()
        for e in alarms:
            h = e.start.replace(minute=0, second=0, microsecond=0)
            while h <= e.end:
                hours.add(h)
                h += timedelta(hours=1)
        return len(alarms) / len(hours), (len(alarms) - n_removed) / len(hours)

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    chunks = [patients[i:i + CHUNK_SIZE] for i in range(0, len(patients), CHUNK_SIZE)]
    paths = S.partition_by_patient(dataset, chunks, WORK_DIR)
    for k, path in enumerate(paths, start=1):
        for pid, rows in read_chunk(path).items():
            unit_of[pid] = Counter(d for _, d in rows).most_common(1)[0][0]
            for e, d in rows:
                tot[d]["corpus"] += 1
            rep = [(e, d) for e, d in rows if M.type_of(kb, e) is not None]
            for e, d in rep:
                tot[d]["replayed"] += 1
            if not rep:
                continue
            for e, d in rep:
                for patient, kind in event_alarm.get(alarm_id(e), ()):
                    ev_n[d][kind] += 1
                    ev_cases[d][kind].add(patient)

            # non-empty state, in arrival order (state per patient, across units)
            reach = None   # latest (end + window) among earlier arrivals
            for e, d in sorted(rep, key=lambda x: x[0].start):   # stable: input order at one instant
                if reach is not None and reach > e.start:
                    tot[d]["nonempty"] += 1
                r = e.end + window
                reach = r if reach is None or r > reach else reach

            def lifted(a, end, rule) -> bool:
                if rule == "1b":
                    return a in withdrawn_at and withdrawn_at[a] < end
                if rule in ("2a", "2b"):
                    return a in lifted_at and lifted_at[a] < end
                return False

            removed = Counter()               # unit -> alarms removed
            stays = defaultdict(list)          # unit -> this patient's alarms there
            for e, d in rep:
                t = tot[d]
                stays[d].append(e)
                a = alarm_id(e)
                rs = rules_of.get(a, ())
                for rule in rs:
                    t[f"{rule}_n"] += 1
                    cases[d][rule].add(pid)
                    if rule != "1a" and lifted(a, e.end, rule):
                        t[f"{rule}_lifted"] += 1
                if rs:
                    t["distinct"] += 1
                    cases[d]["distinct"].add(pid)
                    if any(not lifted(a, e.end, rule) for rule in rs):
                        removed[d] += 1
                filtered = (norm(e.priority) in FILTER_PRIORITIES
                            and (e.end - e.start).total_seconds() < FILTER_SECONDS)
                if rs and filtered:
                    t["filter_also"] += 1
                elif rs:
                    t["filter_passed"] += 1
                    if norm(e.priority) == "hoog":
                        t["filter_passed_high"] += 1
                elif filtered:
                    t["filter_only"] += 1

            # alarm rate per alarm-active clock hour: per stay, and per patient for ALL
            for d, alarms in stays.items():
                w, s = rate(alarms, removed[d])
                rates[d][0].append(w)
                rates[d][1].append(s)
            w, s = rate([e for e, _ in rep], sum(removed.values()))
            rates["ALL"][0].append(w)
            rates["ALL"][1].append(s)
        print(f"  chunk {k}/{len(paths)} ({len(unit_of)} patient(s))", flush=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    # processing times: per patient in the log, so by the patient's main unit
    hist = defaultdict(Counter)
    with Path(run["arrival_times"]).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["patient"] in unit_of:
                hist[unit_of[r["patient"]]][float(r["ms"])] += int(r["alarms"])

    units = sorted(tot)
    for src in (tot, ev_n, hist):          # ALL = the sum over the units
        src["ALL"] = sum((src[u] for u in units), Counter())
    for src in (cases, ev_cases):          # ALL = each patient once
        for key in {k for u in units for k in src[u]}:
            src["ALL"][key] = set().union(*(src[u][key] for u in units))
    # rates["ALL"] holds one value per patient, filled in the patient loop

    rows = []
    for unit in units + ["ALL"]:
        t, c = tot[unit], cases[unit]
        n_corpus, n_rep, n_ne = t["corpus"], t["replayed"], t["nonempty"]
        pm, pq1, pq3 = weighted_quartiles(hist[unit])
        rows += [
            (unit, "corpus_n", n_corpus, "Alarms of the replayed patients, before exclusion"),
            (unit, "excluded_n", n_corpus - n_rep, "Alarms without a blueprint (outside annotated strata or not annotatable)"),
            (unit, "excluded_pct", pct(n_corpus - n_rep, n_corpus), "Without a blueprint, % of corpus (0-100)"),
            (unit, "replayed_n", n_rep, "Alarms with a blueprint, replayed"),
            (unit, "nonempty_n", n_ne, "Alarms arriving in a non-empty patient state"),
            (unit, "nonempty_pct", pct(n_ne, n_rep), "Non-empty, % of replayed alarms (0-100)"),
            (unit, "proc_median", pm, "Processing time per alarm, ms, median"),
            (unit, "proc_q1", pq1, "Processing time per alarm, ms, Q1"),
            (unit, "proc_q3", pq3, "Processing time per alarm, ms, Q3"),
        ]
        for rule in ("1a", "1b", "2a", "2b"):
            n = t[f"{rule}_n"]
            rows += [
                (unit, f"{rule}_n", n, f"Rule {rule}: alarms silenced"),
                (unit, f"{rule}_pct", pct(n, n_ne), f"Rule {rule}: % of non-empty alarms (0-100)"),
                (unit, f"{rule}_cases", len(c[rule]), f"Rule {rule}: cases affected"),
            ]
            if rule != "1a":
                rows.append((unit, f"{rule}_lifted", t[f"{rule}_lifted"],
                             f"Rule {rule}: silences lifted before the alarm ended"))
        d = t["distinct"]
        rows += [
            (unit, "distinct_n", d, "Invalidation + redundancy: distinct alarms silenced"),
            (unit, "distinct_pct", pct(d, n_ne), "Distinct alarms, % of non-empty alarms (0-100)"),
            (unit, "distinct_cases", len(c["distinct"]), "Distinct alarms: cases affected"),
        ]
        for name, vals, desc in (("rate_without", rates[unit][0], "without silences"),
                                 ("rate_with", rates[unit][1], "with silences")):
            m, q1, q3 = quartiles(vals)
            rows += [(unit, f"{name}_median", m, f"Alarms per alarm-active clock hour {desc}, median"),
                     (unit, f"{name}_q1", q1, f"Alarm rate {desc}, Q1"),
                     (unit, f"{name}_q3", q3, f"Alarm rate {desc}, Q3")]
        also, passed, high = t["filter_also"], t["filter_passed"], t["filter_passed_high"]
        rows += [
            (unit, "filter_also_n", also, "Silenced alarms also suppressed by the 5-s delay filter"),
            (unit, "filter_also_pct", pct(also, d), "Also suppressed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_n", passed, "Silenced alarms the filter would pass"),
            (unit, "filter_passed_pct", pct(passed, d), "Passed, % of distinct alarms (0-100)"),
            (unit, "filter_passed_high_n", high, "Passed by the filter, of which high priority"),
            (unit, "filter_passed_high_pct", pct(high, passed), "High priority, % of passed (0-100)"),
            (unit, "filter_only_n", t["filter_only"], "Alarms suppressed by the filter but by no rule"),
        ]
        for kind, rule in EVENT_RULES.items():
            rows += [(unit, f"{rule}_n", ev_n[unit][kind], f"Rule {rule}: clinical events identified"),
                     (unit, f"{rule}_cases", len(ev_cases[unit][kind]), f"Rule {rule}: cases affected")]

    # label combinations (unit-independent)
    with (coverage or COVERAGE).open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            rows.append(("ALL", f"combos_{r['rule']}", int(r["pairs"]),
                         f"Rule {r['rule']}: label combinations a list would need (Section 4.3.2)"))
    rows.append(("ALL", "run_patients_n", len(patients), "Patients in the run"))
    rows.append(("ALL", "run_complete", int(bool(run.get("complete"))), "1 if the engine run completed without errors"))
    return rows


# ═════════════════════════════════════════════════════════════════════════
# Output
# ═════════════════════════════════════════════════════════════════════════

HEADER = ["Table", "Unit", "Key", "Value", "Description"]


def read_existing(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [tuple(r) for r in list(csv.reader(fh, delimiter=";"))[1:]]


def apply_manual(rows: list) -> list:
    """Fill values from MANUAL_PATH (Table;Unit;Key;Value), e.g. OOPS! counts."""
    if not MANUAL_PATH.exists():
        return rows
    with MANUAL_PATH.open(encoding="utf-8") as fh:
        manual = {(r["Table"], r["Unit"], r["Key"]): r["Value"] for r in csv.DictReader(fh, delimiter=";")}
    return [(t, u, k, manual.get((t, u, k), v), d) for t, u, k, v, d in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--table", choices=["3", "4"], help="compute one table, keep the other's rows")
    ap.add_argument("--no-hermit", action="store_true", help="skip the HermiT check (Table 3)")
    args = ap.parse_args()

    kept = [r for r in read_existing(OUT_PATH) if args.table and r[0] != f"Table {args.table}"]
    rows = []
    if args.table in (None, "3"):
        t3 = corpus_rows() + framework_rows(run_hermit=not args.no_hermit)
        check_identity(t3)
        rows += [("Table 3",) + tuple(r) for r in t3]
    if args.table in (None, "4"):
        rows += [("Table 4",) + tuple(r) for r in table4_rows()]
    rows = apply_manual(sorted(kept + rows, key=lambda r: r[0]))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(HEADER)
        w.writerows(rows)
    for r in rows:
        print(f"{r[0]:8} {r[1]:6} {r[2]:26} {r[3]!s:>12}  {r[4]}")
    print(f"\nWrote {OUT_PATH.relative_to(R.ROOT)}"
          + (f" (Table {args.table} recomputed, the other kept)" if args.table else ""))


if __name__ == "__main__":
    main()
