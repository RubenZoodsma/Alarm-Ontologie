"""
filter_comparison_figure.py

Figure for manuscript V9, 4.4.3: the alarms silenced by invalidation (Cat 1:
rules 1a, 1b) and redundancy (Cat 2: rules 2a, 2b), compared with a 5-second
delay filter on low/medium priority alarms. Reads the output of
filter_comparison.py:

    MANUSCRIPT/table_drafts/filter_comparison_rules.csv

and writes MANUSCRIPT/figure_drafts/filter_comparison.{pdf,svg,png,tiff}
(180 mm wide, double column; PDF/SVG vector with embedded fonts; PNG/TIFF 600 dpi).

  A  Per rule and unit: share of the silenced alarms the filter would also
     suppress, or pass (high priority / other priority). Total: distinct
     alarms silenced by any rule (an alarm silenced by several rules once).
  B  Per unit: share of the silenced alarms the filter would also suppress, for
     delays of 2 to 30 s: the total solid, Cat 1 and Cat 2 dotted. The grey
     line and its label mark the 5-s delay on low and medium priority alarms
     of panel A and Table 4.

No titles in the figure: the caption carries them.

Usage:  python3 MANUSCRIPT/scripts/filter_comparison_figure.py [table_dir] [out_dir]
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, NullLocator  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TABLE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "MANUSCRIPT" / "table_drafts"
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "MANUSCRIPT" / "figure_drafts"
BASE_DELAY = 5
MM = 1 / 25.4

INK, INK2, MUTED, GRID, SURFACE = "#1a1a1a", "#4d4d4d", "#8a8984", "#e3e2dd", "#ffffff"
# panel A: passed = blue (dark: high priority), also filtered = neutral grey
C_ALSO, C_HIGH, C_OTHER = "#b4b3ad", "#1c5cab", "#9ec5f4"
DARK_FILLS = {C_HIGH}                                   # white text inside
# panel B: total solid, sub-categories dotted; hue + marker tell the dotted ones apart
# (also in greyscale)
DOTTED = (0, (1, 1.3))
SERIES = {
    "cat1": ("Invalidation (1a, 1b)", "#d95926", "o", DOTTED),
    "cat2": ("Redundancy (2a, 2b)", "#6250d6", "s", DOTTED),
    "any": ("Total", INK, "D", "-"),
}
UNITS = [("PICU", "Pediatric ICU"), ("NICU", "Neonatal ICU")]
ROWS = [  # (y, rule or None for a group header, label)
    (0.0, None, "Invalidation"),
    (1.0, "1a", "1a  Impaired signal or sensor on own pathway"),
    (2.0, "1b", "1b  Asystole contradicted by arterial line"),
    (3.2, None, "Redundancy"),
    (4.2, "2a", "2a  Same process, already alarmed"),
    (5.2, "2b", "2b  Same metric, other sensing pathway"),
    (6.6, "any", "Total"),
]

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 7, "axes.labelsize": 7, "xtick.labelsize": 6.5, "ytick.labelsize": 7,
    "legend.fontsize": 6.5, "axes.edgecolor": MUTED, "axes.linewidth": 0.6,
    "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK,
    "xtick.major.width": 0.6, "xtick.major.size": 2.5, "ytick.major.size": 0,
    "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
})


def load() -> pd.DataFrame:
    rules = pd.read_csv(TABLE_DIR / "filter_comparison_rules.csv", sep=";")
    missing = {"cat1", "cat2"} - set(rules.Rule)
    if missing:
        raise SystemExit(f"{', '.join(sorted(missing))} missing: rerun filter_comparison.py")
    return rules[(rules.Basis == "silenced") & (rules.Filter == "lowmed")]


def panel_a(axes, rules):
    base = rules[rules.Delay_s == BASE_DELAY]
    for ax, (unit, name) in zip(axes, UNITS):
        sel = base[base.Unit == unit].set_index("Rule")
        for y, rule, _ in ROWS:
            if rule is None:
                continue
            row = sel.loc[rule]
            n = int(row.n)
            parts = [(row.also_n, C_ALSO), (row.passed_high_n, C_HIGH),
                     (row.passed_n - row.passed_high_n, C_OTHER)]
            left = 0.0
            for value, color in parts:
                w = 100 * value / n if n else 0
                if w <= 0:
                    continue
                ax.barh(y, w, left=left, height=0.72, color=color, edgecolor=SURFACE, linewidth=0.8)
                if w >= 9:
                    ax.text(left + w / 2, y, f"{w:.0f}", ha="center", va="center", fontsize=6,
                            color=SURFACE if color in DARK_FILLS else INK)
                left += w
            ax.text(102, y, f"{n:,}", ha="left", va="center", fontsize=6, color=INK2,
                    fontweight="bold" if rule == "any" else "normal")
        ax.axhline(5.9, color=MUTED, linewidth=0.5)
        ax.set_xlim(0, 100)
        ax.set_ylim(7.2, -0.6)
        ax.xaxis.set_major_locator(FixedLocator([0, 25, 50, 75, 100]))
        ax.set_xlabel("Silenced alarms (%)")
        ax.set_title(name, fontsize=7, loc="left", pad=3, color=INK)
        ax.text(102, -0.6, "n", ha="left", va="bottom", fontsize=6, color=INK2, style="italic")
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.set_yticks([y for y, _, _ in ROWS])
        ax.set_yticklabels([])
    labels = axes[0].set_yticklabels([lbl for _, _, lbl in ROWS])
    for lbl, (_, rule, _) in zip(labels, ROWS):
        if rule is None or rule == "any":
            lbl.set_fontweight("bold")


def panel_b(axes, rules):
    for ax, (unit, name) in zip(axes, UNITS):
        sel = rules[rules.Unit == unit]
        for key, (label, color, marker, dash) in SERIES.items():
            d = sel[sel.Rule == key].sort_values("Delay_s")
            ax.plot(d.Delay_s, pd.to_numeric(d.also_pct, errors="coerce"), color=color,
                    linestyle=dash, linewidth=1.1, marker=marker, markersize=3.6,
                    markeredgecolor=SURFACE, markeredgewidth=0.6, label=label)
        # 5-s reference: thin solid grey, so it is not read as one of the dotted series
        ax.axvline(BASE_DELAY, color=MUTED, linewidth=0.6, zorder=0)
        ax.text(BASE_DELAY * 1.06, 97, f"{BASE_DELAY}-s delay on low and\nmedium priority alarms",
                ha="left", va="top", fontsize=6, color=INK2, linespacing=1.15,
                bbox={"boxstyle": "round,pad=0.3", "facecolor": SURFACE, "edgecolor": MUTED,
                      "linewidth": 0.5})
        ax.set_xscale("log")
        delays = sorted(sel.Delay_s.unique())
        ax.xaxis.set_major_locator(FixedLocator(delays))
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticklabels([str(int(d)) for d in delays])
        ax.set_xlim(delays[0] / 1.15, delays[-1] * 1.15)
        ax.set_ylim(0, 100)
        ax.yaxis.set_major_locator(FixedLocator([0, 25, 50, 75, 100]))
        ax.tick_params(axis="y", length=2.5, width=0.6, labelsize=6.5, colors=INK2)
        ax.grid(axis="y", color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
        ax.set_xlabel("Filter delay (s)")
        ax.set_title(name, fontsize=7, loc="left", pad=3, color=INK)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    axes[0].set_ylabel("Silenced alarms\nalso suppressed (%)")
    axes[1].tick_params(axis="y", labelleft=False)


def main():
    rules = load()
    fig = plt.figure(figsize=(180 * MM, 128 * MM), facecolor=SURFACE)
    # A needs a wide left margin for the rule names; B does not, so each gets its own grid
    gs_a = fig.add_gridspec(1, 2, wspace=0.16, left=0.335, right=0.935, top=0.885, bottom=0.53)
    gs_b = fig.add_gridspec(1, 2, wspace=0.12, left=0.105, right=0.935, top=0.36, bottom=0.085)
    axes_a = [fig.add_subplot(gs_a[0, i]) for i in range(2)]
    axes_b = [fig.add_subplot(gs_b[0, i]) for i in range(2)]
    panel_a(axes_a, rules)
    panel_b(axes_b, rules)

    # legends above each panel, panel letters at the left edge
    fig.legend(handles=[Patch(color=C_ALSO, label="Also suppressed by the filter"),
                        Patch(color=C_HIGH, label="Passed by the filter, high priority"),
                        Patch(color=C_OTHER, label="Passed by the filter, other priority")],
               loc="lower left", bbox_to_anchor=(axes_a[0].get_position().x0, 0.935),
               ncol=3, frameon=False, handlelength=1.1, handleheight=0.9, columnspacing=1.6,
               borderaxespad=0)
    fig.legend(handles=[Line2D([], [], color=c, linestyle=ls, marker=m, markersize=3.6,
                               markeredgecolor=SURFACE, markeredgewidth=0.6, linewidth=1.1, label=lbl)
                        for lbl, c, m, ls in SERIES.values()],
               loc="lower left", bbox_to_anchor=(axes_b[0].get_position().x0,
                                                 axes_b[0].get_position().y1 + 0.045),
               ncol=3, frameon=False, handlelength=2.6, columnspacing=1.6, borderaxespad=0)
    for letter, ax in (("A", axes_a[0]), ("B", axes_b[0])):
        y = 0.975 if letter == "A" else ax.get_position().y1 + 0.075
        fig.text(0.012, y, letter, fontsize=9, fontweight="bold", va="top", ha="left", color=INK)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext, kw in (("pdf", {}), ("svg", {}), ("png", {"dpi": 600}),
                    ("tiff", {"dpi": 600, "pil_kwargs": {"compression": "tiff_lzw"}})):
        p = OUT_DIR / f"filter_comparison.{ext}"
        fig.savefig(p, facecolor=SURFACE, **kw)
        print(f"Wrote {p}")


if __name__ == "__main__":
    main()
