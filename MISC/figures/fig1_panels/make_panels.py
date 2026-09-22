"""
make_panels.py — visual examples for Fig. 1's outcome boxes A, B and C.

Each panel replaces the text row "Alarm(s) + context -> Inferred
interpretation -> Management action" inside one box, with one worked
example drawn from the MDA-POC regression fixtures
(DATA/CAT_evaluation/events_data.csv):

    A  likely false positive   CAT1a  cat1a_sensor_pos  ECG leads off + asystole
    B  redundant information   CAT2a  cat2a_pos         extremely low HR + low HR
    C  emergent meaning        CAT3a  cat3a_pos         asystole + apnoea

Sized for the A4 figure: each box stays 58 mm wide (one third of a 180 mm
text width, as now); inside its padding that leaves 54 mm, so every panel is
54 x 42.8 mm (the box body; the box header with the circled letter stays as
in the figure). The drawing is laid out on a 580 x 460 grid scaled to that
size; the smallest text prints at 5.1 pt, titles at 6.3 pt.

Run from VS Code; edit SETTINGS below.
"""

from pathlib import Path

SETTINGS = {
    "out_dir": Path(__file__).parent,
    "font": "Helvetica Neue, Helvetica, Arial, sans-serif",
}

W, H = 580, 460                        # drawn at 54 x 42.8 mm
NAVY, MUTED, LINE, TRACK = "#1F2A44", "#5B6478", "#9AA3B5", "#E7EBF2"
AMBER = "#D9962B"
ACCENT = {"A": ("#C8413A", "#FBE4E2"), "B": ("#3264AE", "#E2EBF8"), "C": ("#7652AE", "#ECE4F7")}

FS, FS_S, FS_STEP, FS_ACT = 21.2, 19.4, 19.4, 24   # 5.6, 5.1, 5.1, 6.3 pt at 54 mm


class Panel:
    def __init__(self, key, domain):
        self.key, self.domain = key, domain
        self.acc, self.tint = ACCENT[key]
        self.el = []

    # ---- primitives -------------------------------------------------------
    def text(self, x, y, s, size=FS, weight=400, fill=NAVY, anchor="start", spacing=0):
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        self.el.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
                       f'fill="{fill}" text-anchor="{anchor}"{ls}>{s}</text>')

    def rect(self, x, y, w, h, fill, stroke="none", r=6, sw=2, extra=""):
        self.el.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" '
                       f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')

    def line(self, x1, y1, x2, y2, stroke=LINE, sw=2.2, arrow=False, dash=""):
        m = ' marker-end="url(#arr-%s)"' % self.key if arrow else ""
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.el.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                       f'stroke="{stroke}" stroke-width="{sw}"{d}{m}/>')

    def step(self, y, label):
        self.text(0, y, label, size=FS_STEP, weight=700, fill=self.acc, spacing=1.2)

    def chevron(self, y):
        self.el.append(f'<path d="M280 {y} L290 {y + 8} L300 {y}" fill="none" stroke="{LINE}" '
                       f'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>')

    def check(self, x, y):
        self.el.append(f'<path d="M{x} {y - 6} l5 5 l10 -11" fill="none" stroke="{self.acc}" '
                       f'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>')

    # ---- timeline ---------------------------------------------------------
    X0, X1 = 172, 578

    def tx(self, t):
        a, b = self.domain
        return self.X0 + (t - a) / (b - a) * (self.X1 - self.X0)

    def lane(self, i, label, s, e, kind, hatch=False, inside=None):
        y = 32 + i * 36
        self.text(0, y + 18.5, label, size=FS)
        self.rect(self.X0, y, self.X1 - self.X0, 26, TRACK, r=4)
        fill = AMBER if kind == "tech" else NAVY
        x, w = self.tx(s), self.tx(e) - self.tx(s)
        if hatch:
            self.rect(x, y + 2, w, 22, self.tint, stroke=self.acc, r=3, sw=2)
            self.rect(x, y + 2, w, 22, f"url(#hatch-{self.key})", r=3)
        else:
            self.rect(x, y + 2, w, 22, fill, r=3)
        if inside:
            col = NAVY if hatch else "#FFFFFF"
            self.text(x + 8, y + 18, inside, size=FS_S, fill=col, weight=600 if hatch else 400)
        return x, y, w

    def axis(self, ticks, y=122):
        for t in ticks:
            x = self.tx(t)
            self.line(x, 100, x, 106, stroke=LINE, sw=1.6)
            lab = f"{t} s" if t == ticks[-1] else str(t)
            anchor = "end" if t == ticks[-1] else "middle"
            self.text(x + (6 if t == ticks[-1] else 0), y, lab, size=FS_S, fill=MUTED, anchor=anchor)

    # ---- nodes -----------------------------------------------------------
    def pill(self, x, y, w, label, kind="phys", h=36):
        fill = AMBER if kind == "tech" else NAVY
        self.rect(x, y, w, h, fill, r=h / 2)
        self.text(x + w / 2, y + h / 2 + 7.5, label, size=FS, fill="#FFFFFF", anchor="middle", weight=500)

    def node(self, x, y, w, label, h=36, fill="#FFFFFF", stroke=LINE, col=NAVY, weight=400):
        self.rect(x, y, w, h, fill, stroke=stroke, r=7, sw=2)
        lines = label if isinstance(label, list) else [label]
        n = len(lines)
        for k, s in enumerate(lines):
            self.text(x + w / 2, y + h / 2 + 7.5 + (k - (n - 1) / 2) * 25, s, size=FS, fill=col,
                      anchor="middle", weight=weight)

    # ---- action ----------------------------------------------------------
    def action(self, title, sub, rule, icon):
        y = 384
        self.rect(0, y, W, 72, self.tint, stroke=self.acc, r=12, sw=2)
        cx, cy = 42, y + 36
        self.el.append(f'<circle cx="{cx}" cy="{cy}" r="25" fill="{self.acc}"/>')
        bell = ("M-10 7 L-10 -1 C-10 -9 -5 -13 0 -13 C5 -13 10 -9 10 -1 L10 7 L13 10 L-13 10 Z "
                "M-3.5 13 A3.5 3.5 0 0 0 3.5 13 Z")
        g = [f'<g transform="translate({cx},{cy + 1})">']
        if icon == "merge":
            g.append(f'<path d="{bell}" transform="translate(6,-5) scale(.8)" fill="none" '
                     f'stroke="#FFFFFF" stroke-width="2.4" opacity=".75"/>')
            g.append(f'<path d="{bell}" transform="translate(-3,2) scale(.9)" fill="#FFFFFF"/>')
        else:
            g.append(f'<path d="{bell}" fill="#FFFFFF"/>')
        if icon == "mute":
            g.append(f'<line x1="-15" y1="-15" x2="15" y2="15" stroke="{self.acc}" stroke-width="7"/>'
                     f'<line x1="-15" y1="-15" x2="15" y2="15" stroke="#FFFFFF" stroke-width="3"/>')
        if icon == "alert":
            g.append(f'<circle cx="11" cy="-11" r="8" fill="#FFFFFF" stroke="{self.acc}" stroke-width="2"/>'
                     f'<text x="11" y="-6" font-size="14" font-weight="700" fill="{self.acc}" '
                     f'text-anchor="middle">!</text>')
        g.append("</g>")
        self.el.append("".join(g))
        self.text(84, y + 31, title, size=FS_ACT, weight=700)
        self.text(84, y + 57, sub, size=FS_S + 0.6, fill=MUTED)
        self.text(W - 12, y + 31, rule, size=FS_S, weight=700, fill=self.acc, anchor="end")

    def svg(self):
        defs = (f'<defs>'
                f'<pattern id="hatch-{self.key}" width="7" height="7" patternUnits="userSpaceOnUse" '
                f'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="7" stroke="{self.acc}" '
                f'stroke-width="2.6"/></pattern>'
                f'<marker id="arr-{self.key}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{LINE}"/>'
                f'</marker></defs>')
        body = "\n  ".join(self.el)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="54mm" height="42.8mm" '
                f'viewBox="0 0 {W} {H}" font-family="{SETTINGS["font"]}">\n  {defs}\n  {body}\n</svg>\n')


def panel_a():
    p = Panel("A", (-5, 125))
    p.step(18, "ALARMS + CONTEXT")
    p.lane(0, "ECG leads off", 0, 120, "tech", inside="technical")
    x, y, w = p.lane(1, "Asystole", 30, 50, "phys", hatch=True)
    p.text(x + w + 8, y + 18.5, "flagged", size=FS_S, weight=700, fill=p.acc)
    p.axis([0, 30, 60, 90, 120])
    p.chevron(138)

    p.step(174, "INFERRED INTERPRETATION")
    # the asystole's own sensing pathway
    p.line(136, 204, 578, 204, stroke=MUTED, sw=1.6)
    p.line(136, 204, 136, 212, stroke=MUTED, sw=1.6)
    p.line(578, 204, 578, 212, stroke=MUTED, sw=1.6)
    p.text(357, 197, "its own sensing pathway", size=FS_S, fill=MUTED, anchor="middle")
    p.pill(0, 216, 110, "Asystole")
    p.line(110, 234, 136, 234)
    p.node(136, 216, 124, "HR: absent")
    p.line(260, 234, 282, 234)
    p.node(282, 216, 128, "ECG signal")
    p.line(410, 234, 432, 234)
    p.node(432, 216, 146, "electrodes", stroke=p.acc, col=p.acc, weight=700)
    p.pill(432, 290, 146, "Leads off", kind="tech")
    p.line(505, 290, 505, 256, arrow=True)
    p.text(497, 279, "reports: disconnected", size=FS_S, fill=p.acc, anchor="end", weight=600)
    p.text(0, 312, "Signal behind the asystole", size=FS, weight=700, fill=NAVY)
    p.text(0, 336, "is not being acquired", size=FS, weight=700, fill=NAVY)
    p.chevron(350)

    p.action("Likely false positive", "flag and suppress the asystole", "CAT1a", "mute")
    return p


def panel_b():
    p = Panel("B", (-5, 125))
    p.step(18, "ALARMS + CONTEXT")
    p.lane(0, "HR extr. low", 0, 120, "phys", inside="high priority")
    x, y, w = p.lane(1, "HR low", 30, 60, "phys", hatch=True)
    p.text(x + w + 8, y + 18.5, "medium · silenced", size=FS_S, weight=700, fill=p.acc)
    p.axis([0, 30, 60, 90, 120])
    p.chevron(138)

    p.step(174, "INFERRED INTERPRETATION")
    p.pill(0, 192, 150, "HR extr. low")
    p.pill(0, 262, 150, "HR low")
    p.line(150, 210, 250, 243, arrow=True)
    p.line(150, 280, 250, 255, arrow=True)
    p.text(196, 214, "↓↓", size=FS, weight=700, fill=NAVY, anchor="middle")
    p.text(196, 292, "↓", size=FS, weight=700, fill=NAVY, anchor="middle")
    p.node(254, 231, 118, "heart rate")
    p.line(372, 249, 404, 249, arrow=True)
    p.text(491, 212, "process", size=FS_S, fill=MUTED, anchor="middle")
    p.node(406, 219, 172, ["cardiac", "contraction"], h=60, stroke=p.acc, col=p.acc, weight=700)
    for i, (cx, s) in enumerate([(0, "same process"), (300, "same direction")]):
        p.check(cx + 2, 318)
        p.text(cx + 24, 318, s, size=FS_S + 0.6)
    for i, (cx, s) in enumerate([(0, "not more severe"), (300, "priority ≤ active")]):
        p.check(cx + 2, 342)
        p.text(cx + 24, 342, s, size=FS_S + 0.6)
    p.chevron(356)
    p.action("Redundant: consolidate", "silence HR low while HR extr. low lasts", "CAT2a", "merge")
    return p


def panel_c():
    p = Panel("C", (-5, 95))
    p.step(18, "ALARMS + CONTEXT")
    xa, xb = p.tx(30), p.tx(60)
    p.rect(xa, 27, xb - xa, 72, p.tint, stroke=p.acc, r=4, sw=1.6, extra=' stroke-dasharray="5 4"')
    p.lane(0, "Asystole", 0, 60, "phys")
    p.lane(1, "Apnoea", 30, 90, "phys")
    p.text(xb + 8, 20, "both present", size=FS_S, weight=700, fill=p.acc)
    p.axis([0, 30, 60, 90])
    p.chevron(138)

    p.step(174, "INFERRED INTERPRETATION")
    p.pill(0, 194, 100, "Asystole")
    p.pill(0, 272, 100, "Apnoea")
    p.node(126, 194, 208, "Cardiac arrest", fill=p.tint, stroke=p.acc)
    p.node(126, 272, 208, "Respiratory arrest", fill=p.tint, stroke=p.acc)
    p.line(126, 212, 103, 212, arrow=True)
    p.line(126, 290, 103, 290, arrow=True)
    p.node(360, 217, 218, ["Cardiorespiratory", "arrest"], h=68, fill=p.acc, stroke=p.acc,
           col="#FFFFFF", weight=700)
    p.line(360, 240, 337, 214, arrow=True)
    p.line(360, 262, 337, 288, arrow=True)
    p.text(578, 334, "arrows: evidenced by", size=FS_S, fill=MUTED, anchor="end")
    p.chevron(350)
    p.action("Emergent meaning: notify", "one alarm: cardiorespiratory arrest", "CAT3a", "alert")
    return p


if __name__ == "__main__":
    out = SETTINGS["out_dir"]
    for key, make in (("A", panel_a), ("B", panel_b), ("C", panel_c)):
        path = out / f"fig1_panel_{key}.svg"
        path.write_text(make().svg(), encoding="utf-8")
        print("wrote", path)
