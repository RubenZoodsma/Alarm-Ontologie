"""
cq_temporal_QA.py — runs the temporal competency questions (CQ9-11,
EVALUATION/CQs/temporal/*.rq) against the fabricated confirmation/
falsification dataset (DATA/CQ_evaluation/events_data.csv).

Reuses CODE/evaluation_poc/op_knowledge.py unmodified to build the same
per-tick situational snapshot graph the real POC pipeline produces
(op_inference.py) -- just pointed at the fabricated dataset instead of
DATA/POC_EVENTS/events_data.csv, and written to its own output path so
neither the real POC output nor the real event data is ever touched.

Each temporal/*.rq file is self-contained (its own PREFIX block) and is
executed as-is -- no reasoning/graph-selection logic here beyond building
the Dataset once; every query's own header comment documents which parts
of the graph it depends on (default vs named graph, why).

Usage
-----
  python3 cq_temporal_QA.py
"""

import re
import sys
from pathlib import Path

from rdflib import Dataset, URIRef, Literal
from rdflib.namespace import RDF, XSD, OWL

QUESTION_DIRECTIVE = re.compile(r"^#\s*question:\s*(.+)$", re.MULTILINE)

CQ_DIR = Path(__file__).resolve().parent
CODE_DIR = CQ_DIR.parent
ROOT = CODE_DIR.parent  # Restructured

sys.path.insert(0, str(CODE_DIR / "evaluation_poc" / "core"))
sys.path.insert(0, str(CODE_DIR / "shared"))
import op_knowledge as K  # noqa: E402

FABRICATED_EVENTS = ROOT / "DATA" / "CQ_evaluation" / "events_data.csv"
QUERY_DIR = ROOT / "EVALUATION" / "CQs" / "temporal"
TRIG_OUT = QUERY_DIR / "cq_evaluation_inference.trig"
REPORT_OUT = ROOT / "EVALUATION" / "CQs" / "temporal_report.md"


def build_snapshot_dataset() -> Dataset:
    """
    Same construction as op_inference.py's main(), pointed at the
    fabricated dataset. Returns the in-memory Dataset and also serialises
    it to TRIG_OUT so the snapshot graph can be inspected directly.
    """
    kb = K.load_kb()
    events = K.load_events(FABRICATED_EVENTS)
    groups = K.group_by_patient(events)

    unknown = sorted({e.label for e in events if e.label not in kb.type_index})
    if unknown:
        raise SystemExit(f"[FATAL] unknown label(s) in fabricated dataset: {unknown}")

    ds = Dataset()
    ds.bind("entity", K.ENTITY); ds.bind("inst", K.INST); ds.bind("mda", K.MDA)
    ds.bind("opstate", K.OPSTATE); ds.bind("snapshot", K.SNAP)

    patients = sorted(groups)
    for i in range(len(patients)):
        for j in range(i + 1, len(patients)):
            ds.add((K.patient_iri(patients[i]), OWL.differentFrom, K.patient_iri(patients[j])))

    total_ticks = 0
    for patient, evs in groups.items():
        tl = K.build_timeline(kb, patient, evs)
        for tr in tl.background:
            ds.add(tr)
        for t in tl.ticks:
            situation = tl.situation_at(t)
            gname = URIRef(K.SNAP[f"{patient}_{t.strftime('%Y%m%dT%H%M%S')}"])
            ng = ds.graph(gname)
            for tr in situation:
                ng.add(tr)
            ds.add((gname, RDF.type, K.SNAP.Snapshot))
            ds.add((gname, K.SNAP.atTime, Literal(t.isoformat(), datatype=XSD.dateTime)))
            ds.add((gname, K.SNAP.forPatient, K.patient_iri(patient)))
            total_ticks += 1

    TRIG_OUT.parent.mkdir(parents=True, exist_ok=True)
    ds.serialize(destination=str(TRIG_OUT), format="trig")
    print(f"[snapshot] {len(patients)} patient(s), {total_ticks} tick(s) -> {TRIG_OUT.name}")
    return ds


def run_query(query_path: Path, ds: Dataset) -> dict:
    text = query_path.read_text()
    question_match = QUESTION_DIRECTIVE.search(text)
    result = ds.query(text)
    rows = [[str(t) for t in row] for row in result if not isinstance(row, bool)]
    print(f"[cq_temporal_QA] {query_path.name}: {len(rows)} row(s)")
    return {
        "name": query_path.stem,
        "question": question_match.group(1).strip() if question_match else None,
        "vars": [str(v) for v in result.vars] if result.vars else [],
        "rows": rows,
    }


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(results: list) -> str:
    lines = [
        "# Temporal competency question (CQ9-11) validation report",
        "",
        f"Run against the fabricated confirmation/falsification dataset "
        f"({FABRICATED_EVENTS.relative_to(ROOT)}), via the real operational "
        f"pipeline (CODE/evaluation_poc/op_knowledge.py), unmodified.",
        "",
    ]
    for r in results:
        lines.append(f"## {r['name']}")
        lines.append("")
        if r["question"]:
            lines.append(f"**Question:** {r['question']}")
            lines.append("")
        lines.append(f"**Outcome:** {len(r['rows'])} row(s)")
        lines.append("")
        if r["rows"]:
            lines.append("| " + " | ".join(r["vars"]) + " |")
            lines.append("|" + "|".join("---" for _ in r["vars"]) + "|")
            for row in r["rows"]:
                lines.append("| " + " | ".join(_escape_cell(c) for c in row) + " |")
        else:
            lines.append("_No results._")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ds = build_snapshot_dataset()
    queries = sorted(QUERY_DIR.glob("CQ*.rq"))
    if not queries:
        print(f"[cq_temporal_QA] no queries found under {QUERY_DIR.relative_to(ROOT)}/")
        return
    results = [run_query(q, ds) for q in queries]
    REPORT_OUT.write_text(render_markdown(results))
    print(f"\n[cq_temporal_QA] report written to {REPORT_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
