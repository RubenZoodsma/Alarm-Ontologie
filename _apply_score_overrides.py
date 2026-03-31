"""
Targeted score overrides for conditie_unique_values.csv.
Assigns tech_evidence / clin_evidence where the regex patterns left (3,3) defaults
or where the symmetric counter-score (1) was missing.

Run this once; then re-run alarmBurden.py to regenerate burden_output.csv.
"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent
path = ROOT / "conditie_unique_values.csv"
df = pd.read_csv(path)

# (conditie_value, tech_evidence, clin_evidence)
overrides = [
    # ── HULBUS ventilator alarms ──────────────────────────────────────────────
    ("HULBUS - Apnea",                                      4,  8),  # English apnea = strong clinical
    ("HULBUS - Flow sensor",                                7,  1),  # sensor device issue
    ("HULBUS - Freq too high",                              4,  5),  # ventilator freq limit = clinical threshold
    ("HULBUS - Leak too high",                              7,  3),  # circuit leak = tech
    ("HULBUS - Pressure E-Tube",                           4,  5),  # ETT pressure = clinical threshold

    # ── MEDIBUS environmental/device alarms ───────────────────────────────────
    ("MEDIBUS - Air temperature high",                     7,  1),
    ("MEDIBUS - Air temperature low",                      7,  1),
    ("MEDIBUS - Check patient",                            3,  5),  # generic patient alert
    ("MEDIBUS - Humidity low",                             7,  1),
    ("MEDIBUS - Mattress temperature  setting deviation",  7,  1),
    ("MEDIBUS - Radiant heater after 15 mins. operation",  7,  1),

    # ── PHILIPSMONITOR: clinical limit alarms missed by regex ─────────────────
    ("PHILIPSMONITOR - ?? SpO2r hoog",                     4,  5),
    ("PHILIPSMONITOR - ?? imCO2 hoog",                     4,  5),
    ("PHILIPSMONITOR - ??SDM HI/LO tcpCO2",               4,  5),
    ("PHILIPSMONITOR - ABPmLaag",                          4,  5),
    ("PHILIPSMONITOR - Lage hartfreq.",                    4,  5),
    ("PHILIPSMONITOR - Trect hoog",                        4,  5),
    ("PHILIPSMONITOR - Trect laag",                        4,  5),

    # ── PHILIPSMONITOR: strong tech (sensor disconnect / error) ───────────────
    ("PHILIPSMONITOR - ECG alle leads los",                10, 1),  # all ECG leads off
    ("PHILIPSMONITOR - Resp elektr.los",                   10, 1),  # resp electrode loose
    ("PHILIPSMONITOR - SDM CONCT SENS.",                   10, 1),  # connect sensor prompt
    ("PHILIPSMONITOR - SDM SENS. OFF PAT.",                10, 1),  # sensor off patient
    ("PHILIPSMONITOR - SpO2-sensor error",                 10, 1),  # sensor error
    ("PHILIPSMONITOR - SpO2 geen pols",                    10, 3),  # no pulse detected
    ("PHILIPSMONITOR - SpO2 r geen puls",                  10, 3),  # no pulse (right side)

    # ── PHILIPSMONITOR: medium tech (signal quality / device condition) ───────
    ("PHILIPSMONITOR - CO2 ctr adapter er",                7,  1),
    ("PHILIPSMONITOR - CO2 sens.opwarm",                   7,  1),  # sensor warm-up
    ("PHILIPSMONITOR - CO2 wzig schaal l",                 7,  1),  # scale change
    ("PHILIPSMONITOR - Geen app.geg.",                     7,  1),  # no app data
    ("PHILIPSMONITOR - NiBD manch.ovrdruk",                7,  1),  # cuff overpressure
    ("PHILIPSMONITOR - SDM CHECK APPLI.",                  7,  1),
    ("PHILIPSMONITOR - SDM HEATING REDUCD",                7,  1),
    ("PHILIPSMONITOR - SDM SITE TIMEOUT",                  7,  1),
    ("PHILIPSMONITOR - SDM TC UNSTBL.",                    7,  1),  # TC signal unstable
    ("PHILIPSMONITOR - SpO2 Pols?",                        7,  1),  # signal quality
    ("PHILIPSMONITOR - SpO2 grillig",                      7,  1),  # erratic signal
    ("PHILIPSMONITOR - SpO2 lage PFI",                     7,  1),  # low perfusion index
    ("PHILIPSMONITOR - SpO2 onbk. sensor",                 7,  1),  # unknown sensor
    ("PHILIPSMONITOR - SpO2 r lage PFI",                   7,  1),
    ("PHILIPSMONITOR - SpO2 trage update",                 7,  1),  # slow update
    ("PHILIPSMONITOR - SpO2r Pols?",                       7,  1),

    # ── PHILIPSMONITOR: informational / administrative ────────────────────────
    ("PHILIPSMONITOR - Control. pat-ID",                   3,  1),
    ("PHILIPSMONITOR - ECG/arr-alrmn uit",                 3,  1),  # alarm disabled
    ("PHILIPSMONITOR - SDM READY FOR USE",                 3,  1),
    ("PHILIPSMONITOR - SDM SYSTEM MESSAGE",                3,  1),
    ("PHILIPSMONITOR - SDM TC MSG.",                       3,  1),
    ("PHILIPSMONITOR - Tijd voorb:TimerA",                 3,  1),

    # ── Already-scored entries that were missing the clin counter-score of 1 ──
    ("PHILIPSMONITOR - App. contr.",                       3,  1),  # keep
    ("PHILIPSMONITOR - Delta SpO2 contr. bron",            3,  1),  # keep
]

override_map = {c: (t, cl) for c, t, cl in overrides}

changed = 0
for idx, row in df.iterrows():
    cond = str(row["conditie"]).strip()
    if cond in override_map:
        t_new, c_new = override_map[cond]
        t_old, c_old = row["tech_evidence"], row["clin_evidence"]
        if t_old != t_new or c_old != c_new:
            df.at[idx, "tech_evidence"] = t_new
            df.at[idx, "clin_evidence"] = c_new
            print(f"  [{cond}]  tech {t_old}→{t_new}  clin {c_old}→{c_new}")
            changed += 1

df["tech_evidence"] = df["tech_evidence"].astype("Int64")
df["clin_evidence"] = df["clin_evidence"].astype("Int64")
df.to_csv(path, index=False)
print(f"\nDone. {changed} rows updated → {path.name}")
print("\nNew score distribution:")
print(df.groupby(["tech_evidence", "clin_evidence"]).size().reset_index(name="count").to_string(index=False))
