"""
preview_annotations.py — local work-in-progress view of the annotation
workbook, for visually checking annotations while they are being made.

    python3 CODE/evaluation_qa/preview_annotations.py [workbook.xlsx] [--no-open]

Default workbook: DATA/ANNOTATION/to_authors/p99_annotation.xlsx.
The workbook is written to its annotation CSV (step 1); the build and the
pages go to a sandbox, EVALUATION/WIP_ANNOTATION/ (git-ignored). FRAMEWORK/
and docs/ are never touched, so the published site and the committed
knowledge base stay as they are.

Steps
  1. Workbook -> the annotation CSV it was exported from (consensus/
     p99_annotated.csv), every run, with import_annotation_xlsx.py (its
     validation: layout, identities, reviewStatus). A workbook it refuses is
     shown as an issue and the CSV is left as it was.
  2. Blueprint identity checked on all rows against the corpus the workbook
     was exported from (as build_framework.py does).
  3. Cells that cannot become a concept IRI (a space, '<', ...) are blanked
     and reported, instead of stopping the build.
  4. Only rows with at least one annotation beyond the pre-filled device and
     manufacturer are built (EXCL rows are left out by the build itself); the
     rest are listed as not yet annotated.
  5. build_framework.py runs on that CSV with its outputs redirected to the
     sandbox. Data checks that would stop it (approximates conflicts) and
     those it only reports (device-model axioms, [WARN] lines) are collected.
  6. archetype_report.py renders the same reasoned graph view as the site's
     Knowledge Graphs page from the sandbox knowledge base, with a WIP banner.
  7. index.html: summary, every issue, the rows still to annotate, and a link
     to the graph view.

The graph page fetches the ontology glossary from the published site, so
that panel needs an internet connection; the graphs themselves do not.
"""

import contextlib
import csv 
import html
import importlib.util
import io
import json
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_prep"))
from annotation_xlsx_common import FIRST_ROW  # noqa: E402  (workbook row of the first alarm)
CODE = ROOT / "CODE"
SANDBOX = ROOT / "EVALUATION" / "WIP_ANNOTATION"
DEFAULT_XLSX = ROOT / "DATA" / "ANNOTATION" / "to_authors" / "p99_annotation.xlsx"
PREFILLED = {"alarmLabel", "alarmPriority", "Device.Type", "Device", "Device.Manufacturer", "reviewStatus"}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Issues:
    def __init__(self):
        self.items = []                     # (level, step, message)

    def add(self, level, step, msg):
        msg = str(msg).strip()
        for i, (lv, st, m, n) in enumerate(self.items):
            if (lv, st, m) == (level, step, msg):
                self.items[i] = (lv, st, m, n + 1)
                return
        self.items.append((level, step, msg, 1))
        print(f"[{level}] {step}: {msg.splitlines()[0]}")


def read_csv_rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    return rows[0], rows[1:]


def write_csv_rows(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter=";", lineterminator="\r\n").writerows([header] + rows)


def workbook_comments(xlsx: Path) -> dict:
    """Excel row -> text in the 'Annotator comment' column (only in the workbook)."""
    from annotation_xlsx_common import COMMENT, HEADER_ROW, SHEET_DATA
    ws = load_workbook(xlsx, read_only=True)[SHEET_DATA]
    head = next(ws.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW, values_only=True))
    if COMMENT not in head:
        return {}
    j = head.index(COMMENT)
    return {FIRST_ROW + n: str(r[j]).strip()
            for n, r in enumerate(ws.iter_rows(min_row=FIRST_ROW, values_only=True))
            if j < len(r) and r[j] not in (None, "")}


def meta_paths(xlsx: Path):
    """(annotation CSV the workbook was exported from, corpus it was checked against)."""
    wb = load_workbook(xlsx, read_only=True)
    meta = {r[0]: r[1] for r in wb["_meta"].iter_rows(max_row=5, values_only=True)}
    return ROOT / meta["source_csv"], ROOT / meta["corpus_csv"]


def run_build(bf, issues, build_csv, corpus):
    """build_framework.main() with sandbox paths and collected (not fatal) data checks."""
    bf.CSV_PATH, bf.CORPUS_PATH = build_csv, corpus
    bf.VOCAB_OUT_PATH = SANDBOX / "vocab_generated.ttl"
    bf.KG_OUT_PATH = SANDBOX / "kg_generated.ttl"
    bf.check_blueprint_identity = lambda df: None        # done on all rows in step 2

    orig_approx, orig_model, orig_notation = (bf.check_approximates_axioms, bf.check_model_axioms,
                                              bf.validate_notations)

    def approx(*a):
        try:
            orig_approx(*a)
        except ValueError as e:
            issues.add("ERROR", "approximates axioms", e)

    def model(*a):
        for m, label, dev, exp in orig_model(*a):
            issues.add("ERROR", "device-model axiom", f"{label}: annotated Device {dev}, "
                                                      f"but inference.ttl makes {m} a {exp}")
        return []

    def notation(*a):
        try:
            orig_notation(*a)
        except ValueError as e:
            issues.add("ERROR", "notation", e)

    bf.check_approximates_axioms, bf.check_model_axioms, bf.validate_notations = approx, model, notation
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            bf.main()
        ok = True
    except Exception as e:                                 # noqa: BLE001 — shown on the page
        issues.add("ERROR", "build_framework", f"{type(e).__name__}: {e}")
        ok = False
    for line in log.getvalue().splitlines():
        if line.startswith("[WARN]"):
            issues.add("WARN", "build_framework", line[6:].strip())
    return ok, log.getvalue()


def run_report(issues, xl_rows, row_info):
    ar = load_module("archetype_report_wip", CODE / "evaluation_qa" / "archetype_report.py")
    ar.COMPACT = True
    ar.VOCAB, ar.CATALOGUE = SANDBOX / "vocab_generated.ttl", SANDBOX / "kg_generated.ttl"
    ar.OUT = SANDBOX / "graph.html"
    argv, sys.argv = sys.argv, [sys.argv[0], "--no-open"]
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            ar.main()
    except Exception as e:                                 # noqa: BLE001
        issues.add("ERROR", "archetype_report", f"{type(e).__name__}: {e}")
        return False, log.getvalue()
    finally:
        sys.argv = argv
    page = ar.OUT.read_text(encoding="utf-8")
    banner = ('<div style="position:sticky;top:0;z-index:999;background:#fff2cc;border-bottom:1px solid '
              '#d6b656;padding:6px 12px;font:13px Arial,sans-serif">WORK IN PROGRESS — built from the '
              'annotation workbook, not part of the framework. <a href="index.html">Summary &amp; '
              'issues</a></div>')
    page = enhance_page(page.replace("<body>", "<body>" + banner, 1), xl_rows, row_info, issues)
    ar.OUT.write_text(page, encoding="utf-8")
    return True, log.getvalue()


_EXTRA_CSS = """<style>
  .figure{height:74vh;min-height:420px;padding:0;position:relative;overflow:hidden}
  .figure #diagram, .figure #diagram svg{width:100%;height:100%}
  .zoombar{position:absolute;top:.5rem;right:.5rem;display:flex;gap:.3rem;z-index:5}
  .zoombar button{padding:.2rem .55rem}
  .gotobox{display:inline-flex;align-items:center;gap:.3rem;font-size:.8rem;color:var(--muted)}
  .gotobox input{width:4.2rem}
  #xlmsg{font-size:.78rem;color:#b00020}
  .skipnote{display:flex;align-items:center;justify-content:center;height:100%;padding:2rem;
            font-size:1.05rem;color:#7a5b00;background:#fff8e1;text-align:center}
</style>"""

_EXTRA_BAR = """
    <button id="dirToggle" title="Switch between vertical and horizontal layout"></button>
    <span class="gotobox">Go to #<input id="gotoNum" type="number" min="1"></span>
    <span class="gotobox">Excel row<input id="gotoRow" type="number" min="3"></span>
    <span class="kv" id="xlrow"></span><span id="xlmsg"></span>"""

_EXTRA_JS = """
<script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/dist/svg-pan-zoom.min.js"></script>
<script>
let DIR = 'TD', pz = null;
$('pick').innerHTML = DATA.map((a,i) =>
  `<option value="${i}">${nameOf(a)}${a.skipped ? ' — ' + (a.skipped.startsWith('Excluded') ? 'excluded' : 'not annotated') : ''}</option>`).join('');
try { DIR = localStorage.getItem('wipDir') || 'TD'; } catch (e) {}
function dirLabel(){ $('dirToggle').textContent = DIR === 'TD' ? 'Layout: vertical ↕' : 'Layout: horizontal ↔'; }
$('diagram').insertAdjacentHTML('afterend',
  '<div class="zoombar"><button id="zIn" title="Zoom in">+</button><button id="zOut" title="Zoom out">−</button>' +
  '<button id="zFit" title="Fit to panel">Fit</button><button id="z1" title="Actual size">1:1</button></div>');

async function draw(){
  const a = DATA[idx];
  $('title').textContent = nameOf(a);
  $('cat').textContent = a.category;
  $('prio').textContent = a.priority;
  $('fcount').textContent = a.n_facts + ' fact(s) — every entailed triple reachable from this archetype';
  $('pick').value = idx;
  $('pos').innerHTML = '<b>' + (idx+1) + '</b> / ' + DATA.length;
  $('xlrow').innerHTML = a.xlrow ? 'Excel row <b>' + a.xlrow + '</b>' : '';
  $('prev').disabled = idx === 0;
  $('next').disabled = idx === DATA.length - 1;
  $('gotoNum').max = DATA.length;
  $('facts').innerHTML = a.facts.map(f =>
    `<tr class="group-${f.group}"><td>${f.subject} <span class="pill">${f.subject_cls}</span></td>` +
    `<td>${f.prop}</td><td>${f.value}</td></tr>`).join('');
  if (a.skipped) {
    if (pz) { try { pz.destroy(); } catch (e) {} pz = null; }
    $('fcount').textContent = 'not in the knowledge graph';
    const div = document.createElement('div');
    div.className = 'skipnote';
    div.textContent = a.skipped;
    $('diagram').replaceChildren(div);
    return;
  }
  const mmd = ($('hideType').checked ? a.mmd_notype : a.mmd).replace(/^graph \\w+/, 'graph ' + DIR);
  const {svg} = await mermaid.render('m' + (seq++), mmd);
  $('diagram').innerHTML = svg;
  const el = $('diagram').querySelector('svg');
  el.removeAttribute('height'); el.setAttribute('width', '100%'); el.style.maxWidth = 'none';
  el.style.height = '100%';
  if (pz) { try { pz.destroy(); } catch (e) {} }
  pz = svgPanZoom(el, {zoomEnabled: true, mouseWheelZoomEnabled: false, dblClickZoomEnabled: true,
                       fit: true, center: true, minZoom: 0.05, maxZoom: 20});
}
$('zIn').onclick = () => pz && pz.zoomBy(1.25);
$('zOut').onclick = () => pz && pz.zoomBy(0.8);
$('zFit').onclick = () => pz && (pz.resize(), pz.fit(), pz.center());
$('z1').onclick = () => pz && (pz.zoom(pz.getZoom() / pz.getSizes().realZoom), pz.center());
$('diagram').parentElement.addEventListener('wheel', e => {
  if (!pz || !(e.ctrlKey || e.metaKey)) return;          // plain scroll keeps scrolling the page
  e.preventDefault();
  const r = $('diagram').getBoundingClientRect();
  pz.zoomAtPointBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, {x: e.clientX - r.left, y: e.clientY - r.top});
}, {passive: false});
$('dirToggle').onclick = () => {
  DIR = DIR === 'TD' ? 'LR' : 'TD';
  try { localStorage.setItem('wipDir', DIR); } catch (e) {}
  dirLabel(); draw();
};
function gotoKey(input, fn){
  input.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') fn(+input.value); });
  input.addEventListener('change', () => fn(+input.value));
}
gotoKey($('gotoNum'), n => { $('xlmsg').textContent = ''; if (n >= 1) go(n - 1); });
gotoKey($('gotoRow'), n => {
  const i = DATA.findIndex(a => a.xlrow === n);
  $('xlmsg').textContent = i < 0 ? ' no alarm on row ' + n : '';
  if (i >= 0) go(i);
});
$('hideType').onchange = draw;      // the page bound the earlier draw()
addEventListener('resize', () => pz && (pz.resize(), pz.fit(), pz.center()));
dirLabel(); draw();
</script>"""


def enhance_page(page, xl_rows, row_info, issues):
    """WIP-only additions to the graph page: archetypes in workbook order with
    their Excel row; the workbook rows that are not built shown in place
    ('Excluded – <annotator comment>' for EXCL, 'Not annotated yet' otherwise);
    a vertical/horizontal layout switch, go-to-number and go-to-Excel-row
    fields, and fit/zoom/pan on the diagram."""
    start = page.index("<script>const DATA = ") + len("<script>const DATA = ")
    end = page.index(";</script>", start)
    data = json.loads(page[start:end])
    for a in data:
        a["xlrow"] = xl_rows.get(_row_key(a["label"], a["priority"], a["model"]))
    missing = [a["label"] for a in data if a["xlrow"] is None]
    if missing:
        issues.add("WARN", "graph page", f"{len(missing)} archetype(s) without an Excel row: "
                   + ", ".join(missing[:5]))
    built = {a["xlrow"] for a in data}
    for key, xlrow in xl_rows.items():
        if xlrow in built:
            continue
        info = row_info[key]
        if info["status"] == "EXCL":
            note = "Excluded – " + (info["comment"] or "no reason given")
        else:
            note = "Not annotated yet"
        data.append({"label": info["label"], "model": info["model"], "priority": info["priority"],
                     "xlrow": xlrow, "skipped": note, "category": "—", "facts": [], "n_facts": 0,
                     "mmd": "", "mmd_notype": ""})
    data.sort(key=lambda a: (a["xlrow"] is None, a["xlrow"] or 0))
    page = page[:start] + json.dumps(data, separators=(",", ":")) + page[end:]
    page = page.replace("</head>", _EXTRA_CSS + "</head>", 1)
    anchor = '<button id="next" title="Next (→)">Next ▶</button>'
    page = page.replace(anchor, anchor + _EXTRA_BAR, 1)
    tail = "</script>\n</body></html>"
    i = page.rindex(tail) + len("</script>")
    return page[:i] + _EXTRA_JS + page[i:]


def _row_key(label, priority, model):
    return (" ".join(str(label).split()).casefold(), str(priority).strip().casefold(),
            str(model or "").strip().casefold())


def write_index(xlsx, stats, issues, todo, build_log, report_ok):
    e = html.escape
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    rows = "".join(
        f'<tr class="{lvl.lower()}"><td>{lvl}</td><td>{e(step)}</td><td><pre>{e(msg)}'
        f'{f"  ({n}x)" if n > 1 else ""}</pre></td></tr>'
        for lvl, step, msg, n in sorted(issues.items, key=lambda x: (order[x[0]], x[1])))
    n_err = sum(1 for i in issues.items if i[0] == "ERROR")
    n_warn = sum(1 for i in issues.items if i[0] == "WARN")
    todo_rows = "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td><td>{e(c)}</td></tr>" for a, b, c in todo)
    stat_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in stats.items())
    link = ('<p><a class="btn" href="graph.html">Open the graph view →</a></p>' if report_ok
            else "<p><b>No graph view: the build or the report failed (see issues).</b></p>")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>WIP annotations</title>
<style>
body{{font:14px Arial,sans-serif;margin:0;padding:0 24px 40px;color:#1a1a1a;background:#fff}}
.banner{{background:#fff2cc;border-bottom:1px solid #d6b656;margin:0 -24px 16px;padding:8px 24px}}
table{{border-collapse:collapse;margin:8px 0 20px}} td,th{{border:1px solid #ddd;padding:4px 8px;
text-align:left;vertical-align:top}} pre{{margin:0;white-space:pre-wrap;font:12px Menlo,monospace}}
tr.error td:first-child{{color:#b00020;font-weight:bold}} tr.warn td:first-child{{color:#9a6700}}
.btn{{display:inline-block;background:#1c5cab;color:#fff;padding:8px 14px;border-radius:4px;
text-decoration:none}} details{{margin:12px 0}}
</style></head><body>
<div class="banner"><b>WORK IN PROGRESS</b> — local preview of the annotation workbook; not part of
the framework and not published.</div>
<h1>Annotation preview</h1>
<table>{stat_rows}</table>
{link}
<h2>Issues <small>({n_err} error, {n_warn} warning)</small></h2>
{'<table><tr><th>Level</th><th>Step</th><th>Message</th></tr>' + rows + '</table>' if rows else '<p>None.</p>'}
<details><summary>Not yet annotated ({len(todo)} rows, not built)</summary>
<table><tr><th>alarmLabel</th><th>alarmPriority</th><th>Device.Type</th></tr>{todo_rows}</table></details>
<details><summary>Build log</summary><pre>{e(build_log)}</pre></details>
</body></html>"""
    (SANDBOX / "index.html").write_text(page, encoding="utf-8")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    xlsx = Path(args[0]).resolve() if args else DEFAULT_XLSX
    SANDBOX.mkdir(parents=True, exist_ok=True)
    issues = Issues()
    stats = {"Workbook": str(xlsx.relative_to(ROOT) if xlsx.is_relative_to(ROOT) else xlsx),
             "Built at": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # 1. workbook -> the annotation CSV it was exported from (validated; on a refused
    #    workbook import_annotation_xlsx.py writes nothing, so the CSV stays as it was)
    full_csv, corpus = meta_paths(xlsx)
    stats["Annotation CSV (overwritten from the workbook)"] = str(full_csv.relative_to(ROOT))
    imp = subprocess.run([sys.executable, str(CODE / "data_prep" / "import_annotation_xlsx.py"),
                          str(xlsx), "--out", str(full_csv), "--force"], capture_output=True,
                         text=True, cwd=CODE / "data_prep")
    if imp.returncode != 0:
        issues.add("ERROR", "import workbook", imp.stderr.strip() or imp.stdout.strip())
        write_index(xlsx, stats, issues, [], imp.stdout + imp.stderr, False)
        return finish()
    for line in imp.stdout.splitlines():
        if line.startswith("  ") and ":" in line and "New terms" not in line:
            issues.add("INFO", "new term", line.strip())
        elif line.startswith("[warn]") and "changed since the export" not in line:
            # (that one is expected: this preview rewrites the CSV on every run)
            issues.add("WARN", "import workbook", line[6:].strip())
    header, rows = read_csv_rows(full_csv)

    sys.path.insert(0, str(CODE / "shared"))
    sys.path.insert(0, str(CODE / "framework_build"))
    bf = load_module("build_framework_wip", CODE / "framework_build" / "build_framework.py")

    # 2. identity on all rows
    try:
        bf.CORPUS_PATH = corpus
        with contextlib.redirect_stdout(io.StringIO()):
            bf.check_blueprint_identity(pd.read_csv(full_csv, sep=";", dtype=str))
    except ValueError as e:
        issues.add("ERROR", "blueprint identity", e)

    # 3. cells that cannot become an IRI local name
    scheme_cols = [i for i, c in enumerate(header) if c in bf.COLUMN_SCHEMES]
    for r in rows:
        for i in scheme_cols:
            v = r[i].strip()
            if v and bf._INVALID_NOTATION.search(v):
                issues.add("ERROR", "notation", f"{r[0]} | {header[i]}: {v!r} (left out of the preview)")
                r[i] = ""

    # 4. rows with annotation content
    h = {c: i for i, c in enumerate(header)}
    content = [i for c, i in h.items() if c not in PREFILLED]
    status = Counter(r[h["reviewStatus"]] or "(empty)" for r in rows)
    build = [r for r in rows if any(r[i].strip() for i in content)]
    todo = [(r[h["alarmLabel"]], r[h["alarmPriority"]], r[h["Device.Type"]]) for r in rows
            if not any(r[i].strip() for i in content) and r[h["reviewStatus"]].strip() != "EXCL"]
    build_csv = SANDBOX / "wip_build.csv"
    write_csv_rows(build_csv, header, build)
    stats.update({"Rows in workbook": len(rows),
                  "Rows built (have annotation content; EXCL left out by the build)": len(build),
                  "Rows not yet annotated": len(todo),
                  "reviewStatus": ", ".join(f"{k} {v}" for k, v in sorted(status.items()))})

    # 4b. distinct labels whose IRI local names collide: the build gives them _1, _2, ...
    built = pd.DataFrame([dict(zip(header, r)) for r in build
                          if r[h["reviewStatus"]].strip() != "EXCL"], columns=header)
    with contextlib.redirect_stdout(io.StringIO()):
        resolved = bf.assign_unique_iris(built)
    for kind, local_name, ranked in resolved:
        issues.add("INFO", f"{kind} IRI made unique",
                   f"{local_name}: " + ", ".join(f"{lab!r}{'' if n == 0 else f' -> _{n}'}"
                                                  for n, lab in enumerate(ranked)))

    # 5. build into the sandbox
    ok, build_log = run_build(bf, issues, build_csv, corpus)
    # 6. graph view, in workbook order with the Excel row of each archetype
    xl_rows = {_row_key(r[h["alarmLabel"]], r[h["alarmPriority"]], r[h["Device.Type"]]): n + FIRST_ROW
               for n, r in enumerate(rows)}
    comments = workbook_comments(xlsx)
    row_info = {_row_key(r[h["alarmLabel"]], r[h["alarmPriority"]], r[h["Device.Type"]]):
                {"label": r[h["alarmLabel"]].strip(), "priority": r[h["alarmPriority"]],
                 "model": r[h["Device.Type"]], "status": r[h["reviewStatus"]].strip(),
                 "comment": comments.get(n + FIRST_ROW, "")}
                for n, r in enumerate(rows)}
    report_ok, report_log = run_report(issues, xl_rows, row_info) if ok else (False, "")
    write_index(xlsx, stats, issues, todo, build_log + "\n" + report_log, report_ok)
    return finish()


def finish():
    index = SANDBOX / "index.html"
    print(f"[output] {index}")
    if "--no-open" not in sys.argv:
        webbrowser.open(index.as_uri())


if __name__ == "__main__":
    main()
