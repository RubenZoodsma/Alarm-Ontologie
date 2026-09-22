"""
make_figure.py — the whole of Fig. 1 (MDA proof-of-concept), as vector.

Panels 1-5 and the caption redraw the existing figure; boxes A-C carry the
worked examples from make_panels.py (CAT1a, CAT2a, CAT3a).

Page: A4 landscape (297 x 210 mm). The figure is laid out on a 2400-unit
wide canvas and scaled, centred, into the page minus SETTINGS["margin_mm"].

Outputs (next to this script): fig1_mda_poc.svg, and — when rsvg-convert is
installed (brew install librsvg) — fig1_mda_poc.pdf and a 600 dpi PNG.

Run from VS Code; edit SETTINGS below. The framework figures in panel 2 are
the ones on the current figure; see SETTINGS for the counts as of 2026-09-21.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import make_panels as MP

SETTINGS = {
    "out_dir": Path(__file__).parent,
    "font": MP.SETTINGS["font"],
    "page_mm": (297, 210),          # A4 landscape
    "margin_mm": 8,
    # Panel 2 figures, as on the current figure. Counted from FRAMEWORK/ on
    # 2026-09-21: 21 classes, 43 object/datatype properties (+9 annotation),
    # 306 SKOS concepts, 94 alarm types.
    "owl": "(20 classes, 35 properties)",
    "skos": "(275 concepts)",
    "kb": "(92 alarms, 78% coverage)",
    "enrichment": "(10 → 23 triples/alarm)",
    "caption": True,
    "png_dpi": 600,
}

W = 2400                                # layout canvas width (units)
NAVY, MUTED, LINE = MP.NAVY, MP.MUTED, MP.LINE
BOX_STROKE, BOX_FILL = "#AEB8C8", "#FFFFFF"
GREEN, GREEN_BG, GREEN_LINE = "#3E8E5A", "#F1F7F2", "#9CCAA9"
BLUE_BG, BLUE_LINE = "#F2F6FC", "#B9C8DE"
RED_BELL, BLUE_BELL = "#D2463E", "#3264AE"
T_TITLE, T_BODY, T_SMALL = 26.5, 21.2, 19.4
BELL = ("M-10 7 L-10 -1 C-10 -9 -5 -13 0 -13 C5 -13 10 -9 10 -1 L10 7 L13 10 L-13 10 Z "
        "M-3.5 13 A3.5 3.5 0 0 0 3.5 13 Z")

# Top band: x, y, width, height of each panel.
BAND = {
    "p1": (0, 250, 380, 455),
    "p2": (470, 250, 420, 455),
    "p3": (1060, 270, 360, 435),
    "p4": (1480, 270, 330, 435),
    "p5": (1880, 0, 520, 705),
}
BAND["graph"] = (BAND["p3"][0], 0, BAND["p4"][0] + BAND["p4"][2] - BAND["p3"][0], 215)
OUT_GAP, OUT_W, PANEL_W = 30, 780, 700   # outcome boxes A-C, and the panel inside each

el = []


def text(x, y, s, size=T_BODY, weight=400, fill=NAVY, anchor="start"):
    el.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
              f'fill="{fill}" text-anchor="{anchor}">{s}</text>')


def lines(x, y, rows, size=T_BODY, weight=400, fill=NAVY, anchor="start", lh=None):
    lh = lh or size * 1.12
    for i, s in enumerate(rows):
        text(x, y + i * lh, s, size, weight, fill, anchor)


def rect(x, y, w, h, fill=BOX_FILL, stroke=BOX_STROKE, r=16, sw=2.4, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    el.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" fill="{fill}" '
              f'stroke="{stroke}" stroke-width="{sw}"{d}/>')


def arrow(pts, sw=2.6, color=MUTED):
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    el.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" '
              f'stroke-linejoin="round" marker-end="url(#arr-main)"/>')


def badge(cx, cy, n, fill=NAVY, r=19):
    el.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"/>')
    text(cx, cy + 8, n, size=23, weight=700, fill="#FFFFFF", anchor="middle")


def bell(cx, cy, fill, s=1.0, opacity=1.0):
    el.append(f'<path d="{BELL}" transform="translate({cx:.1f},{cy:.1f}) scale({s})" fill="{fill}" '
              f'opacity="{opacity}"/>')


def person(cx, cy, s=1.0, fill=NAVY):
    el.append(f'<g transform="translate({cx},{cy}) scale({s})" fill="none" stroke="{fill}" '
              f'stroke-width="3.2"><circle cx="0" cy="-14" r="10"/>'
              f'<path d="M-20 24 C-20 6 -10 0 0 0 C10 0 20 6 20 24"/>'
              f'<path d="M-6 10 h12 M0 4 v12" stroke-width="2.6"/></g>')


def cylinder(cx, cy, w=36, h=40, fill=NAVY):
    rx, ry = w / 2, 6
    el.append(f'<g fill="{fill}"><path d="M{cx - rx} {cy - h / 2} v{h} a{rx} {ry} 0 0 0 {w} 0 v{-h} z"/>'
              f'<ellipse cx="{cx}" cy="{cy - h / 2}" rx="{rx}" ry="{ry}" fill="#FFFFFF" stroke="{fill}" '
              f'stroke-width="3"/>'
              f'<path d="M{cx - rx} {cy - 4} a{rx} {ry} 0 0 0 {w} 0 M{cx - rx} {cy + 8} a{rx} {ry} 0 0 0 {w} 0" '
              f'fill="none" stroke="#FFFFFF" stroke-width="2"/></g>')


def box_header(x, y, n, title_rows, sub_rows, badge_fill=NAVY):
    badge(x + 32, y + 34, n, badge_fill)
    lines(x + 60, y + 43, title_rows, T_TITLE, 700)
    ys = y + 43 + len(title_rows) * T_TITLE * 1.1
    lines(x + 60, ys, sub_rows, T_SMALL, 400, MUTED)
    return ys + len(sub_rows) * T_SMALL * 1.12


def center(key):
    x, y, w, h = BAND[key]
    return x + w / 2


# ─── panel 1 · alarm stream ──────────────────────────────────────────────
def panel1(x, y, w, h):
    rect(x, y, w, h)
    box_header(x, y, "1", ["Alarm stream"], ["Per patient, real-world data"])
    mx, my = x + 22, y + 118
    rect(mx, my, 150, 96, fill="#F7F9FC", stroke=NAVY, r=8, sw=3)
    k = 150 / 132
    pts = [(10, 52), (40, 52), (50, 34), (60, 72), (72, 20), (82, 58), (90, 52), (122, 52)]
    el.append(f'<polyline points="{" ".join(f"{mx + a * k:.1f},{my + b:.1f}" for a, b in pts)}" '
              f'fill="none" stroke="{GREEN}" stroke-width="3.2" stroke-linejoin="round"/>')
    el.append(f'<path d="M{mx + 75} {my + 96} v14 M{mx + 50} {my + 112} h50" stroke="{NAVY}" '
              f'stroke-width="3.2" stroke-linecap="round"/>')
    ax = x + 210
    for i in range(3):
        yy = my + 22 + i * 30
        el.append(f'<line x1="{ax}" y1="{yy - 6}" x2="{ax + 22}" y2="{yy - 6}" stroke="{LINE}" stroke-width="2.4"/>')
        el.append(f'<text x="{ax + 32}" y="{yy}" font-size="{T_BODY}" fill="{NAVY}">Alarm'
                  f'<tspan font-size="14" dy="5">{i + 1}</tspan></text>')
    text(ax + 32, my + 114, "…", T_BODY, 700)
    by = y + 290
    for rows in (["Multi-manufacturer"], ["Time-stamped, in sequence"],
                 ["Clinical context", "(vitals, devices, …)"]):
        el.append(f'<circle cx="{x + 26}" cy="{by - 6}" r="3.5" fill="{NAVY}"/>')
        lines(x + 40, by, rows, T_SMALL)
        by += len(rows) * 22 + 16


# ─── panel 2 · MDA semantic framework ────────────────────────────────────
def panel2(x, y, w, h):
    rect(x, y, w, h, fill=GREEN_BG, stroke=GREEN_LINE)
    ys = box_header(x, y, "2", ["MDA semantic framework"],
                    ["Explicit, machine-interpretable", "alarm knowledge"], GREEN)
    ix, iy = x + 16, ys + 12
    ih = y + h - 16 - iy
    rect(ix, iy, w - 32, ih, fill="#FFFFFF", stroke=GREEN, r=10, sw=2, dash="7 5")
    items = [("owl", "OWL ontology", SETTINGS["owl"]), ("skos", "SKOS vocabulary", SETTINGS["skos"]),
             ("kb", "Knowledge base", SETTINGS["kb"])]
    step = ih / 3
    for i, (kind, title, sub) in enumerate(items):
        yy = iy + step * i + step / 2 - 4
        cx, cy = ix + 34, yy + 2
        if kind == "owl":
            el.append(f'<g stroke="{NAVY}" stroke-width="3" fill="#FFFFFF">'
                      f'<line x1="{cx - 10}" y1="{cy + 10}" x2="{cx}" y2="{cy - 12}"/>'
                      f'<line x1="{cx}" y1="{cy - 12}" x2="{cx + 12}" y2="{cy + 10}"/>'
                      f'<circle cx="{cx}" cy="{cy - 12}" r="7"/><circle cx="{cx - 12}" cy="{cy + 12}" r="7"/>'
                      f'<circle cx="{cx + 13}" cy="{cy + 12}" r="7"/></g>')
        elif kind == "skos":
            cylinder(cx, cy, 30, 30)
        else:
            el.append(f'<g stroke="{NAVY}" stroke-width="3" fill="#FFFFFF">'
                      f'<path d="M{cx - 12} {cy - 17} h16 l8 8 v26 h-24 z"/>'
                      f'<path d="M{cx - 6} {cy - 2} h12 M{cx - 6} {cy + 6} h12" stroke-width="2.4"/></g>')
        text(ix + 66, yy - 2, title, T_BODY, 700)
        text(ix + 66, yy + 22, sub, T_SMALL, 400, MUTED)


# ─── situational graph ───────────────────────────────────────────────────
def situational(x, y, w, h):
    rect(x, y, w, h)
    text(x + w / 2, y + 36, "Patient situational graph", T_TITLE, 700, anchor="middle")
    tw = 230
    px, py, pw, ph = x + 24, y + 56, w - 48 - tw - 24, 118
    rect(px, py, pw, ph, fill="#F7F9FC", stroke="#DCE2EC", r=8, sw=1.6)
    frac = [(.1, .66), (.27, .34), (.42, .73), (.6, .25), (.76, .6), (.9, .37)]
    pts = [(px + a * pw, py + b * ph) for a, b in frac]
    el.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="none" '
              f'stroke="{LINE}" stroke-width="2.4"/>')
    el.append(f'<path d="M{pts[1][0]:.1f} {pts[1][1]:.1f} L{pts[3][0]:.1f} {pts[3][1]:.1f} '
              f'M{pts[2][0]:.1f} {pts[2][1]:.1f} L{pts[4][0]:.1f} {pts[4][1]:.1f}" stroke="{LINE}" '
              f'stroke-width="1.8" stroke-dasharray="4 4" fill="none"/>')
    for (a, b), c in zip(pts, [NAVY, BLUE_BELL, RED_BELL, NAVY, GREEN, "#7652AE"]):
        el.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="8" fill="{c}"/>')
    arrow([(px + 16, py + ph - 14), (px + pw - 16, py + ph - 14)], sw=2)
    text(px + 16, py + ph + 22, "time", T_SMALL, 400, MUTED)
    tx = px + pw + 24
    lines(tx, py + 34, ["Structural facts", 'persist <tspan font-weight="700">15 min</tspan>'], T_SMALL)
    lines(tx, py + 92, ["Interpretive facts persist", "for alarm duration"], T_SMALL)


# ─── panel 3 · context-aware reasoning ───────────────────────────────────
def panel3(x, y, w, h):
    rect(x, y, w, h)
    ys = box_header(x, y, "3", ["Context-aware", "reasoning"], ["Continuous SPARQL application"])
    ix, iy = x + 16, ys + 12
    ih = y + h - 16 - iy
    rect(ix, iy, w - 32, ih, fill="#F4F6FA", stroke="#DCE2EC", r=10, sw=1.6)
    for i, row in enumerate(["Alarm knowledge (MDA)", "Clinical context", "Temporal, multi-alarm reasoning"]):
        by = iy + ih * (i + 0.5) / 3 + 6
        el.append(f'<circle cx="{ix + 20}" cy="{by - 6}" r="3.5" fill="{NAVY}"/>')
        text(ix + 34, by, row, T_SMALL)


# ─── panel 4 · update situational awareness ──────────────────────────────
def panel4(x, y, w, h):
    rect(x, y, w, h)
    ys = box_header(x, y, "4", ["Update situational", "awareness"],
                    ["Merge rule outcomes into", "persistent graph"])
    cylinder(x + w / 2, ys + 44, 44, 44)
    lines(x + w / 2, ys + 104, ["Patient-centered", "situational knowledge"], T_SMALL, 700, anchor="middle")
    text(x + w / 2, ys + 150, "(accumulates over time)", T_SMALL, 400, MUTED, anchor="middle")


# ─── panel 5 · clinician view ────────────────────────────────────────────
def panel5(x, y, w, h):
    rect(x, y, w, h, fill=BLUE_BG, stroke=BLUE_LINE)
    ys = box_header(x, y, "5", ["Clinician view"], ["From alarm stream to actionable information"])
    sx, sw_ = x + 16, w - 32
    sub_h = (y + h - 16 - ys - 22) / 2
    n_bells = int((sw_ - 130) // 38)
    for i, (title, note, many) in enumerate([
        ("Device-centered alarm management", ["Exposed to the full RDF alarm stream"], True),
        ("Patient-centered alarm management", ["Exposed to fewer, contextualized alarms",
                                                "and interpretations"], False),
    ]):
        sy = ys + 12 + i * (sub_h + 12)
        rect(sx, sy, sw_, sub_h, fill="#FFFFFF", stroke=BLUE_LINE, r=10, sw=1.8)
        text(sx + 16, sy + 34, title, T_BODY, 700)
        cy = sy + sub_h / 2 - 4
        person(sx + 46, cy + 8)
        if many:
            for r_ in range(2):
                for c in range(n_bells):
                    bell(sx + 104 + c * 38, cy - 10 + r_ * 38, RED_BELL, 1.05)
        else:
            bell(sx + 104, cy + 8, BLUE_BELL, 1.2)
            bell(sx + 144, cy + 8, BLUE_BELL, 1.05, .45)
            bell(sx + 182, cy + 8, BLUE_BELL, 1.05, .25)
        lines(sx + 16, sy + sub_h - 20 - (len(note) - 1) * 21.7, note, T_SMALL, 400, MUTED)


# ─── outcome boxes A-C ───────────────────────────────────────────────────
OUTCOMES = [
    ("A", "Likely false positives", "Technical or physiological conditions invalidate alarm", MP.panel_a,
     "#FDF1F0", "#EFC3BF"),
    ("B", "Likely redundant information", "Multiple alarms convey the same underlying meaning", MP.panel_b,
     "#EFF4FB", "#BFD0EA"),
    ("C", "Emergent combined meaning", "Combination of alarms reveals higher-level clinical meaning",
     MP.panel_c, "#F5F1FB", "#D5C6EC"),
]
PANEL_SCALE = PANEL_W / MP.W
PANEL_TOP = 96
defs_extra = []


def outcome_height():
    return PANEL_TOP + MP.H * PANEL_SCALE + 22


def outcome(x, y, key, title, sub, make, bg, line):
    acc = MP.ACCENT[key][0]
    rect(x, y, OUT_W, outcome_height(), fill=bg, stroke=line)
    badge(x + 32, y + 34, key, acc)
    text(x + 60, y + 43, title, T_TITLE, 700)
    text(x + 60, y + 70, sub, T_SMALL, 400, MUTED)
    p = make()
    svg = p.svg()
    defs_extra.append(svg[svg.index("<defs>") + 6: svg.index("</defs>")])
    body = "\n".join(p.el)
    el.append(f'<g transform="translate({x + (OUT_W - PANEL_W) / 2:.1f},{y + PANEL_TOP}) '
              f'scale({PANEL_SCALE:.4f})">{body}</g>')


def caption(y):
    bold = ("Fig. 1 | MDA proof-of-concept: contextual reasoning over alarm-derived situational "
            "knowledge.")
    rest = ("Per-patient alarms are ingested, enriched using the MDA semantic framework, and "
            "continuously reasoned over with clinical context. Reasoning outputs are classified into "
            "three categories—likely false positives, likely redundant information, and emergent "
            "combined meaning—to inform patient-centered alarm management. Worked examples (A–C) are "
            "regression cases of the proof-of-concept rules. In device-centered alarm management, "
            "clinicians are exposed to the full RDF alarm stream, whereas in patient-centered alarm "
            "management they are exposed to fewer, contextualized alarms and interpretations.")
    rows = textwrap.wrap(bold + " " + rest, 190)
    size, lh = 24, 33
    used = 0
    for i, row in enumerate(rows):
        start, end = used, used + len(row)
        if end <= len(bold):
            inner = f'<tspan font-weight="700">{row}</tspan>'
        elif start < len(bold):
            cut = len(bold) - start
            inner = f'<tspan font-weight="700">{row[:cut]}</tspan>{row[cut:]}'
        else:
            inner = row
        el.append(f'<text x="0" y="{y + i * lh:.1f}" font-size="{size}" fill="{NAVY}">{inner}</text>')
        used = end + 1
    return y + (len(rows) - 1) * lh + 8


def build():
    el.clear()
    defs_extra.clear()
    situational(*BAND["graph"])
    panel1(*BAND["p1"])
    panel2(*BAND["p2"])
    panel3(*BAND["p3"])
    panel4(*BAND["p4"])
    panel5(*BAND["p5"])

    x1, x2, x3, x4, x5 = (BAND[k][0] for k in ("p1", "p2", "p3", "p4", "p5"))
    r1, r2, r3, r4 = (BAND[k][0] + BAND[k][2] for k in ("p1", "p2", "p3", "p4"))
    arrow([(r1 + 4, 460), (x2 - 4, 460)])
    lines((r1 + x2) / 2, 428, ["RDF", "stream"], T_SMALL, 400, MUTED, anchor="middle", lh=20)
    arrow([(r2 + 4, 480), (x3 - 4, 480)])
    enr = SETTINGS["enrichment"]
    lines((r2 + x3) / 2, 382, ["Semantic", "enrichment", enr[:enr.index(" ", 6)], enr[enr.index(" ", 6) + 1:]],
          T_SMALL, 400, MUTED, anchor="middle", lh=21)
    arrow([(r3 + 4, 480), (x4 - 4, 480)])
    arrow([(r4 + 4, 480), (x5 - 4, 480)])
    graph_bottom = BAND["graph"][1] + BAND["graph"][3]
    cx3, cx4 = center("p3"), center("p4")
    arrow([(cx3, BAND["p3"][1] - 2), (cx3, graph_bottom + 4)])
    lines(cx3 - 10, graph_bottom + 21, ["reads", "(accumulated per patient)"], T_SMALL, 400, MUTED,
          anchor="end", lh=20)
    arrow([(cx4, BAND["p4"][1] - 2), (cx4, graph_bottom + 4)])
    text(cx4 + 10, graph_bottom + 35, "updates", T_SMALL, 400, MUTED)

    band_bottom = max(y + h for x, y, w, h in BAND.values())
    split = band_bottom + 55
    top = split + 32
    el.append(f'<path d="M{cx3} {BAND["p3"][1] + BAND["p3"][3] + 2} V{split}" stroke="{MUTED}" '
              f'stroke-width="2.6" fill="none"/>')
    text(cx3 + 14, split - 18, "Classified reasoning outcomes", T_SMALL, 400, MUTED)
    centers = [i * (OUT_W + OUT_GAP) + OUT_W / 2 for i in range(3)]
    el.append(f'<path d="M{centers[0]} {split} H{centers[-1]}" stroke="{MUTED}" stroke-width="2.6" fill="none"/>')
    for cx in centers:
        arrow([(cx, split), (cx, top - 4)])
    for i, (key, title, sub, make, bg, line) in enumerate(OUTCOMES):
        outcome(i * (OUT_W + OUT_GAP), top, key, title, sub, make, bg, line)
    bottom = top + outcome_height()
    if SETTINGS["caption"]:
        bottom = caption(bottom + 56)
    Hc = bottom + 4

    pw, ph = (v * 10 for v in SETTINGS["page_mm"])
    m = SETTINGS["margin_mm"] * 10
    s = min((pw - 2 * m) / W, (ph - 2 * m) / Hc)
    ox, oy = (pw - W * s) / 2, (ph - Hc * s) / 2
    defs = (f'<defs><marker id="arr-main" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" '
            f'markerHeight="6.5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{MUTED}"/>'
            f'</marker>{"".join(defs_extra)}</defs>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{pw / 10:g}mm" height="{ph / 10:g}mm" '
           f'viewBox="0 0 {pw:g} {ph:g}" font-family="{SETTINGS["font"]}">\n'
           f'<rect width="{pw:g}" height="{ph:g}" fill="#FFFFFF"/>\n{defs}\n'
           f'<g transform="translate({ox:.1f},{oy:.1f}) scale({s:.4f})">\n' + "\n".join(el) + "\n</g>\n</svg>\n")
    return svg, s


if __name__ == "__main__":
    out = SETTINGS["out_dir"]
    svg, s = build()
    path = out / "fig1_mda_poc.svg"
    path.write_text(svg, encoding="utf-8")
    pw, ph = SETTINGS["page_mm"]
    print(f"wrote {path} ({pw} x {ph} mm, content scale {s:.3f}: 5.5 pt text prints at {5.5 * s:.1f} pt)")
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-f", "pdf", "-o", str(out / "fig1_mda_poc.pdf"), str(path)], check=True)
        px = round(pw / 25.4 * SETTINGS["png_dpi"])
        subprocess.run(["rsvg-convert", "-w", str(px), "-o", str(out / "fig1_mda_poc.png"), str(path)], check=True)
        print("wrote fig1_mda_poc.pdf and fig1_mda_poc.png")
    else:
        print("rsvg-convert not found: SVG only")
