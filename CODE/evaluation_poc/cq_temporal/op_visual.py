"""
op_visual.py — an operational view of situational awareness over time.

Same simulation as op_inference.py, rendered for a human instead of for a
triple store.  Both drive K.Timeline, so the picture on screen is the same
situation that gets written to kg_inference.trig — it cannot show you something
the reasoner did not actually derive.

Produces one self-contained page:

    op_visual.html    pick a patient, step through time, watch the situational
                      graph appear, merge, decay and empty out.

The page bakes in every tick for every patient, so it needs no server and no
Python once written.  Re-run this script after changing events, the ontology,
the clinical axioms, or the alarm catalogue.

Usage
-----
  python3 op_visual.py             build, then open in the default browser
  python3 op_visual.py --no-open   build only (for scripted runs)
"""

import json
import sys
import webbrowser
from datetime import timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "shared"))
sys.path.append(str(Path(__file__).resolve().parent.parent / "core"))

import op_knowledge as K
from graph_view import to_mermaid, legend_html

OUT = K.ROOT / "EVALUATION" / "MDA-POC" / "op_visual.html"


# ── Simulation → view model ───────────────────────────────────────────────────

def tick_view(kb: K.KB, tl: K.Timeline, t) -> dict:
    """
    Everything the page needs to render one instant.

    The diagram draws anchors + situation, not the situation alone.
    op_inference.py states the background once in the TriG default graph and
    keeps each tick's named graph to just the delta — correct for storage, but
    it leaves the picture in disconnected pieces, because the anchors
    (patient → device → sensor → signal) are exactly the edges that join an
    alarm's condition to the patient it concerns.

    Those anchors come from `background_at`, so only the sensors and signals the
    ACTIVE alarms implicate are drawn.  The timeline's full background is the
    accumulation over the whole stream; showing it here would put an ECG sensor
    on screen while nothing but an SpO2 alarm is firing, which is knowledge about
    the deployment rather than about this instant.

    Two diagrams are built per tick — "situational" (mda:situational-tagged,
    what kg_inference.trig actually carries) and "all" (everything the
    reasoner entailed reachable from this tick's nodes, including
    background/taxonomic facts mda:situational keeps out of the export) — so
    the page can toggle between them without re-reasoning. "all" is always a
    superset; the organ/system chip lists below still read off "situational"
    since that is what the reasoner-derived clinical chain actually is,
    regardless of which diagram is on screen.
    """
    situation = tl.situation_at(t, mode="situational")
    situation_all = tl.situation_at(t, mode="all")
    anchors = tl.background_at(t)
    shown = anchors + situation
    shown_all = anchors + situation_all
    active = tl.active_at(t)
    enabled = sorted({K._local(s) for s in
                      situation.subjects(K.MDA.hasOperationState, None)})
    # Classified by which class the node's concept instantiates (see
    # K.nodes_of_class), not by which clinical-chain property reached it — so a
    # new or renamed edge into Organ/OrganSystem needs no change here.
    organs = sorted(K._local(n) for n in K.nodes_of_class(kb, situation, K.MDA.Organ))
    systems = sorted(K._local(n) for n in K.nodes_of_class(kb, situation, K.MDA.OrganSystem))
    return {
        "iso":     t.isoformat(),
        "clock":   t.strftime("%H:%M:%S"),
        "state":   tl.state_label(t, situation),
        "active":  [{"label": e.label, "device": e.device_id} for e in active],
        "triples": len(situation),          # this tick's own situational content
        "triples_all": len(situation_all),  # this tick's full entailed content
        "context": len(anchors),            # anchors the active alarms reveal
        "enabled": enabled,
        "organs":  organs,
        "systems": systems,
        "mmd":     to_mermaid(shown),
        "mmd_all": to_mermaid(shown_all),
    }


# Above this many alarms, per-alarm ribbon rows stop being readable and the
# ribbon shows a concurrency profile instead.
ROWS_MAX = 18
PROFILE_SAMPLES = 600


def patient_view(kb: K.KB, patient: str, events: list, reasoning_cache: dict = None) -> dict:
    tl = K.build_timeline(kb, patient, events, reasoning_cache=reasoning_cache)
    span_start, span_end = tl.ticks[0], tl.ticks[-1]
    total = max((span_end - span_start).total_seconds(), 1)

    def pct(dt):
        return round((dt - span_start).total_seconds() / total * 100, 4)

    # Per-alarm bars, only while few enough to read.
    bars = []
    if len(tl.events) <= ROWS_MAX:
        bars = [{"label": e.label.split(" - ")[-1],
                 "full": e.label,
                 "left": pct(e.start),
                 "width": max(pct(e.end) - pct(e.start), 0.6),
                 "decay_left": pct(e.end),
                 "decay_width": max(pct(e.end + tl.window) - pct(e.end), 0.6)}
                for e in sorted(tl.events, key=lambda e: e.start)]

    # Concurrency profile — how many alarms are simultaneously active, sampled
    # across the span.  This is what stays readable at hundreds of alarms.
    step = total / PROFILE_SAMPLES
    profile, peak = [], 0
    for i in range(PROFILE_SAMPLES):
        at = span_start + timedelta(seconds=i * step)
        n = sum(1 for e in tl.events if e.start <= at <= e.end)
        peak = max(peak, n)
        profile.append(n)

    return {
        "patient": patient,
        "events":  len(tl.events),
        "window":  int(tl.window.total_seconds()),
        "span":    f"{span_start:%H:%M:%S} … {span_end:%H:%M:%S}",
        "date":    f"{span_start:%Y-%m-%d}",
        "bars":    bars,
        "profile": profile,
        "peak":    peak,
        "ticks":   [dict(tick_view(kb, tl, t), pos=pct(t)) for t in tl.ticks],
    }


# ── Page ──────────────────────────────────────────────────────────────────────

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Situational awareness — operational view</title>
<style>
  :root{--ground:#f4f7f7;--surface:#fff;--ink:#16231f;--muted:#61726d;--faint:#89a09a;
        --line:#e2ebe8;--accent:#0c7d76;--accent-soft:#e4f2f0;--plate:#fff;
        --bar:#c0392b;--bar-soft:#f2c9c4;--shadow:0 1px 2px rgba(0,0,0,.05)}
  @media (prefers-color-scheme:dark){:root{--ground:#0d1412;--surface:#15201d;--ink:#e9f1ee;
        --muted:#93a8a2;--faint:#6b807a;--line:#23322e;--accent:#37b9ae;--accent-soft:#143430;
        --plate:#f7fbfa;--bar:#e0685a;--bar-soft:#4a2a26;--shadow:none}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);line-height:1.5;
       font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:1240px;margin:0 auto;padding:clamp(1.2rem,3vw,2.2rem) clamp(1rem,3vw,2rem) 4rem}
  .eyebrow{font-family:ui-monospace,Menlo,monospace;font-size:.7rem;letter-spacing:.16em;
           text-transform:uppercase;color:var(--accent);font-weight:600}
  h1{margin:.3rem 0 .2rem;font-size:clamp(1.5rem,3vw,2.1rem);line-height:1.1;
     letter-spacing:-.02em;font-weight:680}
  .lede{margin:0 0 1.4rem;color:var(--muted);max-width:70ch;font-size:.94rem}

  .bar{display:flex;flex-wrap:wrap;gap:.7rem;align-items:center;background:var(--surface);
       border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem;box-shadow:var(--shadow)}
  label.f{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--faint);
          font-weight:600;margin-right:.35rem}
  select,button{font:inherit;color:var(--ink);background:var(--surface);
       border:1px solid var(--line);border-radius:8px;padding:.4rem .7rem;cursor:pointer}
  select:hover,button:hover{border-color:var(--accent)}
  button:disabled{opacity:.35;cursor:not-allowed;border-color:var(--line)}
  button.pri{background:var(--accent-soft);border-color:color-mix(in srgb,var(--accent) 40%,var(--line));
             color:var(--accent);font-weight:600;min-width:5.4rem}
  .clock{font-family:ui-monospace,Menlo,monospace;font-size:1.5rem;font-weight:680;
         letter-spacing:-.01em;font-variant-numeric:tabular-nums}
  .date{font-family:ui-monospace,Menlo,monospace;font-size:.72rem;color:var(--faint)}
  .spacer{flex:1}

  .ribbon{margin:1rem 0 .2rem;background:var(--surface);border:1px solid var(--line);
          border-radius:12px;padding:.9rem 1rem .7rem;box-shadow:var(--shadow)}
  .lanes{position:relative}
  .profile{position:relative;left:140px;width:calc(100% - 140px);height:46px;display:flex;
           align-items:flex-end;gap:0;margin-bottom:.5rem}
  .profile i{flex:1;background:var(--accent);opacity:.55;min-height:0}
  .profile i.z{opacity:.12}
  .rows{position:relative;display:flex;flex-direction:column;gap:.32rem}
  .row{position:relative;height:22px}
  .row .name{position:absolute;left:0;top:0;font-size:.74rem;color:var(--muted);
             line-height:22px;width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .track{position:absolute;left:140px;right:0;top:0;height:22px;
         background:linear-gradient(var(--line),var(--line)) center/100% 1px no-repeat}
  .span{position:absolute;top:3px;height:16px;border-radius:4px;background:var(--bar);opacity:.85}
  .decay{position:absolute;top:3px;height:16px;border-radius:4px;background:var(--bar-soft)}
  .head{position:absolute;top:-6px;bottom:-6px;width:2px;background:var(--accent);z-index:5}
  .head::after{content:"";position:absolute;top:-4px;left:-4px;width:10px;height:10px;
               border-radius:50%;background:var(--accent)}
  .headwrap{position:absolute;left:140px;right:0;top:0;bottom:0;pointer-events:none}
  input[type=range]{width:100%;margin:.9rem 0 0;accent-color:var(--accent)}

  .cols{display:grid;grid-template-columns:1fr 300px;gap:1rem;margin-top:1rem;align-items:start}
  @media (max-width:900px){.cols{grid-template-columns:1fr}}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;
         padding:.9rem 1rem;box-shadow:var(--shadow)}
  h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.11em;color:var(--faint);
     font-weight:600;margin:0 0 .55rem}
  .figure{background:var(--plate);border:1px solid var(--line);border-radius:12px;
          padding:.5rem;overflow:auto;min-height:340px;box-shadow:var(--shadow)}
  .figure svg{max-width:none}
  .state{display:inline-block;font-size:.78rem;padding:.15rem .55rem;border-radius:99px;
         background:var(--accent-soft);color:var(--accent);font-weight:600}
  ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:.35rem}
  .al{font-size:.83rem;border-left:3px solid var(--bar);padding:.25rem .5rem;
      background:var(--ground);border-radius:0 6px 6px 0}
  .al .dev{display:block;font-size:.7rem;color:var(--faint);font-family:ui-monospace,Menlo,monospace}
  .chips{display:flex;flex-wrap:wrap;gap:.3rem}
  .chip{font-size:.75rem;padding:.12rem .5rem;border-radius:99px;border:1px solid var(--line);
        background:var(--ground);color:var(--muted)}
  .chip.on{border-color:#b7950b;background:#fdf2b3;color:#6b5900}
  .empty{color:var(--faint);font-size:.83rem;font-style:italic}
  .kv{display:flex;justify-content:space-between;font-size:.8rem;padding:.18rem 0;color:var(--muted)}
  .kv b{color:var(--ink);font-variant-numeric:tabular-nums}
  .legend{display:flex;flex-wrap:wrap;gap:.35rem .8rem;margin:.7rem 0 0;padding:0;list-style:none}
  .legend li{display:flex;align-items:center;gap:.35rem;font-size:.75rem;color:var(--muted)}
  .dot{width:11px;height:11px;border-radius:3px;border:1px solid;flex:none}
  .hint{font-size:.72rem;color:var(--faint);margin:.6rem 0 0;font-family:ui-monospace,Menlo,monospace}
</style></head><body><div class="wrap">
  <span class="eyebrow">Operational view · __NPAT__ patient(s) · __NEV__ event(s)</span>
  <h1>Situational awareness over time</h1>
  <p class="lede">The same simulation <code>op_inference.py</code> writes to
     <code>kg_inference.trig</code>, stepped one state change at a time. Each frame is the
     situational graph at that instant — alarms appearing, conditions merging onto the shared
     patient and device, operation state persisting after an alarm ends, then decaying to nothing.
     <b>Situational</b> shows exactly what gets exported; <b>All entailed</b> shows everything the
     reasoner actually derived at that instant, including background/taxonomic facts (e.g. an
     entailed superclass) the export deliberately leaves out.</p>

  <div class="bar">
    <span><label class="f">Patient</label><select id="pat"></select></span>
    <span><label class="f">View</label><select id="mode">
      <option value="situational">Situational</option>
      <option value="all">All entailed</option>
    </select></span>
    <span class="spacer"></span>
    <button id="first" title="First tick">⏮</button>
    <button id="prev"  title="Previous tick (←)">◀</button>
    <button id="play"  class="pri" title="Play / pause (space)">▶ Play</button>
    <button id="next"  title="Next tick (→)">▶</button>
    <button id="last"  title="Last tick">⏭</button>
    <span class="spacer"></span>
    <span style="text-align:right">
      <span class="clock" id="clock">--:--:--</span><br>
      <span class="date"  id="date"></span>
    </span>
  </div>

  <div class="ribbon">
    <div class="lanes">
      <div class="profile" id="profile" title="simultaneously active alarms"></div>
      <div class="rows" id="rows"></div>
      <div class="headwrap"><div class="head" id="head" style="left:0%"></div></div>
    </div>
    <input type="range" id="scrub" min="0" value="0" step="1">
    <p class="hint" id="hint">← → step · space play/pause</p>
  </div>

  <div class="cols">
    <div>
      <div class="figure"><div id="diagram"></div></div>
      <ul class="legend">__LEGEND__</ul>
    </div>
    <div style="display:flex;flex-direction:column;gap:1rem">
      <div class="panel">
        <h2>At this instant</h2>
        <span class="state" id="state">—</span>
        <div class="kv" style="margin-top:.5rem"><span>Tick</span><b id="tickno">–</b></div>
        <div class="kv"><span id="triplesLabel">Situational triples</span><b id="triples">0</b></div>
        <div class="kv"><span>Anchors revealed</span><b id="context">0</b></div>
      </div>
      <div class="panel"><h2>Active alarms</h2><ul id="alarms"></ul></div>
      <div class="panel"><h2>Inferred enabled</h2><div class="chips" id="enabled"></div></div>
      <div class="panel"><h2>Implicated anatomy</h2><div class="chips" id="anat"></div></div>
    </div>
  </div>
</div>
<script>const DATA = __DATA__, POOL = __POOL__;</script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
mermaid.initialize({startOnLoad:false, theme:'neutral', securityLevel:'loose',
                    flowchart:{useMaxWidth:false}});

const $ = id => document.getElementById(id);
let pid = Object.keys(DATA)[0], idx = 0, seq = 0, timer = null, mode = 'situational';

function buildPatients(){
  $('pat').innerHTML = Object.keys(DATA).map(p =>
    `<option value="${p}">${p} — ${DATA[p].events} event(s)</option>`).join('');
  $('pat').value = pid;
}

function buildRibbon(){
  const P = DATA[pid];
  const peak = Math.max(P.peak, 1);
  $('profile').innerHTML = P.profile.map(n =>
    `<i class="${n?'':'z'}" style="height:${n?Math.round(n/peak*100):4}%"></i>`).join('');
  $('rows').innerHTML = P.bars.map(b => `
    <div class="row">
      <span class="name" title="${b.full}">${b.label}</span>
      <span class="track">
        <span class="decay" style="left:${b.decay_left}%;width:${b.decay_width}%"></span>
        <span class="span"  style="left:${b.left}%;width:${b.width}%"></span>
      </span>
    </div>`).join('');
  $('hint').textContent = P.bars.length
    ? '← → step · space play/pause · bars above = concurrent alarms · solid = active, pale = post-alarm window'
    : `← → step · space play/pause · bars = simultaneously active alarms (peak ${P.peak}); ` +
      `${P.events} alarms is too many to list individually`;
  $('scrub').max = P.ticks.length - 1;
}

function chips(el, items, on){
  el.innerHTML = items.length
    ? items.map(x => `<span class="chip${on?' on':''}">${x}</span>`).join('')
    : '<span class="empty">none</span>';
}

async function draw(){
  const P = DATA[pid], T = P.ticks[idx];
  const situational = mode === 'situational';
  $('clock').textContent = T.clock;
  $('date').textContent  = P.date + ' · ' + P.span;
  $('state').textContent = T.state;
  $('tickno').textContent = (idx+1) + ' / ' + P.ticks.length;
  $('triplesLabel').textContent = situational ? 'Situational triples' : 'All entailed triples';
  $('triples').textContent = situational ? T.triples : T.triples_all;
  $('context').textContent = T.context;
  $('scrub').value = idx;
  $('head').style.left = T.pos + '%';
  $('alarms').innerHTML = T.active.length
    ? T.active.map(a => `<li class="al">${a.label}<span class="dev">${a.device}</span></li>`).join('')
    : '<li class="empty">no active alarm</li>';
  chips($('enabled'), T.enabled, true);
  chips($('anat'), T.organs.concat(T.systems), false);
  $('first').disabled = $('prev').disabled = idx === 0;
  $('last').disabled  = $('next').disabled = idx === P.ticks.length - 1;
  const {svg} = await mermaid.render('m' + (seq++), POOL[situational ? T.ms : T.mf]);
  $('diagram').innerHTML = svg;
}

function go(i){
  const n = DATA[pid].ticks.length;
  idx = Math.max(0, Math.min(n - 1, i));
  if (idx === n - 1) stop();
  draw();
}
function stop(){ if(timer){ clearInterval(timer); timer = null; $('play').textContent = '▶ Play'; } }
function play(){
  if (timer) return stop();
  if (idx === DATA[pid].ticks.length - 1) idx = 0;
  $('play').textContent = '❚❚ Pause';
  timer = setInterval(() => go(idx + 1), 1100);
}

$('pat').onchange = e => { stop(); pid = e.target.value; idx = 0; buildRibbon(); draw(); };
$('mode').onchange = e => { mode = e.target.value; draw(); };
$('first').onclick = () => { stop(); go(0); };
$('prev').onclick  = () => { stop(); go(idx - 1); };
$('next').onclick  = () => { stop(); go(idx + 1); };
$('last').onclick  = () => { stop(); go(DATA[pid].ticks.length - 1); };
$('play').onclick  = play;
$('scrub').oninput = e => { stop(); go(+e.target.value); };
addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft')  { stop(); go(idx - 1); }
  if (e.key === 'ArrowRight') { stop(); go(idx + 1); }
  if (e.key === ' ')          { e.preventDefault(); play(); }
});

buildPatients(); buildRibbon(); draw();
</script>
</body></html>"""


def main() -> None:
    kb = K.load_kb()
    events = K.load_events()
    groups = K.group_by_patient(events)

    unknown = sorted({e.label for e in events if e.label not in kb.type_index})
    if unknown:
        print(f"[WARN]  {len(unknown)} label(s) with no AlarmType — skipped: {unknown}")

    print(f"[events] {len(events)} event(s), {len(groups)} patient(s); "
          f"post-alarm window = {int(kb.window.total_seconds())}s")

    reasoning_cache = {}
    data = {}
    for patient, evs in groups.items():
        view = patient_view(kb, patient, evs, reasoning_cache=reasoning_cache)
        data[patient] = view
        frames = len(view["ticks"])
        drawn = sum(1 for t in view["ticks"] if t["triples"])
        print(f"  {patient:<14} {view['events']:>4} event(s)  {frames:>4} frame(s)  "
              f"({drawn} with content)  peak {view['peak']} concurrent  {view['span']}")

    # Diagrams repeat heavily — the same set of concurrent archetypes yields the
    # same graph — so they are pooled and referenced by index rather than
    # inlined once per frame. Both the situational and "all entailed" variant
    # go into the SAME pool (deduplication does not care which mode a diagram
    # came from — an idle tick's two diagrams are often identical), so the
    # page can toggle modes by swapping which pooled index a frame points at.
    pool, index = [], {}

    def pooled(mmd):
        if mmd not in index:
            index[mmd] = len(pool)
            pool.append(mmd)
        return index[mmd]

    for view in data.values():
        for t in view["ticks"]:
            t["ms"] = pooled(t.pop("mmd"))
            t["mf"] = pooled(t.pop("mmd_all"))
    total_frames = sum(len(v["ticks"]) for v in data.values())
    print(f"\n[diagrams] {len(pool)} unique across {2 * total_frames} frame-view(s) "
          f"({100 - 100 * len(pool) // max(2 * total_frames, 1)}% deduplicated)")

    page = (_PAGE
            .replace("__DATA__", json.dumps(data, separators=(",", ":")))
            .replace("__POOL__", json.dumps(pool, separators=(",", ":")))
            .replace("__LEGEND__", legend_html())
            .replace("__NPAT__", str(len(groups)))
            .replace("__NEV__", str(len(events))))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")

    size_kb = len(page) // 1024
    print(f"\n[output] {OUT.name}  ({size_kb} KB, self-contained)")

    if "--no-open" in sys.argv:
        print(f"[open]   {OUT}")
    else:
        webbrowser.open(OUT.as_uri())
        print(f"[open]   launched in your browser — {OUT}")


if __name__ == "__main__":
    main()
