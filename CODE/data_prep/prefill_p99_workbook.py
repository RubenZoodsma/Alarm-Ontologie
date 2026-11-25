"""
prefill_p99_workbook.py  (one-off, 2026-11-24)

Pre-fills cells of the p99 annotation workbook that can be filled with
certainty, so the co-authors only annotate what needs judgement. Works on the
workbook in place (DATA/ANNOTATION/to_authors/p99_annotation.xlsx): it only
writes EMPTY cells and never overwrites a value already there (reported).

Rules (the 'safe side')
  * Only terms already in the vocabulary (sheet Vocabulary): a new term is a
    modelling decision, left to the annotators.
  * A cell is filled only when the consensus annotation (p75, reviewStatus x)
    already fixes it for the same parameter on the same device, or the label
    states it unambiguously (connection lost, a threshold direction, an ECG
    electrode, ...). Templates below are copied from those consensus rows.
  * Not filled: phases without a term (systolic, diastolic), functional units
    with no matching term (ICP, NIBP, CPP), alarm category where the consensus
    itself is inconsistent (e.g. Ppeak too low Technical vs Ppeak Low
    Physiological), 'not reached/achieved' alarms (consensus models VT Not
    Achieved as a leak), alarm-off messages, bare parameter labels (consensus
    marks those EXCL), and anything else not certain.
  * TherapeuticModality / PhysiologicalProperty / PhysiologicalProcess only
    where the consensus is constant for that device (CardioHelp -> ECMO) or a
    row copies a consensus sibling as a whole.

Copies of a whole consensus row are flagged for review, as before:
  REVIEW       same label, other device model (Medibus incubators)
  REVIEW-PRIO  same label and model, other source priority

Filled cells are written in blue italics (visual only, not exported) and the
Instructions sheet gets a legend line. Data validations that Excel stored as
an extension (openpyxl drops those on load) are re-created.

Usage:  python3 CODE/data_prep/prefill_p99_workbook.py [--dry-run]
"""

import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annotation_xlsx_common import (norm, FIRST_ROW, HEADER_ROW, REVIEW, REVIEW_VALUES, ROOT,  # noqa: E402
                                    SHEET_DATA, SHEET_HELP, SHEET_VOCAB)

XLSX = ROOT / "DATA" / "ANNOTATION" / "to_authors" / "p99_annotation.xlsx"
PREFILL_FONT = Font(name="Arial", size=9, italic=True, color="1F4E9E")

C, A, S, SG, M = "alarmCategory", "Component", "Sensor", "Signal", "Metric"
PHYS, TECH = "Physiological", "Technical"


def t(**kw):
    """Template: keyword names map to columns (dots written as _)."""
    alias = {"cat": C, "comp": A, "cOp": "Component.OperationalState", "FU": "FunctionalUnit",
             "sen": S, "sOp": "Sensor.OperationalState", "pos": "Sensor.AnatomicalPosition",
             "sig": SG, "sQ": "Signal.QualityState", "met": M, "ph": "Metric.Phase",
             "agg": "Metric.Aggregation", "vs": "Metric.ValueState", "rh": "Metric.Rhythm",
             "dOp": "Device.OperationalState", "ther": "TherapeuticModality",
             "prop": "PhysiologicalProperty", "proc": "PhysiologicalProcess", "dev": "Device"}
    return {alias[k]: v for k, v in kw.items()}


INC, DEC = "Increased", "Decreased"
VV = t(cat=TECH, comp="ConnectionUnit", cOp="Disconnected")                       # 8/8 consensus
ECMO = dict(ther="ExtraCorporealMembraneOxygenation")                             # CardioHelp 4/4
# ventilator (consensus: SERVOU/SERVOI/DATEX rows)
RR = lambda vs: t(cat=PHYS, FU="FU_PneumoTachograph", met="RespirationRate", vs=vs)  # noqa: E731
MV = lambda vs: t(cat=PHYS, FU="FU_PneumoTachograph", met="RespirationVolume", agg="Minute", vs=vs)  # noqa: E731
VT = lambda vs: t(cat=PHYS, FU="FU_PneumoTachograph", met="RespirationVolume", ph="Tidal", vs=vs)  # noqa: E731
PEEP = lambda vs: t(cat=PHYS, FU="FU_AirwayPressure", met="AirwayPressure", ph="EndExpiratory", vs=vs)  # noqa: E731
PPEAK = lambda vs: t(cat=PHYS, FU="FU_AirwayPressure", met="AirwayPressure", ph="Peak", vs=vs)  # noqa: E731
LEAK = t(cat=TECH, FU="FU_PneumoTachograph", met="VentilationCircuitLeak", vs=INC)
VENT_APNEA = t(cat=PHYS, FU="FU_PneumoTachograph", met="RespirationRate", rh="Absent")
FLOW_SENSOR = t(cat=TECH, FU="FU_PneumoTachograph", sen="PneumoTachograph")
CIRCUIT_LOS = t(cat=TECH, comp="AirwayCircuit", cOp="Disconnected")              # SERVOU Patiëntcircuit los
# monitor (consensus: PHILIPSMONITOR / Monitor rows)
SPO2 = lambda vs: t(cat=PHYS, FU="FU_PulseOximetry", met="SpO2", vs=vs, prop="SaO2", proc="PulmonaryOxygenation")  # noqa: E731
SPO2_SENSOR = lambda op=None: t(cat=TECH, FU="FU_PulseOximetry", sen="PulseOximeter", **({"sOp": op} if op else {}))  # noqa: E731
SPO2_INTERF = t(cat=TECH, FU="FU_PulseOximetry", sig="PPG_waveform", sQ="Impaired")
SPO2_NOPULSE = t(cat=TECH, FU="FU_PulseOximetry", met="PulseRate", rh="Absent")   # SpO2 geen pols
SPO2_PFI = t(cat=TECH, FU="FU_PulseOximetry", met="PulseFlowIndex", vs=DEC, prop="TissuePerfusion")
SPO2_STORING = t(cat=TECH, FU="FU_PulseOximetry", sen="PulseOximeter", sOp="Enabled", pos="Peripheral",
                 sig="PPG_waveform", sQ="Impaired")                                 # Storing in SpO2
ABP = lambda vs, ph=None: t(cat=PHYS, FU="FU_InvasiveBloodPressure", met="ArterialBloodPressure", vs=vs, **({"ph": ph} if ph else {}))  # noqa: E731
CVP = lambda vs, ph=None: t(cat=PHYS, FU="FU_InvasiveBloodPressure", met="VenousBloodPressure", vs=vs, **({"ph": ph} if ph else {}))  # noqa: E731
ABP_RANGE = t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer", sOp="Malfunction", pos="Arterial")
TEMP = lambda vs: t(cat=PHYS, FU="FU_Temperature", met="BodyTemperature", vs=vs)  # noqa: E731
TEMP_NOSENSOR = t(cat=TECH, FU="FU_Temperature", sen="BodyTemperature_sensor", sOp="Disconnected")
ETCO2 = lambda vs: t(cat=PHYS, FU="FU_Capnograph", met="CO2", ph="EndExpiratory", vs=vs)  # noqa: E731
ECG_LEAD = lambda s: t(cat=TECH, FU="FU_ElectroCardiography", sen=s, sOp="Disconnected")  # noqa: E731
ECG_NOISE = t(cat=TECH, FU="FU_ElectroCardiography", sig="ECG_signal", sQ="Impaired")
ASYSTOLE = t(cat=PHYS, FU="FU_ElectroCardiography", met="HeartRate", rh="Absent")
ECG_PHYS = t(cat=PHYS, FU="FU_ElectroCardiography")
TECH_ONLY = t(cat=TECH)
PHYS_UP, PHYS_DOWN = t(cat=PHYS, vs=INC), t(cat=PHYS, vs=DEC)

# (label, source priority, Device.Type) -> (cells, reviewStatus or None)
P = {}


def add(label, prio, model, cells, status=None):
    P[(label, prio, model)] = (cells, status)


# ── CardioHelp (alarmCategory already set by the annotator) ──
for lab, prio, cells in [
    ("CARDIOHELP - Drukvermindering boven grens", "Medium", {}),
    ("CARDIOHELP - Hb buiten meetbereik", "Hoog", {}),
    ("CARDIOHELP - Hb onder waarschuwingsgrens", "Medium", t(vs=DEC)),
    ("CARDIOHELP - Hct buiten meetbereik", "Hoog", {}),
    ("CARDIOHELP - Hct onder waarschuwingsgrens", "Medium", t(vs=DEC)),
    ("CARDIOHELP - LPM onder alarmgrens", "Hoog", t(vs=DEC)),
    # same parameter as the consensus row 'pArt boven waarschuwingsgrens'
    ("CARDIOHELP - pArt boven alarmgrens", "Hoog",
     t(FU="FU_InvasiveBloodPressure", met="ArterialBloodPressure", vs=INC, prop="ArterialPressure")),
    ("CARDIOHELP - pInt boven waarschuwingsgrens", "Medium", t(vs=INC)),
    ("CARDIOHELP - SvO2 buiten meetbereik", "Hoog", {}),
    ("CARDIOHELP - SvO2 onder waarschuwingsgrens", "Medium", t(vs=DEC)),
    ("CARDIOHELP - TArt boven waarschuwingsgrens", "Medium", t(FU="FU_Temperature", met="Temperature", vs=INC)),
]:
    add(lab, prio, "CardioHelp", {**cells, **t(**ECMO)})

# ── CarescapeR860 ──
D = "CarescapeR860"
add("DATEXOHMEDACOMVENTILATOR - Apnea", "Hoog", D, VENT_APNEA)
add("DATEXOHMEDACOMVENTILATOR - Circuit Leak", "Hoog", D, LEAK)
add("DATEXOHMEDACOMVENTILATOR - Exp Flow Sensor Error", "Hoog", D, {**FLOW_SENSOR, **t(sOp="Malfunction")})
add("DATEXOHMEDACOMVENTILATOR - MVexp Low", "Hoog", D,                       # sibling MVexp High
    {**MV(DEC), **t(ther="VentilationTherapy", prop="RespirationVolume", proc="PulmonaryVentilation")})
add("DATEXOHMEDACOMVENTILATOR - Neo Flow Sensor Error", "Hoog", D, {**FLOW_SENSOR, **t(sOp="Malfunction")})
add("DATEXOHMEDACOMVENTILATOR - Neo Flow Sensor Off", "Hoog", D, FLOW_SENSOR)
add("DATEXOHMEDACOMVENTILATOR - No Neo Flow Sensor", "Hoog", D, {**FLOW_SENSOR, **t(sOp="Disconnected")})
add("DATEXOHMEDACOMVENTILATOR - PEEPe Low", "Hoog", D, PEEP(DEC))
add("DATEXOHMEDACOMVENTILATOR - RR Low", "Hoog", D, RR(DEC))                   # sibling RR High
add("DATEXOHMEDACOMVENTILATOR - Verbinding verbroken", "Hoog", D, VV)

# ── Hemosphere (Edwards): parameters have no metric term; category + direction ──
H = "Hemosphere"
for lab in ["Cable Port 1 – Communication Error", "Cable Port 1 – Hardware Failure",
            "CO: Check Thermal Filament Position", "EDV: Heart Rate Signal Loss",
            "L-Tech Module Slot - Hardware Failure", "L-Tech Module Slot – Communication Error",
            "Module Slot 1 – Hardware Failure", "Oximetry Cable Disconnected",
            "Oximetry: Cable Malfunction – Hardware Failure", "Oximetry: Light Range",
            "Oximetry: Unstable Baseline (1)", "Oximetry: Wedge or Wall Artifact (1)",
            "SV: Heart Rate Signal Loss (2)", "Swan Ganz Module Fault"]:
    add(f"EDWARDS - {lab}", "Hoog", H, TECH_ONLY)
for lab in ["CCI High Alarm Limit", "CO exceeded high limit", "EDVI High Alarm Limit",
            "ScvO2 High Alarm Limit", "SvO2 High Alarm Limit", "SVR exceeded high limit",
            "SVR High Alarm Limit", "SVRI exceeded high limit", "SVRI High Alarm Limit"]:
    add(f"EDWARDS - {lab}", "Hoog", H, PHYS_UP)
for lab in ["CCI Low Alarm Limit", "RVEF is below low limit", "RVEF Low Alarm Limit",
            "ScvO2 Low Alarm Limit", "SVI Low Alarm Limit", "SvO2 Low Alarm Limit",
            "SVR is below low limit", "SVR Low Alarm Limit", "SVRI Low Alarm Limit"]:
    add(f"EDWARDS - {lab}", "Hoog", H, PHYS_DOWN)

# ── LeoniPlus (HULBUS) ──
L = "LeoniPlus"
add("HULBUS - Disconnection", "Hoog", L, CIRCUIT_LOS)
add("HULBUS - Freq too high", "Hoog", L, RR(INC))
add("HULBUS - MV high", "Hoog", L, MV(INC))
add("HULBUS - MV low", "Hoog", L, MV(DEC))
add("HULBUS - Ppeak too high", "Hoog", L,                                     # sibling Ppeak too low; its
    t(FU="FU_AirwayPressure", met="AirwayPressure", ph="Peak", vs=INC))         # category disputed: not set

# ── Medibus: incubators (copies of the same label on another incubator model) ──
AIRT = lambda vs: t(cat=TECH, FU="FU_Temperature", met="AirTemperature", vs=vs,  # noqa: E731
                    ther="ThermoRegulationTherapy")  # AirTemperature unbridged: not patient physiology
add("MEDIBUS - Air temperature high", "Medium", "Caleo", AIRT(INC), "REVIEW")
add("MEDIBUS - Air temperature low", "Laag", "BabyTherm", AIRT(DEC), "REVIEW")
add("MEDIBUS - Air temperature low", "Laag", "Caleo", AIRT(DEC), "REVIEW")
add("MEDIBUS - Humidity Sensor inoperable", "Medium", "Caleo",
    t(cat=TECH, FU="FU_Humidity", sen="Humidity_sensor", sOp="Malfunction",
      ther="ThermoRegulationTherapy", proc="ThermoRegulation"), "REVIEW")
add("MEDIBUS - Mattress temperature  setting deviation", "Medium", "BabyLeo",
    t(cat=TECH, comp="MattrasHeater", cOp="Warning", ther="ThermoRegulationTherapy",
      proc="ThermoRegulation"), "REVIEW")
add("MEDIBUS - Radiant heater after 15 mins. operation", "Medium", "BabyLeo",
    t(cat=TECH, comp="RadiantHeater", cOp="Warning", ther="ThermoRegulationTherapy",
      proc="ThermoRegulation"), "REVIEW")
add("MEDIBUS - Ventilator storing", "Medium", "Caleo", t(cat=TECH, dOp="Malfunction"), "REVIEW")
add("MEDIBUS - Central skin temperature < low limit", "Medium", "BabyLeo", TEMP(DEC))
add("MEDIBUS - Central skin temperature > high limit", "Medium", "BabyLeo", TEMP(INC))
add("MEDIBUS - Humidifier inoperable", "Medium", "Caleo", TECH_ONLY)
add("MEDIBUS - Instellingen verloren", "Medium", "BabyLeo", t(cat=TECH, comp="Settings"))
add("MEDIBUS - System fault", "Medium", "BabyLeo", t(cat=TECH, dOp="Malfunction"))
add("MEDIBUS - Temp. 1  Probe disconnected or fault", "Medium", "Caleo", t(cat=TECH, FU="FU_Temperature"))
for m in ("BabyLeo", "BabyTherm", "Caleo"):
    add("MEDIBUS - Water level low", "Laag", m, TECH_ONLY)
# Medibus without a device model: device not inferred; only device-independent cells
add("MEDIBUS - Apnoe", "Hoog", "", t(cat=PHYS, met="RespirationRate", rh="Absent"))
add("MEDIBUS - Beademingsdruk hoog", "Hoog", "", PPEAK(INC))                   # as SERVOU Beademingsdruk hoog
add("MEDIBUS - Hose kinked", "Hoog", "", TECH_ONLY)
add("MEDIBUS - Lekkage", "Medium", "", LEAK)                                  # as MEDIBUS Volume niet constant
add("MEDIBUS - MV bovengrens", "Hoog", "", MV(INC))                           # sibling MV ondergrens

# ── Draeger Infinity (Monitor) ──
I = "Infinity"
add("Monitor - ADEM. FREQ. HOOG", "Medium", I, t(cat=PHYS, met="RespirationRate", vs=INC))
add("Monitor - APNOE RESP", "Medium", I, t(cat=PHYS, FU="FU_ElectroCardiography", met="RespirationRate", rh="Absent"))
add("Monitor - ARR: Ventriculaire tachycardie", "Hoog", I, ECG_PHYS)
add("Monitor - ART buiten bereik hoog", "Medium", I, ABP_RANGE)               # sibling ... laag
add("Monitor - ART niet aangesloten", "Medium", I,
    t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer", sOp="Disconnected", pos="Arterial"))
add("Monitor - ART transducer storing", "Medium", I, ABP_RANGE)
add("Monitor - Asystolie", "Hoog", I, ASYSTOLE)
add("Monitor - CO2 MCable storing", "Laag", I, t(cat=TECH, FU="FU_Capnograph", sen="Capnometer", sOp="Malfunction"))
add("Monitor - CO2 SENSOR ?", "Medium", I, t(cat=TECH, FU="FU_Capnograph"))
add("Monitor - CO2 STORING", "Medium", I, t(cat=TECH, FU="FU_Capnograph"))
add("Monitor - CO2 verminderde nauwkeurigheid", "Laag", I, t(cat=TECH, FU="FU_Capnograph"))
add("Monitor - DRUKSLANG GEKNIKT", "Medium", I, TECH_ONLY)
add("Monitor - ECG niet aangesloten", "Laag", I, ECG_LEAD("ElectroCardioGraphy_sensor"))
add("Monitor - etCO2 Hoog", "Medium", I, ETCO2(INC))
add("Monitor - etCO2 Laag", "Medium", I, ETCO2(DEC))
add("Monitor - Hoog etCO2", "Medium", I, ETCO2(INC))
add("Monitor - Lage etCO2", "Medium", I, ETCO2(DEC))
add("Monitor - GD1 statische druk", "Medium", I,
    t(cat=TECH, FU="FU_InvasiveBloodPressure", sig="IBP_waveform", sQ="Impaired"))
add("Monitor - PA statische druk", "Medium", I,
    t(cat=TECH, FU="FU_InvasiveBloodPressure", sig="IBP_waveform", sQ="Impaired"))
add("Monitor - Hemopod niet aangesloten", "Laag", I, TECH_ONLY)
add("Monitor - Hoge ARTsys", "Medium", I, {**ABP(INC), **t(prop="ArterialPressure")})
add("Monitor - Lage ARTsys", "Medium", I, {**ABP(DEC), **t(prop="ArterialPressure")})
add("Monitor - Hoge CPP", "Medium", I, PHYS_UP)
add("Monitor - Lage CPP", "Medium", I, PHYS_DOWN)
add("Monitor - Hoge GP1 M", "Medium", I, t(cat=PHYS, FU="FU_InvasiveBloodPressure", ph="Mean", vs=INC))
add("Monitor - Lage GP1 M", "Medium", I, t(cat=PHYS, FU="FU_InvasiveBloodPressure", ph="Mean", vs=DEC))
add("Monitor - Lage GP2 M", "Medium", I, t(cat=PHYS, FU="FU_InvasiveBloodPressure", ph="Mean", vs=DEC))
add("Monitor - Hoge ICP", "Medium", I, PHYS_UP)
add("Monitor - Lage ICP", "Medium", I, PHYS_DOWN)
add("Monitor - ICP niet aangesloten", "Medium", I, TECH_ONLY)
add("Monitor - Hoge NBPmean", "Medium", I, t(cat=PHYS, met="ArterialBloodPressure", ph="Mean", vs=INC))
add("Monitor - Lage NBPmean", "Medium", I, t(cat=PHYS, met="ArterialBloodPressure", ph="Mean", vs=DEC))
add("Monitor - Hoge PA M", "Medium", I, t(cat=PHYS, FU="FU_InvasiveBloodPressure", ph="Mean", vs=INC))
add("Monitor - Lage PA M", "Medium", I, t(cat=PHYS, FU="FU_InvasiveBloodPressure", ph="Mean", vs=DEC))
add("Monitor - Hoge Ta", "Medium", I, TEMP(INC))
add("Monitor - Lage Ta", "Medium", I, TEMP(DEC))
add("Monitor - Lage Pols", "Medium", I, t(cat=PHYS, met="PulseRate", vs=DEC))
add("Monitor - LOSKOPPELEN", "Medium", I, TECH_ONLY)
add("Monitor - MIN VOL HOOG", "Medium", I, MV(INC))
add("Monitor - MIN VOL LAAG", "Medium", I, MV(DEC))
add("Monitor - NIBP open lijn", "Laag", I, TECH_ONLY)
add("Monitor - PA niet aangesloten", "Laag", I, t(cat=TECH, FU="FU_InvasiveBloodPressure"))
add("Monitor - PAW HOOG", "Medium", I, PPEAK(INC))                            # as SERVOI Paw hoog
add("Monitor - PAW LAAG", "Medium", I, PPEAK(DEC))
add("Monitor - SpO2 interferentie gevonden", "Laag", I, SPO2_INTERF)
add("Monitor - SpO2 sensor onbekend", "Laag", I, SPO2_SENSOR())
add("Monitor - SpO2 sensor storing", "Laag", I, SPO2_SENSOR("Malfunction"))
add("Monitor - SYSTEEMFOUT", "Medium", I, t(cat=TECH, dOp="Malfunction"))
add("Monitor - T1a niet aangesloten", "Laag", I, TEMP_NOSENSOR)
add("Monitor - Ta niet aangesloten", "Laag", I, TEMP_NOSENSOR)
add("Monitor - TIDALVOL HOOG", "Medium", I, VT(INC))
add("Monitor - TIDALVOL LAAG", "Medium", I, VT(DEC))
add("Monitor - Ventriculaire fibrillatie", "Hoog", I, ECG_PHYS)
add("Monitor - Verbinding verbroken", "Hoog", I, VV)
add("Monitor - Verbinding verbroken monitor", "Hoog", I, TECH_ONLY)

# ── Philips monitor (device model: IntelliVue family, since 2026-11-24) ──
PH = lambda lab: "PHILIPSMONITOR - " + lab  # noqa: E731
for lab, prio, cells, st in [
    ("?? awRF hoog g", "Medium", t(cat=PHYS, FU="FU_Capnograph", met="RespirationRate", vs=INC), None),
    ("?? awRF laag g", "Medium", t(cat=PHYS, FU="FU_Capnograph", met="RespirationRate", vs=DEC), None),
    ("?? CVDm laag g", "Medium", CVP(DEC, "Mean"), None),
    ("?? etCO2 hoog", "Medium", ETCO2(INC), None),
    ("?? imCO2 hoog", "Medium", t(FU="FU_Capnograph", met="CO2", vs=INC), None),
    ("?? SpO2pr laag", "Medium", SPO2(DEC), None),
    ("?? SpO2r hoog", "Medium", SPO2(INC), None),
    ("?? Thuid laag", "Medium", TEMP(DEC), None),
    ("??? Brady (pols)", "Hoog", t(cat=PHYS, met="PulseRate", vs=DEC), None),
    ("??? Desaturatie", "Hoog", t(cat=PHYS, FU="FU_PulseOximetry", met="SpO2", vs="SeverelyDecreased",
                                  prop="SaO2", proc="PulmonaryOxygenation"), None),   # = Desaturatie
    ("ABP artefact", "Laag", t(cat=TECH, FU="FU_InvasiveBloodPressure", sig="IBP_waveform", sQ="Impaired"), None),
    ("ABP buiten bereik", "Hoog", ABP_RANGE, None),
    ("ABP buiten bereik", "Medium", ABP_RANGE, None),
    ("ABP contr.nulling", "Laag", t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer"), None),
    ("ABP geen transducer", "Medium", t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer",
                                        sOp="Disconnected", pos="Arterial"), None),
    ("ABP losgeraakt", "Hoog", t(cat=TECH, FU="FU_InvasiveBloodPressure"), None),
    ("ABP pulseert niet", "Medium", t(FU="FU_InvasiveBloodPressure"), None),
    ("ABP slecht signaal", "Medium", t(cat=TECH, FU="FU_InvasiveBloodPressure", sig="IBP_waveform", sQ="Impaired"), None),
    ("ABPd hoog", "Medium", ABP(INC), None),
    ("ABPd laag", "Medium", ABP(DEC), None),
    ("ABPm hoog", "Hoog", ABP(INC, "Mean"), "REVIEW-PRIO"),                    # = ABPm hoog [Medium]
    ("ABPmLaag", "Hoog", ABP(DEC, "Mean"), None),
    ("ABPs hoog", "Hoog", ABP(INC), None),
    ("ABPs hoog", "Medium", ABP(INC), None),
    ("ABPs laag", "Medium", ABP(DEC), None),
    ("CO2 geen sensor or", "Medium", t(cat=TECH, FU="FU_Capnograph", sen="Capnometer", sOp="Disconnected"), None),
    ("CO2 Occlusie", "Medium", t(cat=TECH, FU="FU_Capnograph"), None),
    ("CO2 sens.opwarm", "Laag", t(cat=TECH, FU="FU_Capnograph", sen="Capnometer"), None),
    ("Contr SyncOut-kabl", "Medium", TECH_ONLY, None),
    ("Contrl touchscreen", "Laag", TECH_ONLY, None),
    ("Control ECG-kabel", "Medium", t(cat=TECH, FU="FU_ElectroCardiography", sen="ElectroCardioGraphy_sensor"), None),
    ("Control. pat-ID", "Medium", TECH_ONLY, None),
    ("CVD artefact", "Laag", t(cat=TECH, FU="FU_InvasiveBloodPressure", sig="IBP_waveform", sQ="Impaired"), None),
    ("CVD buiten bereik", "Hoog", t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer",
                                    sOp="Malfunction"), None),
    ("CVD geen transducer", "Medium", t(cat=TECH, FU="FU_InvasiveBloodPressure", sen="IBP_transducer",
                                        sOp="Disconnected"), None),
    ("CVD verkleinen", "Laag", {**CVP(INC), **t(prop="VenousPressure")}, None),  # as ABP verkleinen
    ("CVDm hoog", "Medium", CVP(INC, "Mean"), None),
    ("EC10 ontkoppeld", "Medium", TECH_ONLY, None),
    ("ECG ruissignaal", "Medium", ECG_NOISE, None),
    ("EtCO2 laag", "Medium", ETCO2(DEC), None),
    ("F elektrode los", "Medium", ECG_LEAD("ECG_LL_sensor"), None),            # IEC: F = left leg
    ("L elektrode los", "Medium", ECG_LEAD("ECG_LA_sensor"), None),            # L = left arm
    ("N elektrode los", "Medium", ECG_LEAD("ECG_RL_sensor"), None),            # N = neutral, right leg
    ("R elektrode los", "Medium", ECG_LEAD("ECG_RA_sensor"), None),            # R = right arm
    ("MMX losgekop. p.", "Medium", TECH_ONLY, None),
    ("NiBD contr. mancht", "Medium", TECH_ONLY, None),
    ("NiBD defect t", "Medium", TECH_ONLY, None),
    ("NiBD manch.ovrdruk", "Hoog", TECH_ONLY, None),
    ("NiBD meting faalde", "Medium", TECH_ONLY, None),
    ("NiBD onderbroken", "Medium", TECH_ONLY, None),
    ("NiBDm hoog", "Medium", t(cat=PHYS, met="ArterialBloodPressure", ph="Mean", vs=INC), None),
    ("NiBDm laag", "Medium", t(cat=PHYS, met="ArterialBloodPressure", ph="Mean", vs=DEC), None),
    ("Niet te leren ST", "Laag", t(cat=TECH, FU="FU_ElectroCardiography"), None),
    ("Pols hoog", "Medium", t(cat=PHYS, met="PulseRate", vs=INC), None),
    ("Pols laag", "Medium", t(cat=PHYS, met="PulseRate", vs=DEC), None),
    ("SDM HEATING REDUCD", "Medium", TECH_ONLY, None),
    ("SDM SENS. OFF PAT.", "Medium", TECH_ONLY, None),
    ("SDM SITE TIMEOUT", "Medium", TECH_ONLY, None),
    ("SDM TC UNSTBL.", "Medium", TECH_ONLY, None),
    ("SpO2 geen sensor", "Medium", SPO2_SENSOR("Disconnected"), None),
    ("SpO2 interferentie", "Medium", SPO2_INTERF, None),
    ("SpO2 onbk. sensor", "Medium", SPO2_SENSOR(), None),
    ("SpO2 r geen puls", "Medium", SPO2_NOPULSE, None),
    ("SpO2 r geen sensor", "Medium", SPO2_SENSOR("Disconnected"), None),
    ("SpO2 r interferentie", "Medium", SPO2_INTERF, None),
    ("SpO2 r laag", "Medium", SPO2(DEC), None),
    ("SpO2 r lage PFI", "Laag", SPO2_PFI, None),
    ("SpO2 r sensor los", "Medium", SPO2_SENSOR("Disconnected"), None),
    ("SpO2 r zoekend", "Laag", t(cat=TECH, FU="FU_PulseOximetry"), None),
    ("SpO2-sensor error", "Laag", SPO2_SENSOR("Malfunction"), None),
    ("SpO2-sensor error", "Medium", SPO2_SENSOR("Malfunction"), None),
    ("SpO2pr geen puls", "Medium", SPO2_NOPULSE, None),
    ("SpO2pr intrfrentie", "Medium", SPO2_INTERF, None),
    ("SpO2pr lage PFI", "Laag", SPO2_PFI, None),
    ("SpO2pr sensor los", "Medium", SPO2_SENSOR("Disconnected"), None),
    ("SpO2pr zoekend", "Laag", t(cat=TECH, FU="FU_PulseOximetry"), None),
    ("SpO2przwak signaal", "Laag", SPO2_INTERF, None),                         # as SpO2 zwak signaal
    ("ST-II hoog", "Medium", t(cat=PHYS, FU="FU_ElectroCardiography", vs=INC), None),
    ("Storing in SpO2 r", "Medium", SPO2_STORING, None),
    ("Storing in SpO2pr", "Medium", SPO2_STORING, None),
    ("Tachy (pols)", "Hoog", t(cat=PHYS, met="PulseRate", vs=INC), None),
    ("Tbloed geen sensor", "Medium", TEMP_NOSENSOR, None),
    ("Temp geen sensor", "Medium", TEMP_NOSENSOR, None),
    ("Thuid geen sensor", "Medium", TEMP_NOSENSOR, None),
    ("Tkern geen sensor", "Medium", TEMP_NOSENSOR, None),
    ("Trect geen sensor", "Medium", TEMP_NOSENSOR, None),
    ("Trect hoog", "Medium", TEMP(INC), None),
    ("Trect laag", "Medium", TEMP(DEC), None),
    ("Ventrikel Fibr/Tachy", "Hoog", ECG_PHYS, None),
    ("Vtach", "Hoog", ECG_PHYS, None),
    ("Verbinding verbroken", "Hoog", VV, None),
]:
    add(PH(lab), prio, "IntelliVue", cells, st)

add("PRISMAFLEX - Verbinding verbroken", "Hoog", "Prismax", VV)

# ── ServoI / ServoU / Trilogy ──
VENT_FULL = t(ther="VentilationTherapy", proc="PulmonaryVentilation")
add("SERVOI - Apneu", "Hoog", "ServoI", VENT_APNEA)
add("SERVOI - Exp. minuutvol: Hoog", "Hoog", "ServoI",                       # sibling ... Laag
    {**MV(INC), **VENT_FULL, **t(prop="RespirationVolume")})
add("SERVOI - Lekkage buiten bereik", "Hoog", "ServoI", LEAK)
add("SERVOI - PEEP hoog", "Medium", "ServoI", {**PEEP(INC), **VENT_FULL, **t(prop="RespirationPressure")})
add("SERVOI - PEEP laag", "Medium", "ServoI", {**PEEP(DEC), **VENT_FULL, **t(prop="RespirationPressure")})
add("SERVOU - CPAP laag", "Medium", "ServoU", PEEP(DEC))
add("SERVOU - Edi storing door ECG", "Medium", "ServoU", t(cat=TECH, sQ="Impaired"))
add("SERVOU - Exp. cass. vervangen", "Laag", "ServoU", TECH_ONLY)
add("SERVOU - Insp. druk hoog", "Hoog", "ServoU", PPEAK(INC))
add("SERVOU - Patintcircuit los", "Hoog", "ServoU", CIRCUIT_LOS)              # = Patiëntcircuit los
add("SERVOU - Patiënt los > 1 min", "Hoog", "ServoU", CIRCUIT_LOS)
add("SERVOU - PEEP hoog", "Medium", "ServoU", {**PEEP(INC), **VENT_FULL, **t(prop="RespirationPressure")})
add("SERVOU - Te veel lekkage", "Medium", "ServoU", {**LEAK, **VENT_FULL}, "REVIEW-PRIO")
add("SERVOU - Volume uit blokkade", "Medium", "ServoU", t(cat=TECH, **dict(ther="VentilationTherapy", proc="PulmonaryVentilation")), "REVIEW-PRIO")
add("SERVOU - Verbinding verbroken", "Hoog", "ServoU", VV)
add("SERVOU - Werkt op batterijen", "Laag", "ServoU", t(cat=TECH, comp="PowerUnit", cOp="Disconnected"))
add("TRILOGY - Lage inademingsdruk", "Hoog", "Trilogy",
    t(FU="FU_AirwayPressure", met="AirwayPressure", ph="Peak", vs=DEC))         # category disputed: not set
add("TRILOGY - Lage VTe", "Hoog", "Trilogy", VT(DEC))
add("TRILOGY - Verbinding verbroken", "Hoog", "Trilogy",
    {**VV, **t(dev="MechanicalVentilator")})                                    # Device: inference.ttl axiom


def main():
    dry = "--dry-run" in sys.argv
    wb = load_workbook(XLSX)
    ws, vs = wb[SHEET_DATA], wb[SHEET_VOCAB]
    col = {c.value: c.column for c in ws[HEADER_ROW] if c.value}
    vocab = {}
    for j, cell in enumerate(vs[1], start=1):
        vocab[cell.value] = {v for (v,) in vs.iter_rows(min_row=2, min_col=j, max_col=j,
                                                       values_only=True) if v}

    rows = {}
    for r in range(FIRST_ROW, ws.max_row + 1):
        k = tuple(norm(ws.cell(r, col[c]).value) for c in ("alarmLabel", "alarmPriority", "Device.Type"))
        if k[0]:
            rows[k] = r
    missing = [k for k in P if tuple(map(norm, k)) not in rows]
    if missing:
        raise SystemExit("Not in the workbook:\n  " + "\n  ".join(map(str, missing)))
    bad = [(k, c, v) for k, (cells, _) in P.items() for c, v in cells.items()
           if c in vocab and v not in vocab[c]]
    if bad:
        raise SystemExit("Not a vocabulary term:\n  " + "\n  ".join(map(str, bad)))

    filled, kept, status_set, touched = 0, [], 0, 0
    for k, (cells, status) in P.items():
        r = rows[tuple(map(norm, k))]
        wrote = False
        for c, v in cells.items():
            cell = ws.cell(r, col[c])
            if cell.value not in (None, ""):
                if str(cell.value) != v:
                    kept.append((k[0], c, cell.value, v))
                continue
            cell.value, cell.font = v, PREFILL_FONT
            filled += 1
            wrote = True
        if status:
            cell = ws.cell(r, col[REVIEW])
            if cell.value in (None, ""):
                cell.value, cell.font = status, PREFILL_FONT
                status_set += 1
        touched += wrote

    # re-create data validations (Excel's extension format is dropped by openpyxl)
    ws.data_validations.dataValidation = []
    last = ws.max_row
    rl = get_column_letter(col[REVIEW])
    dv = DataValidation(type="list", formula1='"' + ",".join(REVIEW_VALUES) + '"', allow_blank=True,
                        showErrorMessage=True, errorStyle="stop", errorTitle="reviewStatus",
                        error="Use x, EXCL, REVIEW or REVIEW-PRIO (or leave empty).")
    dv.add(f"{rl}{FIRST_ROW}:{rl}{last}")
    ws.add_data_validation(dv)
    n_dv = 1
    for j, cell in enumerate(vs[1], start=1):
        name, n = cell.value, len(vocab.get(cell.value, ()))
        if not name or not n or name not in col:
            continue
        vl, cl = get_column_letter(j), get_column_letter(col[name])
        d = DataValidation(type="list", allow_blank=True, showErrorMessage=True,
                           formula1=f"={SHEET_VOCAB}!${vl}$2:${vl}${n + 1}", errorStyle="warning",
                           errorTitle="New term?",
                           error="Not in the current vocabulary. Yes = keep it as a new term "
                                 "(CamelCase, one term per cell); No = choose again.")
        d.add(f"{cl}{FIRST_ROW}:{cl}{last}")
        ws.add_data_validation(d)
        n_dv += 1

    # legend line on the Instructions sheet
    hs = wb[SHEET_HELP]
    note = "Blue italics – pre-filled automatically from the consensus conventions: please verify"
    if not any(c.value == note for c in hs["A"]):
        anchor = next(c.row for c in hs["A"] if c.value == "White – new: to annotate")
        hs.insert_rows(anchor + 1)
        hs.cell(anchor + 1, 1, note).font = Font(name="Arial", size=10, italic=True, color="1F4E9E")

    print(f"rows in plan {len(P)}, rows with new cells {touched}, cells filled {filled}, "
          f"reviewStatus set {status_set}, data validations {n_dv}")
    for lab, c, old, new in kept:
        print(f"  kept annotator value  {lab} | {c}: {old!r} (plan {new!r})")
    if dry:
        print("dry run: workbook not saved")
    else:
        wb.save(XLSX)
        print(f"saved {XLSX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
