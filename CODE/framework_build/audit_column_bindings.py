"""
audit_column_bindings.py — audit every CSV column's node/leaf classification.

Re-derives exactly what build_framework.py computes for each column (node-kind
for node columns; identity/state/plain role for leaf columns) using its own
functions, so this cannot drift from what a real run actually does. Prints one
row per column for manual review against what each column *should* be.

Usage
-----
  python3 audit_column_bindings.py
"""

import sys
from pathlib import Path

import pandas as pd
from rdflib import Graph

sys.path.append(str(Path(__file__).parent))
import build_framework as R


def main() -> None:
    onto = Graph()
    onto.parse(R.ONTOLOGY_PATH, format="turtle")

    df = pd.read_csv(R.CSV_PATH, sep=";", dtype=str)
    df.columns = df.columns.str.strip()
    columns = [c for c in df.columns if c in R.COLUMN_SCHEMES]
    unmapped = [c for c in df.columns if c not in R.COLUMN_SCHEMES and c != R.LABEL_COLUMN]

    bindings = R.derive_bindings(onto, columns)
    stateful = set(onto.subjects(R.MDA.nodeKind, R.MDA.Stateful))

    print(f"{'column':<28} {'kind':<6} {'classification':<14} {'attaches to':<22} via")
    print("-" * 100)
    for col in columns:
        kind, prop, cls, scheme = bindings[col]
        if kind == "node":
            marker = next((o for o in onto.objects(cls, R.MDA.nodeKind) if o in R.KIND_OF), None)
            # Referential is the ontology's documented DEFAULT for a node-column
            # class carrying no explicit mda:nodeKind marker — not a gap. Only
            # Individuated/Stateful classes need one asserted.
            classification = R.KIND_OF.get(marker, "referential (default)")
            print(f"{col:<28} {'node':<6} {classification:<14} {R._local(cls):<22}")
        else:
            if any(str(o).lower() == "true" for o in onto.objects(prop, R.MDA.refinesUniversalIdentity)):
                role = "identity"
            elif cls in stateful:
                role = "state"
            else:
                role = "plain"
            print(f"{col:<28} {'leaf':<6} {role:<14} {R._local(cls):<22} {R._local(prop)}")

    print()
    print(f"Columns present in CSV but unmapped (ignored by build_framework.py): {unmapped or 'none'}")

    # Cross-check: every scheme with an instantiatesClass or valuesFromScheme
    # binding in the ontology, but whose column is either absent from the CSV
    # or absent from COLUMN_SCHEMES entirely — silently unreachable.
    print()
    print("Ontology-declared scheme bindings with NO corresponding CSV column:")
    bound_classes = {cls for _, _, cls, _ in bindings.values()}
    for scheme, cls in onto.subject_objects(R.MDA.instantiatesClass):
        if cls not in bound_classes:
            print(f"  {R._local(scheme):<24} instantiatesClass {R._local(cls)}")
    for prop, scheme in onto.subject_objects(R.MDA.valuesFromScheme):
        if prop == R.MDA.hasLabel:
            continue  # special-cased directly in build_alarmtype_triples, not via COLUMN_SCHEMES
        doms = [d for d in onto.objects(prop, R.RDFS.domain) if isinstance(d, R.URIRef)]
        if doms and doms[0] not in bound_classes:
            print(f"  {R._local(prop):<24} valuesFromScheme {R._local(scheme)} (domain {R._local(doms[0])})")


if __name__ == "__main__":
    main()
