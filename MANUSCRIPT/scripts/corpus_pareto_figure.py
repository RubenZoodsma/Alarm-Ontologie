"""
corpus_pareto_figure.py

Appendix figures: Pareto plots of the alarm corpus, one figure per department.
Reads DATA/CORPUS/Total_corpus.csv (one row per unique alarm, i.e. per
label + priority + device, with its count) and writes

    MANUSCRIPT/figure_drafts/corpus_pareto_<UNIT>.{pdf,svg,png,tiff}

for UNIT in NICU, PICU, ADULT (180 mm wide, double column; PDF/SVG vector with
embedded fonts; PNG/TIFF 600 dpi).

Layout: one row per device, grouped by device type (vertical header on the
left; groups ordered by alarm volume, devices within a group too). The first
column is the device total (all its unique alarms, all priorities); a
vertical rule separates it from the columns under the "Source alarm
priority" header: the device:priority strata the corpus cut-offs are taken
in (only the priorities that occur in the department). A device without
alarms at a priority leaves that cell empty.

Each panel: bars = alarm count per unique alarm, ranked by count (left axis);
line = cumulative share of the panel's alarms (right axis, %). The dashed
markers give the number of unique alarms needed to reach 50 % and 99 % of the
alarms, with the rule of CODE/data_prep/build_corpus_cutoffs.py: a unique
alarm is kept while the cumulative share before it is still below the
cut-off. For the priority panels these are exactly the rows of
Annotation_corpus_<50|99>.csv; the device-total panels are for reading only.

Each figure carries its appendix title (Appendix 1A/1B/1C); the caption
explains the panels and the table.

Usage:  python3 MANUSCRIPT/scripts/corpus_pareto_figure.py [corpus_csv] [out_dir]
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import EngFormatter, FixedLocator, MaxNLocator  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "DATA" / "CORPUS" / "Total_corpus.csv"
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "MANUSCRIPT" / "figure_drafts"

UNITS = [("Neonatal intensive care", "NICU", "Appendix 1A – Pareto analysis of alarms in the Neonatal ICU, by device and source alarm priority"),
         ("Paediatric intensive care", "PICU", "Appendix 1B – Pareto analysis of alarms in the Pediatric ICU, by device and source alarm priority"),
         ("Adult intensive care", "ADULT", "Appendix 1C – Pareto analysis of alarms in the Adult ICU, by device and source alarm priority")]
PRIORITIES = [("Hoog", "High"), ("Medium", "Medium"), ("Laag", "Low"), ("Unknown", "Unknown")]
CUTOFFS = [0.50, 0.99]

# Device type and manufacturer per gateway device name (Alarm_device); not in
# the corpus itself. Medibus is Draeger's communication protocol, not a device:
# the device behind each Medibus alarm is resolved in annotation.
DEVICE_TYPE = {
    "Philipsmonitor": "Patient monitor",
    "DraegerMonitor": "Patient monitor",
    "Edwards": "Haemodynamic monitor",
    "ServoU": "Mechanical ventilator",
    "ServoI": "Mechanical ventilator",
    "CarescapeR860": "Mechanical ventilator",
    "LeoniPlus": "Mechanical ventilator",
    "Trilogy": "Mechanical ventilator",
    "Medibus": "Dräger device",
    "Caleo": "Thermoregulation",
    "BabyLeo": "Thermoregulation",
    "BabyTherm": "Thermoregulation",
    "Criticool": "Thermoregulation",
    "CardioHelp": "ECMO",
    "Prismax": "Kidney replacement therapy",
}
MANUFACTURER = {
    "Philipsmonitor": "Philips",
    "DraegerMonitor": "Dräger",
    "Edwards": "Edwards Lifesciences",
    "ServoU": "Getinge",
    "ServoI": "Getinge",
    "CarescapeR860": "GE HealthCare",
    "LeoniPlus": "Löwenstein Medical",
    "Trilogy": "Philips",
    "Medibus": "Dräger",
    "Caleo": "Dräger",
    "BabyLeo": "Dräger",
    "BabyTherm": "Dräger",
    "Criticool": "Belmont Medical",
    "CardioHelp": "Getinge",
    "Prismax": "Baxter",
}

INK, INK2, MUTED, SURFACE = "#1a1a1a", "#4d4d4d", "#8a8984", "#ffffff"
C_BAR, C_BAR_TOTAL, C_CUM = "#9ec5f4", "#1c5cab", "#d95926"

# geometry (mm)
FIG_W = 180
LEFT, RIGHT = 30, 10         # margins: type header + device label | right-axis label
GAP, SPLIT = 11, 17           # between panels | between device total and priorities
TOP, BOTTOM = 29, 12          # title + header block | x labels
ROW_H, ROW_GAP, GROUP_GAP = 22, 12, 6
T_GAP, T_ROW = 1, 3.8         # space above the summary table | its row height

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 6, "axes.labelsize": 6, "xtick.labelsize": 5.5, "ytick.labelsize": 5.5,
    "axes.edgecolor": MUTED, "axes.linewidth": 0.5,
    "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 2, "ytick.major.size": 2,
    "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
})


def load() -> pd.DataFrame:
    df = pd.read_csv(CORPUS, sep=";", encoding="utf-8-sig")
    unknown = set(df.Alarm_priority) - {p for p, _ in PRIORITIES}
    if unknown:
        raise SystemExit(f"Unexpected priority value(s) in {CORPUS.name}: {sorted(unknown)}")
    untyped = set(df.Alarm_device) - (set(DEVICE_TYPE) & set(MANUFACTURER))
    if untyped:
        raise SystemExit(f"No device type/manufacturer for: {sorted(untyped)} "
                         "(add to DEVICE_TYPE and MANUFACTURER)")
    return df


def pareto(counts: pd.Series):
    """Counts ranked descending, cumulative share (%), and the cut-off sizes."""
    c = counts.sort_values(ascending=False).to_numpy()
    cum = np.cumsum(c) / c.sum()
    prior = cum - c / c.sum()
    needed = {k: int((prior < k).sum()) for k in CUTOFFS}
    return c, 100 * cum, needed


def draw(ax, counts: pd.Series, total: bool):
    c, cum, needed = pareto(counts)
    x = np.arange(1, len(c) + 1)
    ax.bar(x, c, width=0.85 if len(c) < 60 else 1.0, color=C_BAR_TOTAL if total else C_BAR,
           linewidth=0, zorder=1)
    ax.set_xlim(0.4, len(c) + 0.6)
    ax.set_ylim(0, c[0] * 1.08)
    ax.yaxis.set_major_formatter(EngFormatter(sep=""))
    ax.yaxis.set_major_locator(MaxNLocator(3, integer=True))
    ax.xaxis.set_major_locator(MaxNLocator(4, integer=True, min_n_ticks=1))
    ax.spines["top"].set_visible(False)

    ax2 = ax.twinx()
    ax2.plot(x, cum, color=C_CUM, linewidth=0.8, solid_capstyle="round", zorder=3)
    ax2.set_ylim(0, 108)
    ax2.yaxis.set_major_locator(FixedLocator([0, 50, 100]))
    ax2.tick_params(axis="y", colors=C_CUM)
    ax2.spines["right"].set_color(C_CUM)
    ax2.spines["top"].set_visible(False)

    # cut-off markers: dashed rank line, dot on the cumulative line, label
    k50, k99 = needed[0.50], needed[0.99]
    marks = [(k50, "50%"), (k99, "99%")] if k50 != k99 else [(k50, "50, 99%")]
    for (k, tag), y in zip(marks, (0.62, 0.38)):
        ax2.axvline(k, color=INK2, linewidth=0.5, linestyle=(0, (2.5, 1.5)), zorder=2)
        ax2.plot(k, cum[k - 1], marker="o", markersize=2.2, color=C_CUM,
                 markeredgecolor=SURFACE, markeredgewidth=0.4, zorder=4)
        right = k <= 0.6 * len(c) + 0.5
        ax2.annotate(f"{tag}: {k}", xy=(k, y), xycoords=("data", "axes fraction"),
                     xytext=(1.5 if right else -1.5, 0), textcoords="offset points",
                     ha="left" if right else "right", va="center", fontsize=5, color=INK,
                     bbox=dict(boxstyle="round,pad=0.15", facecolor=SURFACE, edgecolor="none",
                               alpha=0.8), zorder=5)

    ax.text(0, 1.04, f"n = {len(c)} unique · {c.sum():,} alarms", transform=ax.transAxes,
            ha="left", va="bottom", fontsize=5, color=INK2)
    return ax2, needed


def summary_table(fig, fx, fy, top, cols, groups, stats):
    """Booktabs-style table under the panels: per device and stratum the number
    of unique alarms and the p50/p99 cut-off sizes (plus the alarm count for the
    device total). Last row sums each column over the department's devices."""
    sub_total = [("Unique", 8.5), ("Alarms", 13), ("p50", 7.5), ("p99", 7.5)]
    sub_prio = [("Unique", 8.5), ("p50", 7.5), ("p99", 7.5)]
    label_x, x, split, gap = 3, 0, 6, 3
    width = sum(w for _, w in sub_total) + split + (len(cols) - 1) * sum(w for _, w in sub_prio) \
        + (len(cols) - 2) * gap
    x = FIG_W - 3 - width                          # data block right-aligned to the margin
    spans, cells = [], []                          # (x0, x1, label) | (x_right, key, field idx)
    for j, (p, name) in enumerate(cols):
        subs = sub_total if j == 0 else sub_prio
        x0 = x
        for k, (lab, w) in enumerate(subs):
            x += w
            field = [0, 1, 2, 3][k] if j == 0 else [0, 2, 3][k]
            cells.append((x, p, field, lab))
        spans.append((x0, x, name)) 
        x += split if j == 0 else gap
    r = lambda i: top + (i + 0.5) * T_ROW         # noqa: E731  row centre (mm from top)
    rule = lambda y, lw, x0=label_x, x1=FIG_W - 3: fig.add_artist(  # noqa: E731
        Line2D([fx(x0), fx(x1)], [fy(y)] * 2, color=INK, linewidth=lw))
    txt = lambda xx, i, s, **kw: fig.text(fx(xx), fy(r(i)), s, va="center",  # noqa: E731
                                          fontsize=5.5, **{"color": INK, **kw})

    rule(top, 0.8)
    # header: spanner, stratum names (with rules), sub-columns
    x0, x1 = spans[1][0], spans[-1][1]
    txt((x0 + x1) / 2, 0, "Source alarm priority", ha="center", fontweight="bold")
    rule(top + T_ROW, 0.5, x0, x1)
    for a, b, name in spans:
        txt((a + b) / 2, 1, name, ha="center", fontweight="bold")
        rule(top + 2 * T_ROW, 0.5, a + 0.5, b)
    txt(label_x, 2, "Device (manufacturer)", ha="left", fontweight="bold")
    for xr, _, _, lab in cells:
        txt(xr, 2, lab, ha="right", style="italic", color=INK2)
    rule(top + 3 * T_ROW, 0.5)

    i, totals = 3, {}
    for g, (t, devs) in enumerate(groups):
        if g:
            rule(top + i * T_ROW, 0.3)
        txt(label_x, i, t, ha="left", fontweight="bold")
        i += 1
        for dev in devs:
            txt(label_x + 3, i, f"{dev} ({MANUFACTURER[dev]})", ha="left")
            for xr, p, field, _ in cells:
                v = stats.get((dev, p))
                txt(xr, i, "–" if v is None else f"{v[field]:,}", ha="right",
                    color=INK if v is not None else MUTED)
                if v is not None:
                    totals[p, field] = totals.get((p, field), 0) + v[field]
            i += 1
    rule(top + i * T_ROW, 0.5)
    txt(label_x, i, "All devices", ha="left", fontweight="bold")
    for xr, p, field, _ in cells:
        txt(xr, i, f"{totals.get((p, field), 0):,}", ha="right", fontweight="bold")
    rule(top + (i + 1) * T_ROW, 0.8)
    # separator between device total and priorities, as in the panels
    xs = spans[0][1] + split / 2
    fig.add_artist(Line2D([fx(xs)] * 2, [fy(top + T_ROW), fy(top + (i + 1) * T_ROW)],
                          color=MUTED, linewidth=0.6))


def figure(df: pd.DataFrame, dept: str, unit: str, title: str):
    d = df[df.Alarm_department == dept].assign(type=lambda t: t.Alarm_device.map(DEVICE_TYPE))
    prios = [(p, name) for p, name in PRIORITIES if p in set(d.Alarm_priority)]
    cols = [("Total", "Device total")] + prios
    ncol = len(cols)

    type_order = d.groupby("type").Quant_count.sum().sort_values(ascending=False).index
    groups = [(t, d[d.type == t].groupby("Alarm_device").Quant_count.sum()
               .sort_values(ascending=False).index.tolist()) for t in type_order]
    rows = [(t, dev) for t, devs in groups for dev in devs]
    nrow = len(rows)

    panels_h = TOP + nrow * ROW_H + (nrow - 1) * ROW_GAP + (len(groups) - 1) * GROUP_GAP
    table_rows = 3 + len(groups) + nrow + 1          # 3 header rows, type rows, devices, total
    fig_h = panels_h + BOTTOM + T_GAP + table_rows * T_ROW + 3
    fig = plt.figure(figsize=(FIG_W / 25.4, fig_h / 25.4))
    fx = lambda mm: mm / FIG_W          # noqa: E731
    fy = lambda mm: 1 - mm / fig_h      # noqa: E731  (mm from the top)
    fig.text(fx(3), fy(3), title, ha="left", va="top", fontsize=9, fontweight="bold",
             color=INK)
    fig.text(fx(3), fy(8.5), "Ranked alarm counts per unique alarm (label, priority, device), with "
             f"the 50% and 99% cut-offs  ·  {len(d)} unique alarms, {d.Quant_count.sum():,} alarms",
             ha="left", va="top", fontsize=6.5, color=INK2)

    w = (FIG_W - LEFT - RIGHT - SPLIT - GAP * (ncol - 2)) / ncol
    col_x = [LEFT] + [LEFT + w + SPLIT + j * (w + GAP) for j in range(ncol - 1)]

    def cell(dev, p):
        dd = d[d.Alarm_device == dev]
        return dd if p == "Total" else dd[dd.Alarm_priority == p]

    filled = np.array([[not cell(dev, p).empty for p, _ in cols] for _, dev in rows])
    row_top, y, prev = [], TOP, None
    for t, _ in rows:
        if prev is not None:
            y += ROW_GAP + (GROUP_GAP if t != prev else 0)
        row_top.append(y)
        y += ROW_H
        prev = t

    summary, stats = [], {}
    for i, (t, dev) in enumerate(rows):
        last = max(j for j in range(ncol) if filled[i, j])
        for j, (p, name) in enumerate(cols):
            if not filled[i, j]:
                continue
            ax = fig.add_axes([fx(col_x[j]), fy(row_top[i] + ROW_H), fx(w), ROW_H / fig_h])
            sel = cell(dev, p)
            ax2, needed = draw(ax, sel.Quant_count, total=(p == "Total"))
            summary.append((dev, name, len(sel), int(sel.Quant_count.sum()),
                            *[needed[k] for k in CUTOFFS]))
            stats[dev, p] = summary[-1][2:]
            if j == 0:
                ax.set_ylabel(f"{dev}\n({MANUFACTURER[dev]})\n\nAlarms", color=INK)
            if j == last:
                ax2.set_ylabel("Cumulative (%)", color=C_CUM)
            if i == nrow - 1 or not filled[i + 1, j]:
                ax.set_xlabel("Unique alarms (rank)")

    # column headers; spanning "Source alarm priority" header with its rule
    for j, (_, name) in enumerate(cols):
        fig.text(fx(col_x[j]), fy(TOP - 6.5), name, ha="left", va="bottom",
                 fontsize=6.5, fontweight="bold", color=INK)
    x0, x1 = col_x[1], col_x[-1] + w
    fig.text(fx((x0 + x1) / 2), fy(TOP - 13), "Source alarm priority", ha="center",
             va="bottom", fontsize=7, fontweight="bold", color=INK)
    fig.add_artist(Line2D([fx(x0), fx(x1)], [fy(TOP - 12)] * 2, color=INK, linewidth=0.6))

    # vertical rule between device total and priorities
    xs = fx(LEFT + w + SPLIT / 2 - 1)
    fig.add_artist(Line2D([xs, xs], [fy(TOP - 14), fy(panels_h + 6)],
                          color=MUTED, linewidth=0.8))

    summary_table(fig, fx, fy, panels_h + BOTTOM + T_GAP, cols, groups, stats)

    # device-type headers (vertical, with a bracket) and rules between groups
    i = 0
    for g, (t, devs) in enumerate(groups):
        top, bot = row_top[i], row_top[i + len(devs) - 1] + ROW_H
        fig.add_artist(Line2D([fx(5.5)] * 2, [fy(top), fy(bot)], color=INK, linewidth=0.6))
        fig.text(fx(3.5), fy((top + bot) / 2), t, rotation=90, ha="center", va="center",
                 fontsize=6.5, fontweight="bold", color=INK)
        if g:
            ym = fy(top - ROW_GAP / 2 - GROUP_GAP / 2 - 2)
            fig.add_artist(Line2D([fx(2), fx(FIG_W - 2)], [ym, ym], color=MUTED,
                                  linewidth=0.4, linestyle=(0, (1, 1.5))))
        i += len(devs)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "svg", "png", "tiff"):
        kw = {"dpi": 600} if ext in ("png", "tiff") else {}
        fig.savefig(OUT_DIR / f"corpus_pareto_{unit}.{ext}", facecolor=SURFACE, **kw)
    plt.close(fig)
    return summary


def main():
    df = load()
    for dept, unit, title in UNITS:
        summary = figure(df, dept, unit, title)
        print(f"\n{unit} -> {OUT_DIR / f'corpus_pareto_{unit}'}.{{pdf,svg,png,tiff}}")
        print(f"  {'device':<16}{'stratum':<14}{'n':>5}{'alarms':>11}"
              + "".join(f"{int(100 * k):>6}%" for k in CUTOFFS))
        for dev, name, n, total, *needed in summary:
            print(f"  {dev:<16}{name:<14}{n:>5}{total:>11,}" + "".join(f"{v:>7}" for v in needed))


if __name__ == "__main__":
    main()
