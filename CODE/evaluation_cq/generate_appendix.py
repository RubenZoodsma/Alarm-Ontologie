"""
generate_appendix.py -- assembles EVALUATION/CQs/appendix_report.md: a
manuscript-appendix-ready report for every CQ that has an executable query
(CQ1-7, CQ9-11; CQ8 has no defined question in Table 4.3 and no query, and
is therefore absent below rather than invented).

Unlike cq_report.md/temporal_report.md (the engineering audit trail -- full
dev header comments, self-audit history, performance notes), this report is
reader-facing: per CQ, just the natural-language Question (`# question:`),
a one-paragraph reader-facing Rationale (`# rationale:`), the query with its
dev comments stripped, and the top 5 result rows (full row count still
noted when more exist).

Reuses cq_QA.py's base/reasoned graph construction for CQ1-7 and
cq_temporal_QA.py's snapshot Dataset construction for CQ9-11, unmodified --
both scripts' own reports keep being generated independently by their own
`python3 cq_QA.py` / `python3 cq_temporal_QA.py` entry points.

Usage
-----
  python3 generate_appendix.py
"""

import re
from pathlib import Path

import cq_QA as PQ
import cq_temporal_QA as TQ

RATIONALE_DIRECTIVE = re.compile(
    r"^#\s*rationale:\s*(.+(?:\n#\s{3}.+)*)", re.MULTILINE
)
MAX_ROWS_SHOWN = 5

OUT = PQ.ROOT / "EVALUATION" / "CQs" / "appendix_report.md"


def clean_rationale(text: str) -> str:
    m = RATIONALE_DIRECTIVE.search(text)
    if not m:
        return ""
    # directive continuation lines are prefixed "#   " -- strip the comment
    # marker and collapse into one paragraph
    raw = m.group(1)
    lines = [re.sub(r"^#\s*", "", ln).strip() for ln in raw.split("\n")]
    return " ".join(l for l in lines if l)


def clean_query(text: str) -> str:
    """Everything from the first PREFIX line onward -- drops the leading
    `# question:`/`# rationale:`/dev-comment block entirely."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("PREFIX"))
    return "\n".join(lines[start:]).strip()


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_cq(number: str, name: str, question: str, rationale: str,
              query_text: str, vars_: list, rows: list) -> list:
    lines = [
        f"## CQ{number} — {name}",
        "",
        f"**Question:** {question}",
        "",
        f"**Rationale:** {rationale}",
        "",
        "**SPARQL:**",
        "",
        "```sparql",
        query_text,
        "```",
        "",
    ]
    shown = rows[:MAX_ROWS_SHOWN]
    label = f"**Answer** (top {len(shown)} of {len(rows)} row(s)):" if len(rows) > len(shown) \
        else f"**Answer** ({len(rows)} row(s)):"
    lines.append(label)
    lines.append("")
    if shown:
        lines.append("| " + " | ".join(vars_) + " |")
        lines.append("|" + "|".join("---" for _ in vars_) + "|")
        for row in shown:
            lines.append("| " + " | ".join(escape_cell(c) for c in row) + " |")
    else:
        lines.append("_No results._")
    lines.append("")
    return lines


def cq_number_and_name(stem: str) -> tuple:
    # stem like "CQ1_device_measurements" -> ("1", "device measurements")
    m = re.match(r"CQ(\d+)_(.+)", stem)
    num, rest = m.group(1), m.group(2)
    return num, rest.replace("_", " ")


def main():
    out_lines = [
        "# Competency question appendix",
        "",
        "Reader-facing companion to Table 4.3 / Section 5.1.5: per CQ, the "
        "natural-language question, a one-paragraph rationale for the "
        "query's approach, the SPARQL query itself, and its top 5 result "
        "rows. CQ8 is absent -- no question is defined for it in Table 4.3 "
        "and no query exists.",
        "",
        "## Post-enrichment (CQ1–7)",
        "",
    ]

    base = PQ.load_base()
    reasoned = PQ.prepare_graph(PQ.Graph() + base)
    post_queries = sorted(
        (PQ.CQS_DIR / "post_enrichment").glob("CQ*.rq"),
        key=lambda p: int(re.match(r"CQ(\d+)", p.stem).group(1)),
    )
    for q in post_queries:
        text = q.read_text()
        num, name = cq_number_and_name(q.stem)
        r = PQ.run_query(q, base, reasoned)
        out_lines += render_cq(
            num, name, r["question"], clean_rationale(text),
            clean_query(text), r["vars"], r["rows"],
        )

    out_lines += ["## Temporal, enriched (CQ9–11)", ""]
    ds = TQ.build_snapshot_dataset()
    temp_queries = sorted(
        TQ.QUERY_DIR.glob("CQ*.rq"),
        key=lambda p: int(re.match(r"CQ(\d+)", p.stem).group(1)),
    )
    for q in temp_queries:
        text = q.read_text()
        num, name = cq_number_and_name(q.stem)
        r = TQ.run_query(q, ds)
        out_lines += render_cq(
            num, name, r["question"], clean_rationale(text),
            clean_query(text), r["vars"], r["rows"],
        )

    OUT.write_text("\n".join(out_lines))
    print(f"[generate_appendix] wrote {OUT.relative_to(PQ.ROOT)}")


if __name__ == "__main__":
    main()
