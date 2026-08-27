"""
cq_report_html.py — generates docs/cq/index.html: a static, pre-computed
browser for every competency question (CQ1-11) with Previous/Next
navigation.

Reuses cq_QA.py (pre/post-enrichment CQs, against the base graph or its
OWL-RL closure) and cq_temporal_QA.py (CQ9-11, against the fabricated
snapshot dataset) unmodified to actually execute every query, then embeds
the results -- question, SPARQL text, first 5 result rows, full row count
-- as a JSON blob into a single-file page whose JS only renders/paginates
that blob. Same "compute in Python, render in JS" pattern as
archetype_report.py/vocab_browser.py: this ships on GitHub Pages, so there
is no backend and no live query execution in the browser.

Usage
-----
  python3 cq_report_html.py
"""

import json
import re
from pathlib import Path

import cq_QA as Q
import cq_temporal_QA as T

OUT = Q.ROOT / "docs" / "cq" / "index.html"
MAX_ROWS_SHOWN = 5

NUMBER_RE = re.compile(r"CQ(\d+)")


def _number(name: str) -> int:
    m = NUMBER_RE.search(name)
    return int(m.group(1)) if m else 0


def collect_standard() -> list:
    base = Q.load_base()
    reasoned = Q.prepare_graph(Q.Graph() + base)
    entries = []
    for qpath in Q.discover_queries(Q.CQS_DIR):
        r = Q.run_query(qpath, base, reasoned)
        entries.append({
            "stage": r["stage"],
            "name": r["name"],
            "number": _number(r["name"]),
            "question": r["question"],
            "query": r["query_text"],
            "reasoning": "reasoned" if r["used_reasoning"] else "base",
            "vars": r["vars"],
            "rows": r["rows"][:MAX_ROWS_SHOWN],
            "total_rows": len(r["rows"]),
        })
    return entries


def collect_temporal() -> list:
    ds = T.build_snapshot_dataset()
    entries = []
    for qpath in sorted(T.QUERY_DIR.glob("CQ*.rq")):
        r = T.run_query(qpath, ds)
        entries.append({
            "stage": "temporal",
            "name": r["name"],
            "number": _number(r["name"]),
            "question": r["question"],
            "query": qpath.read_text().strip(),
            "reasoning": "temporal",
            "vars": r["vars"],
            "rows": r["rows"][:MAX_ROWS_SHOWN],
            "total_rows": len(r["rows"]),
        })
    return entries


def main() -> None:
    entries = collect_standard() + collect_temporal()
    stage_rank = {s: i for i, s in enumerate(Q.STAGE_ORDER)}
    entries.sort(key=lambda e: (stage_rank.get(e["stage"], len(stage_rank)), e["number"], e["name"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    data_json = json.dumps(entries).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", data_json)
    OUT.write_text(html)
    print(f"[cq_report_html] {len(entries)} competency question(s) -> {OUT.relative_to(Q.ROOT)}")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Competency Questions</title>
<style>
  :root{--ground:#f4f7f7;--surface:#fff;--ink:#16231f;--muted:#61726d;--faint:#89a09a;
        --line:#e2ebe8;--accent:#0c7d76;--accent-soft:#e4f2f0;--shadow:0 1px 2px rgba(0,0,0,.05)}
  @media (prefers-color-scheme:dark){:root{--ground:#0d1412;--surface:#15201d;--ink:#e9f1ee;
        --muted:#93a8a2;--faint:#6b807a;--line:#23322e;--accent:#37b9ae;--accent-soft:#143430;--shadow:none}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);line-height:1.55;
       font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:clamp(1.2rem,3vw,2.2rem) clamp(1rem,3vw,2rem) 4rem}
  .eyebrow{font-family:ui-monospace,Menlo,monospace;font-size:.7rem;letter-spacing:.16em;
           text-transform:uppercase;color:var(--accent);font-weight:600}
  .eyebrow a{color:inherit}
  h1{margin:.3rem 0 .6rem;font-size:clamp(1.5rem,3vw,2.1rem);line-height:1.15;
     letter-spacing:-.02em;font-weight:680}
  .lede{margin:0 0 1.4rem;color:var(--muted);max-width:74ch;font-size:.9rem}
  .bar{display:flex;flex-wrap:wrap;gap:.7rem;align-items:center;background:var(--surface);
       border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem;box-shadow:var(--shadow);
       position:sticky;top:.6rem;z-index:10}
  select,button{font:inherit;color:var(--ink);background:var(--surface);
       border:1px solid var(--line);border-radius:8px;padding:.4rem .7rem;cursor:pointer}
  select:hover,button:hover{border-color:var(--accent)}
  button:disabled{opacity:.35;cursor:not-allowed;border-color:var(--line)}
  select{max-width:min(46vw,420px)}
  .spacer{flex:1}
  .kv{display:inline-flex;gap:.3rem;font-size:.82rem;color:var(--muted);white-space:nowrap}
  .kv b{color:var(--ink)}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;
         padding:1rem 1.1rem;box-shadow:var(--shadow);margin-top:1rem}
  h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.11em;color:var(--faint);
     font-weight:600;margin:0 0 .55rem}
  .head-row{display:flex;justify-content:space-between;align-items:baseline;gap:.6rem;flex-wrap:wrap}
  #cqName{margin:0;font-size:1.15rem;font-weight:650;letter-spacing:-.01em;color:var(--ink);
          text-transform:none;font-family:ui-sans-serif,system-ui,sans-serif;letter-spacing:normal}
  .pills{display:flex;gap:.4rem;flex-wrap:wrap}
  .pill{font-size:.68rem;padding:.15rem .55rem;border-radius:99px;background:var(--accent-soft);
        color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.04em;
        white-space:nowrap}
  .pill.muted{background:var(--line);color:var(--faint)}
  #question{margin:.7rem 0 0;font-size:1rem;color:var(--ink)}
  pre{background:var(--ground);border:1px solid var(--line);border-radius:8px;padding:.75rem .9rem;
      overflow-x:auto;margin:0;font-size:.8rem;line-height:1.5}
  code{font-family:ui-monospace,Menlo,Consolas,monospace}
  .count{font-size:.72rem;color:var(--faint);text-transform:none;letter-spacing:0;font-weight:500}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
     color:var(--faint);padding:.35rem .55rem;border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:.35rem .55rem;border-bottom:1px solid var(--line);vertical-align:top;
     font-family:ui-monospace,Menlo,Consolas,monospace;white-space:nowrap}
  tr:last-child td{border-bottom:none}
  .empty{color:var(--faint);font-size:.85rem;font-style:italic;padding:.3rem 0}
  footer{margin-top:2rem;padding-top:1.1rem;border-top:1px solid var(--line);
         font-size:.82rem;color:var(--faint)}
  footer a{color:var(--muted)}
</style></head><body><div class="wrap">

  <span class="eyebrow"><a href="../index.html">MDA</a> · competency questions</span>
  <h1>Competency Questions</h1>
  <p class="lede">Every competency question the ontology has been tested against, run once
     against the curated ontology, vocabulary and knowledge base and its OWL-RL closure — not a
     live query console, just what each query actually returns. Natural-language question, the
     SPARQL that answers it, and its first 5 result rows (full count noted alongside).</p>

  <div class="bar">
    <button id="prevBtn" title="Previous (←)">← Previous</button>
    <select id="jump"></select>
    <span class="kv"><b id="posNum">1</b> / <span id="posTotal">1</span></span>
    <div class="spacer"></div>
    <button id="nextBtn" title="Next (→)">Next →</button>
  </div>

  <div class="panel">
    <div class="head-row">
      <h2 id="cqName">—</h2>
      <div class="pills">
        <span class="pill" id="stagePill">—</span>
        <span class="pill muted" id="reasoningPill">—</span>
      </div>
    </div>
    <p id="question"></p>
  </div>

  <div class="panel">
    <h2>SPARQL</h2>
    <pre><code id="sparql"></code></pre>
  </div>

  <div class="panel">
    <h2>Results <span class="count" id="rowCount"></span></h2>
    <div style="overflow-x:auto"><table id="resultsTable"></table></div>
  </div>

  <footer>
    <p><a href="../index.html">← Back to MDA overview</a></p>
    <p><a href="https://github.com/RubenZoodsma/Alarm-Ontologie">Source on GitHub</a></p>
  </footer>

</div>
<script>const DATA = __DATA__;</script>
<script>
  const REASONING_LABEL = {
    base: "base graph (asserted only)",
    reasoned: "reasoned (OWL-RL closure)",
    temporal: "temporal snapshot dataset",
  };

  function humanizeStage(stage) {
    return stage.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const jumpEl = document.getElementById("jump");
  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");

  DATA.forEach((e, i) => {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = `${humanizeStage(e.stage)} · ${e.name}`;
    jumpEl.appendChild(opt);
  });

  let idx = 0;

  function render(i) {
    idx = Math.max(0, Math.min(DATA.length - 1, i));
    const e = DATA[idx];

    document.getElementById("cqName").textContent = e.name;
    document.getElementById("stagePill").textContent = humanizeStage(e.stage);
    document.getElementById("reasoningPill").textContent = REASONING_LABEL[e.reasoning] || e.reasoning;
    document.getElementById("question").textContent = e.question || "(no question text)";
    document.getElementById("sparql").textContent = e.query;

    const rowCountEl = document.getElementById("rowCount");
    rowCountEl.textContent = e.total_rows > e.rows.length
      ? `${e.total_rows} row(s) — showing first ${e.rows.length}`
      : `${e.total_rows} row(s)`;

    const table = document.getElementById("resultsTable");
    if (e.rows.length) {
      table.innerHTML =
        "<tr>" + e.vars.map(v => `<th>${escapeHtml(v)}</th>`).join("") + "</tr>" +
        e.rows.map(row =>
          "<tr>" + row.map(c => `<td>${escapeHtml(c)}</td>`).join("") + "</tr>"
        ).join("");
    } else {
      table.innerHTML = "";
    }
    table.style.display = e.rows.length ? "" : "none";
    let emptyEl = document.getElementById("emptyNote");
    if (!e.rows.length) {
      if (!emptyEl) {
        emptyEl = document.createElement("p");
        emptyEl.id = "emptyNote";
        emptyEl.className = "empty";
        table.insertAdjacentElement("afterend", emptyEl);
      }
      emptyEl.textContent = "No results.";
    } else if (emptyEl) {
      emptyEl.remove();
    }

    jumpEl.value = idx;
    document.getElementById("posNum").textContent = idx + 1;
    document.getElementById("posTotal").textContent = DATA.length;
    prevBtn.disabled = idx === 0;
    nextBtn.disabled = idx === DATA.length - 1;

    if (history.replaceState) history.replaceState(null, "", "#" + e.name);
  }

  prevBtn.addEventListener("click", () => render(idx - 1));
  nextBtn.addEventListener("click", () => render(idx + 1));
  jumpEl.addEventListener("change", () => render(Number(jumpEl.value)));
  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "SELECT") return;
    if (ev.key === "ArrowLeft") render(idx - 1);
    if (ev.key === "ArrowRight") render(idx + 1);
  });

  const startName = location.hash.replace("#", "");
  const startIdx = DATA.findIndex(e => e.name === startName);
  render(startIdx >= 0 ? startIdx : 0);
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
