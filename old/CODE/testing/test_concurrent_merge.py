"""
test_concurrent_merge.py — merge of N random concurrent alarms.

Picks N random alarm types from the catalogue, makes them concurrent on a
single patient, and builds the merged situational graph:

    shared merge anchors  (patient → device(s) → sensors → signals)
  + one AlarmMessage per alarm   (hasMessage → Msg → concernsPatient → patient)
  + merged conditions on the shared sensors/signals   (last-value-wins)
  + inferred operation states   (owlrl: quality ⇒ Enabled, propagated up)
  + entailed clinical context   (metric ⇒ property ⇒ process ⇒ organ ⇒ system,
                                 from the bridge axioms — carried by no alarm)

Outputs, written next to this file (CODE/testing/):
    merged_concurrent.ttl    the merged graph (Turtle)
    merged_concurrent.mmd    a Mermaid diagram for visual inspection
    merged_concurrent.html   a self-contained browser preview (open + refresh)

Run directly:      python3 test_concurrent_merge.py [seed]
As a pytest test:  pytest test_concurrent_merge.py
"""

import random
import sys
from datetime import datetime, timedelta

from rdflib import Graph, URIRef, Literal
from pathlib import Path
from rdflib.namespace import RDF

## resolve to CODE/evaluation_poc for op_knowledge.py, CODE/shared for graph_view.py
sys.path.append(str(Path(__file__).resolve().parent.parent / "evaluation_poc"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "shared"))

import op_knowledge as K
from graph_view import to_mermaid, legend_html

OUT_DIR  = Path(__file__).resolve().parent          # CODE/testing/
OUT_TTL  = OUT_DIR / "merged_concurrent.ttl"
OUT_MMD  = OUT_DIR / "merged_concurrent.mmd"
OUT_HTML = OUT_DIR / "merged_concurrent.html"

PATIENT = "johnDoe_01"


# ── Scenario construction ─────────────────────────────────────────────────────

def device_id_for(kb, label: str) -> str:
    """Device id keyed by the archetype's device type, so alarms on the same
    kind of device share one physical device (and therefore merge on it)."""
    arch = K.archetype_structure(kb, kb.type_index[label])
    device_type = arch.concept(K.MDA.Device)
    dtype = K._local(device_type) if device_type else "Device"
    return f"{dtype}_01"


def concurrent_events(kb, n=4, seed=None):
    """N distinct random alarm types, concurrent on one patient."""
    rng = random.Random(seed)
    labels = rng.sample(sorted(kb.type_index), n)
    t0 = datetime(2026, 7, 17, 8, 0, 0)
    return [
        K.Event(PATIENT, lbl, device_id_for(kb, lbl),
                t0 + timedelta(seconds=30 * i),        # staggered starts …
                t0 + timedelta(minutes=20))            # … all still active at t0+5min
        for i, lbl in enumerate(labels)
    ]


def merged_situation(kb, events, at):
    """The merged situational graph at instant `at` (all events active)."""
    background = K.background_graph(kb, events)
    active = sorted((e for e in events if e.start <= at <= e.end), key=lambda e: e.start)

    g = Graph()
    g += background
    for e in active:
        g += K.alarm_message(kb, e)                    # alarm + message → concernsPatient
    merged = K.merge_conditions([K.alarm_condition(kb, e) for e in active], kb.last_wins)
    g += merged                                        # conditions on shared anchors
    g += K.inferred_states(kb, background + merged)    # owlrl operation-state closure
    g += K.clinical_context(kb, background + merged)   # metric → … → organ system
    return g


# ── Entry point / test ────────────────────────────────────────────────────────

def build(seed=None, n=4):
    kb = K.load_kb()
    events = concurrent_events(kb, n=n, seed=seed)
    at = datetime(2026, 7, 17, 8, 5, 0)               # all active here
    g = merged_situation(kb, events, at)
    return kb, events, g


# ── HTML preview (self-contained; open in a browser and refresh) ──────────────

_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Concurrent alarm merge — seed __SEED__</title>
<style>
  :root{--ground:#f4f7f7;--surface:#fff;--ink:#16231f;--muted:#61726d;--faint:#89a09a;
        --line:#e2ebe8;--accent:#0c7d76;--accent-soft:#e4f2f0;--plate:#fff;--plate-line:#dfe7e5}
  @media (prefers-color-scheme:dark){:root{--ground:#0d1412;--surface:#15201d;--ink:#e9f1ee;
        --muted:#93a8a2;--faint:#6b807a;--line:#23322e;--accent:#37b9ae;--accent-soft:#143430}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);line-height:1.55;
       font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:1120px;margin:0 auto;padding:clamp(1.4rem,4vw,3rem) clamp(1rem,3vw,2rem) 4rem}
  .eyebrow{font-family:ui-monospace,Menlo,monospace;font-size:.72rem;letter-spacing:.16em;
           text-transform:uppercase;color:var(--accent);font-weight:600}
  h1{margin:.4rem 0 .3rem;font-size:clamp(1.6rem,3.4vw,2.3rem);line-height:1.08;
     letter-spacing:-.02em;font-weight:680;max-width:22ch;text-wrap:balance}
  .lede{margin:0;color:var(--muted);max-width:64ch}
  .lede b{color:var(--ink)}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:1.7rem 0}
  .tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;
        padding:.9rem 1rem;display:flex;flex-direction:column;gap:.15rem}
  .tile .n{font-size:1.9rem;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1}
  .tile .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--faint)}
  .tile.hi{border-color:color-mix(in srgb,var(--accent) 45%,var(--line));background:var(--accent-soft)}
  .tile.hi .n{color:var(--accent)}
  .grid2{display:grid;grid-template-columns:1.1fr .9fr;gap:1.3rem;align-items:start}
  @media (max-width:720px){.grid2{grid-template-columns:1fr}}
  h2{font-size:.76rem;text-transform:uppercase;letter-spacing:.12em;color:var(--faint);font-weight:600;margin:0 0 .6rem}
  .alarms{display:flex;flex-direction:column;gap:.5rem;margin:0;padding:0;list-style:none}
  .alarm{display:flex;gap:.6rem;background:var(--surface);border:1px solid var(--line);
         border-left:3px solid #c0392b;border-radius:8px;padding:.5rem .8rem;font-size:.9rem}
  .alarm .dev{margin-left:auto;font-size:.72rem;color:var(--faint);white-space:nowrap}
  .legend{display:flex;flex-wrap:wrap;gap:.4rem .9rem;margin:0;padding:0;list-style:none}
  .legend li{display:flex;align-items:center;gap:.4rem;font-size:.8rem;color:var(--muted)}
  .dot{width:11px;height:11px;border-radius:3px;border:1px solid rgba(0,0,0,.15);flex:none}
  .figure{margin:1.5rem 0 .5rem;background:var(--plate);border:1px solid var(--plate-line);
          border-radius:14px;padding:.6rem;overflow-x:auto}
  .figure pre.mermaid{margin:0;min-width:900px;text-align:center;background:transparent}
  figcaption{font-size:.8rem;color:var(--faint);margin:.5rem .2rem 0}
  .note{margin-top:1.4rem;border:1px solid var(--line);border-radius:12px;background:var(--surface);
        padding:1rem 1.15rem;font-size:.9rem;color:var(--muted)}
  .note b{color:var(--ink)} .note code{font-family:ui-monospace,Menlo,monospace;font-size:.85em;
        color:var(--accent);background:var(--accent-soft);padding:.05em .35em;border-radius:4px}
  .foot{margin-top:2rem;font-size:.76rem;color:var(--faint);font-family:ui-monospace,Menlo,monospace}
</style></head><body><div class="wrap">
  <span class="eyebrow">Situational-awareness merge · test seed __SEED__</span>
  <h1>Concurrent alarms, one coherent patient graph</h1>
  <p class="lede">Randomly drawn alarm types fire at once on patient <b>__PATIENT__</b>.
     Each mints its own message, yet all merge onto the <b>shared</b> patient and device
     entities — a single graph to reason over.</p>
  <div class="stats">__STATS__</div>
  <div class="grid2">
    <div><h2>The alarms</h2><ul class="alarms">__ALARMS__</ul></div>
    <div><h2>Legend</h2><ul class="legend">
      __LEGEND__
    </ul></div>
  </div>
  <figure class="figure"><pre class="mermaid">
__MERMAID__
</pre><figcaption>Merged situational graph — all alarms active. Scroll horizontally to follow the structure.</figcaption></figure>
  <div class="note"><p><b>How to read it.</b> Each alarm (red) links via <code>hasMessage</code> to its
  own message (orange); every message <code>concernsPatient</code> the single shared patient (green) —
  that convergence is the merge. From the patient, <code>isMonitoredBy</code> reaches the device(s)
  (blue) and their sensors/signals (teal). Alarm conditions (purple) attach to the shared sensors;
  where a signal is <code>Impaired</code>, the reasoner infers <code>Enabled</code> (yellow) and
  propagates it signal → sensor → device.</p>
  <p><b>The clinical chain (grey) is entailed, not carried.</b> No alarm states any physiology.
  Each metric concept has one bridge axiom in <code>clinical_axioms.ttl</code>, and the reasoner
  derives <code>approximates</code> → <code>isPropertyOf</code> → <code>presentIn</code> →
  <code>organPartOfSystem</code> from there. Alarms that converge on the same organ are implicating
  the same anatomy — that convergence is the point. A chain that stops early is a gap in the
  clinical knowledge base, not a defect in the alarm.</p></div>
  <p class="foot">test_concurrent_merge.py · seed __SEED__ · regenerate and refresh this page for the latest run.</p>
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({startOnLoad:true,theme:'neutral',securityLevel:'loose',flowchart:{useMaxWidth:false}});</script>
</body></html>"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(g: Graph, events: list, mermaid: str, seed) -> str:
    n_pat = len(set(g.subjects(RDF.type, K.MDA.Patient)))
    n_dev = len(set(g.objects(None, K.MDA.isMonitoredBy)))
    n_en  = len(set(g.subjects(K.MDA.hasOperationState, None)))
    tiles = [("", len(events), "Concurrent alarms"), ("hi", n_pat, "Patient (merged)"),
             ("hi", n_dev, "Devices spanned"), ("hi", n_en, "Entities inferred Enabled"),
             ("", len(g), "Triples")]
    stats = "".join(
        f'<div class="tile {c}"><span class="n">{v}</span><span class="k">{k}</span></div>'
        for c, v, k in tiles)
    alarms = "".join(
        f'<li class="alarm"><span>{_esc(e.label)}</span><span class="dev">{_esc(e.device_id)}</span></li>'
        for e in events)
    return (_HTML_TEMPLATE
            .replace("__SEED__", str(seed)).replace("__PATIENT__", _esc(PATIENT))
            .replace("__STATS__", stats).replace("__ALARMS__", alarms)
            .replace("__LEGEND__", legend_html())
            .replace("__MERMAID__", mermaid))


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    kb, events, g = build(seed=seed)

    print(f"[scenario] seed={seed}  {len(events)} concurrent alarms on {PATIENT}:")
    for e in events:
        print(f"    • {e.label}   (device {e.device_id})")

    mermaid = to_mermaid(g)
    g.serialize(destination=str(OUT_TTL), format="turtle")
    OUT_MMD.write_text(mermaid, encoding="utf-8")
    OUT_HTML.write_text(to_html(g, events, mermaid, seed), encoding="utf-8")

    n_pat = len(set(g.subjects(RDF.type, K.MDA.Patient)))
    n_dev = len(set(g.objects(None, K.MDA.isMonitoredBy)))
    n_enabled = len(set(g.subjects(K.MDA.hasOperationState, None)))
    print(f"[merged]   {len(g)} triples · patients={n_pat} · devices={n_dev} · "
          f"enabled-entities={n_enabled}")
    print(f"[output]   {OUT_TTL.name} · {OUT_MMD.name} · {OUT_HTML.name}")
    print(f"[preview]  open {OUT_HTML}")


# pytest entry: the graph must merge to a single patient
def test_merges_to_one_patient():
    _, events, g = build(seed=7)
    assert len(set(g.subjects(RDF.type, K.MDA.Patient))) == 1
    assert all(len(list(g.objects(m, K.MDA.concernsPatient))) == 1
               for m in g.subjects(RDF.type, K.MDA.AlarmMessage))


if __name__ == "__main__":
    main()
